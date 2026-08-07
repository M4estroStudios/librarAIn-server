import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.api.project_status_http import (
    DEFAULT_STATUS,
    clear_project_status_stats_cache,
    count_statuses,
    get_project_stats,
    load_project_status,
    normalize_tree,
    project_status_path,
    save_project_status,
    slugify_label,
    try_handle_project_status_get,
    try_handle_project_status_put,
)


class NormalizeTreeTests(unittest.TestCase):
    def test_invalid_status_becomes_untested(self) -> None:
        tree = normalize_tree(
            {
                "version": 1,
                "roots": [
                    {
                        "id": "ingest",
                        "label": "Ingest",
                        "status": "nope",
                        "children": [{"label": "Child", "status": "ok"}],
                    }
                ],
            }
        )
        self.assertEqual(tree["roots"][0]["status"], DEFAULT_STATUS)
        self.assertEqual(tree["roots"][0]["children"][0]["status"], "ok")
        self.assertEqual(tree["roots"][0]["children"][0]["id"], "child")

    def test_sibling_id_uniqueness(self) -> None:
        tree = normalize_tree(
            {
                "roots": [
                    {
                        "id": "a",
                        "label": "A",
                        "children": [
                            {"id": "x", "label": "One"},
                            {"id": "x", "label": "Two"},
                        ],
                    }
                ]
            }
        )
        ids = [c["id"] for c in tree["roots"][0]["children"]]
        self.assertEqual(ids, ["x", "x_2"])

    def test_preserves_extra_keys(self) -> None:
        tree = normalize_tree(
            {
                "roots": [
                    {
                        "id": "a",
                        "label": "A",
                        "pipeline_phase": "stage3",
                        "children": [],
                    }
                ]
            }
        )
        self.assertEqual(tree["roots"][0]["pipeline_phase"], "stage3")

    def test_description_normalized(self) -> None:
        tree = normalize_tree(
            {
                "roots": [
                    {
                        "id": "a",
                        "label": "A",
                        "description": "Hello",
                        "children": [],
                    }
                ]
            }
        )
        self.assertEqual(tree["roots"][0]["description"], "Hello")
        missing = normalize_tree({"roots": [{"id": "b", "label": "B"}]})
        self.assertEqual(missing["roots"][0]["description"], "")

    def test_priority_normalized(self) -> None:
        tree = normalize_tree(
            {
                "roots": [
                    {
                        "id": "a",
                        "label": "A",
                        "priority": "high",
                        "priority_order": 2,
                        "children": [{"id": "b", "label": "B", "priority": "nope"}],
                    }
                ]
            }
        )
        self.assertEqual(tree["roots"][0]["priority"], "high")
        self.assertEqual(tree["roots"][0]["priority_order"], 2)
        self.assertEqual(tree["roots"][0]["children"][0]["priority"], "")
        self.assertEqual(tree["roots"][0]["children"][0]["priority_order"], 0)

    def test_slugify(self) -> None:
        self.assertEqual(slugify_label("Vision OCR"), "vision_ocr")


class LoadSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_missing_returns_empty(self) -> None:
        tree = load_project_status(self.root)
        self.assertEqual(tree["roots"], [])

    def test_save_and_load_roundtrip(self) -> None:
        saved = save_project_status(
            self.root,
            {
                "version": 1,
                "roots": [
                    {
                        "id": "runtime",
                        "label": "Runtime",
                        "status": "partial",
                        "notes": "note",
                        "children": [],
                    }
                ],
            },
        )
        self.assertTrue(project_status_path(self.root).is_file())
        self.assertIsNotNone(saved["updated_at"])
        loaded = load_project_status(self.root)
        self.assertEqual(loaded["roots"][0]["status"], "partial")
        self.assertEqual(loaded["roots"][0]["notes"], "note")

    def test_count_statuses(self) -> None:
        counts = count_statuses(
            [
                {
                    "status": "ok",
                    "children": [
                        {"status": "broken", "children": []},
                        {"status": "ok", "children": []},
                    ],
                }
            ]
        )
        self.assertEqual(counts["ok"], 2)
        self.assertEqual(counts["broken"], 1)


class HttpHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        seed = {
            "version": 1,
            "roots": [
                {
                    "id": "ingest",
                    "label": "Ingest",
                    "status": "untested",
                    "notes": "",
                    "children": [],
                }
            ],
        }
        project_status_path(self.root).write_text(
            json.dumps(seed), encoding="utf-8"
        )
        self.responses: list[tuple[int, dict]] = []

        def send_json(_handler, status, payload):
            self.responses.append((status, payload))

        self.send_json = send_json
        self.handler = MagicMock()
        self.registry = MagicMock()
        self.registry.running_job_count.return_value = 0
        self.batch = MagicMock()
        self.batch.running_count.return_value = 0
        self.settings = MagicMock()
        self.settings.data_root = str(self.root)

    def tearDown(self) -> None:
        clear_project_status_stats_cache(self.root)
        self._tmp.cleanup()

    def test_get_returns_tree_only(self) -> None:
        handled = try_handle_project_status_get(
            "/api/admin/project-status",
            self.handler,
            data_root=self.root,
            settings=self.settings,
            registry=self.registry,
            research_batch_registry=self.batch,
            sqlite_path=str(self.root / "db" / "biblioteca.db"),
            send_json=self.send_json,
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tree"]["roots"][0]["id"], "ingest")
        self.assertNotIn("stats", payload)

    def test_stats_are_cached(self) -> None:
        clear_project_status_stats_cache(self.root)
        sqlite_path = str(self.root / "db" / "biblioteca.db")
        first = get_project_stats(
            self.root,
            settings=self.settings,
            registry=self.registry,
            research_batch_registry=self.batch,
            sqlite_path=sqlite_path,
            refresh=True,
        )
        self.assertFalse(first["cached"])
        second = get_project_stats(
            self.root,
            settings=self.settings,
            registry=self.registry,
            research_batch_registry=self.batch,
            sqlite_path=sqlite_path,
            refresh=False,
        )
        self.assertTrue(second["cached"])
        self.assertEqual(first["computed_at"], second["computed_at"])

        handled = try_handle_project_status_get(
            "/api/admin/project-status/stats",
            self.handler,
            data_root=self.root,
            settings=self.settings,
            registry=self.registry,
            research_batch_registry=self.batch,
            sqlite_path=sqlite_path,
            send_json=self.send_json,
            query={},
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["cached"])
        self.assertIn("checklist", payload["stats"])

    def test_put_saves_tree(self) -> None:
        body = json.dumps(
            {
                "version": 1,
                "roots": [
                    {
                        "id": "ingest",
                        "label": "Ingest",
                        "status": "ok",
                        "notes": "done",
                        "children": [],
                    }
                ],
            }
        ).encode("utf-8")

        def read_body(_handler, _max):
            return body

        handled = try_handle_project_status_put(
            "/api/admin/project-status",
            self.handler,
            data_root=self.root,
            settings=self.settings,
            registry=self.registry,
            research_batch_registry=self.batch,
            sqlite_path=str(self.root / "db" / "biblioteca.db"),
            send_json=self.send_json,
            read_body=read_body,
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["tree"]["roots"][0]["status"], "ok")
        self.assertNotIn("stats", payload)
        loaded = load_project_status(self.root)
        self.assertEqual(loaded["roots"][0]["notes"], "done")


if __name__ == "__main__":
    unittest.main()

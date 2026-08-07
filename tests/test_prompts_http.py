import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.api.prompts_http import (
    doctor_book_prompts,
    list_prompt_catalog,
    read_prompt,
    resolve_prompt_path,
    snapshot_book_prompts,
    try_handle_prompts_get,
    try_handle_prompts_post,
    write_prompt,
)


class PromptCatalogTests(unittest.TestCase):
    def test_catalog_has_known_ids(self) -> None:
        ids = {item["id"] for item in list_prompt_catalog()}
        self.assertIn("vision", ids)
        self.assertIn("editor", ids)
        self.assertIn("article", ids)

    def test_resolve_rejects_unknown(self) -> None:
        self.assertIsNone(resolve_prompt_path("not-a-real-prompt"))


class PromptReadWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        rel = Path("src/ingestion/pipeline/prompts/vision_prompt.md")
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("base vision prompt\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_and_write(self) -> None:
        payload = read_prompt("vision", self.root)
        self.assertEqual(payload["content"], "base vision prompt\n")
        updated = write_prompt("vision", "updated vision\n", self.root)
        self.assertEqual(updated["content"], "updated vision\n")
        self.assertEqual(
            (self.root / "src/ingestion/pipeline/prompts/vision_prompt.md").read_text(encoding="utf-8"),
            "updated vision\n",
        )

    def test_write_unknown_id(self) -> None:
        with self.assertRaises(KeyError):
            write_prompt("missing", "x", self.root)


class PromptHttpHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        rel = Path("src/ingestion/pipeline/prompts/editor_prompt.md")
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("editor base\n", encoding="utf-8")
        self.responses: list[tuple[int, dict]] = []

        def send_json(_handler, status, payload):
            self.responses.append((status, payload))

        self.send_json = send_json
        self.handler = MagicMock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_catalog(self) -> None:
        handled = try_handle_prompts_get(
            "/api/admin/prompts",
            self.handler,
            query={},
            repo_root=self.root,
            send_json=self.send_json,
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["prompts"])
        editor = next(item for item in payload["prompts"] if item["id"] == "editor")
        self.assertEqual(editor["content"], "editor base\n")
        self.assertIn("content", payload["prompts"][0])

    def test_get_one(self) -> None:
        handled = try_handle_prompts_get(
            "/api/admin/prompts",
            self.handler,
            query={"id": ["editor"]},
            repo_root=self.root,
            send_json=self.send_json,
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "editor base\n")

    def test_post_save(self) -> None:
        body = b'{"id":"editor","content":"editor saved\\n"}'

        def read_body(_handler, _max_bytes):
            return body

        handled = try_handle_prompts_post(
            "/api/admin/prompts",
            self.handler,
            repo_root=self.root,
            send_json=self.send_json,
            read_body=read_body,
        )
        self.assertTrue(handled)
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "editor saved\n")
        self.assertEqual(
            (self.root / "src/ingestion/pipeline/prompts/editor_prompt.md").read_text(encoding="utf-8"),
            "editor saved\n",
        )

    def test_unrelated_path(self) -> None:
        self.assertFalse(
            try_handle_prompts_get(
                "/api/admin/other",
                self.handler,
                repo_root=self.root,
                send_json=self.send_json,
            )
        )


class PromptDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_root = self.root / "data"
        self.sha = "a" * 64
        for rel in (
            "src/ingestion/pipeline/prompts/glm_ocr_prompt.md",
            "src/ingestion/pipeline/prompts/vision_prompt.md",
            "src/ingestion/pipeline/prompts/editor_prompt.md",
            "src/ingestion/pipeline/prompts/toc_aggregate_refine_prompt.md",
            "src/ingestion/pipeline/prompts/index_aggregate_refine_prompt.md",
            "src/ingestion/pipeline/prompts/reicat_vision_prompt.md",
            "src/ingestion/pipeline/prompts/page_guidance_prompt.md",
            "src/ingestion/pipeline/prompts/biblio_extract_prompt.md",
            "src/ingestion/polyindex/prompts/subject_matcher_prompt.md",
            "src/ingestion/polyindex/prompts/time_index_extract_prompt.md",
        ):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"content for {path.name}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_snapshot_and_doctor_ok(self) -> None:
        output_dir = self.data_root / "output" / self.sha
        output_dir.mkdir(parents=True)
        prompts_used = snapshot_book_prompts(output_dir, repo_root=self.root)
        (output_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": self.sha,
                    "slug": "demo",
                    "reicat": {"titolo": "Demo Book"},
                    "prompts_used": prompts_used,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        report = doctor_book_prompts(self.data_root, self.sha, repo_root=self.root)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["ok"])
        self.assertEqual(report["book_title"], "Demo Book")
        self.assertGreater(report["summary"]["ok"], 0)
        self.assertEqual(report["summary"]["drift"], 0)

    def test_doctor_detects_drift(self) -> None:
        output_dir = self.data_root / "output" / self.sha
        output_dir.mkdir(parents=True)
        prompts_used = snapshot_book_prompts(output_dir, repo_root=self.root)
        (output_dir / "manifest.json").write_text(
            json.dumps({"source_sha256": self.sha, "prompts_used": prompts_used}),
            encoding="utf-8",
        )
        vision = self.root / "src/ingestion/pipeline/prompts/vision_prompt.md"
        vision.write_text("changed vision prompt\n", encoding="utf-8")
        report = doctor_book_prompts(self.data_root, self.sha, repo_root=self.root)
        self.assertEqual(report["status"], "drift")
        vision_item = next(item for item in report["items"] if item["id"] == "vision")
        self.assertEqual(vision_item["status"], "drift")
        self.assertNotEqual(vision_item["saved_sha256"], vision_item["current_sha256"])
        self.assertEqual(
            vision_item["current_sha256"],
            hashlib.sha256(b"changed vision prompt\n").hexdigest(),
        )

    def test_doctor_http(self) -> None:
        output_dir = self.data_root / "output" / self.sha
        output_dir.mkdir(parents=True)
        prompts_used = snapshot_book_prompts(output_dir, repo_root=self.root)
        (output_dir / "manifest.json").write_text(
            json.dumps({"source_sha256": self.sha, "prompts_used": prompts_used}),
            encoding="utf-8",
        )
        responses: list[tuple[int, dict]] = []

        def send_json(_handler, status, payload):
            responses.append((status, payload))

        handled = try_handle_prompts_get(
            "/api/admin/prompts/doctor",
            MagicMock(),
            query={"source_sha256": [self.sha]},
            repo_root=self.root,
            data_root=self.data_root,
            send_json=send_json,
        )
        self.assertTrue(handled)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["books"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()

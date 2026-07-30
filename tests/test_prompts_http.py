import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.api.prompts_http import (
    list_prompt_catalog,
    read_prompt,
    resolve_prompt_path,
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


if __name__ == "__main__":
    unittest.main()

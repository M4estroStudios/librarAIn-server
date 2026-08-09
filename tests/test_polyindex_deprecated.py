from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.page_index_refresh import refresh_polyindex_for_pages
from src.ingestion.polyindex.time_index import sync_time_index_from_book
from src.models.settings import Settings
from src.persistence.polyindex_deprecated import (
    add_deprecated_item,
    count_deprecated_for_book,
    delete_deprecated_item,
    list_deprecated_items,
)

SHA = "a" * 64


def _settings(data_root: Path) -> Settings:
    return Settings.model_validate(
        {
            "DATA_ROOT": str(data_root),
            "OPENAI_PROVIDER": "local",
            "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
            "OPENAI_API_KEY": "test-key",
            "EDITOR_MODEL": "test-editor",
            "MAX_PARALLEL_REQUEST": 2,
            "TIME_INDEX_USE_LLM": False,
        }
    )


class TestPolyindexDeprecatedStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_list_count_and_delete(self) -> None:
        item = add_deprecated_item(
            self.root,
            source_sha256=SHA,
            index_kind="time_index",
            label="1848",
            aligned_page=3,
            original_page=3,
            section="years",
        )
        self.assertIsNotNone(item)
        self.assertEqual(count_deprecated_for_book(self.root, SHA), 1)
        listed = list_deprecated_items(self.root, source_sha256=SHA)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["label"], "1848")
        result = delete_deprecated_item(self.root, listed[0]["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(count_deprecated_for_book(self.root, SHA), 0)


class TestTimeIndexSurgicalRefresh(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.polyindex_dir = self.root / "polyindex"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_book(self, pages: dict[int, str]) -> BookOutput:
        book_dir = self.root / "output" / SHA
        pages_dir = book_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page_outputs = []
        manifest_pages = []
        for aligned, text in pages.items():
            file = pages_dir / f"p.{aligned:04d}.libro-a.md"
            file.write_text(text, encoding="utf-8")
            page_outputs.append(BookPageOutput(aligned=aligned, original=aligned, file=file))
            manifest_pages.append({"aligned": aligned, "original": aligned})
        manifest_path = book_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "slug": "libro-a",
                    "original_page_count": max(pages),
                    "pages": manifest_pages,
                    "reicat": {"title": "Libro A"},
                }
            ),
            encoding="utf-8",
        )
        return BookOutput(
            output_dir=book_dir,
            manifest_path=manifest_path,
            slug="libro-a",
            pages=page_outputs,
        )

    def test_marks_missing_time_refs_as_deprecated_without_purge(self) -> None:
        book = self._make_book(
            {
                1: "Nel 1848 scoppiarono i moti.",
                2: "Testo senza date.",
            }
        )
        sync_time_index_from_book(
            self.polyindex_dir, SHA, book, book_title="Libro A", settings=_settings(self.root)
        )
        page_file = book.pages[0].file
        page_file.write_text("Solo testo senza anni.", encoding="utf-8")
        with patch(
            "src.ingestion.polyindex.page_index_refresh.build_openai_client",
            return_value=object(),
        ):
            result = refresh_polyindex_for_pages(
                self.root,
                _settings(self.root),
                SHA,
                [1],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(count_deprecated_for_book(self.root, SHA), 1)
        global_index = json.loads((self.polyindex_dir / "TIME_INDEX.json").read_text(encoding="utf-8"))
        self.assertIn("1848", global_index["years"])
        self.assertEqual(
            global_index["years"]["1848"]["books"][SHA]["aligned_pages"],
            [1],
        )
        deprecated = list_deprecated_items(self.root, source_sha256=SHA)[0]
        delete_result = delete_deprecated_item(self.root, deprecated["id"])
        self.assertTrue(delete_result["ok"])
        self.assertTrue(delete_result["removed_from_index"])
        global_index = json.loads((self.polyindex_dir / "TIME_INDEX.json").read_text(encoding="utf-8"))
        self.assertNotIn("1848", global_index.get("years") or {})


if __name__ == "__main__":
    unittest.main()

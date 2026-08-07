from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.gallery_index import (
    book_gallery_index_json_path,
    extract_gallery_captions,
    sync_gallery_index_from_book,
    write_gallery_index,
)


class TestExtractGalleryCaptions(unittest.TestCase):
    def test_extracts_single_caption(self) -> None:
        text = "Corpo testo\n\n> Veduta del Colosseo\n\nAltro testo\n"
        self.assertEqual(extract_gallery_captions(text), ["Veduta del Colosseo"])

    def test_extracts_empty_caption(self) -> None:
        text = "Testo\n\n>\n\nFine\n"
        self.assertEqual(extract_gallery_captions(text), [""])

    def test_joins_multiline_caption(self) -> None:
        text = "> Prima riga\n> Seconda riga\n"
        self.assertEqual(extract_gallery_captions(text), ["Prima riga Seconda riga"])

    def test_multiple_caption_blocks(self) -> None:
        text = "> Uno\n\ncorpo\n\n> Due\n"
        self.assertEqual(extract_gallery_captions(text), ["Uno", "Due"])

    def test_strips_frontmatter(self) -> None:
        text = (
            "---\n"
            "aligned_page: 3\n"
            "original_page: 3\n"
            "---\n"
            "Intro\n"
            "> Didascalia\n"
        )
        self.assertEqual(extract_gallery_captions(text), ["Didascalia"])

    def test_no_captions(self) -> None:
        self.assertEqual(extract_gallery_captions("Solo testo senza immagini.\n"), [])


class TestSyncGalleryIndexFromBook(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.output_dir = self.tmp / "output"
        self.pages_dir = self.output_dir / "pages"
        self.pages_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_page(self, aligned: int, body: str) -> Path:
        path = self.pages_dir / f"p.{aligned:04d}.demo.md"
        path.write_text(
            f"---\naligned_page: {aligned}\noriginal_page: {aligned}\n---\n{body}",
            encoding="utf-8",
        )
        return path

    def test_writes_gallery_index_json(self) -> None:
        page_a = self._write_page(2, "Testo\n\n> Fontana di Trevi\n")
        page_b = self._write_page(5, ">\n")
        page_c = self._write_page(7, "Nessuna immagine qui.\n")
        book = BookOutput(
            output_dir=self.output_dir,
            manifest_path=self.output_dir / "manifest.json",
            slug="demo",
            pages=[
                BookPageOutput(aligned=2, original=2, file=page_a),
                BookPageOutput(aligned=5, original=5, file=page_b),
                BookPageOutput(aligned=7, original=7, file=page_c),
            ],
        )

        path, stats = sync_gallery_index_from_book(book, request_id="req-1")

        expected = book_gallery_index_json_path(self.output_dir, "demo")
        self.assertEqual(path, expected)
        self.assertTrue(path.is_file())
        self.assertEqual(stats["n_entries"], 2)
        self.assertEqual(stats["n_pages"], 2)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(
            payload["entries"],
            [
                {
                    "aligned_page": 2,
                    "original_page": 2,
                    "caption": "Fontana di Trevi",
                },
                {
                    "aligned_page": 5,
                    "original_page": 5,
                    "caption": "",
                },
            ],
        )

    def test_write_gallery_index_from_stage3_captions(self) -> None:
        path, stats = write_gallery_index(
            self.output_dir,
            "demo",
            [
                (2, 2, ["Fontana di Trevi"]),
                (5, 5, [""]),
                (7, 7, []),
            ],
            request_id="req-2",
        )
        expected = book_gallery_index_json_path(self.output_dir, "demo")
        self.assertEqual(path, expected)
        self.assertEqual(stats["n_entries"], 2)
        self.assertEqual(stats["n_pages"], 2)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["entries"],
            [
                {
                    "aligned_page": 2,
                    "original_page": 2,
                    "caption": "Fontana di Trevi",
                },
                {
                    "aligned_page": 5,
                    "original_page": 5,
                    "caption": "",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()

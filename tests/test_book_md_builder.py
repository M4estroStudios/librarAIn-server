from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ingestion.book_md_builder import build_book_md
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.models.request import PageRange, UsefulPagesEnumeration

SHA = "cafebabe" * 8
SLUG = "test-book"
TITLE = "Test Book"
AUTHOR = "Author One"
YEAR = 1999


def _enumeration(page_count: int = 3) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=1, end=page_count),
        index_range_aligned=PageRange(start=1, end=page_count),
    )


class TestBuildBookMd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.output_dir = self.tmp / "data" / "output" / SHA
        self.pages_dir = self.output_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self.manifest_path = self.output_dir / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "slug": SLUG,
                    "reicat": {
                        "titolo": TITLE,
                        "autore": [AUTHOR],
                        "anno_di_pubblicazione": YEAR,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.page_files: dict[int, Path] = {}
        for aligned in range(1, 4):
            path = self.pages_dir / f"p.{aligned:04d}.{SLUG}.md"
            path.write_text(f"book page {aligned} content\n", encoding="utf-8")
            self.page_files[aligned] = path

        self.book_output = BookOutput(
            output_dir=self.output_dir,
            manifest_path=self.manifest_path,
            slug=SLUG,
            pages=[
                BookPageOutput(aligned=3, original=3, file=self.page_files[3]),
                BookPageOutput(aligned=1, original=1, file=self.page_files[1]),
                BookPageOutput(aligned=2, original=2, file=self.page_files[2]),
            ],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_build_book_md_three_pages_in_ascending_order(self) -> None:
        book_path = build_book_md(self.book_output, _enumeration())

        self.assertEqual(book_path, self.output_dir / f"{SLUG}.md")
        self.assertTrue(book_path.is_file())

        text = book_path.read_text(encoding="utf-8")
        expected_header = f"# {TITLE}\n\n_{AUTHOR} — {YEAR}_\n\n"
        self.assertTrue(text.startswith(expected_header))

        body = text.removeprefix(expected_header)
        self.assertEqual(
            body,
            (
                "book page 1 content\n"
                "\n\n---\n\n<!-- p.2 (orig. p.2) -->\n\n"
                "book page 2 content\n"
                "\n\n---\n\n<!-- p.3 (orig. p.3) -->\n\n"
                "book page 3 content\n"
            ),
        )

    def test_build_book_md_idempotent_on_second_run(self) -> None:
        first_path = build_book_md(self.book_output, _enumeration())
        first_bytes = first_path.read_bytes()

        second_path = build_book_md(self.book_output, _enumeration())
        second_bytes = second_path.read_bytes()

        self.assertEqual(first_path, second_path)
        self.assertEqual(first_bytes, second_bytes)

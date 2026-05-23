from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.toc_builder import build_toc_md
from src.models.request import PageRange, UsefulPagesEnumeration

SHA = "cafebabe" * 8
SLUG = "test-book"
TITLE = "Test Book"


def _enumeration(toc_start: int, toc_end: int, page_count: int = 5) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=toc_start, end=toc_end),
        index_range_aligned=PageRange(start=page_count, end=page_count),
    )


class TestBuildTocMd(unittest.TestCase):
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
                    "reicat": {"titolo": TITLE, "autore": ["Author One"]},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.page_files: dict[int, Path] = {}
        for aligned in range(1, 6):
            path = self.pages_dir / f"p.{aligned:04d}.{SLUG}.md"
            path.write_text(f"toc page {aligned} content\n", encoding="utf-8")
            self.page_files[aligned] = path

        self.book_output = BookOutput(
            output_dir=self.output_dir,
            manifest_path=self.manifest_path,
            slug=SLUG,
            pages=[
                BookPageOutput(aligned=4, original=4, file=self.page_files[4]),
                BookPageOutput(aligned=2, original=2, file=self.page_files[2]),
                BookPageOutput(aligned=5, original=5, file=self.page_files[5]),
                BookPageOutput(aligned=1, original=1, file=self.page_files[1]),
                BookPageOutput(aligned=3, original=3, file=self.page_files[3]),
            ],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_build_toc_md_three_sections_in_ascending_order(self) -> None:
        toc_path = build_toc_md(self.book_output, _enumeration(toc_start=2, toc_end=4))

        self.assertEqual(toc_path, self.output_dir / "TOC.md")
        self.assertTrue(toc_path.is_file())

        text = toc_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"# TOC — {TITLE}\n\n"))

        body = text.removeprefix(f"# TOC — {TITLE}\n\n")
        sections = body.split("\n\n---\n\n")
        self.assertEqual(len(sections), 3)
        self.assertEqual(
            sections,
            [
                "toc page 2 content\n",
                "toc page 3 content\n",
                "toc page 4 content\n",
            ],
        )

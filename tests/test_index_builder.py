from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ingestion.index_builder import build_index_md
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.models.request import PageRange, UsefulPagesEnumeration

SHA = "cafebabe" * 8
SLUG = "test-book"
TITLE = "Test Book"


def _enumeration(index_start: int, index_end: int, page_count: int = 5) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=page_count, end=page_count),
        index_range_aligned=PageRange(start=index_start, end=index_end),
    )


class TestBuildIndexMd(unittest.TestCase):
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
            path.write_text(f"index page {aligned} content\n", encoding="utf-8")
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

    def test_build_index_md_three_sections_in_ascending_order(self) -> None:
        index_path = build_index_md(self.book_output, _enumeration(index_start=2, index_end=4))

        self.assertEqual(index_path, self.output_dir / "INDEX.md")
        self.assertTrue(index_path.is_file())

        text = index_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"# INDEX — {TITLE}\n\n"))

        body = text.removeprefix(f"# INDEX — {TITLE}\n\n")
        sections = body.split("\n\n---\n\n")
        self.assertEqual(len(sections), 3)
        self.assertEqual(
            sections,
            [
                "index page 2 content\n",
                "index page 3 content\n",
                "index page 4 content\n",
            ],
        )

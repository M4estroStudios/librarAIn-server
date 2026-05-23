from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.ingestion.output_writer import BookOutput
from src.ingestion.polyindex.toc_json import (
    ChapterEntry,
    chapter_entries_to_dicts,
    parse_chapters_from_toc_md,
    sync_polyindex_toc_from_book,
    update_polyindex_toc,
)
from src.models.request import PageRange, UsefulPagesEnumeration

SHA = "cafebabe" * 8
SLUG = "test-book"
TITLE = "Test Book"


def _enumeration(
    page_count: int = 100,
    page_offset: int = 0,
) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig + page_offset for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page={aligned: orig for orig, aligned in mapping.items()},
        toc_range_aligned=PageRange(start=1, end=page_count),
        index_range_aligned=PageRange(start=page_count, end=page_count),
    )


def _sample_toc_md(tmp: Path) -> Path:
    toc_path = tmp / "TOC.md"
    toc_path.write_text(
        "\n".join(
            [
                f"# TOC — {TITLE}",
                "",
                "Introduzione generale 3",
                "Capitolo I .............. 14",
                "Cap. 2 — Introduzione 25",
                "riga sporca non riconosciuta !!!",
                "Capitolo IV 50",
                "Cap. 5 — Conclusione 60",
                "",
                "---",
                "",
                "contenuto ocr pagina successiva",
            ]
        ),
        encoding="utf-8",
    )
    return toc_path


class TestParseChaptersFromTocMd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parse_five_chapters_from_mock_toc_md(self) -> None:
        toc_path = _sample_toc_md(self.tmp)
        entries = parse_chapters_from_toc_md(toc_path, _enumeration())

        self.assertEqual(len(entries), 5)
        self.assertEqual(
            entries,
            [
                ChapterEntry(
                    label="Introduzione generale",
                    aligned_page_start=3,
                    aligned_page_end=13,
                    original_page_start=3,
                    original_page_end=13,
                ),
                ChapterEntry(
                    label="Capitolo I",
                    aligned_page_start=14,
                    aligned_page_end=24,
                    original_page_start=14,
                    original_page_end=24,
                ),
                ChapterEntry(
                    label="Cap. 2 — Introduzione",
                    aligned_page_start=25,
                    aligned_page_end=49,
                    original_page_start=25,
                    original_page_end=49,
                ),
                ChapterEntry(
                    label="Capitolo IV",
                    aligned_page_start=50,
                    aligned_page_end=59,
                    original_page_start=50,
                    original_page_end=59,
                ),
                ChapterEntry(
                    label="Cap. 5 — Conclusione",
                    aligned_page_start=60,
                    aligned_page_end=100,
                    original_page_start=60,
                    original_page_end=100,
                ),
            ],
        )

    def test_unmapped_page_is_skipped_with_warning(self) -> None:
        toc_path = self.tmp / "TOC.md"
        toc_path.write_text("Capitolo IX 999\n", encoding="utf-8")
        entries = parse_chapters_from_toc_md(toc_path, _enumeration(page_count=10))
        self.assertEqual(entries, [])


class TestUpdatePolyindexToc(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.chapters = chapter_entries_to_dicts(
            parse_chapters_from_toc_md(_sample_toc_md(self.tmp), _enumeration())
        )
        self.book_entry = {
            "title": TITLE,
            "slug": SLUG,
            "chapters": self.chapters,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_update_on_empty_dir_creates_one_book(self) -> None:
        toc_path = update_polyindex_toc(self.polyindex_dir, SHA, self.book_entry)

        self.assertEqual(toc_path, self.polyindex_dir / "TOC.json")
        data = json.loads(toc_path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "1.0")
        self.assertEqual(len(data["books"]), 1)
        self.assertEqual(data["books"][SHA]["title"], TITLE)
        self.assertEqual(data["books"][SHA]["slug"], SLUG)
        self.assertEqual(len(data["books"][SHA]["chapters"]), 5)

    def test_update_same_sha_replaces_without_growth(self) -> None:
        update_polyindex_toc(self.polyindex_dir, SHA, self.book_entry)
        replacement = {
            "title": "Replaced Title",
            "slug": "replaced-slug",
            "chapters": [],
        }
        update_polyindex_toc(self.polyindex_dir, SHA, replacement)

        data = json.loads((self.polyindex_dir / "TOC.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["books"]), 1)
        self.assertEqual(data["books"][SHA]["title"], "Replaced Title")
        self.assertEqual(data["books"][SHA]["slug"], "replaced-slug")
        self.assertEqual(data["books"][SHA]["chapters"], [])

    def test_concurrent_updates_do_not_corrupt_toc_json(self) -> None:
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(suffix: str) -> None:
            try:
                barrier.wait(timeout=5)
                update_polyindex_toc(
                    self.polyindex_dir,
                    SHA,
                    {
                        "title": f"{TITLE}-{suffix}",
                        "slug": f"{SLUG}-{suffix}",
                        "chapters": self.chapters,
                    },
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        data = json.loads((self.polyindex_dir / "TOC.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["books"]), 1)
        self.assertIn(data["books"][SHA]["title"], {f"{TITLE}-0", f"{TITLE}-1"})
        self.assertEqual(len(data["books"][SHA]["chapters"]), 5)


class TestSyncPolyindexTocFromBook(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.output_dir = self.tmp / "data" / "output" / SHA
        self.output_dir.mkdir(parents=True, exist_ok=True)
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
        self.book_output = BookOutput(
            output_dir=self.output_dir,
            manifest_path=self.manifest_path,
            slug=SLUG,
            pages=[],
        )
        self.toc_md_path = _sample_toc_md(self.output_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sync_writes_toc_json_from_toc_md_and_manifest(self) -> None:
        toc_json_path = sync_polyindex_toc_from_book(
            self.polyindex_dir,
            SHA,
            self.book_output,
            self.toc_md_path,
            _enumeration(),
        )

        self.assertEqual(toc_json_path, self.polyindex_dir / "TOC.json")
        self.assertTrue(toc_json_path.is_file())

        data = json.loads(toc_json_path.read_text(encoding="utf-8"))
        book = data["books"][SHA]
        self.assertEqual(book["title"], TITLE)
        self.assertEqual(book["slug"], SLUG)
        self.assertEqual(len(book["chapters"]), 5)
        self.assertEqual(book["chapters"][0]["label"], "Introduzione generale")
        self.assertEqual(book["chapters"][0]["original_page_start"], 3)
        self.assertEqual(book["chapters"][0]["aligned_page_start"], 3)

    def test_sync_uses_slug_when_manifest_title_missing(self) -> None:
        self.manifest_path.write_text(
            json.dumps({"slug": SLUG, "reicat": {"autore": ["Author One"]}}, ensure_ascii=False),
            encoding="utf-8",
        )

        sync_polyindex_toc_from_book(
            self.polyindex_dir,
            SHA,
            self.book_output,
            self.toc_md_path,
            _enumeration(),
        )

        data = json.loads((self.polyindex_dir / "TOC.json").read_text(encoding="utf-8"))
        self.assertEqual(data["books"][SHA]["title"], SLUG)

    def test_sync_with_page_offset_maps_aligned_pages(self) -> None:
        sync_polyindex_toc_from_book(
            self.polyindex_dir,
            SHA,
            self.book_output,
            self.toc_md_path,
            _enumeration(page_offset=10),
        )

        data = json.loads((self.polyindex_dir / "TOC.json").read_text(encoding="utf-8"))
        first = data["books"][SHA]["chapters"][0]
        self.assertEqual(first["original_page_start"], 3)
        self.assertEqual(first["aligned_page_start"], 13)

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.hashing import compute_file_sha256
from src.ingestion.output_writer import (
    BookOutput,
    BookPageOutput,
    build_page_frontmatter,
    materialize_book_pages,
    split_page_frontmatter,
    stamp_page_metadata,
    strip_page_frontmatter,
)
from src.ingestion.pipeline.stage3 import Stage3PageResult, Stage3Result
from src.models.polyindex_index import BookIndexDocument, BookIndexSubjectEntry
from src.models.polyindex_toc import PolyindexTocChapter
from src.models.request import (
    EnrichedIngestRequest,
    IngestRequest,
    PageRange,
    ReicatMetadata,
    UsefulPagesEnumeration,
)

SHA = "deadbeef" * 8
PAGE_COUNT = 5
SLUG = "test-book"


def _enumeration() -> UsefulPagesEnumeration:
    original_pages = list(range(1, PAGE_COUNT + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=PAGE_COUNT,
        aligned_page_count=PAGE_COUNT,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=PAGE_COUNT, end=PAGE_COUNT),
    )


def _enriched() -> EnrichedIngestRequest:
    return EnrichedIngestRequest(
        request=IngestRequest(
            source_pdf_path="/fake/book.pdf",
            pages_to_remove=[],
            toc_range=PageRange(start=1, end=1),
            index_range=PageRange(start=PAGE_COUNT, end=PAGE_COUNT),
            reicat=ReicatMetadata.model_validate(
                {"titolo": "Test Book", "autore": ["Author One"]}
            ),
        ),
        source_sha256=SHA,
        source_pdf_path="/fake/book.pdf",
        source_pdf_page_count=PAGE_COUNT,
    )


def _settings(data_root: str) -> MagicMock:
    settings = MagicMock()
    settings.data_root = data_root
    return settings


class TestMaterializeBookPages(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.stage3_dir = self.tmp / "data" / "tmp" / SHA / "stage3Editor"
        self.stage3_dir.mkdir(parents=True, exist_ok=True)

        self.stage3_pages: list[Stage3PageResult] = []
        for page in range(1, PAGE_COUNT + 1):
            md_path = self.stage3_dir / f"p.{page:04d}.{SLUG}.md"
            md_path.write_text(f"page {page} content\n", encoding="utf-8")
            self.stage3_pages.append(
                Stage3PageResult(
                    aligned_page=page,
                    original_page=page,
                    md_path=str(md_path),
                    char_count=len(md_path.read_text(encoding="utf-8")),
                    stage2_char_count=10,
                    char_delta=0,
                )
            )

        self.stage3_result = Stage3Result(
            pages=self.stage3_pages,
            skipped_existing=0,
            missing=[],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_materialize_creates_pages_and_manifest(self) -> None:
        result = materialize_book_pages(
            self.stage3_result,
            _enriched(),
            SHA,
            _enumeration(),
            _settings(self.data_root),
            request_id="req-output-001",
        )

        output_dir = Path(self.data_root) / "output" / SHA
        pages_dir = output_dir / "pages"
        manifest_path = output_dir / "manifest.json"

        self.assertEqual(result.slug, SLUG)
        self.assertEqual(result.output_dir, output_dir)
        self.assertEqual(result.manifest_path, manifest_path)
        self.assertEqual(len(result.pages), PAGE_COUNT)

        page_files = sorted(pages_dir.glob("*.md"))
        self.assertEqual(len(page_files), PAGE_COUNT)
        for page in range(1, PAGE_COUNT + 1):
            expected = pages_dir / f"p.{page:04d}.{SLUG}.md"
            self.assertTrue(expected.is_file())
            self.assertEqual(
                expected.read_text(encoding="utf-8"),
                f"page {page} content\n",
            )

        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_sha256"], SHA)
        self.assertEqual(manifest["slug"], SLUG)
        self.assertEqual(manifest["original_page_count"], PAGE_COUNT)
        self.assertEqual(manifest["aligned_page_count"], PAGE_COUNT)
        self.assertEqual(manifest["pages_to_remove"], [])
        self.assertEqual(manifest["toc_range"], {"start": 1, "end": 1})
        self.assertEqual(manifest["index_range"], {"start": PAGE_COUNT, "end": PAGE_COUNT})
        self.assertEqual(manifest["toc_range_aligned"], {"start": 1, "end": 1})
        self.assertEqual(manifest["index_range_aligned"], {"start": PAGE_COUNT, "end": PAGE_COUNT})
        self.assertNotIn("biblio_range", manifest)
        self.assertNotIn("biblio_range_aligned", manifest)
        self.assertEqual(manifest["pipeline_version"], "1.0")
        self.assertIn("generated_at", manifest)
        self.assertIn("prompts_used", manifest)
        self.assertTrue(isinstance(manifest["prompts_used"].get("items"), list))
        self.assertGreater(len(manifest["prompts_used"]["items"]), 0)
        for item in manifest["prompts_used"]["items"]:
            snap = output_dir / item["file"]
            self.assertTrue(snap.is_file(), item["file"])
            self.assertTrue(item.get("sha256"))
        self.assertEqual(len(manifest["pages"]), PAGE_COUNT)

        aligned_values = [entry["aligned"] for entry in manifest["pages"]]
        self.assertEqual(aligned_values, sorted(aligned_values))
        for entry in manifest["pages"]:
            self.assertTrue(str(entry["file"]).startswith("pages/p."))
            self.assertTrue(str(entry["file"]).endswith(f".{SLUG}.md"))

        reicat = manifest["reicat"]
        self.assertIn("titolo", reicat)
        self.assertIn("autore", reicat)
        self.assertEqual(reicat["titolo"], "Test Book")
        self.assertEqual(reicat["autore"], ["Author One"])
        md_formatting = manifest["md_formatting"]
        self.assertIn("md_h1", md_formatting)
        self.assertIn("md_no_invent", md_formatting)
        self.assertTrue(md_formatting["md_h1"])

    def test_materialize_strips_stage_model_marker(self) -> None:
        md_path = self.stage3_dir / f"p.0001.{SLUG}.md"
        md_path.write_text(
            "<!-- librarain:model=gpt-test -->\npage 1 content\n",
            encoding="utf-8",
        )
        result = materialize_book_pages(
            self.stage3_result,
            _enriched(),
            SHA,
            _enumeration(),
            _settings(self.data_root),
        )
        page_path = result.output_dir / "pages" / f"p.0001.{SLUG}.md"
        text = page_path.read_text(encoding="utf-8")
        self.assertNotIn("<!-- librarain:model=", text)
        self.assertEqual(text, "page 1 content\n")
        stage3_text = md_path.read_text(encoding="utf-8")
        self.assertIn("<!-- librarain:model=gpt-test -->", stage3_text)

    def test_second_call_is_idempotent(self) -> None:
        settings = _settings(self.data_root)
        enriched = _enriched()
        useful_pages = _enumeration()

        first = materialize_book_pages(
            self.stage3_result,
            enriched,
            SHA,
            useful_pages,
            settings,
        )
        first_hashes = {
            path: compute_file_sha256(path)
            for path in sorted((first.output_dir / "pages").glob("*.md"))
        }
        manifest_hash = compute_file_sha256(first.manifest_path)

        second = materialize_book_pages(
            self.stage3_result,
            enriched,
            SHA,
            useful_pages,
            settings,
        )
        second_hashes = {
            path: compute_file_sha256(path)
            for path in sorted((second.output_dir / "pages").glob("*.md"))
        }

        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(manifest_hash, compute_file_sha256(second.manifest_path))

    def test_materialize_persists_ranges_and_removals(self) -> None:
        original_count = 5
        removed = [3]
        kept = [1, 2, 4, 5]
        o2a = {1: 1, 2: 2, 4: 3, 5: 4}
        a2o = {aligned: original for original, aligned in o2a.items()}
        stage3_pages = []
        for original in kept:
            aligned = o2a[original]
            md_path = self.stage3_dir / f"p.{aligned:04d}.{SLUG}.md"
            md_path.write_text(f"page {aligned}\n", encoding="utf-8")
            stage3_pages.append(
                Stage3PageResult(
                    aligned_page=aligned,
                    original_page=original,
                    md_path=str(md_path),
                    char_count=8,
                    stage2_char_count=8,
                    char_delta=0,
                )
            )
        stage3_result = Stage3Result(pages=stage3_pages, skipped_existing=0, missing=[])
        enriched = EnrichedIngestRequest(
            request=IngestRequest(
                source_pdf_path="/fake/book.pdf",
                pages_to_remove=removed,
                toc_range=PageRange(start=1, end=2),
                index_range=PageRange(start=4, end=5),
                biblio_range=PageRange(start=5, end=5),
                reicat=ReicatMetadata.model_validate(
                    {"titolo": "Test Book", "autore": ["Author One"]}
                ),
            ),
            source_sha256=SHA,
            source_pdf_path="/fake/book.pdf",
            source_pdf_page_count=original_count,
        )
        useful = UsefulPagesEnumeration(
            source_sha256=SHA,
            original_page_count=original_count,
            aligned_page_count=len(kept),
            useful_original_pages=kept,
            original_page_to_aligned_page=o2a,
            aligned_page_to_original_page=a2o,
            toc_range_aligned=PageRange(start=1, end=2),
            index_range_aligned=PageRange(start=3, end=4),
            biblio_range_aligned=PageRange(start=4, end=4),
        )

        materialize_book_pages(
            stage3_result,
            enriched,
            SHA,
            useful,
            _settings(self.data_root),
        )
        manifest = json.loads(
            (Path(self.data_root) / "output" / SHA / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["original_page_count"], 5)
        self.assertEqual(manifest["aligned_page_count"], 4)
        self.assertEqual(manifest["pages_to_remove"], [3])
        self.assertEqual(manifest["toc_range"], {"start": 1, "end": 2})
        self.assertEqual(manifest["index_range"], {"start": 4, "end": 5})
        self.assertEqual(manifest["biblio_range"], {"start": 5, "end": 5})
        self.assertEqual(manifest["toc_range_aligned"], {"start": 1, "end": 2})
        self.assertEqual(manifest["index_range_aligned"], {"start": 3, "end": 4})
        self.assertEqual(manifest["biblio_range_aligned"], {"start": 4, "end": 4})
        self.assertEqual(len(manifest["pages"]), 4)
        self.assertEqual(
            [(p["original"], p["aligned"]) for p in manifest["pages"]],
            [(1, 1), (2, 2), (4, 3), (5, 4)],
        )

    def test_materialize_rejects_aligned_count_mismatch(self) -> None:
        incomplete = Stage3Result(
            pages=self.stage3_pages[:-1],
            skipped_existing=0,
            missing=[PAGE_COUNT],
        )
        with self.assertRaisesRegex(ValueError, r"missing aligned pages: \[5\]"):
            materialize_book_pages(
                incomplete,
                _enriched(),
                SHA,
                _enumeration(),
                _settings(self.data_root),
            )


class TestStampPageMetadata(unittest.TestCase):
    def test_build_and_split_frontmatter_roundtrip(self) -> None:
        frontmatter = build_page_frontmatter(
            aligned_page=39,
            original_page=39,
            chapter_number="2",
            chapter_name="Introduzione",
            index_success=[],
            index_failed=["Acqua Claudia"],
        )
        body = "Testo pagina.\n"
        raw_fm, raw_body = split_page_frontmatter(frontmatter + body)
        self.assertIsNotNone(raw_fm)
        self.assertIn("aligned_page: 39", raw_fm or "")
        self.assertIn("chapter_number: \"2\"", raw_fm or "")
        self.assertIn("success:", raw_fm or "")
        self.assertIn("failed:", raw_fm or "")
        self.assertIn('- "Acqua Claudia"', raw_fm or "")
        self.assertEqual(raw_body, body)
        self.assertEqual(strip_page_frontmatter(frontmatter + body), body)

    def test_stamp_keeps_unlinked_index_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            page_path = pages_dir / "p.0039.test-book.md"
            page_path.write_text(
                "<!-- librarain:model=test -->\nTesto senza match esplicito.\n",
                encoding="utf-8",
            )
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[BookPageOutput(aligned=39, original=39, file=page_path)],
            )
            chapters = [
                PolyindexTocChapter(
                    label="Cap. 2 — Introduzione",
                    aligned_page_start=30,
                    aligned_page_end=50,
                    original_page_start=30,
                    original_page_end=50,
                )
            ]
            index_document = BookIndexDocument(
                subjects={
                    "acqua-claudia": BookIndexSubjectEntry(
                        canonical_label="Acqua Claudia",
                        aligned_pages=[39],
                        original_pages=[39],
                    )
                },
                page_subjects={"39": ["acqua-claudia"]},
            )
            stats = stamp_page_metadata(book, chapters, index_document, request_id="req")
            self.assertEqual(stats["pages_updated"], 1)
            text = page_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"))
            self.assertIn("aligned_page: 39", text)
            self.assertIn("original_page: 39", text)
            self.assertIn('chapter_number: "2"', text)
            self.assertIn('chapter_name: "Introduzione"', text)
            self.assertIn("index_connections:", text)
            success_block = text.split("success:", 1)[1].split("failed:", 1)[0]
            failed_block = text.split("failed:", 1)[1].split("---", 1)[0]
            self.assertIn("regex:", success_block)
            self.assertIn("[]", success_block.split("ai:", 1)[0])
            self.assertIn("ai:", success_block)
            self.assertIn("regex:", failed_block)
            self.assertIn('- "Acqua Claudia"', failed_block.split("ai:", 1)[1])
            self.assertNotIn("key:", text)
            self.assertNotIn("label:", text)
            self.assertNotIn("<!-- librarain:model=", text)
            self.assertIn("Testo senza match esplicito.", text)

    def test_stamp_splits_success_and_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            page_path = pages_dir / "p.0039.test-book.md"
            page_path.write_text(
                "[Acqua Claudia](p.0100.test-book.md)\n",
                encoding="utf-8",
            )
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[BookPageOutput(aligned=39, original=39, file=page_path)],
            )
            index_document = BookIndexDocument(
                subjects={
                    "acqua-claudia": BookIndexSubjectEntry(
                        canonical_label="Acqua Claudia",
                        aligned_pages=[39],
                        original_pages=[39],
                    ),
                    "augusto": BookIndexSubjectEntry(
                        canonical_label="Augusto",
                        aligned_pages=[39],
                        original_pages=[39],
                    ),
                },
                page_subjects={"39": ["acqua-claudia", "augusto"]},
            )
            stamp_page_metadata(book, [], index_document)
            text = page_path.read_text(encoding="utf-8")
            success_block = text.split("success:", 1)[1].split("failed:", 1)[0]
            failed_block = text.split("failed:", 1)[1].split("---", 1)[0]
            self.assertIn('- "Acqua Claudia"', success_block)
            self.assertIn('- "Augusto"', failed_block)
            self.assertNotIn("key:", text)
            self.assertNotIn("chapter_number:", text)

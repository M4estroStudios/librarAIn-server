from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.hashing import compute_file_sha256
from src.ingestion.output_writer import materialize_book_pages
from src.ingestion.pipeline.stage3 import Stage3PageResult, Stage3Result
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
        self.assertEqual(manifest["pipeline_version"], "1.0")
        self.assertIn("generated_at", manifest)
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

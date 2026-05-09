from __future__ import annotations

import copy
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import build_aligned_pdf
from src.ingestion.request_validation import run_ingest_gate_phase, validate_and_enrich_request
from src.models.request import (
    IngestInputErrorCode,
    IngestInputValidationError,
    SourceHashGateStatus,
    UsefulPagesEnumeration,
)
from src.persistence.book_sqlite import upsert_book_reicat


def _minimal_pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class PageEnumerationTests(unittest.TestCase):
    def test_enumeration_matches_aligned_pdf_maps(self) -> None:
        pdf_body = _minimal_pdf_bytes(10)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [1, 3, 10],
                "toc_range": {"start": 4, "end": 5},
                "index_range": {"start": 6, "end": 9},
                "reicat": {"titolo": "T", "autore": ["A"]},
            }
            enriched = validate_and_enrich_request(payload)
            self.assertEqual(enriched.source_pdf_page_count, 10)
            aligned = build_aligned_pdf(enriched, str(processed_dir))
            enumerated = build_useful_pages_enumeration(enriched, aligned)

        self.assertIsInstance(enumerated, UsefulPagesEnumeration)
        self.assertEqual(dict(enumerated.original_page_to_aligned_page), aligned.original_page_to_aligned_page)
        self.assertEqual(dict(enumerated.aligned_page_to_original_page), aligned.aligned_page_to_original_page)
        self.assertEqual(enumerated.toc_range_aligned.start, aligned.original_page_to_aligned_page[4])
        self.assertEqual(enumerated.toc_range_aligned.end, aligned.original_page_to_aligned_page[5])
        self.assertEqual(enumerated.index_range_aligned.start, aligned.original_page_to_aligned_page[6])
        self.assertEqual(enumerated.index_range_aligned.end, aligned.original_page_to_aligned_page[9])
        self.assertEqual(enumerated.useful_original_pages, [2, 4, 5, 6, 7, 8, 9])

    def test_enumeration_without_alignment_duplicate_path(self) -> None:
        pdf_body = _minimal_pdf_bytes(20)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "library.db"
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [1, 2],
                "toc_range": {"start": 10, "end": 12},
                "index_range": {"start": 15, "end": 18},
                "reicat": {"titolo": "B", "autore": ["A"]},
                "options": {"force_metadata_update_on_duplicate_hash": True},
            }
            enriched_first = validate_and_enrich_request(payload)
            phase_first = run_ingest_gate_phase(enriched_first, str(sqlite_path))
            upsert_book_reicat(enriched_first, str(sqlite_path))
            enriched_dup = validate_and_enrich_request(copy.deepcopy(payload))
            phase_dup = run_ingest_gate_phase(enriched_dup, str(sqlite_path))
            self.assertEqual(phase_dup.gate.status, SourceHashGateStatus.DUPLICATE_SOURCE_HASH)

            enumerated = build_useful_pages_enumeration(enriched_dup, None)

        self.assertEqual(enumerated.original_page_count, 20)
        self.assertEqual(enumerated.aligned_page_count, 18)
        self.assertEqual(enumerated.useful_original_pages[0], 3)
        self.assertEqual(len(enumerated.useful_original_pages), 18)
        self.assertEqual(enumerated.toc_range_aligned.start, 8)
        self.assertEqual(enumerated.toc_range_aligned.end, 10)
        self.assertEqual(enumerated.index_range_aligned.start, 13)
        self.assertEqual(enumerated.index_range_aligned.end, 16)

    def test_enumeration_rejects_alignment_forward_drift(self) -> None:
        pdf_body = _minimal_pdf_bytes(8)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [8],
                "toc_range": {"start": 1, "end": 2},
                "index_range": {"start": 3, "end": 7},
                "reicat": {"titolo": "C", "autore": ["A"]},
            }
            enriched = validate_and_enrich_request(payload)
            aligned = build_aligned_pdf(enriched, str(processed_dir))
            mutated = aligned.model_copy(
                update={
                    "original_page_to_aligned_page": {**aligned.original_page_to_aligned_page, 1: 999}
                }
            )
            with self.assertRaises(ValueError) as ctx:
                build_useful_pages_enumeration(enriched, mutated)

        parsed = IngestInputValidationError.model_validate_json(str(ctx.exception))
        self.assertEqual(parsed.code, IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH)


if __name__ == "__main__":
    unittest.main()

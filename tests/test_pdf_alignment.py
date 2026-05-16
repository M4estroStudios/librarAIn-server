from __future__ import annotations

import hashlib
import tempfile
import unittest
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from src.ingestion.pdf_alignment import (
    DEFAULT_PAGE_RANGE_PER_THREAD,
    _alignment_chunk_specs,
    build_aligned_pdf,
    build_page_removal_mapping,
    maybe_run_pdf_alignment,
    resolve_aligned_pdf_path_for_stage1,
)
from src.ingestion.request_validation import run_ingest_gate_phase, validate_and_enrich_request
from src.models.request import (
    IngestInputErrorCode,
    IngestInputValidationError,
    SourceHashGateStatus,
)
from src.persistence.book_sqlite import upsert_book_reicat


def _minimal_pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _valid_payload(source_pdf_path: str, *, pdf_pages: int) -> dict:
    toc_end = min(20, pdf_pages)
    toc_start = min(10, toc_end)
    index_end = pdf_pages
    index_start = max(toc_end + 1, pdf_pages - 20)
    if index_start > index_end:
        index_start = max(1, index_end - 5)
    return {
        "schema_version": "1.0",
        "source_pdf_path": source_pdf_path,
        "pages_to_remove": [1, 2],
        "toc_range": {"start": toc_start, "end": toc_end},
        "index_range": {"start": index_start, "end": index_end},
        "reicat": {
            "titolo": "Test Book",
            "autore": ["Author Name"],
        },
        "options": {"force_metadata_update_on_duplicate_hash": True},
    }


class PdfAlignmentTests(unittest.TestCase):
    def test_alignment_chunk_specs_ten_wide(self) -> None:
        self.assertEqual(
            _alignment_chunk_specs(25, 10),
            [(0, 10), (10, 20), (20, 25)],
        )
        self.assertEqual(
            DEFAULT_PAGE_RANGE_PER_THREAD,
            10,
        )
        self.assertEqual(_alignment_chunk_specs(10, 10), [(0, 10)])
        self.assertEqual(_alignment_chunk_specs(1, 10), [(0, 1)])

    def test_build_page_removal_mapping_dense(self) -> None:
        n, fwd, rev = build_page_removal_mapping(10, [1, 2, 10])
        self.assertEqual(n, 7)
        self.assertEqual(fwd[3], 1)
        self.assertEqual(fwd[9], 7)
        self.assertEqual(rev[1], 3)
        self.assertEqual(rev[7], 9)
        self.assertEqual(len(fwd), 7)

    def test_build_page_removal_mapping_empty_removal_is_identity(self) -> None:
        n, fwd, rev = build_page_removal_mapping(5, [])
        self.assertEqual(n, 5)
        for i in range(1, 6):
            self.assertEqual(fwd[i], i)
            self.assertEqual(rev[i], i)

    def test_build_aligned_pdf_writes_expected_pages(self) -> None:
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
                "reicat": {"titolo": "Aligned", "autore": ["Someone"]},
            }
            enriched = validate_and_enrich_request(payload)

            outcome = build_aligned_pdf(enriched, str(processed_dir))

            digest = hashlib.sha256(pdf_body).hexdigest().lower()
            aligned_path = processed_dir / f"{digest}.pdf"
            self.assertEqual(outcome.source_sha256, digest)
            self.assertEqual(Path(outcome.aligned_pdf_path), aligned_path.resolve())
            self.assertTrue(aligned_path.is_file())

            reader = PdfReader(str(aligned_path), strict=False)
            self.assertEqual(len(reader.pages), 7)

            expected_o2a = {2: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}
            self.assertEqual(dict(outcome.original_page_to_aligned_page), expected_o2a)
            self.assertEqual(dict(outcome.aligned_page_to_original_page), {v: k for k, v in expected_o2a.items()})

    def test_build_aligned_pdf_with_empty_pages_to_remove_copies_all_pages(self) -> None:
        pdf_body = _minimal_pdf_bytes(15)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            payload = deepcopy(_valid_payload(str(raw_path), pdf_pages=15))
            payload["pages_to_remove"] = []
            enriched = validate_and_enrich_request(payload)
            outcome = build_aligned_pdf(enriched, str(processed_dir))

            digest = hashlib.sha256(pdf_body).hexdigest().lower()
            aligned_path = Path(outcome.aligned_pdf_path)
            self.assertEqual(outcome.original_page_count, 15)
            self.assertEqual(outcome.aligned_page_count, 15)
            self.assertEqual(Path(outcome.aligned_pdf_path), (processed_dir / f"{digest}.pdf").resolve())
            reader_out = PdfReader(str(aligned_path), strict=False)
            self.assertEqual(len(reader_out.pages), 15)
            for pg in range(1, 16):
                self.assertEqual(outcome.original_page_to_aligned_page[pg], pg)

    def test_build_aligned_pdf_idempotent_rewrite_same_path(self) -> None:
        pdf_body = _minimal_pdf_bytes(6)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [1, 6],
                "toc_range": {"start": 2, "end": 3},
                "index_range": {"start": 4, "end": 5},
                "reicat": {"titolo": "T", "autore": ["A"]},
            }
            enriched = validate_and_enrich_request(payload)
            first = build_aligned_pdf(enriched, str(processed_dir))
            second = build_aligned_pdf(enriched, str(processed_dir))

            self.assertEqual(first.aligned_pdf_path, second.aligned_pdf_path)
            self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))

    def test_build_aligned_pdf_rejects_missing_source_path_before_open(self) -> None:
        pdf_body = _minimal_pdf_bytes(7)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            enriched = validate_and_enrich_request(
                {
                    "schema_version": "1.0",
                    "source_pdf_path": str(raw_path),
                    "pages_to_remove": [],
                    "toc_range": {"start": 1, "end": 3},
                    "index_range": {"start": 4, "end": 7},
                    "reicat": {"titolo": "T", "autore": ["A"]},
                }
            )
            broken = enriched.model_copy(
                update={"source_pdf_path": str(Path(tmp_dir) / "nonexistent.pdf")}
            )
            with self.assertRaises(ValueError) as ctx:
                build_aligned_pdf(broken, str(processed_dir))
        parsed = IngestInputValidationError.model_validate_json(str(ctx.exception))
        self.assertEqual(parsed.code, IngestInputErrorCode.PDF_NOT_FOUND)

    def test_build_aligned_pdf_rejects_removed_page_above_pdf_length(self) -> None:
        pdf_body = _minimal_pdf_bytes(6)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [5, 6],
                "toc_range": {"start": 1, "end": 2},
                "index_range": {"start": 3, "end": 4},
                "reicat": {"titolo": "T", "autore": ["A"]},
            }
            enriched = validate_and_enrich_request(payload)
            tampered = enriched.model_copy(
                update={
                    "request": enriched.request.model_copy(
                        update={"pages_to_remove": [1, 500]}
                    )
                }
            )

            with self.assertRaises(ValueError) as ctx:
                build_aligned_pdf(tampered, str(processed_dir))

        parsed = IngestInputValidationError.model_validate_json(str(ctx.exception))
        self.assertEqual(parsed.code, IngestInputErrorCode.PAGES_INVALID)

    def test_build_aligned_pdf_rejects_digest_mismatch(self) -> None:
        pdf_body = _minimal_pdf_bytes(8)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            processed_dir = Path(tmp_dir) / "processed"
            enriched = validate_and_enrich_request(
                deepcopy(_valid_payload(str(raw_path), pdf_pages=8))
            )
            raw_path.write_bytes(_minimal_pdf_bytes(9))

            with self.assertRaises(ValueError) as ctx:
                build_aligned_pdf(enriched, str(processed_dir))

        self.assertIn("SOURCE_DIGEST_MISMATCH", str(ctx.exception))

    def test_maybe_run_pdf_alignment_skipped_when_duplicate_hash_path(self) -> None:
        pdf_body = _minimal_pdf_bytes(20)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            payload = deepcopy(_valid_payload(str(raw_path), pdf_pages=20))
            enriched_once = validate_and_enrich_request(payload)
            phase_first = run_ingest_gate_phase(enriched_once, str(sqlite_path))
            upsert_book_reicat(enriched_once, str(sqlite_path))
            enriched_dup = validate_and_enrich_request(deepcopy(payload))
            phase_dup = run_ingest_gate_phase(enriched_dup, str(sqlite_path))

            self.assertEqual(phase_first.gate.status, SourceHashGateStatus.NEW_HASH)
            self.assertEqual(phase_dup.gate.status, SourceHashGateStatus.DUPLICATE_SOURCE_HASH)

            processed_dir = Path(tmp_dir) / "processed"

            aligned_first = maybe_run_pdf_alignment(enriched_once, phase_first, str(processed_dir))
            assert aligned_first is not None
            self.assertTrue(Path(aligned_first.aligned_pdf_path).is_file())

            aligned_second = maybe_run_pdf_alignment(enriched_dup, phase_dup, str(processed_dir))
            self.assertIsNone(aligned_second)

            resolved = resolve_aligned_pdf_path_for_stage1(
                enriched_dup,
                None,
                str(processed_dir),
                page_range_per_thread=DEFAULT_PAGE_RANGE_PER_THREAD,
            )
            self.assertTrue(resolved.is_file())
            self.assertEqual(
                resolved.resolve(), Path(aligned_first.aligned_pdf_path).resolve()
            )

    def test_resolve_aligned_pdf_rebuilds_when_skipped_and_missing(
        self,
    ) -> None:
        pdf_body = _minimal_pdf_bytes(20)
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "book.pdf"
            raw_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            payload = deepcopy(_valid_payload(str(raw_path), pdf_pages=20))
            enriched_once = validate_and_enrich_request(payload)
            phase_first = run_ingest_gate_phase(enriched_once, str(sqlite_path))
            upsert_book_reicat(enriched_once, str(sqlite_path))
            enriched_dup = validate_and_enrich_request(deepcopy(payload))
            phase_dup = run_ingest_gate_phase(enriched_dup, str(sqlite_path))
            processed_dir = Path(tmp_dir) / "processed"
            aligned_first = maybe_run_pdf_alignment(
                enriched_once, phase_first, str(processed_dir)
            )
            assert aligned_first is not None
            maybe_run_pdf_alignment(enriched_dup, phase_dup, str(processed_dir))
            Path(aligned_first.aligned_pdf_path).unlink()
            resolved = resolve_aligned_pdf_path_for_stage1(
                enriched_dup,
                None,
                str(processed_dir),
                page_range_per_thread=DEFAULT_PAGE_RANGE_PER_THREAD,
            )
            self.assertTrue(resolved.is_file())


if __name__ == "__main__":
    unittest.main()

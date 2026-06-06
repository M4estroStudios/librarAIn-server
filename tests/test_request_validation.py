from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from src.ingestion.request_validation import (
    _compute_file_sha256,
    init_books_schema,
    insert_book_minimal,
    run_ingest_gate_phase,
    source_hash_gate,
    upsert_book_reicat,
    validate_and_enrich_request,
)
from src.models.request import IngestInputErrorCode, SourceHashGateStatus


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


class RequestValidationTests(unittest.TestCase):
    def test_compute_file_sha256_returns_expected_digest(self) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(b"abc123")
            tmp_path = Path(tmp_file.name)
        try:
            digest = _compute_file_sha256(tmp_path)
            self.assertEqual(digest, hashlib.sha256(b"abc123").hexdigest())
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_and_enrich_request_success(self) -> None:
        pdf_body = _minimal_pdf_bytes(130)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(pdf_body)
            tmp_path = Path(tmp_file.name)
        try:
            result = validate_and_enrich_request(
                _valid_payload(str(tmp_path), pdf_pages=130)
            )
            self.assertEqual(result.request.source_pdf_path, str(tmp_path))
            self.assertEqual(result.source_pdf_path, str(tmp_path))
            self.assertEqual(result.source_pdf_page_count, 130)
            self.assertEqual(result.source_sha256, hashlib.sha256(pdf_body).hexdigest())
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_and_enrich_request_invalid_payload(self) -> None:
        payload = _valid_payload("dummy.pdf", pdf_pages=130)
        payload["toc_range"] = {"start": 20, "end": 10}
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)
        self.assertEqual(error_payload["field"], "payload")

    def test_validate_and_enrich_request_missing_pdf(self) -> None:
        payload = _valid_payload("/tmp/this-file-does-not-exist.pdf", pdf_pages=130)
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.PDF_NOT_FOUND.value)
        self.assertEqual(error_payload["field"], "source_pdf_path")

    def test_validate_and_enrich_request_empty_title_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf", pdf_pages=130)
        payload["reicat"]["titolo"] = "   "
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_empty_authors_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf", pdf_pages=130)
        payload["reicat"]["autore"] = []
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_overlap_removed_pages_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf", pdf_pages=130)
        payload["pages_to_remove"] = [12]
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_zero_page_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf", pdf_pages=130)
        payload["pages_to_remove"] = [0, 2]
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_normalizes_pages_and_whitespace(self) -> None:
        pdf_body = _minimal_pdf_bytes(40)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(pdf_body)
            tmp_path = Path(tmp_file.name)
        try:
            payload = _valid_payload(str(tmp_path), pdf_pages=40)
            payload["pages_to_remove"] = [5, 2, 5, 3]
            payload["book_id_hint"] = "  test-book  "
            payload["reicat"] = deepcopy(payload["reicat"])
            payload["reicat"]["titolo"] = "  Test Book  "
            payload["reicat"]["autore"] = ["  Author Name  ", "  "]
            result = validate_and_enrich_request(payload)
            self.assertEqual(result.request.pages_to_remove, [2, 3, 5])
            self.assertEqual(result.request.book_id_hint, "test-book")
            self.assertEqual(result.request.reicat.title, "Test Book")
            self.assertEqual(result.request.reicat.authors, ["Author Name"])
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_and_enrich_request_empty_file_is_unreadable_pdf(self) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
        try:
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(tmp_path),
                "pages_to_remove": [],
                "toc_range": {"start": 1, "end": 1},
                "index_range": {"start": 1, "end": 1},
                "reicat": {"titolo": "T", "autore": ["A"]},
                "options": {"force_metadata_update_on_duplicate_hash": True},
            }
            with self.assertRaises(ValueError) as ctx:
                validate_and_enrich_request(payload)
        finally:
            tmp_path.unlink(missing_ok=True)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.PDF_NOT_FOUND.value)
        self.assertEqual(error_payload["field"], "source_pdf_path")

    def test_validate_and_enrich_request_path_with_spaces_edge_case(self) -> None:
        pdf_body = _minimal_pdf_bytes(60)
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "my sample pdf.pdf"
            file_path.write_bytes(pdf_body)
            result = validate_and_enrich_request(
                _valid_payload(str(file_path), pdf_pages=60)
            )
            self.assertEqual(result.source_pdf_path, str(file_path))
            self.assertEqual(result.source_sha256, hashlib.sha256(pdf_body).hexdigest())

    def test_validate_and_enrich_request_rejects_page_above_pdf_length(self) -> None:
        pdf_body = _minimal_pdf_bytes(36)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(pdf_body)
            tmp_path = Path(tmp_file.name)
        try:
            payload = _valid_payload(str(tmp_path), pdf_pages=36)
            payload["toc_range"] = {"start": 35, "end": 37}
            with self.assertRaises(ValueError) as ctx:
                validate_and_enrich_request(payload)
        finally:
            tmp_path.unlink(missing_ok=True)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.PAGES_INVALID.value)
        self.assertEqual(error_payload["field"], "toc_range")

    def test_source_hash_gate_returns_new_hash_when_digest_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            init_books_schema(str(sqlite_path))
            digest = hashlib.sha256(b"new-book").hexdigest()
            result = source_hash_gate(digest, str(sqlite_path))
        self.assertEqual(result.status, SourceHashGateStatus.NEW_HASH)
        self.assertFalse(result.should_skip_pipeline)
        self.assertEqual(result.source_sha256, digest)

    def test_source_hash_gate_returns_duplicate_when_digest_already_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            init_books_schema(str(sqlite_path))
            digest = hashlib.sha256(b"duplicate-book").hexdigest()
            insert_book_minimal(
                sqlite_path=str(sqlite_path),
                source_sha256=digest,
                schema_version="1.0",
                title="Book",
                authors_json='["Author"]',
            )
            result = source_hash_gate(digest, str(sqlite_path))
        self.assertEqual(result.status, SourceHashGateStatus.DUPLICATE_SOURCE_HASH)
        self.assertTrue(result.should_skip_pipeline)

    def test_source_hash_gate_rejects_invalid_hash_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            init_books_schema(str(sqlite_path))
            with self.assertRaises(ValueError):
                source_hash_gate("not-a-sha", str(sqlite_path))

    def test_init_books_schema_creates_books_table_with_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            init_books_schema(str(sqlite_path))
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='books'"
                ).fetchone()
            self.assertIsNotNone(row)

    def test_insert_book_minimal_rejects_duplicate_source_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            digest = hashlib.sha256(b"duplicate-book").hexdigest()
            init_books_schema(str(sqlite_path))
            insert_book_minimal(
                sqlite_path=str(sqlite_path),
                source_sha256=digest,
                schema_version="1.0",
                title="Book",
                authors_json='["Author"]',
            )
            with self.assertRaises(RuntimeError):
                insert_book_minimal(
                    sqlite_path=str(sqlite_path),
                    source_sha256=digest,
                    schema_version="1.0",
                    title="Book",
                    authors_json='["Author"]',
                )

    def test_insert_book_minimal_sets_audit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            digest = hashlib.sha256(b"audit-book").hexdigest()
            init_books_schema(str(sqlite_path))
            insert_book_minimal(
                sqlite_path=str(sqlite_path),
                source_sha256=digest,
                schema_version="1.0",
                title="Book",
                authors_json='["Author"]',
            )
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                row = conn.execute(
                    """
                    SELECT created_at, updated_at, last_seen_at
                    FROM books
                    WHERE source_sha256 = ?
                    """,
                    (digest,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(bool(row[0]))
            self.assertTrue(bool(row[1]))
            self.assertTrue(bool(row[2]))

    def test_upsert_book_reicat_inserts_updates_and_audits(self) -> None:
        pdf_body = _minimal_pdf_bytes(130)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "book.pdf"
            pdf_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            payload_a = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_a["reicat"]["titolo"] = "First Title"
            payload_a["reicat"]["sottotitolo"] = "Sub A"
            payload_a["reicat"]["editore"] = "Publisher A"
            payload_a["reicat"]["curatore"] = ["Curator A"]
            enriched_a = validate_and_enrich_request(payload_a)
            r1 = upsert_book_reicat(enriched_a, str(sqlite_path))
            self.assertTrue(r1.was_inserted)
            digest = enriched_a.source_sha256
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                row = conn.execute(
                    """
                    SELECT title, subtitle, publisher, editors_json
                    FROM books
                    WHERE source_sha256 = ?
                    """,
                    (digest,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "First Title")
            self.assertEqual(row[1], "Sub A")
            self.assertEqual(row[2], "Publisher A")
            self.assertIn("Curator A", json.loads(row[3] or "[]"))
            payload_b = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_b["reicat"]["titolo"] = "Second Title"
            payload_b["reicat"]["sottotitolo"] = None
            payload_b["reicat"]["editore"] = None
            enriched_b = validate_and_enrich_request(payload_b)
            self.assertFalse(enriched_b.request.reicat.editors)
            r2 = upsert_book_reicat(enriched_b, str(sqlite_path))
            self.assertFalse(r2.was_inserted)
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                events = conn.execute(
                    """
                    SELECT operation, prior_snapshot_json
                    FROM book_metadata_audit ORDER BY id
                    """
                ).fetchall()
                row_after = conn.execute(
                    """
                    SELECT title, subtitle FROM books WHERE source_sha256 = ?
                    """,
                    (digest,),
                ).fetchone()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0][0], "insert")
            self.assertIsNone(events[0][1])
            self.assertEqual(events[1][0], "update")
            self.assertIsNotNone(events[1][1])
            prior = json.loads(str(events[1][1]))
            self.assertEqual(prior.get("titolo"), "First Title")
            assert row_after is not None
            self.assertEqual(row_after[0], "Second Title")

    def test_upsert_rejects_pdf_changed_after_validate(self) -> None:
        pdf_body = _minimal_pdf_bytes(80)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "book.pdf"
            pdf_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            enriched = validate_and_enrich_request(
                _valid_payload(str(pdf_path), pdf_pages=80)
            )
            pdf_path.write_bytes(_minimal_pdf_bytes(40))
            with self.assertRaises(ValueError) as ctx:
                upsert_book_reicat(enriched, str(sqlite_path))
            err_payload = json.loads(str(ctx.exception))
            self.assertEqual(
                err_payload["code"], IngestInputErrorCode.SOURCE_DIGEST_MISMATCH.value
            )

    def test_run_ingest_gate_phase_new_hash_does_not_skip(self) -> None:
        pdf_body = _minimal_pdf_bytes(130)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "book.pdf"
            pdf_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            enriched = validate_and_enrich_request(
                _valid_payload(str(pdf_path), pdf_pages=130)
            )
            phase = run_ingest_gate_phase(enriched, str(sqlite_path))
        self.assertEqual(phase.gate.status, SourceHashGateStatus.NEW_HASH)
        self.assertFalse(phase.pipeline_skipped)
        assert phase.book_upsert is not None
        self.assertTrue(phase.book_upsert.was_inserted)
        self.assertIsNone(phase.duplicate_skip_audit_row_id)

    def test_run_ingest_gate_phase_duplicate_updates_metadata_when_forced(self) -> None:
        pdf_body = _minimal_pdf_bytes(130)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "book.pdf"
            pdf_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            payload_first = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_first["reicat"]["titolo"] = "First Title"
            enriched_first = validate_and_enrich_request(payload_first)
            self.assertEqual(
                run_ingest_gate_phase(enriched_first, str(sqlite_path)).gate.status,
                SourceHashGateStatus.NEW_HASH,
            )
            upsert_book_reicat(enriched_first, str(sqlite_path))
            digest = enriched_first.source_sha256
            payload_second = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_second["reicat"]["titolo"] = "Second Title"
            enriched_second = validate_and_enrich_request(payload_second)
            self.assertEqual(enriched_second.source_sha256, digest)
            phase = run_ingest_gate_phase(enriched_second, str(sqlite_path))
            self.assertEqual(phase.gate.status, SourceHashGateStatus.DUPLICATE_SOURCE_HASH)
            self.assertTrue(phase.pipeline_skipped)
            assert phase.book_upsert is not None
            self.assertFalse(phase.book_upsert.was_inserted)
            self.assertIsNone(phase.duplicate_skip_audit_row_id)
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                row = conn.execute(
                    "SELECT title FROM books WHERE source_sha256 = ?", (digest,)
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "Second Title")

    def test_run_ingest_gate_phase_duplicate_without_metadata_touch_only(self) -> None:
        pdf_body = _minimal_pdf_bytes(130)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "book.pdf"
            pdf_path.write_bytes(pdf_body)
            sqlite_path = Path(tmp_dir) / "biblioteca.db"
            payload_first = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_first["reicat"]["titolo"] = "Stable Title"
            enriched_first = validate_and_enrich_request(payload_first)
            run_ingest_gate_phase(enriched_first, str(sqlite_path))
            upsert_book_reicat(enriched_first, str(sqlite_path))
            digest = enriched_first.source_sha256
            payload_second = deepcopy(_valid_payload(str(pdf_path), pdf_pages=130))
            payload_second["reicat"]["titolo"] = "Ignored Title"
            payload_second["options"] = {"force_metadata_update_on_duplicate_hash": False}
            phase = run_ingest_gate_phase(
                validate_and_enrich_request(payload_second), str(sqlite_path)
            )
            self.assertTrue(phase.pipeline_skipped)
            self.assertIsNone(phase.book_upsert)
            assert phase.duplicate_skip_audit_row_id is not None
            with closing(sqlite3.connect(str(sqlite_path))) as conn:
                row = conn.execute(
                    "SELECT title FROM books WHERE source_sha256 = ?", (digest,)
                ).fetchone()
                evt = conn.execute(
                    """
                    SELECT operation FROM book_metadata_audit
                    WHERE id = ?
                    """,
                    (phase.duplicate_skip_audit_row_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            assert evt is not None
            self.assertEqual(row[0], "Stable Title")
            self.assertEqual(evt[0], "duplicate_skip_no_metadata")


if __name__ == "__main__":
    unittest.main()

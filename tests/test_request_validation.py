from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.ingestion.request_validation import _compute_file_sha256, validate_and_enrich_request
from src.models.request import IngestInputErrorCode


def _valid_payload(source_pdf_path: str) -> dict:
    return {
        "schema_version": "1.0",
        "source_pdf_path": source_pdf_path,
        "pages_to_remove": [1, 2],
        "toc_range": {"start": 10, "end": 20},
        "index_range": {"start": 100, "end": 120},
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
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(b"pdf-content")
            tmp_path = Path(tmp_file.name)
        try:
            result = validate_and_enrich_request(_valid_payload(str(tmp_path)))
            self.assertEqual(result.request.source_pdf_path, str(tmp_path))
            self.assertEqual(result.source_pdf_path, str(tmp_path))
            self.assertEqual(result.source_sha256, hashlib.sha256(b"pdf-content").hexdigest())
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_and_enrich_request_invalid_payload(self) -> None:
        payload = _valid_payload("dummy.pdf")
        payload["toc_range"] = {"start": 20, "end": 10}
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)
        self.assertEqual(error_payload["field"], "payload")

    def test_validate_and_enrich_request_missing_pdf(self) -> None:
        payload = _valid_payload("/tmp/this-file-does-not-exist.pdf")
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.PDF_NOT_FOUND.value)
        self.assertEqual(error_payload["field"], "source_pdf_path")

    def test_validate_and_enrich_request_empty_title_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf")
        payload["reicat"]["titolo"] = "   "
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_empty_authors_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf")
        payload["reicat"]["autore"] = []
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_overlap_removed_pages_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf")
        payload["pages_to_remove"] = [12]
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_zero_page_is_bad_input(self) -> None:
        payload = _valid_payload("dummy.pdf")
        payload["pages_to_remove"] = [0, 2]
        with self.assertRaises(ValueError) as ctx:
            validate_and_enrich_request(payload)
        error_payload = json.loads(str(ctx.exception))
        self.assertEqual(error_payload["code"], IngestInputErrorCode.INPUT_SCHEMA_INVALID.value)

    def test_validate_and_enrich_request_normalizes_pages_and_whitespace(self) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_file.write(b"x")
            tmp_path = Path(tmp_file.name)
        try:
            payload = _valid_payload(str(tmp_path))
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

    def test_validate_and_enrich_request_empty_file_edge_case(self) -> None:
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
        try:
            result = validate_and_enrich_request(_valid_payload(str(tmp_path)))
            self.assertEqual(result.source_sha256, hashlib.sha256(b"").hexdigest())
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_and_enrich_request_path_with_spaces_edge_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "my sample pdf.pdf"
            file_path.write_bytes(b"space-path")
            result = validate_and_enrich_request(_valid_payload(str(file_path)))
            self.assertEqual(result.source_pdf_path, str(file_path))
            self.assertEqual(result.source_sha256, hashlib.sha256(b"space-path").hexdigest())


if __name__ == "__main__":
    unittest.main()

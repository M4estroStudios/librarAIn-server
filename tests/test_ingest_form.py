from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from src.api.ingest_form import (
    InvalidPagesSpec,
    InvalidRangeField,
    _parse_pages_spec,
    build_ingest_payload_from_form,
    parse_multipart_form,
    parse_multipart_form_stream,
)


def _multipart_body(
    fields: dict[str, str], files: dict[str, tuple[str, bytes]], boundary: str
) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, (filename, payload) in files.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + payload
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


_BASE_FIELDS = {
    "titolo": "Storia di Roma",
    "autore": "Mommsen, Theodor",
    "toc_range": "5-8",
    "index_range": "200-210",
}


class TestParseMultipartForm(unittest.TestCase):
    def test_text_fields_and_file_extracted(self) -> None:
        boundary = "testboundary123"
        body = _multipart_body(
            {"titolo": "Il libro", "notes": "ciao"},
            {"pdf_file": ("book.pdf", b"%PDF-1.4 fake")},
            boundary,
        )
        fields, files = parse_multipart_form(
            body, f"multipart/form-data; boundary={boundary}"
        )
        self.assertEqual(fields["titolo"], "Il libro")
        self.assertEqual(fields["notes"], "ciao")
        self.assertEqual(files["pdf_file"], ("book.pdf", b"%PDF-1.4 fake"))

    def test_invalid_content_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_multipart_form(b"", "application/json")

    def test_missing_boundary_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_multipart_form(b"", "multipart/form-data")


class TestParseMultipartFormStream(unittest.TestCase):
    def test_stream_parser_matches_in_memory_parser(self) -> None:
        boundary = "streamboundary9"
        body = _multipart_body(
            {"titolo": "Il libro", "notes": "ciao"},
            {"pdf_file": ("book.pdf", b"%PDF-1.4 streamed")},
            boundary,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "upload.part"
            parsed = parse_multipart_form_stream(
                BytesIO(body),
                f"multipart/form-data; boundary={boundary}",
                content_length=len(body),
                max_bytes=len(body) + 1,
                pdf_part_path=pdf_path,
            )
            self.assertEqual(parsed.text_fields["titolo"], "Il libro")
            self.assertEqual(parsed.text_fields["notes"], "ciao")
            self.assertIsNotNone(parsed.pdf)
            assert parsed.pdf is not None
            self.assertEqual(parsed.pdf.filename, "book.pdf")
            self.assertEqual(parsed.pdf.size, len(b"%PDF-1.4 streamed"))
            self.assertEqual(parsed.pdf.path.read_bytes(), b"%PDF-1.4 streamed")

    def test_stream_parser_handles_large_pdf_in_chunks(self) -> None:
        boundary = "largeboundary1"
        pdf_bytes = b"%PDF-" + (b"x" * (3 * 1024 * 1024))
        body = _multipart_body(_BASE_FIELDS, {"pdf_file": ("big.pdf", pdf_bytes)}, boundary)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "big.part"
            parsed = parse_multipart_form_stream(
                BytesIO(body),
                f"multipart/form-data; boundary={boundary}",
                content_length=len(body),
                max_bytes=len(body) + 1,
                pdf_part_path=pdf_path,
                chunk_size=64 * 1024,
            )
            assert parsed.pdf is not None
            self.assertEqual(parsed.pdf.size, len(pdf_bytes))
            self.assertEqual(parsed.pdf.path.read_bytes(), pdf_bytes)

    def test_stream_parser_rejects_oversized_content_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                parse_multipart_form_stream(
                    BytesIO(b""),
                    "multipart/form-data; boundary=x",
                    content_length=1024,
                    max_bytes=512,
                    pdf_part_path=Path(tmp) / "x.part",
                )


class TestParsePagesSpec(unittest.TestCase):
    def test_single_and_ranges(self) -> None:
        self.assertEqual(_parse_pages_spec("1, 3, 5-7"), [1, 3, 5, 6, 7])

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(_parse_pages_spec("   "), [])

    def test_inverted_range_rejected(self) -> None:
        with self.assertRaises(InvalidPagesSpec):
            _parse_pages_spec("9-3")

    def test_zero_page_rejected(self) -> None:
        with self.assertRaises(InvalidPagesSpec):
            _parse_pages_spec("0")

    def test_non_numeric_rejected(self) -> None:
        with self.assertRaises(InvalidPagesSpec):
            _parse_pages_spec("abc")


class TestBuildIngestPayload(unittest.TestCase):
    def test_minimal_payload(self) -> None:
        payload = build_ingest_payload_from_form(dict(_BASE_FIELDS))
        self.assertEqual(payload["toc_range"], {"start": 5, "end": 8})
        self.assertEqual(payload["index_range"], {"start": 200, "end": 210})
        self.assertEqual(payload["reicat"]["titolo"], "Storia di Roma")
        self.assertEqual(payload["reicat"]["autore"], ["Mommsen", "Theodor"])
        self.assertEqual(payload["pages_to_remove"], [])
        self.assertTrue(
            payload["options"]["force_metadata_update_on_duplicate_hash"]
        )

    def test_notes_fields_propagated(self) -> None:
        fields = dict(_BASE_FIELDS)
        fields["notes"] = "nota generale"
        fields["index_notes"] = "nota indice"
        fields["page_notes"] = "nota pagine"
        payload = build_ingest_payload_from_form(fields)
        self.assertEqual(payload["notes"], "nota generale")
        self.assertEqual(payload["index_notes"], "nota indice")
        self.assertEqual(payload["page_notes"], "nota pagine")

    def test_empty_notes_omitted(self) -> None:
        payload = build_ingest_payload_from_form(dict(_BASE_FIELDS))
        self.assertNotIn("notes", payload)
        self.assertNotIn("index_notes", payload)
        self.assertNotIn("page_notes", payload)

    def test_missing_toc_range_raises(self) -> None:
        fields = dict(_BASE_FIELDS)
        fields["toc_range"] = ""
        with self.assertRaises(InvalidRangeField) as ctx:
            build_ingest_payload_from_form(fields)
        self.assertEqual(ctx.exception.field, "toc_range")

    def test_non_contiguous_index_range_raises(self) -> None:
        fields = dict(_BASE_FIELDS)
        fields["index_range"] = "1,5"
        with self.assertRaises(InvalidRangeField) as ctx:
            build_ingest_payload_from_form(fields)
        self.assertEqual(ctx.exception.field, "index_range")

    def test_pages_to_remove_parsed(self) -> None:
        fields = dict(_BASE_FIELDS)
        fields["pages_to_remove"] = "1-3, 10"
        payload = build_ingest_payload_from_form(fields)
        self.assertEqual(payload["pages_to_remove"], [1, 2, 3, 10])


if __name__ == "__main__":
    unittest.main()

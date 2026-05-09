from __future__ import annotations

import json
import os
import re
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from src.core.config import ConfigurationError, load_settings
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import maybe_run_pdf_alignment
from src.ingestion.request_validation import (
    run_ingest_gate_phase,
    validate_and_enrich_request,
)
from src.models.request import IngestInputErrorCode, IngestInputValidationError


class InvalidPagesSpec(ValueError):
    pass


class InvalidRangeField(Exception):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message_text = message
        super().__init__(message)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _multipart_boundary(content_type: str) -> bytes:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Content-Type must be multipart/form-data")
    for segment in content_type.split(";"):
        segment = segment.strip()
        if segment.lower().startswith("boundary="):
            boundary_value = segment.split("=", 1)[1].strip().strip('"')
            return boundary_value.encode("utf-8")
    raise ValueError("multipart boundary not found")


def _parse_content_disposition(value: str) -> tuple[str | None, str | None]:
    name_match = re.search(r'\bname="([^"]+)"', value)
    filename_match = re.search(r'\bfilename="([^"]*)"', value)
    name = name_match.group(1) if name_match else None
    filename = filename_match.group(1) if filename_match else None
    return name, filename


def parse_multipart_form(
    body: bytes, content_type: str
) -> tuple[dict[str, str], dict[str, tuple[str | None, bytes]]]:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    raw_parts = body.split(delimiter)
    text_fields: dict[str, str] = {}
    files: dict[str, tuple[str | None, bytes]] = {}
    for raw in raw_parts:
        chunk = raw.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        sep = chunk.find(b"\r\n\r\n")
        if sep == -1:
            continue
        headers_blob = chunk[:sep].decode("utf-8", errors="replace")
        payload = chunk[sep + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        content_disposition: str | None = None
        for line in headers_blob.split("\r\n"):
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                content_disposition = line.split(":", 1)[1].strip()
                break
        if not content_disposition:
            continue
        field_name, filename = _parse_content_disposition(content_disposition)
        if not field_name:
            continue
        if filename is not None:
            files[field_name] = (filename, payload)
        else:
            text_fields[field_name] = payload.decode("utf-8")
    return text_fields, files


def _parse_pages_spec(raw: str) -> list[int]:
    if not raw.strip():
        return []
    pages: set[int] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            try:
                start = int(left.strip())
                end = int(right.strip())
            except ValueError as exc:
                raise InvalidPagesSpec(f"invalid page range token: {piece!r}") from exc
            if start < 1 or end < 1:
                raise InvalidPagesSpec("page numbers must be >= 1")
            if start > end:
                raise InvalidPagesSpec("in a page range x-y, x must be <= y")
            pages.update(range(start, end + 1))
        else:
            try:
                n = int(piece)
            except ValueError as exc:
                raise InvalidPagesSpec(f"invalid page token: {piece!r}") from exc
            if n < 1:
                raise InvalidPagesSpec("page numbers must be >= 1")
            pages.add(n)
    return sorted(pages)


def _sorted_pages_form_single_interval(pages: list[int]) -> bool:
    if len(pages) <= 1:
        return True
    expected = pages[-1] - pages[0] + 1
    return expected == len(pages)


def _parse_contiguous_range_field(raw: str, field: str) -> tuple[int, int]:
    stripped = raw.strip()
    if not stripped:
        raise InvalidRangeField(field, "value is required")
    try:
        pages = _parse_pages_spec(stripped)
    except InvalidPagesSpec as exc:
        raise InvalidRangeField(field, str(exc)) from exc
    if not pages:
        raise InvalidRangeField(field, "value is required")
    if not _sorted_pages_form_single_interval(pages):
        raise InvalidRangeField(
            field,
            "must be a single contiguous interval (e.g. 10-18 or 10,11,12)",
        )
    return pages[0], pages[-1]


def _split_str_list(raw: str) -> list[str] | None:
    pieces = [segment.strip() for segment in raw.replace(";", ",").split(",")]
    cleaned = [segment for segment in pieces if segment]
    return cleaned or None


def _optional_trimmed(fields: dict[str, str], key: str) -> str | None:
    raw = fields.get(key, "").strip()
    return raw or None


def build_ingest_payload_from_form(fields: dict[str, str]) -> dict[str, Any]:
    pages_raw = fields.get("pages_to_remove", "").strip()
    toc_spec = fields.get("toc_range", "").strip()
    index_spec = fields.get("index_range", "").strip()
    toc_start, toc_end = _parse_contiguous_range_field(toc_spec, "toc_range")
    index_start, index_end = _parse_contiguous_range_field(index_spec, "index_range")

    reicat_payload: dict[str, Any] = {
        "titolo": fields.get("titolo", "").strip(),
        "autore": _split_str_list(fields.get("autore", "")) or [],
    }

    subtitle = _optional_trimmed(fields, "sottotitolo")
    complements = _optional_trimmed(fields, "complementi_del_titolo")
    editors = _split_str_list(fields.get("curatore", "") or "")
    translators = _split_str_list(fields.get("traduttore", "") or "")
    edition = _optional_trimmed(fields, "numero_edizione")
    publication_year_raw = fields.get("anno_di_pubblicazione", "").strip()
    publication_type = _optional_trimmed(fields, "tipo_di_pubblicazione")
    publication_place = _optional_trimmed(fields, "luogo_di_pubblicazione")
    publisher = _optional_trimmed(fields, "editore")
    page_count_raw = fields.get("numero_pagine", "").strip()
    series_title = _optional_trimmed(fields, "titolo_collana")
    series_number = _optional_trimmed(fields, "numero_nella_collana")
    isbn = _optional_trimmed(fields, "isbn")

    if subtitle:
        reicat_payload["sottotitolo"] = subtitle
    if complements:
        reicat_payload["complementi_del_titolo"] = complements
    if editors:
        reicat_payload["curatore"] = editors
    if translators:
        reicat_payload["traduttore"] = translators
    if edition:
        reicat_payload["numero_edizione"] = edition
    if publication_year_raw:
        reicat_payload["anno_di_pubblicazione"] = int(publication_year_raw)
    if publication_type:
        reicat_payload["tipo_di_pubblicazione"] = publication_type
    if publication_place:
        reicat_payload["luogo_di_pubblicazione"] = publication_place
    if publisher:
        reicat_payload["editore"] = publisher
    if page_count_raw:
        reicat_payload["numero_pagine"] = int(page_count_raw)
    if series_title:
        reicat_payload["titolo_collana"] = series_title
    if series_number:
        reicat_payload["numero_nella_collana"] = series_number
    if isbn:
        reicat_payload["isbn"] = isbn

    book_id_hint_raw = fields.get("book_id_hint", "").strip()
    force_meta = fields.get("force_metadata_update_on_duplicate_hash")
    if force_meta is None:
        force_flag = True
    else:
        force_flag = str(force_meta).lower() in ("1", "true", "on", "yes")

    ingest_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "pages_to_remove": _parse_pages_spec(pages_raw) if pages_raw else [],
        "toc_range": {"start": toc_start, "end": toc_end},
        "index_range": {"start": index_start, "end": index_end},
        "reicat": reicat_payload,
        "options": {"force_metadata_update_on_duplicate_hash": force_flag},
    }
    if book_id_hint_raw:
        ingest_payload["book_id_hint"] = book_id_hint_raw
    return ingest_payload


def _safe_filename(name: str) -> str:
    base = Path(name).name
    if not base or base in {".", ".."}:
        return "upload.pdf"
    return base


def _save_uploaded_pdf(
    data_root: Path, original_name: str, content: bytes
) -> Path:
    target_dir = data_root / "input" / "raw"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(original_name)
    token = secrets.token_hex(6)
    path = target_dir / f"{token}_{safe}"
    path.write_bytes(content)
    return path


def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        raise ValueError("Content-Length is required")
    length = int(length_header)
    if length < 0 or length > max_bytes:
        raise ValueError("invalid Content-Length")
    return handler.rfile.read(length)


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(
    handler: BaseHTTPRequestHandler,
    status: int,
    content: bytes,
    content_type: str,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def run_ingest_http_server() -> None:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from exc

    host = os.environ.get("INGEST_HTTP_HOST", "127.0.0.1")
    port_raw = os.environ.get("INGEST_HTTP_PORT", "8765")
    port = int(port_raw)
    max_upload = int(os.environ.get("INGEST_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
    repo_root = _repo_root()
    web_dir = repo_root / "web"
    data_root = Path(settings.data_root)

    class IngestHandler(BaseHTTPRequestHandler):
        server_version = "librarAIn-ingest-http/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                index_file = web_dir / "index.html"
                if not index_file.exists():
                    _send_json(self, 500, {"ok": False, "error": "web/index.html missing"})
                    return
                _send_bytes(self, 200, index_file.read_bytes(), "text/html; charset=utf-8")
                return
            if parsed.path == "/health":
                _send_json(self, 200, {"ok": True})
                return
            self.send_error(404, "Not Found")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/ingest/submit":
                self.send_error(404, "Not Found")
                return
            content_type = self.headers.get("Content-Type") or ""
            try:
                body = _read_body(self, max_upload)
                text_fields, files = parse_multipart_form(body, content_type)
            except (ValueError, OSError):
                form_error = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message="multipart form could not be parsed",
                    field="form",
                )
                _send_json(self, 400, {"ok": False, "errors": form_error.model_dump(mode="json")})
                return

            try:
                ingest_payload = build_ingest_payload_from_form(text_fields)
            except InvalidPagesSpec as exc:
                page_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=str(exc),
                    field="pages_to_remove",
                )
                _send_json(self, 400, {"ok": False, "errors": page_err.model_dump(mode="json")})
                return
            except InvalidRangeField as exc:
                range_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=exc.message_text,
                    field=exc.field,
                )
                _send_json(self, 400, {"ok": False, "errors": range_err.model_dump(mode="json")})
                return
            except ValueError as exc:
                form_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=str(exc),
                    field="payload",
                )
                _send_json(self, 400, {"ok": False, "errors": form_err.model_dump(mode="json")})
                return

            uploaded = files.get("pdf_file")
            if uploaded is None:
                err = IngestInputValidationError(
                    code=IngestInputErrorCode.PDF_NOT_FOUND,
                    message="PDF file upload is required",
                    field="pdf_file",
                )
                _send_json(self, 400, {"ok": False, "errors": err.model_dump(mode="json")})
                return
            filename, file_bytes = uploaded
            if not file_bytes:
                err = IngestInputValidationError(
                    code=IngestInputErrorCode.PDF_NOT_FOUND,
                    message="empty PDF upload",
                    field="pdf_file",
                )
                _send_json(self, 400, {"ok": False, "errors": err.model_dump(mode="json")})
                return
            saved_path = _save_uploaded_pdf(data_root, filename or "upload.pdf", file_bytes)
            ingest_payload["source_pdf_path"] = str(saved_path)

            try:
                enriched = validate_and_enrich_request(ingest_payload)
            except ValueError as exc:
                try:
                    err_model = IngestInputValidationError.model_validate_json(str(exc))
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    _send_json(
                        self,
                        400,
                        {
                            "ok": False,
                            "errors": {
                                "code": "VALIDATION_FAILED",
                                "message": str(exc),
                                "field": None,
                            },
                        },
                    )
                return

            ingest_gate_phase = run_ingest_gate_phase(enriched, settings.sqlite_path)

            try:
                pdf_alignment = maybe_run_pdf_alignment(
                    enriched,
                    ingest_gate_phase,
                    settings.processed_pdf_input_dir,
                    page_range_per_thread=settings.page_range_per_thread,
                )
            except ValueError as exc:
                try:
                    err_model = IngestInputValidationError.model_validate_json(str(exc))
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    _send_json(
                        self,
                        400,
                        {
                            "ok": False,
                            "errors": {
                                "code": "PDF_ALIGNMENT_FAILED",
                                "message": str(exc),
                                "field": None,
                            },
                        },
                    )
                return

            try:
                useful_pages_enumeration = build_useful_pages_enumeration(
                    enriched, pdf_alignment
                )
            except ValueError as exc:
                try:
                    err_model = IngestInputValidationError.model_validate_json(str(exc))
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    _send_json(
                        self,
                        400,
                        {
                            "ok": False,
                            "errors": {
                                "code": "PAGE_ENUMERATION_FAILED",
                                "message": str(exc),
                                "field": None,
                            },
                        },
                    )
                return

            payload_out: dict[str, Any] = {
                "ok": True,
                "enriched": enriched.model_dump(mode="json", by_alias=True),
                "ingest_gate_phase": ingest_gate_phase.model_dump(mode="json", by_alias=True),
                "pdf_alignment": (
                    pdf_alignment.model_dump(mode="json", by_alias=True)
                    if pdf_alignment is not None
                    else None
                ),
                "useful_pages_enumeration": useful_pages_enumeration.model_dump(
                    mode="json", by_alias=True
                ),
            }

            _send_json(self, 200, payload_out)

    httpd = HTTPServer((host, port), IngestHandler)
    print(f"ingest http server listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("shutdown requested")


if __name__ == "__main__":
    run_ingest_http_server()

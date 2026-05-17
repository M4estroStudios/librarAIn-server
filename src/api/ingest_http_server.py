from __future__ import annotations

import json
import os
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from src.api.ingest_form import (
    InvalidPagesSpec,
    InvalidRangeField,
    build_ingest_payload_from_form,
    parse_multipart_form,
)
from src.core.config import ConfigurationError, load_settings
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL, logInit
from src.ingestion.pipeline import run_stage1_ingest_step
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import maybe_run_pdf_alignment
from src.ingestion.request_validation import (
    run_ingest_gate_phase,
    validate_and_enrich_request,
)
from src.models.request import IngestInputErrorCode, IngestInputValidationError


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
    logInit(INFO_LOG_LEVEL)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        Log(ERROR_LOG_LEVEL, "ingest server configuration failed", {"error": str(exc)})
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
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing", {"path": str(index_file)})
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
            except (ValueError, OSError) as exc:
                Log(WARNING_LOG_LEVEL, "ingest multipart parse failed", {"error": str(exc)})
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
                Log(WARNING_LOG_LEVEL, "ingest form pages spec invalid", {"error": str(exc)})
                page_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=str(exc),
                    field="pages_to_remove",
                )
                _send_json(self, 400, {"ok": False, "errors": page_err.model_dump(mode="json")})
                return
            except InvalidRangeField as exc:
                Log(
                    WARNING_LOG_LEVEL,
                    "ingest form range field invalid",
                    {"field": exc.field, "error": exc.message_text},
                )
                range_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=exc.message_text,
                    field=exc.field,
                )
                _send_json(self, 400, {"ok": False, "errors": range_err.model_dump(mode="json")})
                return
            except ValueError as exc:
                Log(WARNING_LOG_LEVEL, "ingest form payload invalid", {"error": str(exc)})
                form_err = IngestInputValidationError(
                    code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    message=str(exc),
                    field="payload",
                )
                _send_json(self, 400, {"ok": False, "errors": form_err.model_dump(mode="json")})
                return

            uploaded = files.get("pdf_file")
            if uploaded is None:
                Log(WARNING_LOG_LEVEL, "ingest submit rejected: pdf_file missing")
                err = IngestInputValidationError(
                    code=IngestInputErrorCode.PDF_NOT_FOUND,
                    message="PDF file upload is required",
                    field="pdf_file",
                )
                _send_json(self, 400, {"ok": False, "errors": err.model_dump(mode="json")})
                return
            filename, file_bytes = uploaded
            if not file_bytes:
                Log(WARNING_LOG_LEVEL, "ingest submit rejected: empty PDF upload")
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
                    Log(
                        WARNING_LOG_LEVEL,
                        "ingest validation failed",
                        {"errors": err_model.model_dump(mode="json")},
                    )
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    Log(WARNING_LOG_LEVEL, "ingest validation failed", {"error": str(exc)})
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
            Log(
                INFO_LOG_LEVEL,
                "ingest gate phase completed",
                {
                    "source_sha256": enriched.source_sha256[:16],
                    "pipeline_skipped": ingest_gate_phase.pipeline_skipped,
                },
            )

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
                    Log(
                        WARNING_LOG_LEVEL,
                        "ingest pdf alignment failed",
                        {"errors": err_model.model_dump(mode="json")},
                    )
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    Log(WARNING_LOG_LEVEL, "ingest pdf alignment failed", {"error": str(exc)})
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
                    Log(
                        WARNING_LOG_LEVEL,
                        "ingest page enumeration failed",
                        {"errors": err_model.model_dump(mode="json")},
                    )
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    Log(WARNING_LOG_LEVEL, "ingest page enumeration failed", {"error": str(exc)})
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

            try:
                stage1_result = run_stage1_ingest_step(
                    enriched,
                    pdf_alignment,
                    useful_pages_enumeration,
                    settings,
                )
            except ValueError as exc:
                try:
                    err_model = IngestInputValidationError.model_validate_json(str(exc))
                    Log(
                        WARNING_LOG_LEVEL,
                        "ingest stage1 failed",
                        {"errors": err_model.model_dump(mode="json")},
                    )
                    _send_json(
                        self, 400, {"ok": False, "errors": err_model.model_dump(mode="json")}
                    )
                except ValueError:
                    Log(WARNING_LOG_LEVEL, "ingest stage1 failed", {"error": str(exc)})
                    _send_json(
                        self,
                        400,
                        {
                            "ok": False,
                            "errors": {
                                "code": "STAGE1_FAILED",
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
                "stage1": stage1_result.model_dump(mode="json"),
            }

            Log(
                INFO_LOG_LEVEL,
                "ingest submit completed",
                {
                    "source_sha256": enriched.source_sha256[:16],
                    "stage1_pages": len(stage1_result.pages),
                    "skipped_existing": stage1_result.skipped_existing,
                },
            )
            _send_json(self, 200, payload_out)

    httpd = HTTPServer((host, port), IngestHandler)
    Log(INFO_LOG_LEVEL, "ingest http server listening", {"url": f"http://{host}:{port}"})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        Log(INFO_LOG_LEVEL, "ingest http server shutdown requested")


if __name__ == "__main__":
    run_ingest_http_server()

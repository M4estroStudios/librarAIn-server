from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src.api.ingest_form import (
    InvalidPagesSpec,
    InvalidRangeField,
    build_ingest_payload_from_form,
    parse_multipart_form_stream,
)
from src.api.ingest_pipeline_runner import run_full_pipeline
from src.api.job_registry import JobRegistry
from src.api.research_handlers import ResearchBatchRegistry, build_research_routes
from src.ingestion.polyindex.index_json import (
    SubjectMergeError,
    list_multibook_subjects,
    merge_polyindex_subjects,
)
from src.persistence.book_pages_audit import audit_all_books
from src.persistence.book_page_exclude import PageExcludeError, exclude_book_page
from src.persistence.book_page_preview import (
    PagePreviewError,
    confirm_page_transcript,
    ensure_page_render_png,
    load_page_transcript,
    save_page_transcript,
)
from src.persistence.book_page_repair import PageRepairError, run_book_gaps_repair, run_book_page_repair
from src.core.config import ConfigurationError, load_settings
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL, logInit
from src.ingestion.pipeline.engine import require_gpu_vram_at_pipeline_start
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, make_event
from src.models.request import IngestInputErrorCode, IngestInputValidationError, IngestInputValidationException


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_filename(name: str) -> str:
    base = Path(name).name
    if not base or base in {".", ".."}:
        return "upload.pdf"
    return base


def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        raise ValueError("Content-Length is required")
    length = int(length_header)
    if length < 0 or length > max_bytes:
        raise ValueError("invalid Content-Length")
    return handler.rfile.read(length)


def _request_content_length(handler: BaseHTTPRequestHandler) -> int:
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        raise ValueError("Content-Length is required")
    return int(length_header)


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    if status >= 400:
        path = urllib.parse.urlparse(handler.path).path
        detail: str | None = None
        if isinstance(payload, dict):
            raw = payload.get("error") or payload.get("message")
            if raw is not None:
                detail = str(raw)
        Log(
            ERROR_LOG_LEVEL if status >= 500 else WARNING_LOG_LEVEL,
            "http json response",
            {"path": path, "status": status, "error": detail or str(payload)[:200]},
        )
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


def _send_validation_error(
    handler: BaseHTTPRequestHandler,
    code: IngestInputErrorCode,
    message: str,
    field: str | None = None,
) -> None:
    err = IngestInputValidationError(code=code, message=message, field=field)
    payload = err.model_dump(mode="json")
    _send_json(handler, 400, {"ok": False, "error": message, "errors": payload})


def _sse_write(handler: BaseHTTPRequestHandler, event_name: str, data: Any) -> bool:
    """Write a single SSE frame.  Returns False if the connection was lost."""
    try:
        line = f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        handler.wfile.write(line.encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def build_ingest_server(
    settings: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    max_upload: int = 512 * 1024 * 1024,
    api_token: str = "",
    max_concurrent_jobs: int = 1,
) -> tuple[ThreadingHTTPServer, JobRegistry]:
    """Build the HTTP server (without starting it) and its job registry.

    Separated from run_ingest_http_server so tests can bind to an ephemeral
    port and inject configuration without touching the environment.
    """
    repo_root = _repo_root()
    web_dir = repo_root / "web"
    data_root = Path(settings.data_root)
    max_concurrent_jobs = max(1, max_concurrent_jobs)

    registry = JobRegistry()
    research_batch_registry = ResearchBatchRegistry()
    job_semaphore = threading.Semaphore(max_concurrent_jobs)

    research_try_get, research_try_post = build_research_routes(
        data_root=data_root,
        web_dir=web_dir,
        batch_registry=research_batch_registry,
        send_json=_send_json,
        send_bytes=_send_bytes,
        read_json_body=_read_body,
    )

    class IngestHandler(BaseHTTPRequestHandler):
        server_version = "librarAIn-ingest-http/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            if len(args) < 2:
                return
            try:
                status = int(str(args[1]))
            except ValueError:
                return
            path = urllib.parse.urlparse(self.path).path
            if path.endswith("/events"):
                return
            if status < 400:
                return
            Log(
                ERROR_LOG_LEVEL if status >= 500 else WARNING_LOG_LEVEL,
                "http request failed",
                {
                    "path": path,
                    "status": status,
                    "request": str(args[0])[:200],
                },
            )

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            path = urllib.parse.urlparse(self.path).path
            Log(
                ERROR_LOG_LEVEL if code >= 500 else WARNING_LOG_LEVEL,
                "http error response",
                {"path": path, "status": code, "message": message or explain or ""},
            )
            super().send_error(code, message, explain)

        def _is_authorized(self, query: dict[str, list[str]] | None = None) -> bool:
            """API token check. A no-op when INGEST_API_TOKEN is unset."""
            if not api_token:
                return True
            header = self.headers.get("X-API-Token", "")
            if header and secrets.compare_digest(header, api_token):
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and secrets.compare_digest(
                auth.removeprefix("Bearer ").strip(), api_token
            ):
                return True
            if query:
                for candidate in query.get("token", []):
                    if secrets.compare_digest(candidate, api_token):
                        return True
            return False

        def _require_auth(self, query: dict[str, list[str]] | None = None) -> bool:
            if self._is_authorized(query):
                return True
            _send_json(self, 401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if research_try_get(self, path, query):
                return

            if path in ("/", "/index.html"):
                index_file = web_dir / "index.html"
                if not index_file.exists():
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing",
                        {"path": str(index_file)})
                    _send_json(self, 500, {"ok": False, "error": "web/index.html missing"})
                    return
                _send_bytes(self, 200, index_file.read_bytes(), "text/html; charset=utf-8")
                return

            if path in ("/admin", "/admin.html"):
                admin_file = web_dir / "admin.html"
                if not admin_file.exists():
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing",
                        {"path": str(admin_file)})
                    _send_json(self, 500, {"ok": False, "error": "web/admin.html missing"})
                    return
                _send_bytes(self, 200, admin_file.read_bytes(), "text/html; charset=utf-8")
                return

            if path == "/log.js":
                log_js = web_dir / "log.js"
                if log_js.is_file():
                    _send_bytes(self, 200, log_js.read_bytes(), "text/javascript; charset=utf-8")
                    return

            if path == "/mockup/lab.html":
                self.send_response(302)
                self.send_header("Location", "/index.html?mock=1")
                self.end_headers()
                return

            if path.startswith("/mockup/"):
                rel = path[len("/mockup/") :].lstrip("/")
                if rel and ".." not in rel.replace("\\", "/"):
                    mock_root = (web_dir / "mockup").resolve()
                    asset = (mock_root / rel).resolve()
                    try:
                        asset.relative_to(mock_root)
                    except ValueError:
                        pass
                    else:
                        if asset.is_file():
                            types = {
                                ".html": "text/html; charset=utf-8",
                                ".js": "text/javascript; charset=utf-8",
                                ".json": "application/json; charset=utf-8",
                                ".css": "text/css; charset=utf-8",
                                ".svg": "image/svg+xml",
                            }
                            _send_bytes(
                                self,
                                200,
                                asset.read_bytes(),
                                types.get(asset.suffix.lower(), "application/octet-stream"),
                            )
                            return

            if path == "/api/admin/subjects":
                if not self._require_auth(query):
                    return
                try:
                    min_books = int(query.get("min_books", ["2"])[0])
                except ValueError:
                    min_books = 2
                subjects = list_multibook_subjects(
                    data_root / "polyindex", min_books=max(1, min_books)
                )
                _send_json(self, 200, {"ok": True, "subjects": subjects})
                return

            if path == "/api/admin/book-pages-audit":
                if not self._require_auth(query):
                    return
                report = audit_all_books(data_root)
                sha_filter = (query.get("source_sha256") or [""])[0].strip().lower()
                if sha_filter:
                    report["books"] = [
                        book for book in report["books"]
                        if book["source_sha256"] == sha_filter
                    ]
                _send_json(self, 200, {"ok": True, **report})
                return

            if path == "/api/admin/book-pages/render":
                if not self._require_auth(query):
                    return
                source_sha256 = (query.get("source_sha256") or [""])[0].strip()
                aligned_raw = (query.get("aligned_page") or [""])[0].strip()
                if not source_sha256:
                    _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                    return
                try:
                    aligned_page = int(aligned_raw)
                except ValueError:
                    _send_json(self, 400, {"ok": False, "error": "aligned_page must be an integer"})
                    return
                if aligned_page < 1:
                    _send_json(self, 400, {"ok": False, "error": "aligned_page must be positive"})
                    return
                try:
                    png_path = ensure_page_render_png(
                        data_root, source_sha256, aligned_page
                    )
                except PagePreviewError as exc:
                    _send_json(self, 400, {"ok": False, "error": str(exc)})
                    return
                _send_bytes(self, 200, png_path.read_bytes(), "image/png")
                return

            if path == "/api/admin/book-pages/transcript":
                if not self._require_auth(query):
                    return
                source_sha256 = (query.get("source_sha256") or [""])[0].strip()
                aligned_raw = (query.get("aligned_page") or [""])[0].strip()
                if not source_sha256:
                    _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                    return
                try:
                    aligned_page = int(aligned_raw)
                except ValueError:
                    _send_json(self, 400, {"ok": False, "error": "aligned_page must be an integer"})
                    return
                if aligned_page < 1:
                    _send_json(self, 400, {"ok": False, "error": "aligned_page must be positive"})
                    return
                try:
                    text, stage_key, producer_model = load_page_transcript(
                        data_root, source_sha256, aligned_page
                    )
                except PagePreviewError as exc:
                    _send_json(self, 400, {"ok": False, "error": str(exc)})
                    return
                _send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "source_sha256": source_sha256.strip().lower(),
                        "aligned_page": aligned_page,
                        "stage": stage_key,
                        "text": text,
                        "producer_model": producer_model,
                    },
                )
                return

            if path == "/health":
                _send_json(self, 200, {"ok": True})
                return

            parts = path.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "ingest" and parts[4] in ("events", "status"):
                if not self._require_auth(urllib.parse.parse_qs(parsed.query)):
                    return
                job_id = parts[3]
                action = parts[4]
                if action == "events":
                    self._handle_events(job_id)
                else:
                    self._handle_status(job_id)
                return

            self.send_error(404, "Not Found")

        def _handle_subjects_merge(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            target_id = payload.get("target_id")
            source_ids = payload.get("source_ids")
            if not isinstance(target_id, str) or not target_id.strip():
                _send_json(self, 400, {"ok": False, "error": "target_id is required"})
                return
            if not isinstance(source_ids, list) or not all(
                isinstance(sid, str) for sid in source_ids
            ):
                _send_json(self, 400, {"ok": False, "error": "source_ids must be a list of strings"})
                return
            try:
                result = merge_polyindex_subjects(
                    data_root / "polyindex", target_id.strip(), source_ids
                )
            except SubjectMergeError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            Log(INFO_LOG_LEVEL, "admin subjects merge done",
                {"target_id": target_id, "source_count": len(source_ids)})
            _send_json(self, 200, {"ok": True, "result": result})

        def _handle_book_page_exclude(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            source_sha256 = payload.get("source_sha256")
            aligned_page = payload.get("aligned_page")
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            if not isinstance(aligned_page, int) or aligned_page < 1:
                _send_json(self, 400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            try:
                result = exclude_book_page(
                    data_root, source_sha256.strip(), aligned_page
                )
            except PageExcludeError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            Log(INFO_LOG_LEVEL, "admin book page excluded",
                {"source_sha256": source_sha256[:16], "aligned_page": aligned_page})
            _send_json(self, 200, {"ok": True, "result": result})

        def _handle_book_page_transcript_save(self) -> None:
            try:
                body = _read_body(self, 8 * 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            source_sha256 = payload.get("source_sha256")
            aligned_page = payload.get("aligned_page")
            text = payload.get("text")
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            if not isinstance(aligned_page, int) or aligned_page < 1:
                _send_json(self, 400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            if not isinstance(text, str):
                _send_json(self, 400, {"ok": False, "error": "text must be a string"})
                return
            try:
                result = save_page_transcript(
                    data_root, source_sha256.strip(), aligned_page, text
                )
            except PagePreviewError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            Log(INFO_LOG_LEVEL, "admin book page transcript saved",
                {"source_sha256": source_sha256[:16], "aligned_page": aligned_page,
                 "stage": result.get("stage")})
            _send_json(self, 200, {"ok": True, "result": result})

        def _handle_book_page_transcript_confirm(self) -> None:
            try:
                body = _read_body(self, 8 * 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            source_sha256 = payload.get("source_sha256")
            aligned_page = payload.get("aligned_page")
            text = payload.get("text")
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            if not isinstance(aligned_page, int) or aligned_page < 1:
                _send_json(self, 400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            if not isinstance(text, str):
                _send_json(self, 400, {"ok": False, "error": "text must be a string"})
                return
            try:
                result = confirm_page_transcript(
                    data_root, source_sha256.strip(), aligned_page, text
                )
            except PagePreviewError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            Log(INFO_LOG_LEVEL, "admin book page transcript confirmed",
                {"source_sha256": source_sha256[:16], "aligned_page": aligned_page})
            _send_json(self, 200, {"ok": True, "result": result})

        def _handle_book_page_repair(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            source_sha256 = payload.get("source_sha256")
            aligned_page = payload.get("aligned_page")
            missing_in = payload.get("missing_in")
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            if not isinstance(aligned_page, int) or aligned_page < 1:
                _send_json(self, 400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            if missing_in is not None and (
                not isinstance(missing_in, list)
                or not all(isinstance(stage, str) for stage in missing_in)
            ):
                _send_json(self, 400, {"ok": False, "error": "missing_in must be a list of strings"})
                return
            job_id = registry.create_job()
            status_url = f"/api/ingest/{job_id}/status"
            events_url = f"/api/ingest/{job_id}/events"
            sha = source_sha256.strip()
            stages_hint = missing_in if isinstance(missing_in, list) else []

            def _worker() -> None:
                def reporter(ev: dict) -> None:
                    registry.emit(job_id, ev)

                acquired = job_semaphore.acquire(blocking=False)
                if not acquired:
                    registry.emit(job_id, make_event(
                        "queue",
                        "progress",
                        message="waiting for a free ingest slot",
                        max_concurrent_jobs=max_concurrent_jobs,
                    ))
                    job_semaphore.acquire()
                try:
                    registry.set_global_total(job_id, 3)
                    result = run_book_page_repair(
                        data_root,
                        settings,
                        sha,
                        aligned_page,
                        missing_in=stages_hint,
                        request_id=job_id,
                        progress=reporter,
                    )
                    registry.emit(job_id, make_event(
                        "page_repair",
                        STATUS_DONE,
                        result=result,
                    ))
                except PageRepairError as exc:
                    registry.emit(job_id, make_event(
                        "page_repair",
                        STATUS_ERROR,
                        message=str(exc),
                    ))
                except IngestInputValidationException as exc:
                    registry.emit(job_id, make_event(
                        "page_repair",
                        STATUS_ERROR,
                        message=exc.detail.message,
                        code=exc.detail.code.value,
                        field=exc.detail.field,
                    ))
                except Exception as exc:
                    Log(ERROR_LOG_LEVEL, "admin book page repair worker error",
                        {"job_id": job_id, "error": str(exc)})
                    registry.emit(job_id, make_event(
                        "page_repair",
                        STATUS_ERROR,
                        message=str(exc),
                    ))
                finally:
                    job_semaphore.release()

            threading.Thread(
                target=_worker, daemon=True, name=f"repair-{job_id[:8]}"
            ).start()
            Log(INFO_LOG_LEVEL, "admin book page repair job started",
                {"job_id": job_id, "source_sha256": sha[:16], "aligned_page": aligned_page})
            _send_json(self, 202, {
                "ok": True,
                "job_id": job_id,
                "status_url": status_url,
                "events_url": events_url,
            })

        def _handle_book_gaps_repair(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            source_sha256 = payload.get("source_sha256")
            gap_pages = payload.get("gap_pages")
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            if not isinstance(gap_pages, list) or not gap_pages:
                _send_json(self, 400, {"ok": False, "error": "gap_pages must be a non-empty list"})
                return
            for entry in gap_pages:
                if not isinstance(entry, dict):
                    _send_json(self, 400, {"ok": False, "error": "gap_pages entries must be objects"})
                    return
                aligned = entry.get("aligned")
                if not isinstance(aligned, int) or aligned < 1:
                    _send_json(self, 400, {"ok": False, "error": "each gap page must have positive aligned"})
                    return
                missing_in = entry.get("missing_in")
                if missing_in is not None and (
                    not isinstance(missing_in, list)
                    or not all(isinstance(stage, str) for stage in missing_in)
                ):
                    _send_json(self, 400, {"ok": False, "error": "missing_in must be a list of strings"})
                    return
            job_id = registry.create_job()
            status_url = f"/api/ingest/{job_id}/status"
            events_url = f"/api/ingest/{job_id}/events"
            sha = source_sha256.strip()
            gap_payload = gap_pages

            def _worker() -> None:
                def reporter(ev: dict) -> None:
                    registry.emit(job_id, ev)

                acquired = job_semaphore.acquire(blocking=False)
                if not acquired:
                    registry.emit(job_id, make_event(
                        "queue",
                        "progress",
                        message="waiting for a free ingest slot",
                        max_concurrent_jobs=max_concurrent_jobs,
                    ))
                    job_semaphore.acquire()
                try:
                    aligned_count = len({entry["aligned"] for entry in gap_payload})
                    registry.set_global_total(job_id, max(aligned_count * 3, 1))
                    result = run_book_gaps_repair(
                        data_root,
                        settings,
                        sha,
                        gap_payload,
                        request_id=job_id,
                        progress=reporter,
                    )
                    registry.emit(job_id, make_event(
                        "gaps_repair",
                        STATUS_DONE,
                        result=result,
                    ))
                except PageRepairError as exc:
                    registry.emit(job_id, make_event(
                        "gaps_repair",
                        STATUS_ERROR,
                        message=str(exc),
                    ))
                except IngestInputValidationException as exc:
                    registry.emit(job_id, make_event(
                        "gaps_repair",
                        STATUS_ERROR,
                        message=exc.detail.message,
                        code=exc.detail.code.value,
                        field=exc.detail.field,
                    ))
                except Exception as exc:
                    Log(ERROR_LOG_LEVEL, "admin book gaps repair worker error",
                        {"job_id": job_id, "error": str(exc)})
                    registry.emit(job_id, make_event(
                        "gaps_repair",
                        STATUS_ERROR,
                        message=str(exc),
                    ))
                finally:
                    job_semaphore.release()

            threading.Thread(
                target=_worker, daemon=True, name=f"gaps-repair-{job_id[:8]}"
            ).start()
            Log(INFO_LOG_LEVEL, "admin book gaps repair job started",
                {"job_id": job_id, "source_sha256": sha[:16], "page_count": len(gap_pages)})
            _send_json(self, 202, {
                "ok": True,
                "job_id": job_id,
                "status_url": status_url,
                "events_url": events_url,
            })

        def _handle_status(self, job_id: str) -> None:
            snapshot = registry.get_status(job_id)
            if snapshot is None:
                _send_json(self, 404, {"ok": False, "error": "job not found"})
                return
            _send_json(self, 200, {"ok": True, **snapshot})

        def _handle_events(self, job_id: str) -> None:
            snapshot = registry.get_status(job_id)
            if snapshot is None:
                _send_json(self, 404, {"ok": False, "error": "job not found"})
                return

            last_seq_raw = self.headers.get("Last-Event-ID", "-1")
            try:
                last_seq = int(last_seq_raw)
            except ValueError:
                last_seq = -1

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            Log(INFO_LOG_LEVEL, "SSE subscriber connected", {"job_id": job_id, "last_seq": last_seq})
            terminal_statuses = {STATUS_DONE, STATUS_ERROR}

            for ev in registry.subscribe(job_id, last_seq=last_seq):
                event_name = ev.get("status", "progress")
                ok = _sse_write(self, event_name, ev)
                if not ok:
                    Log(INFO_LOG_LEVEL, "SSE client disconnected", {"job_id": job_id})
                    break
                if event_name in terminal_statuses:
                    break

            Log(INFO_LOG_LEVEL, "SSE subscriber done", {"job_id": job_id})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if research_try_post(self, parsed.path):
                return
            if parsed.path == "/api/admin/subjects/merge":
                if not self._require_auth():
                    return
                self._handle_subjects_merge()
                return
            if parsed.path == "/api/admin/book-pages/exclude":
                if not self._require_auth():
                    return
                self._handle_book_page_exclude()
                return
            if parsed.path == "/api/admin/book-pages/transcript/confirm":
                if not self._require_auth():
                    return
                self._handle_book_page_transcript_confirm()
                return
            if parsed.path == "/api/admin/book-pages/transcript":
                if not self._require_auth():
                    return
                self._handle_book_page_transcript_save()
                return
            if parsed.path == "/api/admin/book-pages/repair":
                if not self._require_auth():
                    return
                self._handle_book_page_repair()
                return
            if parsed.path == "/api/admin/book-pages/repair-all":
                if not self._require_auth():
                    return
                self._handle_book_gaps_repair()
                return
            if parsed.path != "/api/ingest/submit":
                self.send_error(404, "Not Found")
                return
            if not self._require_auth():
                return

            content_type = self.headers.get("Content-Type") or ""
            part_path = data_root / "input" / "raw" / f".upload_{secrets.token_hex(8)}.part"
            try:
                content_length = _request_content_length(self)
                parsed = parse_multipart_form_stream(
                    self.rfile,
                    content_type,
                    content_length=content_length,
                    max_bytes=max_upload,
                    pdf_part_path=part_path,
                )
                text_fields = parsed.text_fields
            except (ValueError, OSError) as exc:
                part_path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest multipart parse failed", {"error": str(exc)})
                _send_validation_error(
                    self,
                    IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                    "multipart form could not be parsed",
                    "form",
                )
                return

            try:
                ingest_payload = build_ingest_payload_from_form(text_fields)
            except InvalidPagesSpec as exc:
                if parsed.pdf is not None:
                    parsed.pdf.path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest form pages spec invalid", {"error": str(exc)})
                _send_validation_error(
                    self, IngestInputErrorCode.INPUT_SCHEMA_INVALID, str(exc), "pages_to_remove"
                )
                return
            except InvalidRangeField as exc:
                if parsed.pdf is not None:
                    parsed.pdf.path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest form range field invalid",
                    {"field": exc.field, "error": exc.message_text})
                _send_validation_error(
                    self, IngestInputErrorCode.INPUT_SCHEMA_INVALID, exc.message_text, exc.field
                )
                return
            except ValueError as exc:
                if parsed.pdf is not None:
                    parsed.pdf.path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest form payload invalid", {"error": str(exc)})
                _send_validation_error(
                    self, IngestInputErrorCode.INPUT_SCHEMA_INVALID, str(exc), "payload"
                )
                return

            uploaded = parsed.pdf
            if uploaded is None:
                part_path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest submit rejected: pdf_file missing")
                _send_validation_error(
                    self, IngestInputErrorCode.PDF_NOT_FOUND, "PDF file upload is required", "pdf_file"
                )
                return
            if uploaded.size == 0:
                uploaded.path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest submit rejected: empty PDF upload")
                _send_validation_error(
                    self, IngestInputErrorCode.PDF_NOT_FOUND, "empty PDF upload", "pdf_file"
                )
                return
            with uploaded.path.open("rb") as pdf_handle:
                if pdf_handle.read(4) != b"%PDF":
                    uploaded.path.unlink(missing_ok=True)
                    Log(WARNING_LOG_LEVEL, "ingest submit rejected: not a PDF (magic bytes)")
                    _send_validation_error(
                        self,
                        IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                        "uploaded file is not a PDF",
                        "pdf_file",
                    )
                    return

            saved_path = uploaded.path.with_name(
                f"{secrets.token_hex(6)}_{_safe_filename(uploaded.filename or 'upload.pdf')}"
            )
            uploaded.path.rename(saved_path)
            Log(INFO_LOG_LEVEL, "ingest raw PDF saved",
                {"path": str(saved_path), "bytes": uploaded.size})

            try:
                require_gpu_vram_at_pipeline_start(settings, skip_vision_editor=False)
            except IngestInputValidationException as exc:
                saved_path.unlink(missing_ok=True)
                Log(WARNING_LOG_LEVEL, "ingest submit blocked by gpu vram preflight",
                    {"error": exc.detail.message})
                _send_validation_error(
                    self,
                    exc.detail.code,
                    exc.detail.message,
                    exc.detail.field,
                )
                return

            job_id = registry.create_job()
            events_url = f"/api/ingest/{job_id}/events"
            status_url = f"/api/ingest/{job_id}/status"

            def _worker() -> None:
                def reporter(ev: dict) -> None:
                    registry.emit(job_id, ev)

                acquired = job_semaphore.acquire(blocking=False)
                if not acquired:
                    registry.emit(job_id, make_event(
                        "queue",
                        "progress",
                        message="waiting for a free ingest slot",
                        max_concurrent_jobs=max_concurrent_jobs,
                    ))
                    job_semaphore.acquire()
                try:
                    run_full_pipeline(
                        ingest_payload,
                        saved_path,
                        settings,
                        reporter=reporter,
                        set_global_total=lambda total: registry.set_global_total(job_id, total),
                    )
                except IngestInputValidationException:
                    pass
                except Exception as exc:
                    Log(ERROR_LOG_LEVEL, "ingest pipeline worker unhandled error",
                        {"job_id": job_id, "error": str(exc)})
                    registry.emit(job_id, make_event(
                        "pipeline",
                        STATUS_ERROR,
                        message=str(exc),
                    ))
                finally:
                    job_semaphore.release()

            t = threading.Thread(target=_worker, daemon=True, name=f"ingest-{job_id[:8]}")
            t.start()

            Log(INFO_LOG_LEVEL, "ingest job started",
                {"job_id": job_id, "events_url": events_url})
            _send_json(self, 202, {
                "ok": True,
                "job_id": job_id,
                "events_url": events_url,
                "status_url": status_url,
            })

    httpd = ThreadingHTTPServer((host, port), IngestHandler)
    return httpd, registry


def run_ingest_http_server() -> None:
    logInit(INFO_LOG_LEVEL)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        Log(ERROR_LOG_LEVEL, "ingest server configuration failed", {"error": str(exc)})
        raise SystemExit(str(exc)) from exc

    host = os.environ.get("INGEST_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("INGEST_HTTP_PORT", "8765"))
    max_upload = int(os.environ.get("INGEST_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
    api_token = os.environ.get("INGEST_API_TOKEN", "").strip()
    max_concurrent_jobs = max(1, int(os.environ.get("INGEST_MAX_CONCURRENT_JOBS", "1")))

    if host not in ("127.0.0.1", "localhost") and not api_token:
        Log(
            WARNING_LOG_LEVEL,
            "ingest server bound to a non-loopback address WITHOUT auth token; "
            "set INGEST_API_TOKEN to protect the API",
            {"host": host},
        )

    httpd, _registry = build_ingest_server(
        settings,
        host=host,
        port=port,
        max_upload=max_upload,
        api_token=api_token,
        max_concurrent_jobs=max_concurrent_jobs,
    )
    Log(INFO_LOG_LEVEL, "ingest http server listening", {"url": f"http://{host}:{port}"})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        Log(INFO_LOG_LEVEL, "ingest http server shutdown requested")


if __name__ == "__main__":
    run_ingest_http_server()

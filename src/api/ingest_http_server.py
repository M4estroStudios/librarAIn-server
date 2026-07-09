from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src.search.article_catalog import research_status_summary
from src.api.admin_embeddings import (
    try_handle_admin_embeddings_get,
    try_handle_admin_embeddings_post,
)
from src.api.chat_completions_handler import handle_chat_completions
from src.api.etaly_export_handler import build_etaly_export_routes
from src.api.system_preflight import evaluate_preflight, normalize_preflight_operation
from src.api.ingest_form import (
    InvalidPagesSpec,
    InvalidRangeField,
    build_ingest_payload_from_form,
    parse_multipart_form_stream,
)
from src.api.ingest_pipeline_runner import run_full_pipeline
from src.api.ingest_pipeline_runner_glm import run_glm_ingest_pipeline
from src.api.ingest_form import _parse_pages_spec
from src.api.reicat_vision_suggest import suggest_reicat_metadata
from src.api.job_history import list_active_jobs_with_batches, list_job_history
from src.api.job_registry import JobRegistry
from src.api.research_handlers import ResearchBatchRegistry, build_research_routes
from src.search.research_runner import ResearchConcurrencyLimiter, ResearchDedupIndex
from src.ingestion.polyindex.index_json import (
    SubjectMergeError,
    SubjectUpdateError,
    get_polyindex_subject,
    list_multibook_subjects,
    merge_polyindex_subjects,
    remove_polyindex_subject_book,
    update_polyindex_subject_metadata,
    update_polyindex_subject_pages,
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
from src.persistence.book_page_repair import (
    PageRepairError,
    normalize_repair_pipeline_mode,
    repair_global_step_count,
    run_book_gaps_repair,
    run_book_page_repair,
)
from src.core.config import ConfigurationError, load_settings
from src.core.hashing import new_job_id
from src.core.log import DEBUG_LOG_LEVEL, ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL, logInit
from src.ingestion.pipeline.engine import require_gpu_vram_at_pipeline_start
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.request import IngestInputErrorCode, IngestInputValidationError, IngestInputValidationException

_CLIENT_DISCONNECT_ERRORS = (
    ConnectionAbortedError,
    ConnectionResetError,
    BrokenPipeError,
)


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


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/")


_WEB_PAGE_ALIASES = frozenset({
    "/",
    "/index.html",
    "/index2.html",
    "/dashboard",
    "/dashboard.html",
    "/admin",
    "/admin.html",
    "/ricerca",
    "/ricerca.html",
    "/jobs",
    "/jobs.html",
})


def _is_web_page_visit(path: str) -> bool:
    if path in _WEB_PAGE_ALIASES:
        return True
    return path.endswith(".html")


def _http_inbound_log_level(path: str, status: int, method: str) -> int:
    if status >= 500:
        return ERROR_LOG_LEVEL
    if status >= 400:
        return WARNING_LOG_LEVEL
    if _is_web_page_visit(path):
        return INFO_LOG_LEVEL
    if _is_api_path(path):
        if method.upper() == "GET":
            return DEBUG_LOG_LEVEL
        return INFO_LOG_LEVEL
    return DEBUG_LOG_LEVEL


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except _CLIENT_DISCONNECT_ERRORS:
        pass


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
    except _CLIENT_DISCONNECT_ERRORS:
        return False


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _listening_pids_for_port(port: int) -> set[str]:
    if os.name == "nt":
        output = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            errors="ignore",
            check=False,
        ).stdout
        pids: set[str] = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                pids.add(parts[-1])
        return pids

    result = subprocess.run(
        ["sh", "-c", f"command -v lsof >/dev/null 2>&1 && lsof -ti tcp:{port} || true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {pid for pid in result.stdout.split() if pid.isdigit()}


def _stop_existing_server_processes(port: int) -> None:
    stopped: list[str] = []
    for pid in sorted(_listening_pids_for_port(port)):
        if pid == "0" or pid == str(os.getpid()):
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        stopped.append(pid)
    if stopped:
        Log(
            INFO_LOG_LEVEL,
            "stopped existing ingest server process(es)",
            {"port": port, "pids": stopped},
        )


def _settings_sqlite_path(settings: Any) -> str:
    sqlite_path = getattr(settings, "sqlite_path", None)
    if sqlite_path:
        return str(sqlite_path)
    return str(Path(settings.data_root) / "db" / "biblioteca.db")


def build_ingest_server(
    settings: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    max_upload: int = 512 * 1024 * 1024,
    api_token: str = "",
    max_concurrent_jobs: int = 1,
    max_concurrent_research: int = 1,
    research_dedup_ttl_seconds: float = 3600.0,
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
    research_dedup_index = ResearchDedupIndex(ttl_seconds=research_dedup_ttl_seconds)
    research_concurrency = ResearchConcurrencyLimiter(max_concurrent_research)
    job_semaphore = threading.Semaphore(max_concurrent_jobs)

    etaly_try_get, etaly_try_post = build_etaly_export_routes(
        data_root=data_root,
        web_dir=web_dir,
        settings=settings,
        send_json=_send_json,
        send_bytes=_send_bytes,
        read_json_body=_read_body,
    )

    research_try_get, research_try_post = build_research_routes(
        data_root=data_root,
        web_dir=web_dir,
        settings=settings,
        registry=registry,
        dedup_index=research_dedup_index,
        batch_registry=research_batch_registry,
        concurrency_limiter=research_concurrency,
        send_json=_send_json,
        send_bytes=_send_bytes,
        read_json_body=_read_body,
        sse_write=_sse_write,
    )

    class IngestHandler(BaseHTTPRequestHandler):
        server_version = "librarAIn-ingest-http/1.0"

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except _CLIENT_DISCONNECT_ERRORS:
                pass

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
            message = "http page visit" if _is_web_page_visit(path) else "http"
            Log(
                _http_inbound_log_level(path, status, self.command),
                message,
                {"method": self.command, "path": path, "status": status},
            )

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
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

            if etaly_try_get(self, path, query):
                return

            if research_try_get(self, path, query):
                return

            if path in ("/dashboard", "/dashboard.html"):
                dash_file = web_dir / "dashboard.html"
                if not dash_file.exists():
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing",
                        {"path": str(dash_file)})
                    _send_json(self, 500, {"ok": False, "error": "web/dashboard.html missing"})
                    return
                _send_bytes(self, 200, dash_file.read_bytes(), "text/html; charset=utf-8")
                return

            if path.startswith("/dashboard/"):
                rel = path[len("/dashboard/") :].lstrip("/")
                if rel and ".." not in rel.replace("\\", "/"):
                    dash_root = (web_dir / "dashboard").resolve()
                    asset = (dash_root / rel).resolve()
                    try:
                        asset.relative_to(dash_root)
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

            if path == "/api/system/preflight":
                if not self._require_auth(query):
                    return
                operation_raw = (query.get("operation", [""])[0] or "").strip()
                operation = normalize_preflight_operation(operation_raw)
                if operation is None:
                    _send_json(
                        self,
                        400,
                        {"ok": False, "error": "invalid operation", "operation": operation_raw},
                    )
                    return
                result = evaluate_preflight(settings, operation)
                _send_json(self, 200, {"ok": result["ok"], **result})
                return

            if path == "/api/system/status":
                if not self._require_auth(query):
                    return
                from src.ingestion.pipeline.gpu_vram import collect_gpu_vram_snapshots
                from src.api.system_preflight import _list_lmstudio_models

                snapshots = collect_gpu_vram_snapshots(gpu_device="all")
                models_payload, lm_root = _list_lmstudio_models(settings)
                vram = [
                    {
                        "device_index": s.device_index,
                        "used_gb": round(s.used_gb, 2),
                        "free_gb": round(s.free_gb, 2),
                        "total_gb": round(s.total_gb, 2),
                    }
                    for s in snapshots
                ]
                loaded = [
                    m.get("key") or m.get("display_name")
                    for m in models_payload
                    if m.get("loaded_instances")
                ]
                research_summary = research_status_summary(data_root)
                active_jobs = registry.running_job_count() + research_batch_registry.running_count()
                _send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "vram": vram,
                        "loaded_models": loaded,
                        "lmstudio_root": lm_root,
                        "active_jobs": active_jobs,
                        "research": research_summary,
                    },
                )
                return

            if path == "/api/system/jobs/history":
                if not self._require_auth(query):
                    return
                book = (query.get("book", [""])[0] or "").strip()
                job_id_filter = (query.get("id", [""])[0] or "").strip()
                date_filter = (query.get("date", [""])[0] or "").strip()
                try:
                    limit = min(500, max(1, int(query.get("limit", ["200"])[0] or 200)))
                except ValueError:
                    limit = 200
                jobs = list_job_history(
                    sqlite_path=_settings_sqlite_path(settings),
                    registry=registry,
                    batch_registry=research_batch_registry,
                    book=book,
                    job_id=job_id_filter,
                    date=date_filter,
                    limit=limit,
                )
                _send_json(self, 200, {"ok": True, "jobs": jobs, "count": len(jobs)})
                return

            if path == "/api/system/jobs":
                if not self._require_auth(query):
                    return
                include_finished = (query.get("include_finished", ["0"])[0] or "0").lower() in (
                    "1",
                    "true",
                    "yes",
                )
                try:
                    limit = min(100, max(1, int(query.get("limit", ["30"])[0] or 30)))
                except ValueError:
                    limit = 30
                jobs = list_active_jobs_with_batches(
                    registry=registry,
                    batch_registry=research_batch_registry,
                    limit=limit,
                    include_finished=include_finished,
                )
                active_count = sum(1 for job in jobs if job.get("is_active"))
                _send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "jobs": jobs,
                        "active_jobs": active_count,
                    },
                )
                return

            parts = path.split("/")
            if (
                len(parts) == 5
                and parts[1] == "api"
                and parts[2] == "system"
                and parts[3] == "jobs"
            ):
                if not self._require_auth(query):
                    return
                job_id = parts[4]
                summary = registry.get_job_summary(job_id)
                if summary is None:
                    summary = research_batch_registry.get_job_summary(job_id)
                if summary is None:
                    _send_json(self, 404, {"ok": False, "error": "job not found"})
                    return
                _send_json(self, 200, {"ok": True, "job": summary})
                return

            if (
                len(parts) == 6
                and parts[1] == "api"
                and parts[2] == "system"
                and parts[3] == "jobs"
                and parts[5] == "events"
            ):
                if not self._require_auth(query):
                    return
                self._handle_system_job_events(parts[4])
                return

            if path in ("/index2.html",):
                index2_file = web_dir / "index2.html"
                if not index2_file.exists():
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing",
                        {"path": str(index2_file)})
                    _send_json(self, 500, {"ok": False, "error": "web/index2.html missing"})
                    return
                _send_bytes(self, 200, index2_file.read_bytes(), "text/html; charset=utf-8")
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

            if path in ("/jobs", "/jobs.html"):
                jobs_file = web_dir / "jobs.html"
                if not jobs_file.exists():
                    Log(ERROR_LOG_LEVEL, "ingest server static web asset missing",
                        {"path": str(jobs_file)})
                    _send_json(self, 500, {"ok": False, "error": "web/jobs.html missing"})
                    return
                _send_bytes(self, 200, jobs_file.read_bytes(), "text/html; charset=utf-8")
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

            if path == "/article-source-viewer.js":
                viewer_js = web_dir / "article-source-viewer.js"
                if viewer_js.is_file():
                    _send_bytes(self, 200, viewer_js.read_bytes(), "text/javascript; charset=utf-8")
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

            if path == "/api/admin/subject":
                if not self._require_auth(query):
                    return
                canonical_id = (query.get("canonical_id") or [""])[0].strip()
                if not canonical_id:
                    _send_json(self, 400, {"ok": False, "error": "canonical_id is required"})
                    return
                subject = get_polyindex_subject(data_root / "polyindex", canonical_id)
                if subject is None:
                    _send_json(self, 404, {"ok": False, "error": "subject not found"})
                    return
                _send_json(self, 200, {"ok": True, "subject": subject})
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

            if try_handle_admin_embeddings_get(
                path,
                self,
                data_root=data_root,
                settings=settings,
                send_json=_send_json,
                require_auth=lambda: self._require_auth(query),
            ):
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

        def _handle_subject_update(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            canonical_id = payload.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                _send_json(self, 400, {"ok": False, "error": "canonical_id is required"})
                return
            aliases = payload.get("aliases")
            if aliases is not None and (
                not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases)
            ):
                _send_json(self, 400, {"ok": False, "error": "aliases must be a list of strings"})
                return
            time_range = payload.get("time_range")
            if time_range is not None and not isinstance(time_range, str):
                _send_json(self, 400, {"ok": False, "error": "time_range must be a string"})
                return
            clear_time_range = payload.get("clear_time_range") is True
            try:
                subject = update_polyindex_subject_metadata(
                    data_root / "polyindex",
                    canonical_id.strip(),
                    aliases=aliases,
                    time_range=time_range,
                    clear_time_range=clear_time_range,
                )
            except SubjectUpdateError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            _send_json(self, 200, {"ok": True, "subject": subject})

        def _handle_subject_pages(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            canonical_id = payload.get("canonical_id")
            source_sha256 = payload.get("source_sha256")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                _send_json(self, 400, {"ok": False, "error": "canonical_id is required"})
                return
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            add_pages = payload.get("add_pages")
            remove_pages = payload.get("remove_pages")
            if add_pages is not None and (
                not isinstance(add_pages, list)
                or not all(isinstance(page, int) for page in add_pages)
            ):
                _send_json(self, 400, {"ok": False, "error": "add_pages must be a list of integers"})
                return
            if remove_pages is not None and (
                not isinstance(remove_pages, list)
                or not all(isinstance(page, int) for page in remove_pages)
            ):
                _send_json(self, 400, {"ok": False, "error": "remove_pages must be a list of integers"})
                return
            book_title = payload.get("book_title")
            book_slug = payload.get("book_slug")
            if book_title is not None and not isinstance(book_title, str):
                _send_json(self, 400, {"ok": False, "error": "book_title must be a string"})
                return
            if book_slug is not None and not isinstance(book_slug, str):
                _send_json(self, 400, {"ok": False, "error": "book_slug must be a string"})
                return
            try:
                subject = update_polyindex_subject_pages(
                    data_root / "polyindex",
                    canonical_id.strip(),
                    source_sha256.strip(),
                    add_pages=add_pages,
                    remove_pages=remove_pages,
                    book_title=book_title,
                    book_slug=book_slug,
                )
            except (SubjectUpdateError, ValueError) as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            _send_json(self, 200, {"ok": True, "subject": subject})

        def _handle_subject_book_remove(self) -> None:
            try:
                body = _read_body(self, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError) as exc:
                _send_json(self, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return
            canonical_id = payload.get("canonical_id")
            source_sha256 = payload.get("source_sha256")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                _send_json(self, 400, {"ok": False, "error": "canonical_id is required"})
                return
            if not isinstance(source_sha256, str) or not source_sha256.strip():
                _send_json(self, 400, {"ok": False, "error": "source_sha256 is required"})
                return
            try:
                subject = remove_polyindex_subject_book(
                    data_root / "polyindex",
                    canonical_id.strip(),
                    source_sha256.strip(),
                )
            except SubjectUpdateError as exc:
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            _send_json(self, 200, {"ok": True, "subject": subject})

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

        def _handle_reicat_suggest(self) -> None:
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
            except (ValueError, OSError) as exc:
                part_path.unlink(missing_ok=True)
                _send_json(self, 400, {"ok": False, "error": f"multipart form could not be parsed: {exc}"})
                return

            uploaded = parsed.pdf
            if uploaded is None:
                part_path.unlink(missing_ok=True)
                _send_json(self, 400, {"ok": False, "error": "pdf_file upload is required"})
                return
            if uploaded.size == 0:
                uploaded.path.unlink(missing_ok=True)
                _send_json(self, 400, {"ok": False, "error": "empty PDF upload"})
                return
            with uploaded.path.open("rb") as pdf_handle:
                pdf_magic = pdf_handle.read(4)
            if pdf_magic != b"%PDF":
                uploaded.path.unlink(missing_ok=True)
                _send_json(self, 400, {"ok": False, "error": "uploaded file is not a PDF"})
                return

            saved_path = uploaded.path.with_name(
                f"{secrets.token_hex(6)}_{_safe_filename(uploaded.filename or 'upload.pdf')}"
            )
            uploaded.path.rename(saved_path)
            pages_one_based: list[int] | None = None
            reicat_pages_raw = (parsed.text_fields.get("reicat_pages") or "").strip()
            if reicat_pages_raw:
                try:
                    pages_one_based = _parse_pages_spec(reicat_pages_raw)
                except ValueError as exc:
                    saved_path.unlink(missing_ok=True)
                    _send_json(self, 400, {"ok": False, "error": str(exc)})
                    return
            try:
                result = suggest_reicat_metadata(
                    saved_path,
                    settings,
                    pages_one_based=pages_one_based,
                )
            except IngestInputValidationException as exc:
                saved_path.unlink(missing_ok=True)
                _send_validation_error(
                    self,
                    exc.detail.code,
                    exc.detail.message,
                    exc.detail.field,
                )
                return
            except ValueError as exc:
                saved_path.unlink(missing_ok=True)
                _send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                saved_path.unlink(missing_ok=True)
                Log(ERROR_LOG_LEVEL, "reicat suggest handler failed", {"error": str(exc)})
                _send_json(self, 500, {"ok": False, "error": str(exc)})
                return
            finally:
                saved_path.unlink(missing_ok=True)

            _send_json(self, 200, {"ok": True, **result})

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
            pipeline_mode = normalize_repair_pipeline_mode(payload.get("pipeline_mode"))
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
            sha = source_sha256.strip()
            job_id, _started_at = new_job_id(f"{sha[:16]}_repair_p{aligned_page}")
            registry.create_job(job_id=job_id)
            status_url = f"/api/ingest/{job_id}/status"
            events_url = f"/api/ingest/{job_id}/events"
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
                    registry.set_global_total(
                        job_id,
                        repair_global_step_count(1, pipeline_mode=pipeline_mode),
                    )
                    registry.emit(
                        job_id,
                        make_event(
                            "page_repair",
                            STATUS_STARTED,
                            source_sha256=sha,
                            aligned_page=aligned_page,
                            missing_in=stages_hint,
                            pipeline_mode=pipeline_mode,
                            message=(
                                "Riparazione lacune: " + ", ".join(stages_hint)
                                if stages_hint
                                else f"Riparazione pagina {aligned_page}"
                            ),
                        ),
                    )
                    result = run_book_page_repair(
                        data_root,
                        settings,
                        sha,
                        aligned_page,
                        missing_in=stages_hint,
                        request_id=job_id,
                        progress=reporter,
                        pipeline_mode=pipeline_mode,
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
            pipeline_mode = normalize_repair_pipeline_mode(payload.get("pipeline_mode"))
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
            sha = source_sha256.strip()
            job_id, _started_at = new_job_id(f"{sha[:16]}_gaps_repair")
            registry.create_job(job_id=job_id)
            status_url = f"/api/ingest/{job_id}/status"
            events_url = f"/api/ingest/{job_id}/events"
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
                    registry.set_global_total(
                        job_id,
                        repair_global_step_count(aligned_count, pipeline_mode=pipeline_mode),
                    )
                    registry.emit(
                        job_id,
                        make_event(
                            "gaps_repair",
                            STATUS_STARTED,
                            source_sha256=sha,
                            pipeline_mode=pipeline_mode,
                            message=f"Riparazione {aligned_count} pagine con lacune",
                        ),
                    )
                    result = run_book_gaps_repair(
                        data_root,
                        settings,
                        sha,
                        gap_payload,
                        request_id=job_id,
                        progress=reporter,
                        pipeline_mode=pipeline_mode,
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

        def _handle_system_job_events(self, job_id: str) -> None:
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

            job_kind = snapshot.get("job_kind")
            if job_kind == "research":
                terminal_statuses = {"succeeded", "failed"}
            else:
                terminal_statuses = {STATUS_DONE, STATUS_ERROR}

            for ev in registry.subscribe(job_id, last_seq=last_seq):
                event_name = ev.get("status", "progress")
                ok = _sse_write(self, event_name, ev)
                if not ok:
                    break
                if event_name in terminal_statuses:
                    break

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
            if parsed.path == "/api/chat/completions":
                if not self._require_auth():
                    return
                handle_chat_completions(
                    self,
                    data_root=data_root,
                    settings=settings,
                    read_json_body=_read_body,
                    send_json=_send_json,
                )
                return
            if parsed.path.startswith("/api/etaly/"):
                try:
                    if etaly_try_post(self, parsed.path):
                        return
                except Exception as exc:
                    Log(
                        ERROR_LOG_LEVEL,
                        "etaly export POST handler crashed",
                        {"path": parsed.path, "error": str(exc)},
                    )
                    _send_json(self, 500, {"ok": False, "error": str(exc)})
                    return
                _send_json(self, 404, {"ok": False, "error": "not found"})
                return
            if parsed.path.startswith("/api/research/"):
                try:
                    if research_try_post(self, parsed.path):
                        return
                except Exception as exc:
                    Log(
                        ERROR_LOG_LEVEL,
                        "research POST handler crashed",
                        {"path": parsed.path, "error": str(exc)},
                    )
                    _send_json(self, 500, {"ok": False, "error": str(exc)})
                    return
                _send_json(self, 404, {"ok": False, "error": "not found"})
                return
            if parsed.path == "/api/admin/subjects/merge":
                if not self._require_auth():
                    return
                self._handle_subjects_merge()
                return
            if parsed.path == "/api/admin/subject/update":
                if not self._require_auth():
                    return
                self._handle_subject_update()
                return
            if parsed.path == "/api/admin/subject/pages":
                if not self._require_auth():
                    return
                self._handle_subject_pages()
                return
            if parsed.path == "/api/admin/subject/book/remove":
                if not self._require_auth():
                    return
                self._handle_subject_book_remove()
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
            if try_handle_admin_embeddings_post(
                parsed.path,
                self,
                data_root=data_root,
                settings=settings,
                registry=registry,
                job_semaphore=job_semaphore,
                send_json=_send_json,
                require_auth=self._require_auth,
            ):
                return
            if parsed.path == "/api/ingest/reicat-suggest":
                if not self._require_auth():
                    return
                self._handle_reicat_suggest()
                return
            if parsed.path not in ("/api/ingest/submit", "/api/ingest2/submit"):
                self.send_error(404, "Not Found")
                return
            pipeline_runner = (
                run_glm_ingest_pipeline
                if parsed.path == "/api/ingest2/submit"
                else run_full_pipeline
            )
            ocr_backend = "glm" if parsed.path == "/api/ingest2/submit" else "easyocr"
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
                pdf_magic = pdf_handle.read(4)
            if pdf_magic != b"%PDF":
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
                require_gpu_vram_at_pipeline_start(
                    settings,
                    skip_vision_editor=False,
                    ocr_backend=ocr_backend,
                )
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

            pdf_stem = Path(uploaded.filename or "upload.pdf").stem
            job_id, _started_at = new_job_id(pdf_stem)
            registry.create_job(job_id=job_id)
            events_url = f"/api/ingest/{job_id}/events"
            status_url = f"/api/ingest/{job_id}/status"

            def _worker() -> None:
                def reporter(ev: dict) -> None:
                    registry.emit(job_id, ev)

                reicat = ingest_payload.get("reicat")
                book_title = None
                if isinstance(reicat, dict):
                    book_title = str(reicat.get("titolo") or "").strip() or None
                if not book_title:
                    book_title = str(ingest_payload.get("titolo") or "").strip() or None
                registry.emit(
                    job_id,
                    make_event(
                        "pipeline",
                        "started",
                        titolo=book_title,
                        book_title=book_title,
                        message=book_title or "Avvio ingestione libro",
                    ),
                )

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
                    pipeline_runner(
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

    httpd = ExclusiveThreadingHTTPServer((host, port), IngestHandler)
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
    max_concurrent_research = max(
        1, int(os.environ.get("RESEARCH_MAX_CONCURRENT_JOBS", "1"))
    )
    research_dedup_ttl_seconds = float(
        os.environ.get("RESEARCH_DEDUP_TTL_SECONDS", "3600")
    )

    if host not in ("127.0.0.1", "localhost") and not api_token:
        Log(
            WARNING_LOG_LEVEL,
            "ingest server bound to a non-loopback address WITHOUT auth token; "
            "set INGEST_API_TOKEN to protect the API",
            {"host": host},
        )

    _stop_existing_server_processes(port)

    httpd, _registry = build_ingest_server(
        settings,
        host=host,
        port=port,
        max_upload=max_upload,
        api_token=api_token,
        max_concurrent_jobs=max_concurrent_jobs,
        max_concurrent_research=max_concurrent_research,
        research_dedup_ttl_seconds=research_dedup_ttl_seconds,
    )
    Log(INFO_LOG_LEVEL, "ingest http server listening", {"url": f"http://{host}:{port}"})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        Log(INFO_LOG_LEVEL, "ingest http server shutdown requested")


if __name__ == "__main__":
    run_ingest_http_server()

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, bind_log_context, reset_log_context
from src.models.settings import Settings
from src.persistence.research_runs import (
    create_research_run_accepted,
    mark_research_run_failed,
    mark_research_run_running,
    mark_research_run_succeeded,
)
from src.search.article_catalog import (
    generate_article_for_poh,
    list_ingested_books,
    list_missing_articles,
    research_status_summary,
    resolve_article_file,
    search_articles,
)
from src.search.article_llm import query_log_fields
from src.search.request_schema import ResearchInputValidationError
from src.search.request_validation import validate_research_request
from src.search.research_runner import (
    RESEARCH_PIPELINE_VERSION,
    ResearchConcurrencyLimiter,
    ResearchDedupIndex,
    ResearchRunResult,
    build_article_response,
    compute_dedup_key,
    persist_query_markdown,
    run_research,
)

SendJson = Callable[[BaseHTTPRequestHandler, int, Any], None]
SendBytes = Callable[[BaseHTTPRequestHandler, int, bytes, str], None]
SseWrite = Callable[[BaseHTTPRequestHandler, str, Any], bool]
_RESEARCH_TERMINAL = frozenset({"succeeded", "failed"})
_MAX_EVENTS = 50


class ResearchBatchRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, *, total: int) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "total": total,
                "done": 0,
                "generated": [],
                "errors": [],
                "request_ids": [],
                "created_at": now,
                "updated_at": now,
            }
        return job_id

    def set_total(self, job_id: str, total: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["total"] = total
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def append_generated(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["generated"].append(item)
            request_id = item.get("request_id")
            if request_id:
                job["request_ids"].append(request_id)
            job["done"] = len(job["generated"]) + len(job["errors"])
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def append_error(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["errors"].append(item)
            job["done"] = len(job["generated"]) + len(job["errors"])
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def finish(self, job_id: str, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


def _validation_error_response(exc: ValueError) -> dict[str, Any]:
    try:
        detail = ResearchInputValidationError.model_validate_json(str(exc))
        payload = detail.model_dump(mode="json")
        return {"error": detail.message, "errors": payload}
    except Exception:
        return {"error": str(exc)}


def _tail_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(events) <= _MAX_EVENTS:
        return events
    return events[-_MAX_EVENTS:]


def _record_research_run_accepted(
    settings: Settings,
    *,
    request_id: str,
    query: str,
    poh_id: str | None = None,
    poh_label: str | None = None,
) -> None:
    poh = None
    if poh_id or poh_label:
        from src.search.request_schema import ResearchPoh

        poh = ResearchPoh(
            id=poh_id or "",
            label=poh_label or poh_id or "",
        )
    create_research_run_accepted(
        settings.sqlite_path,
        request_id=request_id,
        query=query,
        poh=poh,
        pipeline_version=RESEARCH_PIPELINE_VERSION,
    )


def _record_research_run_succeeded(settings: Settings, result: ResearchRunResult, *, request_id: str) -> None:
    mark_research_run_succeeded(
        settings.sqlite_path,
        request_id=request_id,
        context_books=result.audit.context_books_loaded,
        subjects_matched=result.audit.subjects_matched,
        citations_count=len(result.postprocess.citations),
    )


def _record_research_run_failed(settings: Settings, *, request_id: str, last_error: str) -> None:
    mark_research_run_failed(
        settings.sqlite_path,
        request_id=request_id,
        last_error=last_error,
    )


def _start_research_worker(
    *,
    registry: JobRegistry,
    dedup_index: ResearchDedupIndex,
    data_root: Path,
    settings: Settings,
    request_id: str,
    payload: dict[str, Any],
    dedup_key: str | None,
    concurrency_limiter: ResearchConcurrencyLimiter,
) -> None:
    def _worker() -> None:
        def reporter(event: dict[str, Any]) -> None:
            registry.emit(request_id, event)

        log_fields: dict[str, str] = {"research_subject": "(unknown)"}
        request_token, _sha_token = bind_log_context(request_id=request_id)
        try:
            registry.emit(
                request_id,
                {"phase": "research", "status": "started"},
            )
            mark_research_run_running(settings.sqlite_path, request_id=request_id)
            request = validate_research_request(payload)
            log_fields = query_log_fields(request.query, request.poh)
            Log(
                INFO_LOG_LEVEL,
                f"research worker started: {log_fields['research_subject']}",
                {"request_id": request_id, **log_fields},
            )
            result = run_research(
                request,
                data_root=data_root,
                settings=settings,
                request_id=request_id,
                reporter=reporter,
            )
            markdown_path = persist_query_markdown(data_root, request_id, result.markdown)
            result.markdown_path = str(markdown_path)
            _record_research_run_succeeded(settings, result, request_id=request_id)
            article_payload = build_article_response(result)
            article_payload["audit"] = {
                "context_books": result.audit.context_books,
                "subjects_matched": result.audit.subjects_matched,
            }
            registry.emit(
                request_id,
                {
                    "phase": "research",
                    "status": "succeeded",
                    "result": article_payload,
                },
            )
            if dedup_key:
                dedup_index.register(dedup_key, request_id)
        except ValueError as exc:
            detail = _validation_error_response(exc)
            _record_research_run_failed(settings, request_id=request_id, last_error=detail["error"])
            registry.emit(
                request_id,
                {
                    "phase": "research",
                    "status": "failed",
                    "message": detail["error"],
                },
            )
        except Exception as exc:
            Log(
                ERROR_LOG_LEVEL,
                f"research worker failed: {log_fields['research_subject']}",
                {"request_id": request_id, "error": str(exc), **log_fields},
            )
            _record_research_run_failed(settings, request_id=request_id, last_error=str(exc))
            registry.emit(
                request_id,
                {
                    "phase": "research",
                    "status": "failed",
                    "message": str(exc),
                },
            )
        finally:
            reset_log_context(request_token, None)
            concurrency_limiter.release()

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"research-{request_id[:8]}",
    ).start()


def build_research_routes(
    *,
    data_root: Path,
    web_dir: Path,
    settings: Settings,
    registry: JobRegistry,
    dedup_index: ResearchDedupIndex,
    batch_registry: ResearchBatchRegistry,
    concurrency_limiter: ResearchConcurrencyLimiter,
    send_json: SendJson,
    send_bytes: SendBytes,
    read_json_body: Callable[[BaseHTTPRequestHandler, int], bytes],
    sse_write: SseWrite,
) -> tuple[
    Callable[[BaseHTTPRequestHandler, str, dict[str, list[str]]], bool],
    Callable[[BaseHTTPRequestHandler, str], bool],
]:
    def _handle_research_status(handler: BaseHTTPRequestHandler, request_id: str) -> None:
        snapshot = registry.get_status(request_id)
        if snapshot is None:
            send_json(handler, 404, {"error": "request not found"})
            return
        events = _tail_events(snapshot.get("events") or [])
        send_json(
            handler,
            200,
            {
                "request_id": request_id,
                "status": snapshot["status"],
                "pipeline_version": snapshot.get("pipeline_version"),
                "last_error": snapshot.get("error"),
                "events": events,
            },
        )

    def _handle_research_article(handler: BaseHTTPRequestHandler, request_id: str) -> None:
        snapshot = registry.get_status(request_id)
        if snapshot is None:
            send_json(handler, 404, {"error": "request not found"})
            return
        status = snapshot.get("status")
        if status != "succeeded":
            send_json(
                handler,
                409,
                {
                    "error": "research job not succeeded",
                    "status": status,
                },
            )
            return
        result = snapshot.get("result")
        if not isinstance(result, dict):
            send_json(handler, 500, {"error": "article payload missing"})
            return
        send_json(handler, 200, result)

    def _handle_research_events(
        handler: BaseHTTPRequestHandler,
        request_id: str,
    ) -> None:
        snapshot = registry.get_status(request_id)
        if snapshot is None:
            send_json(handler, 404, {"error": "request not found"})
            return

        last_seq_raw = handler.headers.get("Last-Event-ID", "-1")
        try:
            last_seq = int(last_seq_raw)
        except ValueError:
            last_seq = -1

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        for ev in registry.subscribe(request_id, last_seq=last_seq):
            event_name = ev.get("status", "progress")
            if not sse_write(handler, event_name, ev):
                break
            if event_name in _RESEARCH_TERMINAL:
                break

    def try_get(handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> bool:
        require_auth = getattr(handler, "_require_auth", None)
        if require_auth is None:
            return False
        if path in ("/ricerca", "/ricerca.html"):
            page = web_dir / "ricerca.html"
            if not page.is_file():
                send_json(handler, 500, {"ok": False, "error": "web/ricerca.html missing"})
                return True
            send_bytes(handler, 200, page.read_bytes(), "text/html; charset=utf-8")
            return True

        if path.startswith("/articolo/") and path.endswith(".html"):
            article_name = path.removeprefix("/articolo/")
            resolved = resolve_article_file(data_root, article_name)
            if resolved is None:
                handler.send_error(404, "Article Not Found")
                return True
            send_bytes(handler, 200, resolved.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/api/research/books":
            if not require_auth(query):
                return True
            send_json(handler, 200, {"ok": True, "books": list_ingested_books(data_root)})
            return True

        if path == "/api/research/status":
            if not require_auth(query):
                return True
            send_json(handler, 200, {"ok": True, **research_status_summary(data_root)})
            return True

        if path == "/api/research/missing":
            if not require_auth(query):
                return True
            book_sha = query.get("book_sha", [None])[0]
            missing = list_missing_articles(data_root, book_sha=book_sha)
            send_json(handler, 200, {"ok": True, "missing": missing, "count": len(missing)})
            return True

        if path == "/api/research/search":
            if not require_auth(query):
                return True
            q = (query.get("q", [""])[0] or "").strip()
            if len(q) < 2:
                send_json(handler, 400, {"ok": False, "error": "query must be at least 2 characters"})
                return True
            results = search_articles(data_root, q)
            send_json(handler, 200, {"ok": True, "query": q, "results": results, "count": len(results)})
            return True

        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[3] == "generate":
            if parts[4] == "status":
                if not require_auth(query):
                    return True
                job_id = query.get("job_id", [""])[0]
                snapshot = batch_registry.get(job_id)
                if snapshot is None:
                    send_json(handler, 404, {"ok": False, "error": "job not found"})
                    return True
                send_json(handler, 200, {"ok": True, **snapshot})
                return True

        if len(parts) == 4 and parts[1] == "api" and parts[2] == "research":
            request_id = parts[3]
            if not require_auth(query):
                return True
            _handle_research_status(handler, request_id)
            return True

        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[4] == "article":
            request_id = parts[3]
            if not require_auth(query):
                return True
            _handle_research_article(handler, request_id)
            return True

        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[4] == "events":
            request_id = parts[3]
            if not require_auth(query):
                return True
            _handle_research_events(handler, request_id)
            return True

        return False

    def try_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
        require_auth = getattr(handler, "_require_auth", None)
        if path == "/api/research/submit":
            if require_auth is None or not require_auth():
                return require_auth is not None
            try:
                body = read_json_body(handler, 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send_json(handler, 400, {"error": f"invalid JSON body: {exc}"})
                return True
            try:
                request = validate_research_request(payload)
            except ValueError as exc:
                send_json(handler, 400, _validation_error_response(exc))
                return True

            index_path = data_root / "polyindex" / "INDEX.json"
            dedup_key: str | None = None
            if request.options.dedup:
                dedup_key = compute_dedup_key(request, index_path=index_path)
                existing = dedup_index.lookup(dedup_key)
                if existing is not None:
                    send_json(
                        handler,
                        202,
                        {"request_id": existing, "status": "accepted", "deduplicated": True},
                    )
                    return True

            if not concurrency_limiter.try_acquire():
                send_json(handler, 429, {"error": "research queue full"})
                return True

            request_id = registry.create_job(
                job_kind="research",
                pipeline_version=RESEARCH_PIPELINE_VERSION,
            )
            _record_research_run_accepted(
                settings,
                request_id=request_id,
                query=request.query,
                poh_id=request.poh.id if request.poh else None,
                poh_label=request.poh.label if request.poh else None,
            )
            _start_research_worker(
                registry=registry,
                dedup_index=dedup_index,
                data_root=data_root,
                settings=settings,
                request_id=request_id,
                payload=payload,
                dedup_key=dedup_key,
                concurrency_limiter=concurrency_limiter,
            )
            send_json(handler, 202, {"request_id": request_id, "status": "accepted"})
            return True

        if path != "/api/research/generate":
            return False
        if require_auth is None or not require_auth():
            return require_auth is not None
        try:
            body = read_json_body(handler, 1024 * 1024)
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return True

        book_sha = payload.get("book_sha")
        poh_ids = payload.get("poh_ids")
        if book_sha is not None and not isinstance(book_sha, str):
            send_json(handler, 400, {"ok": False, "error": "book_sha must be a string"})
            return True
        if poh_ids is not None and (
            not isinstance(poh_ids, list) or not all(isinstance(pid, str) for pid in poh_ids)
        ):
            send_json(handler, 400, {"ok": False, "error": "poh_ids must be a list of strings"})
            return True

        job_id = batch_registry.create(total=0)
        send_json(
            handler,
            202,
            {
                "ok": True,
                "job_id": job_id,
                "total": 0,
                "status_url": f"/api/research/generate/status?job_id={job_id}",
            },
        )

        def _worker() -> None:
            try:
                if poh_ids:
                    targets = [{"poh_id": pid} for pid in poh_ids]
                else:
                    missing = list_missing_articles(
                        data_root,
                        book_sha=book_sha.strip() if isinstance(book_sha, str) and book_sha.strip() else None,
                    )
                    targets = [{"poh_id": item["poh_id"], "label": item["label"]} for item in missing]

                if not targets:
                    batch_registry.set_total(job_id, 0)
                    batch_registry.finish(job_id, "succeeded")
                    return

                batch_registry.set_total(job_id, len(targets))
                target_preview = ", ".join(
                    f"{item.get('label') or item['poh_id']} ({item['poh_id']})"
                    for item in targets[:5]
                )
                if len(targets) > 5:
                    target_preview += f", +{len(targets) - 5} more"
                Log(
                    INFO_LOG_LEVEL,
                    f"research batch started: {len(targets)} article(s)",
                    {
                        "job_id": job_id,
                        "total": len(targets),
                        "target_preview": target_preview,
                    },
                )

                for item in targets:
                    poh_id = str(item["poh_id"])
                    poh_label = str(item.get("label") or poh_id)
                    request_id = registry.create_job(
                        job_kind="research",
                        pipeline_version=RESEARCH_PIPELINE_VERSION,
                    )
                    _record_research_run_accepted(
                        settings,
                        request_id=request_id,
                        query=poh_label,
                        poh_id=poh_id,
                        poh_label=poh_label,
                    )
                    registry.emit(
                        request_id,
                        {"phase": "queue", "status": "waiting"},
                    )
                    concurrency_limiter.acquire()

                    def reporter(event: dict[str, Any], *, rid: str = request_id) -> None:
                        registry.emit(rid, event)

                    request_token, _sha_token = bind_log_context(request_id=request_id)
                    try:
                        registry.emit(request_id, {"phase": "research", "status": "started"})
                        mark_research_run_running(settings.sqlite_path, request_id=request_id)
                        Log(
                            INFO_LOG_LEVEL,
                            f"research batch item started: {poh_label} ({poh_id})",
                            {"job_id": job_id, "request_id": request_id, "poh_id": poh_id, "poh_label": poh_label},
                        )
                        catalog_result, research_result = generate_article_for_poh(
                            data_root,
                            poh_id,
                            settings=settings,
                            request_id=request_id,
                            reporter=reporter,
                            publish_no_material=False,
                        )
                        _record_research_run_succeeded(settings, research_result, request_id=request_id)
                        registry.emit(
                            request_id,
                            {
                                "phase": "research",
                                "status": "succeeded",
                                "result": {"poh_id": poh_id, "url": catalog_result["url"]},
                            },
                        )
                        batch_registry.append_generated(job_id, catalog_result)
                        Log(
                            INFO_LOG_LEVEL,
                            f"research batch item completed: {poh_label} ({poh_id})",
                            {
                                "job_id": job_id,
                                "request_id": request_id,
                                "poh_id": poh_id,
                                "poh_label": poh_label,
                                "url": catalog_result["url"],
                            },
                        )
                    except Exception as exc:
                        _record_research_run_failed(settings, request_id=request_id, last_error=str(exc))
                        registry.emit(
                            request_id,
                            {
                                "phase": "research",
                                "status": "failed",
                                "message": str(exc),
                            },
                        )
                        batch_registry.append_error(
                            job_id,
                            {"poh_id": poh_id, "request_id": request_id, "error": str(exc)},
                        )
                        Log(
                            ERROR_LOG_LEVEL,
                            f"research batch item failed: {poh_label} ({poh_id})",
                            {
                                "job_id": job_id,
                                "request_id": request_id,
                                "poh_id": poh_id,
                                "poh_label": poh_label,
                                "error": str(exc),
                            },
                        )
                    finally:
                        reset_log_context(request_token, None)
                        concurrency_limiter.release()
                snapshot = batch_registry.get(job_id)
                errors = len(snapshot["errors"]) if snapshot else 0
                status = "failed" if errors else "succeeded"
                batch_registry.finish(job_id, status)
            except Exception as exc:
                Log(ERROR_LOG_LEVEL, "research batch worker failed", {"job_id": job_id, "error": str(exc)})
                batch_registry.append_error(job_id, {"error": str(exc)})
                batch_registry.finish(job_id, "failed")

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"research-batch-{job_id[:8]}",
        ).start()
        return True

    return try_get, try_post

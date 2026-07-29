from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from src.api.research_batch_registry import ResearchBatchRegistry
from src.api.research_batch_worker import spawn_research_batch_worker
from src.api.research_merge_article import handle_merge_article_request
from src.core.hashing import new_job_id
from src.search.poh_overlap import list_poh_overlaps
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL, bind_log_context, reset_log_context
from src.models.settings import Settings
from src.persistence.research_runs import (
    create_research_run_accepted,
    mark_research_run_failed,
    mark_research_run_running,
    mark_research_run_succeeded,
)
from src.search.article_health_audit import audit_articles_health
from src.persistence.book_page_preview import PagePreviewError, ensure_page_render_png
from src.persistence.book_pages_audit import audit_book
from src.search.article_catalog import (
    list_ingested_books,
    list_missing_articles,
    research_status_summary,
    resolve_article_file,
    search_poh_catalog,
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

if TYPE_CHECKING:
    from src.api.job_registry import JobRegistry

SendJson = Callable[[BaseHTTPRequestHandler, int, Any], None]
SendBytes = Callable[[BaseHTTPRequestHandler, int, bytes, str], None]
SseWrite = Callable[[BaseHTTPRequestHandler, str, Any], bool]
_RESEARCH_TERMINAL = frozenset({"succeeded", "failed"})
_MAX_EVENTS = 50


def _research_job_stem(*, poh_id: str | None, query: str) -> str:
    if poh_id:
        return poh_id
    return query_log_fields(query, None)["query_hash"][:32]


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
            registry.emit(
                request_id,
                {
                    "phase": "research",
                    "status": "info",
                    "query": request.query,
                    "poh_id": request.poh.id if request.poh else None,
                    "poh_label": request.poh.label if request.poh else None,
                    "message": log_fields.get("research_subject") or request.query,
                },
            )
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
                set_global_total=lambda total: registry.set_global_total(request_id, total),
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
        if path in ("/ricerca", "/ricerca.html"):
            page = web_dir / "ricerca.html"
            if not page.is_file():
                send_json(handler, 500, {"ok": False, "error": "web/ricerca.html missing"})
                return True
            send_bytes(handler, 200, page.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/api/research/book-pages/render":
            source_sha256 = (query.get("source_sha256") or [""])[0].strip()
            aligned_raw = (query.get("aligned_page") or [""])[0].strip()
            if not source_sha256:
                send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
                return True
            try:
                aligned_page = int(aligned_raw)
            except ValueError:
                send_json(handler, 400, {"ok": False, "error": "aligned_page must be an integer"})
                return True
            if aligned_page < 1:
                send_json(handler, 400, {"ok": False, "error": "aligned_page must be positive"})
                return True
            try:
                png_path = ensure_page_render_png(data_root, source_sha256, aligned_page)
            except PagePreviewError as exc:
                send_json(handler, 400, {"ok": False, "error": str(exc)})
                return True
            send_bytes(handler, 200, png_path.read_bytes(), "image/png")
            return True

        if path == "/api/research/books/meta":
            source_sha256 = (query.get("source_sha256") or [""])[0].strip()
            if not source_sha256:
                send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
                return True
            entry = audit_book(data_root, source_sha256)
            if entry is None:
                send_json(handler, 404, {"ok": False, "error": "book not found"})
                return True
            send_json(
                handler,
                200,
                {
                    "ok": True,
                    "source_sha256": entry["source_sha256"],
                    "title": entry["title"],
                    "viewer_pages": entry["viewer_pages"],
                },
            )
            return True

        if path.startswith("/articolo/") and path.endswith(".html"):
            article_name = path.removeprefix("/articolo/")
            mock_scenario = (query.get("mock", [""])[0] or "").strip()
            if mock_scenario:
                mock_path = web_dir / "mockup" / "fixtures" / "dashboard-articolo-mock.html"
                stem = article_name[:-5] if article_name.endswith(".html") else article_name
                specific = web_dir / "mockup" / "fixtures" / f"dashboard-articolo-{stem}.html"
                chosen = specific if specific.is_file() else mock_path
                if chosen.is_file():
                    send_bytes(handler, 200, chosen.read_bytes(), "text/html; charset=utf-8")
                    return True
            resolved = resolve_article_file(data_root, article_name)
            if resolved is None:
                handler.send_error(404, "Article Not Found")
                return True
            send_bytes(handler, 200, resolved.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/api/research/books":
            send_json(handler, 200, {"ok": True, "books": list_ingested_books(data_root)})
            return True

        if path == "/api/research/status":
            send_json(handler, 200, {"ok": True, **research_status_summary(data_root)})
            return True

        if path == "/api/research/missing":
            book_sha = query.get("book_sha", [None])[0]
            missing = list_missing_articles(data_root, book_sha=book_sha)
            send_json(handler, 200, {"ok": True, "missing": missing, "count": len(missing)})
            return True

        if path == "/api/research/articles/audit":
            audit = audit_articles_health(data_root)
            send_json(handler, 200, {"ok": True, **audit})
            return True

        if path == "/api/research/poh-overlaps":
            book_sha = (query.get("book_sha", [""])[0] or "").strip()
            if not book_sha:
                send_json(handler, 400, {"ok": False, "error": "book_sha is required"})
                return True
            overlaps = list_poh_overlaps(data_root, book_sha, settings=settings)
            send_json(handler, 200, {"ok": True, "overlaps": overlaps, "count": len(overlaps)})
            return True

        if path == "/api/research/search":
            q = (query.get("q", [""])[0] or "").strip()
            if len(q) < 2:
                send_json(handler, 400, {"ok": False, "error": "query must be at least 2 characters"})
                return True
            results = search_poh_catalog(data_root, q)
            Log(INFO_LOG_LEVEL, "research catalog search", {"query": q, "count": len(results)})
            send_json(handler, 200, {"ok": True, "query": q, "results": results, "count": len(results)})
            return True

        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[3] == "generate":
            if parts[4] == "status":
                job_id = query.get("job_id", [""])[0]
                snapshot = batch_registry.get(job_id)
                if snapshot is None:
                    send_json(handler, 404, {"ok": False, "error": "job not found"})
                    return True
                send_json(handler, 200, {"ok": True, **snapshot})
                return True

        if len(parts) == 4 and parts[1] == "api" and parts[2] == "research":
            request_id = parts[3]
            _handle_research_status(handler, request_id)
            return True

        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[4] == "article":
            request_id = parts[3]
            _handle_research_article(handler, request_id)
            return True

        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[4] == "events":
            request_id = parts[3]
            _handle_research_events(handler, request_id)
            return True

        return False

    def try_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
        if path == "/api/research/submit":
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
                    Log(
                        INFO_LOG_LEVEL,
                        "research submit deduplicated",
                        {"existing_request_id": existing, "dedup_key": dedup_key},
                    )
                    send_json(
                        handler,
                        202,
                        {
                            "request_id": existing,
                            "status": "accepted",
                            "deduplicated": True,
                            "events_url": f"/api/research/{existing}/events",
                            "status_url": f"/api/research/{existing}",
                            "system_events_url": f"/api/system/jobs/{existing}/events",
                            "system_status_url": f"/api/system/jobs/{existing}",
                        },
                    )
                    return True

            if not concurrency_limiter.try_acquire():
                Log(WARNING_LOG_LEVEL, "research submit queue full", {"query": request.query})
                send_json(handler, 429, {"error": "research queue full"})
                return True

            request_id, _started_at = new_job_id(
                _research_job_stem(
                    poh_id=request.poh.id if request.poh else None,
                    query=request.query,
                )
            )
            registry.create_job(
                job_id=request_id,
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
            Log(
                INFO_LOG_LEVEL,
                "research submit accepted",
                {
                    "request_id": request_id,
                    "query": request.query,
                    "poh_id": request.poh.id if request.poh else None,
                    "dedup": bool(dedup_key),
                },
            )
            send_json(handler, 202, {
                "request_id": request_id,
                "status": "accepted",
                "events_url": f"/api/research/{request_id}/events",
                "status_url": f"/api/research/{request_id}",
                "system_events_url": f"/api/system/jobs/{request_id}/events",
                "system_status_url": f"/api/system/jobs/{request_id}",
            })
            return True

        if path == "/api/research/merge-article":
            try:
                body = read_json_body(handler, 8 * 1024 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return True
            request_id = registry.create_job(job_kind="research-merge")
            try:
                result = handle_merge_article_request(
                    data_root, settings, payload, request_id=request_id
                )
                send_json(handler, 200, result)
            except ValueError as exc:
                send_json(handler, 400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                Log(ERROR_LOG_LEVEL, "research merge-article failed", {"error": str(exc), "request_id": request_id})
                send_json(handler, 500, {"ok": False, "error": str(exc)})
            return True

        if path == "/api/research/generate/resume":
            try:
                body = read_json_body(handler, 64 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return True
            job_id = str(payload.get("job_id") or "").strip()
            if not job_id:
                send_json(handler, 400, {"ok": False, "error": "job_id is required"})
                return True
            if not batch_registry.resume(job_id):
                send_json(handler, 404, {"ok": False, "error": "interrupted batch not found"})
                return True
            send_json(
                handler,
                202,
                {
                    "ok": True,
                    "job_id": job_id,
                    "resumed": True,
                    "status_url": f"/api/research/generate/status?job_id={job_id}",
                },
            )
            spawn_research_batch_worker(
                job_id,
                data_root=data_root,
                settings=settings,
                registry=registry,
                batch_registry=batch_registry,
                concurrency_limiter=concurrency_limiter,
                record_accepted=_record_research_run_accepted,
                record_succeeded=_record_research_run_succeeded,
                record_failed=_record_research_run_failed,
                research_job_stem=_research_job_stem,
                resume=True,
            )
            return True

        if path == "/api/research/generate/abort":
            try:
                body = read_json_body(handler, 64 * 1024)
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
                return True
            job_id = str(payload.get("job_id") or "").strip()
            if not job_id:
                send_json(handler, 400, {"ok": False, "error": "job_id is required"})
                return True
            if not batch_registry.abort(job_id):
                send_json(handler, 404, {"ok": False, "error": "interrupted batch not found"})
                return True
            send_json(handler, 200, {"ok": True, "job_id": job_id, "status": "aborted"})
            return True

        if path != "/api/research/generate":
            return False
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

        job_id = batch_registry.create(
            total=0,
            book_sha=book_sha.strip() if isinstance(book_sha, str) and book_sha.strip() else None,
            poh_ids=poh_ids,
        )
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
        spawn_research_batch_worker(
            job_id,
            data_root=data_root,
            settings=settings,
            registry=registry,
            batch_registry=batch_registry,
            concurrency_limiter=concurrency_limiter,
            record_accepted=_record_research_run_accepted,
            record_succeeded=_record_research_run_succeeded,
            record_failed=_record_research_run_failed,
            research_job_stem=_research_job_stem,
            resume=False,
            book_sha=book_sha.strip() if isinstance(book_sha, str) and book_sha.strip() else None,
            poh_ids=poh_ids,
        )
        return True

    return try_get, try_post

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_display import job_display_label, job_display_status
from src.api.job_registry import JobRegistry
from src.api.research_batch_registry import ResearchBatchRegistry
from src.core.hashing import new_job_id
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.request import IngestInputValidationException
from src.models.settings import Settings
from src.persistence.book_page_repair import (
    PageRepairError,
    repair_global_step_count,
    run_book_gaps_repair,
)
from src.persistence.book_pages_audit import audit_book
from src.persistence.pipeline_runs import get_pipeline_run_by_request_id, list_pipeline_runs
from src.persistence.research_runs import list_research_runs

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]


def _parse_date_prefix(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        return None


def _matches_date(started_at: str | None, date_prefix: str | None) -> bool:
    if not date_prefix:
        return True
    if not started_at:
        return False
    return started_at.startswith(date_prefix)


def _historical_display_status(status: str, finished_at: Any) -> str:
    if status in ("done", "succeeded", "completed"):
        return "completato"
    if status in ("error", "failed"):
        return "errore"
    if not finished_at and status in ("running", "accepted", "queued"):
        return "interrotto"
    return job_display_status(status)


def _history_row_from_pipeline(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    finished_at = row.get("finished_at")
    display = _historical_display_status(status, finished_at)
    book_title = row.get("book_title")
    sha = str(row.get("source_sha256") or "")
    return {
        "job_id": row.get("request_id"),
        "job_kind": "ingest",
        "status": status,
        "display_status": display,
        "display_status_label": job_display_label(display),
        "book_title": book_title,
        "source_sha256": sha or None,
        "title": f"Ingest: {book_title}" if book_title else "Ingestione libro",
        "subtitle": f"{sha[:16]}…" if sha else None,
        "created_at": row.get("started_at"),
        "updated_at": row.get("finished_at") or row.get("started_at"),
        "error": row.get("last_error"),
        "is_active": False,
        "is_batch": False,
        "is_historical": True,
    }


def _history_row_from_research(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    finished_at = row.get("finished_at")
    display = _historical_display_status(status, finished_at)
    poh_id = row.get("poh_id")
    preview = str(row.get("query_preview") or "").strip()
    title = f"Articolo: {poh_id}" if poh_id else f"Research: {preview or 'articolo'}"
    return {
        "job_id": row.get("request_id"),
        "job_kind": "research",
        "status": status,
        "display_status": display,
        "display_status_label": job_display_label(display),
        "book_title": None,
        "poh_id": poh_id,
        "query": preview or None,
        "title": title,
        "subtitle": preview if poh_id and preview and poh_id != preview else None,
        "created_at": row.get("started_at"),
        "updated_at": row.get("finished_at") or row.get("started_at"),
        "error": row.get("last_error"),
        "is_active": False,
        "is_batch": False,
        "is_historical": True,
    }


def _live_row(summary: dict[str, Any]) -> dict[str, Any]:
    status = str(summary.get("status") or "")
    events = summary.get("events")
    display = job_display_status(status, events if isinstance(events, list) else None)
    row = dict(summary)
    row["display_status"] = display
    row["display_status_label"] = job_display_label(display)
    row["is_historical"] = False
    row["is_batch"] = summary.get("job_kind") == "research_batch"
    return row


def list_job_history(
    *,
    sqlite_path: str,
    registry: JobRegistry,
    batch_registry: ResearchBatchRegistry,
    book: str = "",
    job_id: str = "",
    date: str = "",
    limit: int = 200,
    include_active: bool = False,
) -> list[dict[str, Any]]:
    book_filter = book.strip().lower()
    id_filter = job_id.strip().lower()
    date_prefix = _parse_date_prefix(date)
    cap = max(1, min(limit, 500))

    by_id: dict[str, dict[str, Any]] = {}

    for row in list_pipeline_runs(sqlite_path, limit=cap * 2):
        item = _history_row_from_pipeline(row)
        by_id[str(item["job_id"])] = item

    for row in list_research_runs(sqlite_path, limit=cap * 2):
        item = _history_row_from_research(row)
        by_id[str(item["job_id"])] = item

    if include_active:
        for state_summary in registry.list_jobs(include_finished=True, limit=cap):
            job_id_value = str(state_summary.get("job_id") or "")
            if not job_id_value:
                continue
            by_id[job_id_value] = _live_row(state_summary)

    rows = list(by_id.values())
    if book_filter:
        rows = [
            row
            for row in rows
            if book_filter in str(row.get("book_title") or "").lower()
            or book_filter in str(row.get("title") or "").lower()
            or book_filter in str(row.get("poh_id") or "").lower()
            or book_filter in str(row.get("query") or "").lower()
        ]
    if id_filter:
        rows = [
            row
            for row in rows
            if id_filter in str(row.get("job_id") or "").lower()
            or id_filter in str(row.get("source_sha256") or "").lower()
        ]
    if date_prefix:
        rows = [
            row
            for row in rows
            if _matches_date(str(row.get("created_at") or ""), date_prefix)
        ]

    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    rows.sort(key=lambda item: 0 if item.get("is_active") else 1)
    return rows[:cap]


def list_active_jobs_with_batches(
    *,
    registry: JobRegistry,
    batch_registry: ResearchBatchRegistry,
    limit: int = 50,
    include_finished: bool = True,
) -> list[dict[str, Any]]:
    from src.api.job_display import enrich_batch_summary

    jobs = registry.list_jobs(include_finished=include_finished, limit=limit)
    batches = batch_registry.list_jobs(include_finished=include_finished, limit=limit)
    by_id = {str(job["job_id"]): job for job in jobs}
    enriched_batches: list[dict[str, Any]] = []
    for batch in batches:
        child_ids = batch.get("request_ids") or []
        children = [by_id[str(child_id)] for child_id in child_ids if str(child_id) in by_id]
        enriched_batches.append(enrich_batch_summary(batch, children))
    all_jobs = jobs + enriched_batches
    all_jobs.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    all_jobs.sort(key=lambda item: 0 if item.get("is_active") else 1)
    return all_jobs[:limit]


def try_handle_job_retry_post(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    job_semaphore: threading.Semaphore,
    max_concurrent_jobs: int,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    if path != "/api/system/jobs/retry":
        return False
    try:
        body = read_body(handler, 64 * 1024)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
        return True
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        send_json(handler, 400, {"ok": False, "error": "job_id is required"})
        return True
    run = get_pipeline_run_by_request_id(settings.sqlite_path, job_id)
    if run is None:
        send_json(handler, 404, {"ok": False, "error": "ingest job not found"})
        return True
    if str(run.get("status") or "") not in {"failed", "error"}:
        send_json(handler, 409, {"ok": False, "error": "only failed ingest jobs can be retried"})
        return True
    sha = str(run.get("source_sha256") or "").strip().lower()
    if not sha:
        send_json(handler, 409, {"ok": False, "error": "failed job has no source_sha256"})
        return True
    audit = audit_book(data_root, sha)
    gap_pages = audit.get("missing_pages") if isinstance(audit, dict) else None
    use_gaps = isinstance(gap_pages, list) and len(gap_pages) > 0
    new_id, _started_at = new_job_id(f"{sha[:16]}_retry")
    registry.create_job(job_id=new_id)
    status_url = f"/api/ingest/{new_id}/status"
    events_url = f"/api/ingest/{new_id}/events"

    def _worker() -> None:
        def reporter(ev: dict[str, Any]) -> None:
            registry.emit(new_id, ev)

        acquired = job_semaphore.acquire(blocking=False)
        if not acquired:
            registry.emit(
                new_id,
                make_event(
                    "queue",
                    "progress",
                    message="waiting for a free ingest slot",
                    max_concurrent_jobs=max_concurrent_jobs,
                ),
            )
            job_semaphore.acquire()
        try:
            if use_gaps:
                aligned_count = len({entry["aligned"] for entry in gap_pages})
                registry.set_global_total(
                    new_id,
                    repair_global_step_count(aligned_count, pipeline_mode="classic"),
                )
                registry.emit(
                    new_id,
                    make_event(
                        "gaps_repair",
                        STATUS_STARTED,
                        source_sha256=sha,
                        message=f"Rilancio: riparazione {aligned_count} pagine",
                        retry_of=job_id,
                    ),
                )
                result = run_book_gaps_repair(
                    data_root,
                    settings,
                    sha,
                    gap_pages,
                    request_id=new_id,
                    progress=reporter,
                    pipeline_mode="classic",
                )
                registry.emit(new_id, make_event("gaps_repair", STATUS_DONE, result=result))
            else:
                from src.api.ingest_pipeline_runner import run_resume_pipeline_from_sha

                registry.emit(
                    new_id,
                    make_event(
                        "pipeline",
                        STATUS_STARTED,
                        source_sha256=sha,
                        message="Rilancio ingest dallo stato disponibile",
                        retry_of=job_id,
                    ),
                )
                result = run_resume_pipeline_from_sha(
                    data_root,
                    settings,
                    sha,
                    reporter,
                    lambda total: registry.set_global_total(new_id, total),
                    request_id=new_id,
                )
                registry.emit(new_id, make_event("pipeline", STATUS_DONE, result=result))
        except PageRepairError as exc:
            registry.emit(new_id, make_event("pipeline", STATUS_ERROR, message=str(exc)))
        except IngestInputValidationException as exc:
            registry.emit(
                new_id,
                make_event(
                    "pipeline",
                    STATUS_ERROR,
                    message=exc.detail.message,
                    code=exc.detail.code.value,
                    field=exc.detail.field,
                ),
            )
        except Exception as exc:
            Log(
                ERROR_LOG_LEVEL,
                "ingest job retry worker error",
                {"job_id": new_id, "retry_of": job_id, "error": str(exc)},
            )
            registry.emit(new_id, make_event("pipeline", STATUS_ERROR, message=str(exc)))
        finally:
            job_semaphore.release()

    threading.Thread(target=_worker, daemon=True, name=f"retry-{new_id[:8]}").start()
    Log(
        INFO_LOG_LEVEL,
        "ingest job retry started",
        {
            "job_id": new_id,
            "retry_of": job_id,
            "source_sha256": sha[:16],
            "mode": "gaps_repair" if use_gaps else "resume",
        },
    )
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": new_id,
            "retry_of": job_id,
            "mode": "gaps_repair" if use_gaps else "resume",
            "status_url": status_url,
            "events_url": events_url,
        },
    )
    return True

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.api.job_control import (
    pipeline_run_can_resume,
    try_handle_job_cancel_post,
    try_handle_job_resume_post,
    try_handle_job_retry_post,
    try_handle_job_terminate_post,
)
from src.api.job_display import job_display_label, job_display_status
from src.api.job_registry import JobRegistry
from src.api.research_batch_registry import ResearchBatchRegistry
from src.persistence.pipeline_runs import list_pipeline_runs
from src.persistence.research_runs import list_research_runs

_INTERRUPTED_PIPELINE_STATUSES = frozenset({"running", "accepted", "queued"})

__all__ = [
    "list_active_jobs_with_batches",
    "list_job_history",
    "try_handle_job_cancel_post",
    "try_handle_job_resume_post",
    "try_handle_job_retry_post",
    "try_handle_job_terminate_post",
]


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


def _timing_from_bounds(started_at: Any, finished_at: Any) -> dict[str, float] | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    total_seconds = round((end - start).total_seconds(), 2)
    if total_seconds < 0:
        return None
    return {"total_seconds": total_seconds}


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _book_audit_complete(data_root: Path | None, source_sha256: str) -> bool | None:
    sha = str(source_sha256 or "").strip().lower()
    if not sha or data_root is None:
        return None
    try:
        from src.persistence.book_pages_audit import audit_book

        audit = audit_book(data_root, sha)
    except Exception:
        return None
    if not isinstance(audit, dict):
        return None
    return bool(audit.get("complete"))


def _historical_display_status(
    status: str,
    finished_at: Any,
    *,
    last_error: str | None = None,
    failed_pages: int | None = None,
    succeeded_pages: int | None = None,
    total_pages: int | None = None,
) -> str:
    if status in ("error", "failed"):
        return "errore"
    if status == "aborted":
        return "annullato"
    if not finished_at and status in _INTERRUPTED_PIPELINE_STATUSES:
        return "interrotto"
    if status in ("done", "succeeded", "completed"):
        if str(last_error or "").strip():
            return "errore"
        failed = int(failed_pages or 0)
        if failed > 0:
            return "errore"
        total = int(total_pages or 0)
        succeeded = int(succeeded_pages or 0)
        if total > 0 and succeeded < total:
            return "errore"
        return "completato"
    return job_display_status(status)


def _history_error_message(
    row: dict[str, Any],
    *,
    display_status: str,
) -> str | None:
    last_error = row.get("last_error")
    if last_error:
        return str(last_error)
    if display_status != "errore":
        return None
    failed_pages = int(row.get("failed_pages") or 0)
    if failed_pages > 0:
        return f"{failed_pages} pagine fallite"
    total_pages = int(row.get("total_pages") or 0)
    succeeded_pages = int(row.get("succeeded_pages") or 0)
    if total_pages > 0 and succeeded_pages < total_pages:
        return f"Completate {succeeded_pages} su {total_pages} pagine"
    return None


def _history_row_from_pipeline(
    row: dict[str, Any],
    *,
    data_root: Path | None = None,
    prior_failed_error: str | None = None,
    attempt_number: int | None = None,
    attempt_total: int | None = None,
) -> dict[str, Any]:
    status = str(row.get("status") or "")
    finished_at = row.get("finished_at")
    display = _historical_display_status(
        status,
        finished_at,
        last_error=row.get("last_error"),
        failed_pages=row.get("failed_pages"),
        succeeded_pages=row.get("succeeded_pages"),
        total_pages=row.get("total_pages"),
    )
    sha = str(row.get("source_sha256") or "")
    book_complete = _book_audit_complete(data_root, sha)
    if display == "completato" and book_complete is False:
        display = "errore"
    elif (
        display == "completato"
        and prior_failed_error
        and status in ("done", "succeeded", "completed")
    ):
        display = "recuperato"
    book_title = row.get("book_title")
    interrupted = display == "interrotto"
    resumable = interrupted or pipeline_run_can_resume(row, data_root)
    timing = row.get("timing") if isinstance(row.get("timing"), dict) else None
    if timing is None:
        timing = _timing_from_bounds(row.get("started_at"), finished_at)
    elif "total_seconds" not in timing:
        fallback = _timing_from_bounds(row.get("started_at"), finished_at)
        if fallback is not None:
            timing = {**timing, **fallback}
    status_note = None
    if prior_failed_error and display in ("recuperato", "completato"):
        status_note = "Dopo errore: " + str(prior_failed_error).strip()
    elif book_complete is False:
        status_note = "Libro ancora incompleto in biblioteca"
    error = _history_error_message(row, display_status=display)
    if not error and display == "errore" and book_complete is False:
        error = status_note
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
        "error": error,
        "status_note": status_note,
        "attempt_number": attempt_number,
        "attempt_total": attempt_total,
        "book_complete": book_complete,
        "timing": timing,
        "is_active": False,
        "is_batch": False,
        "is_historical": True,
        "resumable": resumable,
        "terminable": interrupted,
    }


def _history_row_from_research(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    finished_at = row.get("finished_at")
    display = _historical_display_status(
        status,
        finished_at,
        last_error=row.get("last_error"),
    )
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
        "error": _history_error_message(row, display_status=display),
        "timing": _timing_from_bounds(row.get("started_at"), finished_at),
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


def _should_skip_superseded_running_row(
    row: dict[str, Any],
    *,
    latest_finished_at: datetime | None,
) -> bool:
    if latest_finished_at is None:
        return False
    if row.get("finished_at"):
        return False
    if str(row.get("status") or "") not in _INTERRUPTED_PIPELINE_STATUSES:
        return False
    started = _parse_iso_datetime(row.get("started_at"))
    return started is not None and started <= latest_finished_at


def _pipeline_rows_with_book_context(
    rows: list[dict[str, Any]],
    *,
    data_root: Path | None,
) -> list[dict[str, Any]]:
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sha = str(row.get("source_sha256") or "").strip().lower()
        if not sha:
            continue
        by_sha.setdefault(sha, []).append(row)

    latest_finished_at: dict[str, datetime] = {}
    for sha, group in by_sha.items():
        best: datetime | None = None
        for row in group:
            when = _parse_iso_datetime(row.get("finished_at") or row.get("started_at"))
            if when is None:
                continue
            if best is None or when > best:
                best = when
        if best is not None:
            latest_finished_at[sha] = best

    enriched: list[dict[str, Any]] = []
    visible_by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sha = str(row.get("source_sha256") or "").strip().lower()
        if _should_skip_superseded_running_row(row, latest_finished_at=latest_finished_at.get(sha)):
            continue
        if sha:
            visible_by_sha.setdefault(sha, []).append(row)

    visible_attempt_no: dict[str, int] = {}
    visible_attempt_total: dict[str, int] = {}
    for sha, group in visible_by_sha.items():
        ordered = sorted(group, key=lambda item: str(item.get("started_at") or ""))
        visible_attempt_total[sha] = len(ordered)
        for index, candidate in enumerate(ordered, start=1):
            visible_attempt_no[str(candidate.get("request_id") or "")] = index

    for row in rows:
        sha = str(row.get("source_sha256") or "").strip().lower()
        if _should_skip_superseded_running_row(row, latest_finished_at=latest_finished_at.get(sha)):
            continue
        group = by_sha.get(sha, [])
        ordered = sorted(
            group,
            key=lambda item: str(item.get("started_at") or ""),
        )
        request_id = str(row.get("request_id") or "")
        attempt_number = visible_attempt_no.get(request_id)
        attempt_total = visible_attempt_total.get(sha) if sha else None

        prior_failed_error = None
        finished_at = _parse_iso_datetime(row.get("finished_at") or row.get("started_at"))
        if finished_at is not None:
            latest_prior: datetime | None = None
            for other in ordered:
                other_id = str(other.get("request_id") or "")
                if other_id == str(row.get("request_id") or ""):
                    continue
                if str(other.get("status") or "") not in ("failed", "error"):
                    continue
                other_when = _parse_iso_datetime(other.get("finished_at") or other.get("started_at"))
                if other_when is None or other_when >= finished_at:
                    continue
                if latest_prior is None or other_when > latest_prior:
                    latest_prior = other_when
                    prior_failed_error = str(other.get("last_error") or "tentativo fallito")

        enriched.append(
            _history_row_from_pipeline(
                row,
                data_root=data_root,
                prior_failed_error=prior_failed_error,
                attempt_number=attempt_number,
                attempt_total=attempt_total,
            )
        )
    return enriched


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
    data_root: Path | None = None,
) -> list[dict[str, Any]]:
    book_filter = book.strip().lower()
    id_filter = job_id.strip().lower()
    date_prefix = _parse_date_prefix(date)
    cap = max(1, min(limit, 500))
    root = data_root if data_root is not None else Path(sqlite_path).resolve().parent

    by_id: dict[str, dict[str, Any]] = {}

    pipeline_rows = list_pipeline_runs(sqlite_path, limit=cap * 2)
    for item in _pipeline_rows_with_book_context(pipeline_rows, data_root=root):
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

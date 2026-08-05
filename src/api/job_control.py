from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.core.hashing import new_job_id
from src.core.errors import ShutdownRequested
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.request import IngestInputValidationException
from src.models.settings import Settings
from src.persistence.book_page_repair import (
    PageRepairError,
    build_repair_progress_baseline,
    resolve_resume_pipeline_mode,
    run_book_gaps_repair,
)
from src.persistence.book_pages_audit import audit_book
from src.persistence.pipeline_runs import (
    create_pipeline_run,
    get_pipeline_run_by_request_id,
    mark_pipeline_run_finished,
)

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]

_INTERRUPTED_PIPELINE_STATUSES = frozenset({"running", "accepted", "queued"})
_FAILED_PIPELINE_STATUSES = frozenset({"failed", "error"})


def pipeline_run_is_interrupted(run: dict[str, Any]) -> bool:
    status = str(run.get("status") or "")
    return not run.get("finished_at") and status in _INTERRUPTED_PIPELINE_STATUSES


def _book_has_missing_pages(data_root: Path, sha: str) -> bool:
    try:
        audit = audit_book(data_root, sha)
    except Exception:
        return False
    gaps = audit.get("missing_pages") if isinstance(audit, dict) else None
    return isinstance(gaps, list) and len(gaps) > 0


def pipeline_run_can_resume(run: dict[str, Any], data_root: Path | None = None) -> bool:
    if pipeline_run_is_interrupted(run):
        return True
    if str(run.get("status") or "") != "aborted":
        return False
    sha = str(run.get("source_sha256") or "").strip().lower()
    if not sha or data_root is None:
        return False
    return True


def _start_ingest_continue_job(
    *,
    job_id: str,
    sha: str,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    job_semaphore: threading.Semaphore,
    max_concurrent_jobs: int,
    action_label: str,
) -> tuple[str, str, str, str]:
    audit = audit_book(data_root, sha)
    gap_pages = audit.get("missing_pages") if isinstance(audit, dict) else None
    use_gaps = isinstance(gap_pages, list) and len(gap_pages) > 0
    new_id, _started_at = new_job_id(f"{sha[:16]}_{action_label}")
    registry.create_job(job_id=new_id, job_kind="repair")
    status_url = f"/api/ingest/{new_id}/status"
    events_url = f"/api/ingest/{new_id}/events"
    mode = "gaps_repair" if use_gaps else "resume"
    aligned_count = len({entry["aligned"] for entry in gap_pages}) if use_gaps else 0
    if use_gaps:
        create_pipeline_run(
            settings.sqlite_path,
            request_id=new_id,
            source_sha256=sha,
            pipeline_version="gaps_repair",
            total_pages=aligned_count,
        )

    def _worker() -> None:
        def reporter(ev: dict[str, Any]) -> None:
            payload = dict(ev)
            # Baseline already counts pages present on disk; skips must not double-count.
            if payload.get("status") == "page_skipped":
                payload["counts_as_step"] = False
            registry.emit(new_id, payload)

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
        finish_status: str | None = None
        finish_error: str | None = None
        succeeded_pages = 0
        try:
            pipeline_mode = resolve_resume_pipeline_mode(settings)
            if use_gaps:
                baseline = build_repair_progress_baseline(audit, pipeline_mode=pipeline_mode)
                registry.set_global_progress(
                    new_id,
                    step=int(baseline["done_steps"]),
                    total=int(baseline["total_steps"]),
                )
                for baseline_ev in baseline["events"]:
                    registry.emit(new_id, dict(baseline_ev))
                registry.emit(
                    new_id,
                    make_event(
                        "gaps_repair",
                        STATUS_STARTED,
                        source_sha256=sha,
                        message=f"{action_label}: riparazione {aligned_count} pagine",
                        retry_of=job_id,
                        pipeline_mode=pipeline_mode,
                        page_total=int(baseline["expected_page_count"]),
                    ),
                )
                result = run_book_gaps_repair(
                    data_root,
                    settings,
                    sha,
                    gap_pages,
                    request_id=new_id,
                    progress=reporter,
                    pipeline_mode=pipeline_mode,
                )
                registry.emit(new_id, make_event("gaps_repair", STATUS_DONE, result=result))
                written = result.get("aligned_pages_written") if isinstance(result, dict) else None
                succeeded_pages = len(written) if isinstance(written, list) else aligned_count
                finish_status = "succeeded"
            else:
                from src.api.ingest_pipeline_runner import run_resume_pipeline_from_sha

                registry.emit(
                    new_id,
                    make_event(
                        "pipeline",
                        STATUS_STARTED,
                        source_sha256=sha,
                        message=f"{action_label}: ripresa dallo stato disponibile",
                        retry_of=job_id,
                        pipeline_mode=pipeline_mode,
                    ),
                )
                result = run_resume_pipeline_from_sha(
                    data_root,
                    settings,
                    sha,
                    reporter,
                    lambda total: registry.set_global_total(new_id, total),
                    request_id=new_id,
                    pipeline_mode=pipeline_mode,
                )
                registry.emit(new_id, make_event("pipeline", STATUS_DONE, result=result))
        except PageRepairError as exc:
            finish_status = "failed"
            finish_error = str(exc)
            registry.emit(new_id, make_event("pipeline", STATUS_ERROR, message=str(exc)))
        except IngestInputValidationException as exc:
            finish_status = "failed"
            finish_error = exc.detail.message
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
        except ShutdownRequested:
            Log(
                INFO_LOG_LEVEL,
                "ingest continue interrupted by shutdown",
                {"job_id": new_id, "retry_of": job_id},
            )
        except Exception as exc:
            finish_status = "failed"
            finish_error = str(exc)
            Log(
                ERROR_LOG_LEVEL,
                "ingest job continue worker error",
                {"job_id": new_id, "retry_of": job_id, "error": str(exc)},
            )
            registry.emit(new_id, make_event("pipeline", STATUS_ERROR, message=str(exc)))
        finally:
            if use_gaps and finish_status is not None:
                mark_pipeline_run_finished(
                    settings.sqlite_path,
                    request_id=new_id,
                    status=finish_status,
                    succeeded_pages=succeeded_pages,
                    failed_pages=0 if finish_status == "succeeded" else aligned_count,
                    last_error=finish_error,
                )
            job_semaphore.release()

    threading.Thread(target=_worker, daemon=True, name=f"{action_label}-{new_id[:8]}").start()
    Log(
        INFO_LOG_LEVEL,
        "ingest job continue started",
        {
            "job_id": new_id,
            "retry_of": job_id,
            "source_sha256": sha[:16],
            "mode": mode,
            "action": action_label,
        },
    )
    return new_id, mode, status_url, events_url


def _read_job_id_payload(
    handler: BaseHTTPRequestHandler,
    *,
    send_json: SendJson,
    read_body: ReadBody,
) -> str | None:
    try:
        body = read_body(handler, 64 * 1024)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
        return None
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        send_json(handler, 400, {"ok": False, "error": "job_id is required"})
        return None
    return job_id


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
    job_id = _read_job_id_payload(handler, send_json=send_json, read_body=read_body)
    if job_id is None:
        return True
    run = get_pipeline_run_by_request_id(settings.sqlite_path, job_id)
    if run is None:
        send_json(handler, 404, {"ok": False, "error": "ingest job not found"})
        return True
    if str(run.get("status") or "") not in _FAILED_PIPELINE_STATUSES:
        send_json(handler, 409, {"ok": False, "error": "only failed ingest jobs can be retried"})
        return True
    sha = str(run.get("source_sha256") or "").strip().lower()
    if not sha:
        send_json(handler, 409, {"ok": False, "error": "failed job has no source_sha256"})
        return True
    new_id, mode, status_url, events_url = _start_ingest_continue_job(
        job_id=job_id,
        sha=sha,
        data_root=data_root,
        settings=settings,
        registry=registry,
        job_semaphore=job_semaphore,
        max_concurrent_jobs=max_concurrent_jobs,
        action_label="retry",
    )
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": new_id,
            "retry_of": job_id,
            "mode": mode,
            "status_url": status_url,
            "events_url": events_url,
        },
    )
    return True


def try_handle_job_resume_post(
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
    if path != "/api/system/jobs/resume":
        return False
    job_id = _read_job_id_payload(handler, send_json=send_json, read_body=read_body)
    if job_id is None:
        return True
    run = get_pipeline_run_by_request_id(settings.sqlite_path, job_id)
    if run is None:
        send_json(handler, 404, {"ok": False, "error": "ingest job not found"})
        return True
    if not pipeline_run_can_resume(run, data_root):
        send_json(
            handler,
            409,
            {"ok": False, "error": "only interrupted or incomplete aborted ingest jobs can be resumed"},
        )
        return True
    sha = str(run.get("source_sha256") or "").strip().lower()
    if not sha:
        send_json(handler, 409, {"ok": False, "error": "interrupted job has no source_sha256"})
        return True
    new_id, mode, status_url, events_url = _start_ingest_continue_job(
        job_id=job_id,
        sha=sha,
        data_root=data_root,
        settings=settings,
        registry=registry,
        job_semaphore=job_semaphore,
        max_concurrent_jobs=max_concurrent_jobs,
        action_label="resume",
    )
    mark_pipeline_run_finished(
        settings.sqlite_path,
        request_id=job_id,
        status="aborted",
        succeeded_pages=int(run.get("succeeded_pages") or 0),
        failed_pages=int(run.get("failed_pages") or 0),
        last_error=f"resumed as {new_id}",
    )
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": new_id,
            "resumed_from": job_id,
            "mode": mode,
            "status_url": status_url,
            "events_url": events_url,
        },
    )
    return True


def try_handle_job_terminate_post(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    settings: Settings,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    if path != "/api/system/jobs/terminate":
        return False
    job_id = _read_job_id_payload(handler, send_json=send_json, read_body=read_body)
    if job_id is None:
        return True
    run = get_pipeline_run_by_request_id(settings.sqlite_path, job_id)
    if run is None:
        send_json(handler, 404, {"ok": False, "error": "ingest job not found"})
        return True
    if not pipeline_run_is_interrupted(run):
        send_json(handler, 409, {"ok": False, "error": "only interrupted ingest jobs can be terminated"})
        return True
    mark_pipeline_run_finished(
        settings.sqlite_path,
        request_id=job_id,
        status="aborted",
        succeeded_pages=int(run.get("succeeded_pages") or 0),
        failed_pages=int(run.get("failed_pages") or 0),
        last_error="terminated by user",
    )
    Log(INFO_LOG_LEVEL, "ingest job terminated", {"job_id": job_id})
    send_json(handler, 200, {"ok": True, "job_id": job_id, "status": "aborted"})
    return True

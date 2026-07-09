from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.ingestion.polyindex.subject_embeddings_backfill import (
    embedding_backfill_status,
    run_subject_embedding_backfill,
)
from src.ingestion.progress import STATUS_ERROR, make_event
from src.models.settings import Settings

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]


def try_handle_admin_embeddings_get(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    send_json: SendJson
) -> bool:
    if path != "/api/admin/embeddings/status":
        return False
    if not require_auth():
        return True
    status = embedding_backfill_status(data_root / "polyindex", settings)
    send_json(
        handler,
        200,
        {
            "ok": True,
            "model": status.model,
            "total_subjects": status.total_subjects,
            "embedded_count": status.embedded_count,
            "missing_count": status.missing_count,
        },
    )
    return True


def try_handle_admin_embeddings_post(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    job_semaphore: threading.Semaphore,
    send_json: SendJson
) -> bool:
    if path != "/api/admin/embeddings/generate":
        return False
    status = embedding_backfill_status(data_root / "polyindex", settings)
    if status.missing_count == 0:
        send_json(
            handler,
            200,
            {
                "ok": True,
                "skipped": True,
                "model": status.model,
                "missing_count": 0,
            },
        )
        return True

    job_id = registry.create_job()
    status_url = f"/api/ingest/{job_id}/status"
    events_url = f"/api/ingest/{job_id}/events"

    def _worker() -> None:
        def reporter(ev: dict[str, Any]) -> None:
            registry.emit(job_id, ev)

        acquired = job_semaphore.acquire(blocking=False)
        if not acquired:
            registry.emit(
                job_id,
                make_event(
                    "queue",
                    "progress",
                    message="waiting for a free job slot",
                ),
            )
            job_semaphore.acquire()
        try:
            registry.set_global_total(job_id, status.missing_count)
            result = run_subject_embedding_backfill(
                data_root / "polyindex",
                settings,
                request_id=job_id,
                progress=reporter,
            )
            Log(
                INFO_LOG_LEVEL,
                "admin embeddings backfill job completed",
                {"job_id": job_id, **result},
            )
        except Exception as exc:
            Log(
                ERROR_LOG_LEVEL,
                "admin embeddings backfill worker error",
                {"job_id": job_id, "error": str(exc)},
            )
            registry.emit(
                job_id,
                make_event(
                    "subject_embeddings",
                    STATUS_ERROR,
                    message=str(exc),
                ),
            )
        finally:
            job_semaphore.release()

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"embeddings-{job_id[:8]}",
    ).start()
    Log(
        INFO_LOG_LEVEL,
        "admin embeddings backfill job started",
        {
            "job_id": job_id,
            "missing_count": status.missing_count,
            "model": status.model,
        },
    )
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": job_id,
            "status_url": status_url,
            "events_url": events_url,
            "missing_count": status.missing_count,
            "model": status.model,
        },
    )
    return True

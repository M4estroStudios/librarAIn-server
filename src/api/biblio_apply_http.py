from __future__ import annotations

import json
import threading
from pathlib import Path

from src.api.biblio_apply import BiblioApplyError, run_biblio_apply, validate_apply_notes
from src.core.hashing import new_job_id
from src.core.openai_client import use_compute_mode
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.settings import Settings, normalize_compute_mode
from src.persistence.polyindex_conflicts import delete_conflict_item, list_conflict_items


def try_handle_biblio_apply_get(
    route: str,
    handler,
    *,
    data_root: Path,
    send_json,
    query: dict[str, list[str]],
) -> bool:
    if route != "/api/admin/biblio/conflicts":
        return False
    sha = (query.get("source_sha256") or [""])[0].strip()
    items = list_conflict_items(data_root, source_sha256=sha or None)
    send_json(
        handler,
        200,
        {
            "ok": True,
            "source_sha256": sha or None,
            "count": len(items),
            "items": items,
        },
    )
    return True


def try_handle_biblio_apply_post(
    path: str,
    handler,
    *,
    data_root: Path,
    settings: Settings,
    send_json,
    read_body,
    registry=None,
    job_semaphore=None,
) -> bool:
    if path == "/api/admin/biblio/conflicts/delete":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            result = delete_conflict_item(data_root, str(payload.get("id") or ""))
            send_json(handler, 200 if result.get("ok") else 404, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    if path != "/api/admin/biblio/apply-changes":
        return False
    if registry is None or job_semaphore is None:
        send_json(handler, 500, {"ok": False, "error": "job registry unavailable"})
        return True
    try:
        payload = json.loads(read_body(handler, 8 * 1024 * 1024).decode("utf-8"))
        source_sha256 = str(payload.get("source_sha256") or "").strip()
        pipeline_notes = str(payload.get("pipeline_notes") or "")
        pages = payload.get("pages") if isinstance(payload.get("pages"), list) else []
        if not source_sha256:
            send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
            return True
        if not pages:
            send_json(handler, 400, {"ok": False, "error": "pages required"})
            return True
        validate_apply_notes(pipeline_notes, [page for page in pages if isinstance(page, dict)])
        compute_mode = normalize_compute_mode(payload.get("compute_mode"))
    except BiblioApplyError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if compute_mode == "cloud":
        missing_cloud = settings.missing_cloud_config(job_kind="biblio")
        if missing_cloud:
            send_json(
                handler,
                400,
                {
                    "ok": False,
                    "error": "cloud compute requires: " + ", ".join(missing_cloud),
                    "field": "compute_mode",
                },
            )
            return True

    job_id, _ = new_job_id(f"{source_sha256[:16]}_biblio_apply")
    registry.create_job(job_id=job_id, job_kind="biblio", compute_mode=compute_mode)

    def _worker() -> None:
        acquired = job_semaphore.acquire(blocking=False)
        if not acquired:
            registry.emit(
                job_id,
                make_event("queue", "progress", message="waiting for a free ingest slot"),
            )
            job_semaphore.acquire()
        try:
            registry.emit(
                job_id,
                make_event(
                    "biblio_apply",
                    STATUS_STARTED,
                    source_sha256=source_sha256,
                    message="biblio apply job started",
                    compute_mode=compute_mode,
                ),
            )
            with use_compute_mode(compute_mode, settings):
                job_settings = settings.for_compute_mode(compute_mode)
                result = run_biblio_apply(
                    data_root,
                    job_settings,
                    source_sha256=source_sha256,
                    pipeline_notes=pipeline_notes,
                    pages=pages,
                )
            registry.emit(
                job_id,
                make_event(
                    "biblio_apply",
                    STATUS_DONE,
                    source_sha256=source_sha256,
                    result=result,
                ),
            )
        except Exception as exc:
            registry.emit(
                job_id,
                make_event(
                    "biblio_apply",
                    STATUS_ERROR,
                    source_sha256=source_sha256,
                    message=str(exc),
                ),
            )
        finally:
            job_semaphore.release()

    threading.Thread(target=_worker, daemon=True, name=f"apply-{job_id[:8]}").start()
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": job_id,
            "stage": "biblio_apply",
            "compute_mode": compute_mode,
            "status_url": f"/api/ingest/{job_id}/status",
            "events_url": f"/api/ingest/{job_id}/events",
        },
    )
    return True

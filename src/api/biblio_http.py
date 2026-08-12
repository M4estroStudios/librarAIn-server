from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.api.biblio_artifacts import (
    list_biblio_candidates,
    read_book_artifact,
    update_manifest_reicat,
)
from src.api.biblio_handlers import (
    biblio_graph,
    discard_review_item,
    list_biblio_review_queue,
    resolve_review_item,
    run_biblio_only_job,
    search_biblio,
    update_biblio_node,
)
from src.api.biblio_polyindex_jobs import try_handle_polyindex_preflight_get, try_handle_polyindex_run_post
from src.core.hashing import new_job_id
from src.core.openai_client import use_compute_mode
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.request import PageRange
from src.models.settings import Settings, normalize_compute_mode


def try_handle_biblio_get(
    path: str,
    handler,
    *,
    data_root: Path,
    send_json,
    query: dict[str, list[str]] | None = None,
) -> bool:
    parsed = urlparse(path if "://" in path else f"http://x{path}")
    route = parsed.path
    if query is None:
        query = parse_qs(parsed.query)

    if route == "/api/admin/biblio/candidates":
        send_json(handler, 200, list_biblio_candidates(data_root))
        return True
    if try_handle_polyindex_preflight_get(
        path,
        handler,
        data_root=data_root,
        send_json=send_json,
        query=query,
    ):
        return True
    if route == "/api/admin/biblio/deprecated":
        from src.persistence.polyindex_deprecated import list_deprecated_items

        sha = (query.get("source_sha256") or [""])[0].strip()
        items = list_deprecated_items(data_root, source_sha256=sha or None)
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
    from src.api.biblio_apply_http import try_handle_biblio_apply_get

    if try_handle_biblio_apply_get(route, handler, data_root=data_root, send_json=send_json, query=query):
        return True
    if route == "/api/admin/biblio/artifact":
        send_json(
            handler,
            200,
            read_book_artifact(
                data_root,
                (query.get("source_sha256") or [""])[0],
                (query.get("kind") or [""])[0],
            ),
        )
        return True
    if route == "/api/admin/biblio/search":
        mode = (query.get("mode") or ["cita"])[0].strip() or "cita"
        payload = search_biblio(
            data_root,
            authors=(query.get("authors") or [""])[0],
            title=(query.get("title") or [""])[0],
            year=(query.get("year") or [""])[0],
            entry_id=(query.get("id") or [""])[0],
            mode=mode,
        )
        send_json(handler, 200, payload)
        return True
    if route == "/api/admin/biblio/review":
        send_json(handler, 200, list_biblio_review_queue(data_root))
        return True
    if route == "/api/admin/biblio/graph":
        send_json(handler, 200, biblio_graph(data_root))
        return True
    return False


def _parse_year(year_raw: object) -> int | None:
    if isinstance(year_raw, int):
        return year_raw
    if isinstance(year_raw, str) and year_raw.strip().isdigit():
        return int(year_raw.strip())
    return None


def try_handle_biblio_post(
    path: str,
    handler,
    *,
    data_root: Path,
    settings: Settings,
    registry,
    job_semaphore,
    send_json,
    read_body,
) -> bool:
    if path == "/api/admin/biblio/deprecated/delete":
        try:
            from src.persistence.polyindex_deprecated import delete_deprecated_item

            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            result = delete_deprecated_item(data_root, str(payload.get("id") or ""))
            send_json(handler, 200 if result.get("ok") else 404, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    from src.api.biblio_apply_http import try_handle_biblio_apply_post

    if try_handle_biblio_apply_post(
        path,
        handler,
        data_root=data_root,
        settings=settings,
        registry=registry,
        job_semaphore=job_semaphore,
        send_json=send_json,
        read_body=read_body,
    ):
        return True

    if path == "/api/admin/biblio/indices/refresh":
        try:
            from src.ingestion.polyindex.page_index_refresh import refresh_polyindex_for_pages

            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            pages_raw = payload.get("aligned_pages") or []
            if not isinstance(pages_raw, list):
                send_json(handler, 400, {"ok": False, "error": "aligned_pages must be a list"})
                return True
            aligned_pages = [int(page) for page in pages_raw if int(page) > 0]
            compute_mode = normalize_compute_mode(payload.get("compute_mode"))
            with use_compute_mode(compute_mode, settings):
                job_settings = settings.for_compute_mode(compute_mode)
                result = refresh_polyindex_for_pages(
                    data_root,
                    job_settings,
                    str(payload.get("source_sha256") or ""),
                    aligned_pages,
                    prompt_notes=str(payload["prompt_notes"])
                    if isinstance(payload.get("prompt_notes"), str)
                    else None,
                )
            send_json(handler, 200 if result.get("ok") else 400, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if path == "/api/admin/biblio/review/discard":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            result = discard_review_item(
                data_root,
                source_sha256=str(payload.get("source_sha256") or ""),
                aligned_page=int(payload.get("aligned_page")),
                line=payload.get("line") if isinstance(payload.get("line"), int) else None,
                raw=payload.get("raw") if isinstance(payload.get("raw"), str) else None,
            )
            send_json(handler, 200, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if path == "/api/admin/biblio/review/resolve":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            result = resolve_review_item(
                data_root,
                source_sha256=str(payload.get("source_sha256") or ""),
                aligned_page=int(payload.get("aligned_page")),
                line=payload.get("line") if isinstance(payload.get("line"), int) else None,
                raw=payload.get("raw") if isinstance(payload.get("raw"), str) else None,
                authors=str(payload.get("authors") or "unknown"),
                title=str(payload.get("title") or "unknown"),
                year=_parse_year(payload.get("year")),
                extras=payload.get("extras") if isinstance(payload.get("extras"), dict) else None,
                link_to_id=str(payload["link_to_id"]) if payload.get("link_to_id") else None,
            )
            send_json(handler, 200, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if path == "/api/admin/biblio/node/update":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            result = update_biblio_node(
                data_root,
                node_id=str(payload.get("id") or ""),
                authors=str(payload.get("authors") or ""),
                title=str(payload.get("title") or ""),
                year=_parse_year(payload.get("year")),
                extras=payload.get("extras") if isinstance(payload.get("extras"), dict) else None,
            )
            send_json(handler, 200, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if path == "/api/admin/biblio/reicat/update":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            reicat_raw = payload.get("reicat")
            if not isinstance(reicat_raw, dict):
                send_json(handler, 400, {"ok": False, "error": "reicat object is required"})
                return True
            result = update_manifest_reicat(
                data_root,
                str(payload.get("source_sha256") or ""),
                reicat_raw,
            )
            send_json(handler, 200 if result.get("ok") else 400, result)
        except Exception as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    if path == "/api/admin/biblio/run":
        try:
            payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
            source_sha256 = str(payload.get("source_sha256") or "").strip()
            range_raw = payload.get("biblio_range") or {}
            if not source_sha256:
                send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
                return True
            biblio_range = PageRange(
                start=int(range_raw.get("start")),
                end=int(range_raw.get("end")),
            )
            compute_mode = normalize_compute_mode(payload.get("compute_mode"))
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

        job_id, _ = new_job_id(f"{source_sha256[:16]}_biblio")
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
                        "polyindex_biblio",
                        STATUS_STARTED,
                        source_sha256=source_sha256,
                        message="Biblio-only job started",
                        compute_mode=compute_mode,
                    ),
                )
                with use_compute_mode(compute_mode, settings):
                    job_settings = settings.for_compute_mode(compute_mode)
                    result = run_biblio_only_job(
                        data_root,
                        job_settings,
                        source_sha256,
                        biblio_range,
                        request_id=job_id,
                    )
                registry.emit(
                    job_id,
                    make_event(
                        "polyindex_biblio",
                        STATUS_DONE,
                        source_sha256=source_sha256,
                        result=result,
                    ),
                )
            except Exception as exc:
                registry.emit(
                    job_id,
                    make_event(
                        "polyindex_biblio",
                        STATUS_ERROR,
                        source_sha256=source_sha256,
                        message=str(exc),
                    ),
                )
            finally:
                job_semaphore.release()

        threading.Thread(target=_worker, daemon=True, name=f"biblio-{job_id[:8]}").start()
        send_json(
            handler,
            202,
            {
                "ok": True,
                "job_id": job_id,
                "compute_mode": compute_mode,
                "status_url": f"/api/ingest/{job_id}/status",
                "events_url": f"/api/ingest/{job_id}/events",
            },
        )
        return True

    if try_handle_polyindex_run_post(
        path,
        handler,
        data_root=data_root,
        settings=settings,
        registry=registry,
        job_semaphore=job_semaphore,
        send_json=send_json,
        read_body=read_body,
    ):
        return True

    return False

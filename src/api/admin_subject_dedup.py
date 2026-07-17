from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.ingestion.polyindex.index_json import SubjectDeleteError, delete_polyindex_subject
from src.ingestion.polyindex.subject_dedup_suggest import (
    dismiss_cluster,
    dismiss_pairs,
    emit_dedup_error,
    list_open_suggestions,
    run_subject_dedup_scan,
)
from src.ingestion.progress import make_event
from src.models.settings import Settings

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]


def try_handle_admin_subject_dedup_get(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    send_json: SendJson,
) -> bool:
    if path != "/api/admin/subjects/dedup/suggestions":
        return False
    payload = list_open_suggestions(data_root / "polyindex")
    send_json(handler, 200, {"ok": True, **payload})
    return True


def try_handle_admin_subject_dedup_post(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    job_semaphore: threading.Semaphore,
    send_json: SendJson,
    read_body: ReadBody,
    sqlite_path: str,
) -> bool:
    if path == "/api/admin/subjects/dedup/scan":
        return _handle_scan(
            handler,
            data_root=data_root,
            settings=settings,
            registry=registry,
            job_semaphore=job_semaphore,
            send_json=send_json,
            read_body=read_body,
        )
    if path == "/api/admin/subjects/dedup/dismiss":
        return _handle_dismiss(
            handler,
            data_root=data_root,
            send_json=send_json,
            read_body=read_body,
        )
    if path == "/api/admin/subject/delete":
        return _handle_delete(
            handler,
            data_root=data_root,
            send_json=send_json,
            read_body=read_body,
            sqlite_path=sqlite_path,
        )
    return False


def _parse_json_body(
    handler: BaseHTTPRequestHandler,
    read_body: ReadBody,
    send_json: SendJson,
) -> dict[str, Any] | None:
    try:
        body = read_body(handler, 1024 * 1024)
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, OSError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
        return None
    if not isinstance(payload, dict):
        send_json(handler, 400, {"ok": False, "error": "JSON body must be an object"})
        return None
    return payload


def _handle_scan(
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    job_semaphore: threading.Semaphore,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    payload = _parse_json_body(handler, read_body, send_json)
    if payload is None:
        return True

    min_similarity = payload.get("min_similarity")
    if min_similarity is not None:
        try:
            min_similarity = float(min_similarity)
        except (TypeError, ValueError):
            send_json(handler, 400, {"ok": False, "error": "min_similarity must be a number"})
            return True
        if not 0.0 <= min_similarity <= 1.0:
            send_json(handler, 400, {"ok": False, "error": "min_similarity must be between 0 and 1"})
            return True

    use_llm = payload.get("use_llm")
    if use_llm is not None and not isinstance(use_llm, bool):
        send_json(handler, 400, {"ok": False, "error": "use_llm must be a boolean"})
        return True

    limit = payload.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            send_json(handler, 400, {"ok": False, "error": "limit must be an integer"})
            return True
        if limit < 1:
            send_json(handler, 400, {"ok": False, "error": "limit must be >= 1"})
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
                make_event("queue", "progress", message="waiting for a free job slot"),
            )
            job_semaphore.acquire()
        try:
            result = run_subject_dedup_scan(
                data_root / "polyindex",
                settings,
                data_root=data_root,
                request_id=job_id,
                min_similarity=min_similarity,
                use_llm=use_llm,
                limit=limit,
                progress=reporter,
            )
            Log(
                INFO_LOG_LEVEL,
                "admin subject dedup scan completed",
                {
                    "job_id": job_id,
                    "clusters": len(result.get("clusters") or []),
                },
            )
        except Exception as exc:
            Log(
                ERROR_LOG_LEVEL,
                "admin subject dedup scan failed",
                {"job_id": job_id, "error": str(exc)},
            )
            emit_dedup_error(reporter, str(exc))
        finally:
            job_semaphore.release()

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"dedup-{job_id[:8]}",
    ).start()
    Log(INFO_LOG_LEVEL, "admin subject dedup scan started", {"job_id": job_id})
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": job_id,
            "status_url": status_url,
            "events_url": events_url,
        },
    )
    return True


def _handle_dismiss(
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    payload = _parse_json_body(handler, read_body, send_json)
    if payload is None:
        return True

    polyindex_dir = data_root / "polyindex"
    dismissed: list[str] = []

    pair_ids = payload.get("pair_ids")
    if pair_ids is not None:
        if not isinstance(pair_ids, list) or not all(isinstance(item, str) for item in pair_ids):
            send_json(handler, 400, {"ok": False, "error": "pair_ids must be a list of strings"})
            return True
        dismissed.extend(dismiss_pairs(polyindex_dir, pair_ids))

    member_ids = payload.get("member_ids")
    if member_ids is not None:
        if not isinstance(member_ids, list) or not all(isinstance(item, str) for item in member_ids):
            send_json(handler, 400, {"ok": False, "error": "member_ids must be a list of strings"})
            return True
        dismissed.extend(dismiss_cluster(polyindex_dir, member_ids))

    cluster_key = payload.get("cluster_key")
    if cluster_key is not None:
        if not isinstance(cluster_key, str) or not cluster_key.strip():
            send_json(handler, 400, {"ok": False, "error": "cluster_key must be a non-empty string"})
            return True
        members = [part for part in cluster_key.split("|") if part]
        dismissed.extend(dismiss_cluster(polyindex_dir, members))

    if not dismissed and pair_ids is None and member_ids is None and cluster_key is None:
        send_json(
            handler,
            400,
            {"ok": False, "error": "provide pair_ids, member_ids, or cluster_key"},
        )
        return True

    unique = list(dict.fromkeys(dismissed))
    send_json(
        handler,
        200,
        {
            "ok": True,
            "dismissed_pairs": unique,
            "suggestions": list_open_suggestions(polyindex_dir),
        },
    )
    return True


def _handle_delete(
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    send_json: SendJson,
    read_body: ReadBody,
    sqlite_path: str,
) -> bool:
    payload = _parse_json_body(handler, read_body, send_json)
    if payload is None:
        return True
    canonical_id = payload.get("canonical_id")
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        send_json(handler, 400, {"ok": False, "error": "canonical_id is required"})
        return True
    try:
        result = delete_polyindex_subject(
            data_root / "polyindex",
            canonical_id.strip(),
            data_root=data_root,
            sqlite_path=sqlite_path,
        )
    except SubjectDeleteError as exc:
        send_json(handler, 404, {"ok": False, "error": str(exc)})
        return True
    Log(INFO_LOG_LEVEL, "admin subject delete done", {"canonical_id": canonical_id})
    send_json(handler, 200, {"ok": True, "result": result})
    return True

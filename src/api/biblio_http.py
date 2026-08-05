from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.api.biblio_handlers import (
    biblio_graph,
    discard_review_item,
    list_biblio_review_queue,
    resolve_review_item,
    run_biblio_only_job,
    search_biblio,
    update_biblio_node,
)
from src.core.hashing import new_job_id
from src.core.openai_client import use_compute_mode
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.models.request import PageRange
from src.models.settings import Settings, normalize_compute_mode


def list_biblio_candidates(data_root: Path) -> dict[str, Any]:
    from src.persistence.book_pages_audit import audit_all_books

    report = audit_all_books(data_root)
    books_out: list[dict[str, Any]] = []
    for book in report.get("books") or []:
        if not isinstance(book, dict):
            continue
        sha = str(book.get("source_sha256") or "")
        if not sha:
            continue
        stages = book.get("stages") if isinstance(book.get("stages"), dict) else {}
        output_stage = stages.get("output") if isinstance(stages.get("output"), dict) else {}
        output_present = int(output_stage.get("present_count") or 0)
        if output_present < 1 and not book.get("complete"):
            continue
        manifest_path = data_root / "output" / sha / "manifest.json"
        biblio_path = data_root / "output" / sha / "BIBLIO.json"
        biblio_range = None
        authors = None
        year = None
        original_page_count = None
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = {}
            if isinstance(manifest, dict):
                opc = manifest.get("original_page_count")
                if isinstance(opc, int) and opc > 0:
                    original_page_count = opc
                raw_range = manifest.get("biblio_range")
                if isinstance(raw_range, dict):
                    start = raw_range.get("start")
                    end = raw_range.get("end")
                    if isinstance(start, int) and isinstance(end, int):
                        biblio_range = {"start": start, "end": end}
                reicat = manifest.get("reicat")
                if isinstance(reicat, dict):
                    autores = reicat.get("autore") or reicat.get("authors")
                    if isinstance(autores, list):
                        authors = ", ".join(str(a) for a in autores if str(a).strip())
                    year = reicat.get("anno_di_pubblicazione") or reicat.get("publication_year")
        entry_count = 0
        if biblio_path.is_file():
            try:
                local = json.loads(biblio_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                local = {}
            if isinstance(local, dict) and isinstance(local.get("entries"), list):
                entry_count = len(local["entries"])
                if biblio_range is None and isinstance(local.get("biblio_range_original"), dict):
                    raw_range = local["biblio_range_original"]
                    start = raw_range.get("start")
                    end = raw_range.get("end")
                    if isinstance(start, int) and isinstance(end, int):
                        biblio_range = {"start": start, "end": end}
        books_out.append(
            {
                "source_sha256": sha,
                "title": book.get("title") or sha[:16],
                "slug": book.get("slug"),
                "authors": authors,
                "year": year,
                "expected_page_count": book.get("expected_page_count"),
                "original_page_count": original_page_count,
                "complete": bool(book.get("complete")),
                "output_pages": output_present,
                "eligible": output_present > 0,
                "has_biblio": biblio_path.is_file(),
                "biblio_entry_count": entry_count,
                "biblio_range": biblio_range,
            }
        )
    books_out.sort(key=lambda item: str(item.get("title") or "").casefold())
    return {"ok": True, "count": len(books_out), "books": books_out}


def try_handle_biblio_get(path: str, handler, *, data_root: Path, send_json) -> bool:
    parsed = urlparse(path if "://" in path else f"http://x{path}")
    route = parsed.path
    query = parse_qs(parsed.query)

    if route == "/api/admin/biblio/candidates":
        send_json(handler, 200, list_biblio_candidates(data_root))
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

    return False

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import openai

from src.api.biblio_handlers import (
    BiblioJobError,
    _book_output_from_disk,
    _load_reicat_from_manifest,
    _run_async,
    run_biblio_only_job,
)
from src.core.hashing import new_job_id, validate_source_sha256
from src.core.log import INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client, use_compute_mode
from src.ingestion.book_md_builder import build_book_md
from src.ingestion.index_builder import build_index_md
from src.ingestion.index_cross_links import apply_index_cross_links
from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.ingestion.polyindex.index_json import sync_polyindex_index_from_book
from src.ingestion.polyindex.time_index import sync_time_index_from_book_async
from src.ingestion.polyindex.toc_json import sync_polyindex_toc_from_book
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_STARTED, make_event
from src.ingestion.toc_builder import build_toc_md
from src.ingestion.toc_index_refine import refine_index_md, refine_toc_md
from src.models.request import PageRange, UsefulPagesEnumeration
from src.models.settings import Settings, normalize_compute_mode
from src.persistence.book_page_exclude import load_book_exclusions

POLYINDEX_RERUN_STAGES = frozenset(
    {
        "polyindex_toc",
        "polyindex_index",
        "time_index",
        "polyindex_biblio",
    }
)

STAGE_JOB_KIND = {
    "polyindex_toc": "biblio",
    "polyindex_index": "ingest",
    "time_index": "biblio",
    "polyindex_biblio": "biblio",
}


def _aligned_range_from_manifest(
    manifest: dict[str, Any],
    key: str,
    o2a: dict[int, int],
) -> PageRange:
    aligned_raw = manifest.get(f"{key}_aligned")
    if isinstance(aligned_raw, dict):
        start = aligned_raw.get("start")
        end = aligned_raw.get("end")
        if isinstance(start, int) and isinstance(end, int) and start >= 1 and end >= start:
            return PageRange(start=start, end=end)
    raw = manifest.get(key)
    if isinstance(raw, dict):
        start = raw.get("start")
        end = raw.get("end")
        if isinstance(start, int) and isinstance(end, int):
            try:
                return PageRange(start=o2a[start], end=o2a[end])
            except KeyError:
                pass
    return PageRange(start=1, end=1)


def _load_book_context(data_root: Path, source_sha256: str) -> dict[str, Any]:
    sha = validate_source_sha256(source_sha256)
    book_output = _book_output_from_disk(data_root, sha)
    manifest = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise BiblioJobError("manifest must be an object")
    original_page_count = int(manifest.get("original_page_count") or 0)
    if original_page_count < 1:
        raise BiblioJobError("invalid original_page_count in manifest")
    _excluded, pages_to_remove = load_book_exclusions(data_root, sha, manifest=manifest)
    aligned_total, o2a, a2o = build_page_removal_mapping(original_page_count, pages_to_remove)
    useful = UsefulPagesEnumeration(
        source_sha256=sha,
        original_page_count=original_page_count,
        aligned_page_count=aligned_total,
        useful_original_pages=sorted(o2a.keys()),
        original_page_to_aligned_page=o2a,
        aligned_page_to_original_page=a2o,
        toc_range_aligned=_aligned_range_from_manifest(manifest, "toc_range", o2a),
        index_range_aligned=_aligned_range_from_manifest(manifest, "index_range", o2a),
        biblio_range_aligned=_aligned_range_from_manifest(manifest, "biblio_range", o2a),
    )
    return {
        "sha": sha,
        "book_output": book_output,
        "manifest": manifest,
        "useful": useful,
        "reicat": _load_reicat_from_manifest(manifest),
    }


def _refine_cache_dir(data_root: Path, source_sha256: str) -> Path:
    return Path(data_root) / "tmp" / source_sha256 / "polyindexRefine"


async def _regenerate_toc_md(
    ctx: dict[str, Any],
    *,
    data_root: Path,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str,
    prompt_notes: str | None,
) -> Path:
    toc_md = build_toc_md(ctx["book_output"], ctx["useful"])
    return await refine_toc_md(
        toc_md,
        client,
        settings,
        source_sha256=ctx["sha"],
        request_id=request_id,
        cache_dir=_refine_cache_dir(data_root, ctx["sha"]),
        prompt_notes=prompt_notes,
    )


async def _regenerate_index_md(
    ctx: dict[str, Any],
    *,
    data_root: Path,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str,
    prompt_notes: str | None,
) -> Path:
    index_md = build_index_md(ctx["book_output"], ctx["useful"])
    index_md = await refine_index_md(
        index_md,
        client,
        settings,
        source_sha256=ctx["sha"],
        request_id=request_id,
        cache_dir=_refine_cache_dir(data_root, ctx["sha"]),
        prompt_notes=prompt_notes,
    )
    await apply_index_cross_links(
        index_md,
        ctx["book_output"],
        ctx["useful"],
        client=client,
        settings=settings,
        request_id=request_id,
    )
    build_book_md(ctx["book_output"], ctx["useful"])
    return index_md


def run_polyindex_toc_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    ctx = _load_book_context(data_root, source_sha256)
    openai_client = client or build_openai_client(settings)
    rid = request_id or ctx["sha"]
    toc_md = _run_async(
        _regenerate_toc_md(
            ctx,
            data_root=data_root,
            client=openai_client,
            settings=settings,
            request_id=rid,
            prompt_notes=prompt_notes,
        )
    )
    path = sync_polyindex_toc_from_book(
        data_root / "polyindex",
        ctx["sha"],
        ctx["book_output"],
        toc_md,
        ctx["useful"],
    )
    Log(
        INFO_LOG_LEVEL,
        "polyindex toc-only job completed",
        {
            "source_sha256": ctx["sha"][:16],
            "toc_md_path": str(toc_md),
            "path": str(path),
        },
    )
    return {
        "ok": True,
        "stage": "polyindex_toc",
        "source_sha256": ctx["sha"],
        "toc_md_path": str(toc_md),
        "toc_json_path": str(path),
    }


def run_polyindex_index_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    ctx = _load_book_context(data_root, source_sha256)
    openai_client = client or build_openai_client(settings)
    rid = request_id or ctx["sha"]
    index_md = _run_async(
        _regenerate_index_md(
            ctx,
            data_root=data_root,
            client=openai_client,
            settings=settings,
            request_id=rid,
            prompt_notes=prompt_notes,
        )
    )
    reicat = ctx["manifest"].get("reicat") if isinstance(ctx["manifest"].get("reicat"), dict) else {}
    book_title = str(reicat.get("title") or reicat.get("titolo") or "") or None
    path, stats = sync_polyindex_index_from_book(
        data_root / "polyindex",
        ctx["sha"],
        index_md,
        ctx["useful"],
        openai_client,
        settings.sqlite_path,
        settings,
        rid,
        prompt_notes=prompt_notes,
        book_title=book_title,
        book_slug=ctx["book_output"].slug,
    )
    Log(INFO_LOG_LEVEL, "polyindex index-only job completed", {"source_sha256": ctx["sha"][:16], **stats})
    return {
        "ok": True,
        "stage": "polyindex_index",
        "source_sha256": ctx["sha"],
        "index_md_path": str(index_md),
        "index_json_path": str(path),
        **stats,
    }


def run_polyindex_time_index_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    ctx = _load_book_context(data_root, source_sha256)
    openai_client = client or build_openai_client(settings)
    title = None
    reicat = ctx["manifest"].get("reicat")
    if isinstance(reicat, dict):
        raw_title = reicat.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()
    path, stats = _run_async(
        sync_time_index_from_book_async(
            data_root / "polyindex",
            ctx["sha"],
            ctx["book_output"],
            book_title=title,
            request_id=request_id or ctx["sha"],
            client=openai_client,
            settings=settings,
            prompt_notes=prompt_notes,
        )
    )
    Log(INFO_LOG_LEVEL, "polyindex time-index-only job completed", {"source_sha256": ctx["sha"][:16], **stats})
    return {
        "ok": True,
        "stage": "time_index",
        "source_sha256": ctx["sha"],
        "time_index_path": str(path),
        **stats,
    }


def run_polyindex_stage_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    stage: str,
    *,
    biblio_range: PageRange | None = None,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    stage_key = (stage or "").strip()
    if stage_key not in POLYINDEX_RERUN_STAGES:
        raise BiblioJobError(
            "stage must be one of: " + ", ".join(sorted(POLYINDEX_RERUN_STAGES))
        )
    if stage_key == "polyindex_toc":
        return run_polyindex_toc_job(
            data_root,
            settings,
            source_sha256,
            client=client,
            request_id=request_id,
            prompt_notes=prompt_notes,
        )
    if stage_key == "polyindex_index":
        return run_polyindex_index_job(
            data_root,
            settings,
            source_sha256,
            client=client,
            request_id=request_id,
            prompt_notes=prompt_notes,
        )
    if stage_key == "time_index":
        return run_polyindex_time_index_job(
            data_root,
            settings,
            source_sha256,
            client=client,
            request_id=request_id,
            prompt_notes=prompt_notes,
        )
    if biblio_range is None:
        raise BiblioJobError("biblio_range is required for polyindex_biblio")
    result = run_biblio_only_job(
        data_root,
        settings,
        source_sha256,
        biblio_range,
        client=client,
        request_id=request_id,
        prompt_notes=prompt_notes,
    )
    result["stage"] = "polyindex_biblio"
    return result


def try_handle_polyindex_run_post(
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
    if path != "/api/admin/biblio/polyindex/run":
        return False
    try:
        payload = json.loads(read_body(handler, 1024 * 1024).decode("utf-8"))
        source_sha256 = str(payload.get("source_sha256") or "").strip()
        stage = str(payload.get("stage") or "").strip()
        if not source_sha256:
            send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
            return True
        if stage not in POLYINDEX_RERUN_STAGES:
            send_json(
                handler,
                400,
                {
                    "ok": False,
                    "error": "stage must be one of: " + ", ".join(sorted(POLYINDEX_RERUN_STAGES)),
                },
            )
            return True
        biblio_range = None
        if stage == "polyindex_biblio":
            range_raw = payload.get("biblio_range") or {}
            biblio_range = PageRange(
                start=int(range_raw.get("start")),
                end=int(range_raw.get("end")),
            )
        compute_mode = normalize_compute_mode(payload.get("compute_mode"))
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True

    job_kind = STAGE_JOB_KIND.get(stage, "biblio")
    if compute_mode == "cloud":
        missing_cloud = settings.missing_cloud_config(job_kind=job_kind)
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

    job_id, _ = new_job_id(f"{source_sha256[:16]}_{stage}")
    registry.create_job(job_id=job_id, job_kind=job_kind, compute_mode=compute_mode)

    def _poly_worker() -> None:
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
                    stage,
                    STATUS_STARTED,
                    source_sha256=source_sha256,
                    message=f"{stage} job started",
                    compute_mode=compute_mode,
                ),
            )
            with use_compute_mode(compute_mode, settings):
                job_settings = settings.for_compute_mode(compute_mode)
                result = run_polyindex_stage_job(
                    data_root,
                    job_settings,
                    source_sha256,
                    stage,
                    biblio_range=biblio_range,
                    request_id=job_id,
                )
            registry.emit(
                job_id,
                make_event(
                    stage,
                    STATUS_DONE,
                    source_sha256=source_sha256,
                    result=result,
                ),
            )
        except Exception as exc:
            registry.emit(
                job_id,
                make_event(
                    stage,
                    STATUS_ERROR,
                    source_sha256=source_sha256,
                    message=str(exc),
                ),
            )
        finally:
            job_semaphore.release()

    threading.Thread(target=_poly_worker, daemon=True, name=f"poly-{job_id[:8]}").start()
    send_json(
        handler,
        202,
        {
            "ok": True,
            "job_id": job_id,
            "stage": stage,
            "compute_mode": compute_mode,
            "status_url": f"/api/ingest/{job_id}/status",
            "events_url": f"/api/ingest/{job_id}/events",
        },
    )
    return True

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
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client, use_compute_mode
from src.ingestion.book_md_builder import build_book_md
from src.ingestion.index_builder import build_index_md
from src.ingestion.index_cross_links import apply_index_cross_links, book_index_json_path
from src.ingestion.output_writer import BookOutput, load_book_index_document, stamp_page_metadata
from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.ingestion.polyindex.index_json import sync_polyindex_index_from_book
from src.ingestion.polyindex.index_md_parser import parse_index_md
from src.ingestion.polyindex.time_index import sync_time_index_from_book_async
from src.ingestion.polyindex.toc_json import parse_chapters_from_toc_md, sync_polyindex_toc_from_book
from src.ingestion.progress import (
    PHASE_POLYINDEX_INDEX,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.ingestion.toc_builder import build_toc_md
from src.ingestion.toc_index_refine import refine_index_md, refine_toc_md
from src.models.request import PageRange, UsefulPagesEnumeration
from src.models.settings import Settings, normalize_compute_mode
from src.persistence.book_page_exclude import load_book_exclusions
from src.persistence.biblio_stage_runs import (
    create_biblio_stage_run,
    mark_biblio_stage_run_done,
    mark_biblio_stage_run_failed,
)

POLYINDEX_RERUN_STAGES = frozenset(
    {
        "polyindex_toc",
        "polyindex_index",
        "library_index",
        "time_index",
        "polyindex_biblio",
    }
)

STAGE_JOB_KIND = {
    "polyindex_toc": "biblio",
    "polyindex_index": "biblio",
    "library_index": "biblio",
    "time_index": "biblio",
    "polyindex_biblio": "biblio",
}


def _estimate_index_cross_link_steps(
    index_md_path: Path,
    book_output: BookOutput,
    useful: UsefulPagesEnumeration,
    *,
    max_subjects: int | None = None,
) -> int:
    subjects = [s for s in parse_index_md(index_md_path, useful) if s.aligned_pages]
    if max_subjects is not None and max_subjects > 0:
        subjects = subjects[:max_subjects]
    if not subjects:
        return 1
    index_set = useful.index_range_aligned.as_set()
    pages_by = {page.aligned: page for page in book_output.pages}
    n_index = sum(
        1
        for aligned in index_set
        if (page := pages_by.get(aligned)) is not None and page.file.is_file()
    )
    content = {
        aligned
        for subject in subjects
        for aligned in subject.aligned_pages
        if aligned not in index_set and aligned in pages_by
    }
    return max(1, 1 + n_index + len(content))


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
    progress: ProgressReporter | None = None,
    max_subjects: int | None = None,
) -> Path:
    index_md = build_index_md(ctx["book_output"], ctx["useful"])
    if progress is not None:
        progress(
            make_event(
                PHASE_POLYINDEX_INDEX,
                STATUS_STARTED,
                page_total=_estimate_index_cross_link_steps(
                    index_md,
                    ctx["book_output"],
                    ctx["useful"],
                    max_subjects=max_subjects,
                ),
                message="Preparazione INDEX…",
            )
        )
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
        progress=progress,
        max_subjects=max_subjects,
        parallel_pages=True,
    )
    toc_md_path = ctx["book_output"].output_dir / "TOC.md"
    if toc_md_path.is_file():
        chapters = parse_chapters_from_toc_md(toc_md_path, ctx["useful"])
        index_document = load_book_index_document(
            book_index_json_path(ctx["book_output"].output_dir, ctx["book_output"].slug)
        )
        stamp_page_metadata(
            ctx["book_output"],
            chapters,
            index_document,
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


def run_index_cross_links_only_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    max_subjects: int | None = None,
    parallel_pages: bool = True,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    ctx = _load_book_context(data_root, source_sha256)
    index_md = ctx["book_output"].output_dir / "INDEX.md"
    if not index_md.is_file():
        raise BiblioJobError("INDEX.md missing")
    openai_client = client or build_openai_client(settings)
    rid = request_id or ctx["sha"]
    stats = _run_async(
        apply_index_cross_links(
            index_md,
            ctx["book_output"],
            ctx["useful"],
            client=openai_client,
            settings=settings,
            request_id=rid,
            progress=progress,
            max_subjects=max_subjects,
            parallel_pages=parallel_pages,
        )
    )
    Log(
        INFO_LOG_LEVEL,
        "index cross links only job completed",
        {
            "source_sha256": ctx["sha"][:16],
            "max_subjects": max_subjects,
            "parallel_pages": parallel_pages,
            **stats,
        },
    )
    return {
        "ok": True,
        "stage": "polyindex_index",
        "cross_links_only": True,
        "source_sha256": ctx["sha"],
        "index_md_path": str(index_md),
        "max_subjects": max_subjects,
        "parallel_pages": parallel_pages,
        **stats,
    }


def run_polyindex_index_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
    progress: ProgressReporter | None = None,
    max_subjects: int | None = None,
    sync_library: bool = True,
) -> dict[str, Any]:
    ctx = _load_book_context(data_root, source_sha256)
    openai_client = client or build_openai_client(settings)
    rid = request_id or ctx["sha"]
    Log(
        INFO_LOG_LEVEL,
        "polyindex index job regenerate start",
        {"source_sha256": ctx["sha"][:16], "request_id": rid},
    )
    index_md = _run_async(
        _regenerate_index_md(
            ctx,
            data_root=data_root,
            client=openai_client,
            settings=settings,
            request_id=rid,
            prompt_notes=prompt_notes,
            progress=progress,
            max_subjects=max_subjects,
        )
    )
    result: dict[str, Any] = {
        "ok": True,
        "stage": "polyindex_index",
        "source_sha256": ctx["sha"],
        "index_md_path": str(index_md),
        "book_index_json_path": str(
            book_index_json_path(ctx["book_output"].output_dir, ctx["book_output"].slug)
        ),
        "sync_library": bool(sync_library),
    }
    if not sync_library:
        Log(
            INFO_LOG_LEVEL,
            "polyindex index book-only job completed",
            {"source_sha256": ctx["sha"][:16]},
        )
        return result

    reicat = ctx["manifest"].get("reicat") if isinstance(ctx["manifest"].get("reicat"), dict) else {}
    book_title = str(reicat.get("title") or reicat.get("titolo") or "") or None
    Log(
        INFO_LOG_LEVEL,
        "polyindex index job sync start",
        {
            "source_sha256": ctx["sha"][:16],
            "request_id": rid,
            "index_md_path": str(index_md),
        },
    )
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
    result["index_json_path"] = str(path)
    result.update(stats)
    return result


def run_library_index_sync_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    """Merge book INDEX.md subjects into global data/polyindex/INDEX.json."""
    ctx = _load_book_context(data_root, source_sha256)
    index_md = ctx["book_output"].output_dir / "INDEX.md"
    if not index_md.is_file():
        raise BiblioJobError("INDEX.md missing — esegui prima BOOKs (indice libro)")
    openai_client = client or build_openai_client(settings)
    rid = request_id or ctx["sha"]
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
    Log(
        INFO_LOG_LEVEL,
        "library index sync job completed",
        {"source_sha256": ctx["sha"][:16], **stats},
    )
    return {
        "ok": True,
        "stage": "library_index",
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
    progress: ProgressReporter | None = None,
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
            progress=progress,
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
    progress: ProgressReporter | None = None,
    cross_links_only: bool = False,
    cross_link_max_subjects: int | None = None,
    sync_library: bool = True,
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
        if cross_links_only:
            return run_index_cross_links_only_job(
                data_root,
                settings,
                source_sha256,
                client=client,
                request_id=request_id,
                max_subjects=cross_link_max_subjects,
                progress=progress,
            )
        return run_polyindex_index_job(
            data_root,
            settings,
            source_sha256,
            client=client,
            request_id=request_id,
            prompt_notes=prompt_notes,
            progress=progress,
            max_subjects=cross_link_max_subjects,
            sync_library=sync_library,
        )
    if stage_key == "library_index":
        return run_library_index_sync_job(
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
            progress=progress,
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
        progress=progress,
    )
    result["stage"] = "polyindex_biblio"
    return result


def try_handle_polyindex_preflight_get(
    path: str,
    handler,
    *,
    data_root: Path,
    send_json,
    query: dict[str, list[str]] | None = None,
    settings: Settings | None = None,
) -> bool:
    from urllib.parse import urlparse
    from src.ingestion.index_cross_links_preflight import run_index_cross_links_preflight

    route = urlparse(path if "://" in path else f"http://x{path}").path
    if route != "/api/admin/biblio/polyindex/preflight":
        return False
    if query is None:
        from urllib.parse import parse_qs, urlparse as up

        query = parse_qs(up(path if "://" in path else f"http://x{path}").query)
    source_sha256 = (query.get("source_sha256") or [""])[0].strip()
    if not source_sha256:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    from src.core.config import load_settings

    preflight_settings = settings or load_settings()
    try:
        result = _run_async(
            run_index_cross_links_preflight(
                data_root,
                preflight_settings,
                source_sha256,
            )
        )
    except Exception as exc:
        send_json(handler, 500, {"ok": False, "error": str(exc)})
        return True
    send_json(handler, 200, {"ok": bool(result.get("ok")), **result})
    return True


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
        cross_links_only = bool(payload.get("cross_links_only"))
        # Default True for backward compat; Indice/BOOKs passa False.
        sync_library = True if "sync_library" not in payload else bool(payload.get("sync_library"))
        cross_link_max_subjects_raw = payload.get("cross_link_max_subjects")
        cross_link_max_subjects = None
        if cross_link_max_subjects_raw is not None:
            cross_link_max_subjects = int(cross_link_max_subjects_raw)
            if cross_link_max_subjects < 1:
                raise ValueError("cross_link_max_subjects must be >= 1")
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
    try:
        create_biblio_stage_run(
            settings.sqlite_path,
            request_id=job_id,
            source_sha256=source_sha256,
            stage=stage,
            compute_mode=compute_mode,
        )
    except Exception as exc:
        Log(
            ERROR_LOG_LEVEL,
            "biblio stage run persist start failed",
            {"job_id": job_id, "stage": stage, "error": str(exc)},
        )

    prompt_notes_raw = payload.get("prompt_notes")
    prompt_notes = str(prompt_notes_raw).strip() if prompt_notes_raw else None
    if prompt_notes == "":
        prompt_notes = None

    def _poly_worker() -> None:
        acquired = job_semaphore.acquire(blocking=False)
        if not acquired:
            registry.emit(
                job_id,
                make_event("queue", "progress", message="waiting for a free ingest slot"),
            )
            job_semaphore.acquire()

        def progress(ev: dict[str, Any]) -> None:
            page_total = ev.get("page_total")
            if ev.get("status") == STATUS_STARTED and page_total is not None:
                registry.set_global_total(job_id, max(1, int(page_total)))
            registry.emit(job_id, ev)

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
                    client=build_openai_client(job_settings),
                    request_id=job_id,
                    prompt_notes=prompt_notes,
                    progress=progress,
                    cross_links_only=cross_links_only,
                    cross_link_max_subjects=cross_link_max_subjects,
                    sync_library=sync_library,
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
            try:
                mark_biblio_stage_run_done(
                    settings.sqlite_path,
                    request_id=job_id,
                    result=result if isinstance(result, dict) else None,
                )
            except Exception as persist_exc:
                Log(
                    ERROR_LOG_LEVEL,
                    "biblio stage run persist done failed",
                    {"job_id": job_id, "stage": stage, "error": str(persist_exc)},
                )
        except Exception as exc:
            Log(
                ERROR_LOG_LEVEL,
                "biblio polyindex job failed",
                {
                    "stage": stage,
                    "job_id": job_id,
                    "source_sha256": source_sha256[:16],
                    "compute_mode": compute_mode,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            registry.emit(
                job_id,
                make_event(
                    stage,
                    STATUS_ERROR,
                    source_sha256=source_sha256,
                    message=str(exc),
                ),
            )
            try:
                mark_biblio_stage_run_failed(
                    settings.sqlite_path,
                    request_id=job_id,
                    last_error=str(exc),
                )
            except Exception as persist_exc:
                Log(
                    ERROR_LOG_LEVEL,
                    "biblio stage run persist failed failed",
                    {"job_id": job_id, "stage": stage, "error": str(persist_exc)},
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

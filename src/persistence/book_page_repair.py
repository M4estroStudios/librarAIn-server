from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from src.core.errors import ShutdownRequested
from src.core.hashing import compute_file_sha256
from src.core.log import INFO_LOG_LEVEL, Log
from src.core.text import slugify as _slugify
from src.ingestion.orchestrator import (
    NullOrchestratorRegistry,
    OrchestratorStageError,
    PipelineContext,
    _build_pipeline_context,
    _prepare_page_jobs,
    _run_book_artifact_phases,
    _run_editor_phase_only,
    _run_glm_ocr_phase,
    _run_polyindex_phases,
    _run_render_phase,
    _run_stage1_phase,
    _run_vision_editor_phases,
)
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pipeline.engine import require_gpu_vram_at_pipeline_start
from src.ingestion.pipeline.stage3 import Stage3Result
from src.ingestion.progress import (
    PHASE_STAGE3_EDITOR,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.ingestion.request_validation import validate_and_enrich_request
from src.models.request import (
    EnrichedIngestRequest,
    IngestRequest,
    PageRange,
    ReicatMetadata,
    UsefulPagesEnumeration,
)
from src.models.settings import Settings
from src.persistence.book_page_exclude import (
    _aligned_to_original_from_manifest,
    load_book_exclusions,
)
from src.persistence.book_page_preview import mark_page_pending_review
from src.persistence.book_page_exclude import _resolve_slug
from src.persistence.book_pages_audit import STAGE_DIRS, _discover_aligned_from_dir, _load_manifest
from src.persistence.pipeline_runs import _sqlite_connection, reicat_alias_snapshot_from_row


class PageRepairError(ValueError):
    pass


_REPAIR_STAGE_ORDER = ("stage1OCR", "stage2Vision", "stage3Editor", "output")
RepairPipelineMode = Literal["classic", "glm_ocr"]


def normalize_repair_pipeline_mode(value: object) -> RepairPipelineMode:
    mode = str(value or "glm_ocr").strip().lower().replace("-", "_")
    if mode in {"classic", "easyocr"}:
        return "classic"
    return "glm_ocr"


def resolve_resume_pipeline_mode(settings: Settings) -> RepairPipelineMode:
    from src.ingestion.pipeline.glm_ocr_stage import resolve_glm_ocr_model

    if resolve_glm_ocr_model(settings):
        return "glm_ocr"
    return "classic"


_REPAIR_INDEX_PHASE_STEPS = 4


def repair_global_step_count(
    page_count: int,
    *,
    pipeline_mode: RepairPipelineMode,
    include_index_phases: bool = False,
) -> int:
    stages_per_page = 2 if pipeline_mode == "glm_ocr" else 3
    extra = _REPAIR_INDEX_PHASE_STEPS if include_index_phases else 0
    return max(page_count * stages_per_page + extra, 1)


def build_repair_progress_baseline(
    audit: dict[str, Any],
    *,
    pipeline_mode: RepairPipelineMode,
) -> dict[str, Any]:
    """Book-level done/total for repair UI, including pages already on disk."""
    expected = max(1, int(audit.get("expected_page_count") or 0))
    stages = audit.get("stages") if isinstance(audit.get("stages"), dict) else {}

    def _present(stage_key: str) -> int:
        entry = stages.get(stage_key)
        if not isinstance(entry, dict):
            return 0
        return max(0, min(expected, int(entry.get("present_count") or 0)))

    missing_pages = audit.get("missing_pages")
    missing_count = len(missing_pages) if isinstance(missing_pages, list) else 0
    gaps_done = max(0, expected - missing_count)
    if pipeline_mode == "glm_ocr":
        glm_done = min(_present("stage1OCR"), _present("stage2Vision"))
        phase_baselines = (
            ("stage1_glm_ocr", glm_done),
            ("stage3_editor", _present("stage3Editor")),
        )
    else:
        phase_baselines = (
            ("stage1_ocr", _present("stage1OCR")),
            ("stage2_vision", _present("stage2Vision")),
            ("stage3_editor", _present("stage3Editor")),
        )
    done_steps = sum(done for _phase, done in phase_baselines)
    total_steps = repair_global_step_count(
        expected,
        pipeline_mode=pipeline_mode,
        include_index_phases=True,
    )
    events = [
        {
            "phase": "gaps_repair",
            "status": "baseline",
            "done": gaps_done,
            "page_total": expected,
            "counts_as_step": False,
        }
    ]
    for phase, done in phase_baselines:
        events.append(
            {
                "phase": phase,
                "status": "baseline",
                "done": done,
                "page_total": expected,
                "counts_as_step": False,
            }
        )
    return {
        "expected_page_count": expected,
        "done_steps": min(done_steps, total_steps),
        "total_steps": total_steps,
        "events": events,
    }


def _book_output_from_manifest(
    data_root: Path,
    source_sha256: str,
    manifest: dict[str, Any],
) -> BookOutput:
    sha = source_sha256.strip().lower()
    slug = str(manifest.get("slug") or "")
    output_dir = data_root / "output" / sha
    pages: list[BookPageOutput] = []
    raw_pages = manifest.get("pages")
    if isinstance(raw_pages, list):
        for entry in raw_pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            original = entry.get("original")
            rel = entry.get("file")
            if not isinstance(aligned, int) or not isinstance(original, int):
                continue
            if not isinstance(rel, str) or not rel.strip():
                continue
            pages.append(
                BookPageOutput(
                    aligned=aligned,
                    original=original,
                    file=output_dir / rel,
                )
            )
    return BookOutput(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        slug=slug,
        pages=sorted(pages, key=lambda page: page.aligned),
    )


async def _run_repair_index_phases(
    ctx: PipelineContext,
    enriched: EnrichedIngestRequest,
    data_root: Path,
    source_sha256: str,
) -> None:
    from src.core.openai_client import build_openai_client

    manifest_path = data_root / "output" / source_sha256.strip().lower() / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PageRepairError(f"unable to reload manifest for index rebuild: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PageRepairError("manifest for index rebuild must be an object")
    full_useful = build_useful_pages_enumeration(enriched, None)
    ctx.useful_pages = full_useful
    ctx.render_page_total = len(full_useful.useful_original_pages)
    if ctx.openai_client is None:
        ctx.openai_client = build_openai_client(ctx.settings)
    book_output = _book_output_from_manifest(data_root, source_sha256, manifest)
    if not book_output.pages:
        raise PageRepairError("no output pages available for index rebuild")
    try:
        toc_md_path, index_md_path = await _run_book_artifact_phases(ctx, book_output)
        await _run_polyindex_phases(ctx, book_output, toc_md_path, index_md_path)
    except OrchestratorStageError as exc:
        raise PageRepairError(str(exc.cause)) from exc
    except ShutdownRequested:
        raise


def infer_repair_entry_stage(missing_in: list[str]) -> str:
    for stage_key in _REPAIR_STAGE_ORDER:
        if stage_key in missing_in:
            return stage_key
    return "output"


def infer_gaps_repair_entry_stage(gap_pages: list[dict[str, Any]]) -> str:
    for stage_key in _REPAIR_STAGE_ORDER:
        for page in gap_pages:
            missing_in = page.get("missing_in")
            if isinstance(missing_in, list) and stage_key in missing_in:
                return stage_key
    return "output"


def _find_raw_pdf_by_digest(data_root: Path, source_sha256: str) -> Path | None:
    raw_dir = data_root / "input" / "raw"
    if not raw_dir.is_dir():
        return None
    digest = source_sha256.strip().lower()
    for pdf_path in sorted(raw_dir.glob("*.pdf")):
        try:
            if compute_file_sha256(pdf_path).lower() == digest:
                return pdf_path
        except OSError:
            continue
    return None


def _resolve_source_pdf(data_root: Path, source_sha256: str) -> Path:
    raw_pdf = _find_raw_pdf_by_digest(data_root, source_sha256)
    if raw_pdf is not None:
        return raw_pdf
    processed = data_root / "input" / "processed" / f"{source_sha256.strip().lower()}.pdf"
    if processed.is_file():
        return processed
    raise PageRepairError("source pdf not found for book")


def _count_pdf_pages(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path), strict=False).pages)


def _load_book_sqlite_row(sqlite_path: str, source_sha256: str) -> sqlite3.Row | None:
    sha = source_sha256.strip().lower()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM books WHERE source_sha256 = ?",
                (sha,),
            ).fetchone()
    except sqlite3.Error:
        return None


def _discover_max_aligned_page(data_root: Path, source_sha256: str) -> int:
    sha = source_sha256.strip().lower()
    tmp_root = data_root / "tmp" / sha
    found: set[int] = set()
    for stage_name in ("stage1OCR", "stage2Vision", "stage3Editor"):
        found |= _discover_aligned_from_dir(tmp_root / stage_name)
    found |= _discover_aligned_from_dir(data_root / "output" / sha / "pages")
    return max(found) if found else 0


def load_manifest_for_repair(
    data_root: Path,
    source_sha256: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    sha = source_sha256.strip().lower()
    manifest_path = data_root / "output" / sha / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is not None:
        return manifest
    if not sqlite_path:
        raise PageRepairError("manifest not found for book")
    row = _load_book_sqlite_row(sqlite_path, sha)
    if row is None:
        raise PageRepairError("manifest not found for book")
    slug = _resolve_slug(data_root, sha, None)
    if not slug:
        raise PageRepairError("book slug not found")
    aligned_count = _discover_max_aligned_page(data_root, sha)
    if aligned_count < 1:
        raise PageRepairError("no staged pages found for book")
    try:
        original_count = _count_pdf_pages(_resolve_source_pdf(data_root, sha))
    except PageRepairError:
        original_count = aligned_count
    excluded_aligned, pages_to_remove = load_book_exclusions(data_root, sha, manifest=None)
    manifest_data: dict[str, Any] = {
        "source_sha256": sha,
        "slug": slug,
        "original_page_count": original_count,
        "aligned_page_count": aligned_count,
        "pages": [],
        "reicat": reicat_alias_snapshot_from_row(row),
        "pipeline_version": str(row["schema_version"]),
    }
    if excluded_aligned:
        manifest_data["excluded_aligned_pages"] = excluded_aligned
    if pages_to_remove:
        manifest_data["pages_to_remove"] = pages_to_remove
    return manifest_data


def build_enriched_from_manifest(
    data_root: Path,
    manifest: dict[str, Any],
) -> EnrichedIngestRequest:
    source_sha256 = str(manifest["source_sha256"]).strip().lower()
    reicat_raw = manifest.get("reicat")
    if not isinstance(reicat_raw, dict):
        raise PageRepairError("manifest missing reicat object")
    reicat = ReicatMetadata.model_validate(reicat_raw)
    _, pages_to_remove = load_book_exclusions(
        data_root, source_sha256, manifest=manifest
    )
    pdf_path = _resolve_source_pdf(data_root, source_sha256)
    schema_version = str(manifest.get("pipeline_version", "1.0"))
    request = IngestRequest(
        schema_version=schema_version,  # type: ignore[arg-type]
        source_pdf_path=str(pdf_path),
        pages_to_remove=pages_to_remove,
        toc_range=PageRange(start=1, end=1),
        index_range=PageRange(start=1, end=1),
        reicat=reicat,
    )
    raw_pdf = _find_raw_pdf_by_digest(data_root, source_sha256)
    if raw_pdf is not None and raw_pdf == pdf_path:
        return validate_and_enrich_request(request.model_dump())
    original_count = manifest.get("original_page_count")
    if not isinstance(original_count, int) or original_count < 1:
        original_count = _count_pdf_pages(pdf_path)
    return EnrichedIngestRequest(
        request=request,
        source_sha256=source_sha256,
        source_pdf_path=str(pdf_path),
        source_pdf_page_count=original_count,
    )


def resolve_original_page(
    manifest: dict[str, Any],
    useful_pages: UsefulPagesEnumeration,
    aligned_page: int,
) -> int:
    mapping = _aligned_to_original_from_manifest(manifest)
    if aligned_page in mapping:
        return mapping[aligned_page]
    original = useful_pages.aligned_page_to_original_page.get(aligned_page)
    if original is None:
        raise PageRepairError(f"aligned page {aligned_page} is not part of this book")
    return original


def filter_useful_pages_to_single(
    useful_pages: UsefulPagesEnumeration,
    original_page: int,
) -> UsefulPagesEnumeration:
    if original_page not in useful_pages.useful_original_pages:
        raise PageRepairError(f"original page {original_page} is not in useful pages")
    return useful_pages.model_copy(update={"useful_original_pages": [original_page]})


def filter_useful_pages_to_aligned(
    useful_pages: UsefulPagesEnumeration,
    aligned_pages: list[int],
) -> UsefulPagesEnumeration:
    originals: list[int] = []
    for aligned_page in sorted(set(aligned_pages)):
        if aligned_page < 1:
            raise PageRepairError("aligned_page must be positive")
        original_page = useful_pages.aligned_page_to_original_page.get(aligned_page)
        if original_page is None:
            raise PageRepairError(f"aligned page {aligned_page} is not part of this book")
        if original_page not in useful_pages.useful_original_pages:
            raise PageRepairError(f"original page {original_page} is not in useful pages")
        originals.append(original_page)
    if not originals:
        raise PageRepairError("no aligned pages selected for repair")
    return useful_pages.model_copy(update={"useful_original_pages": originals})


def merge_repaired_page_into_output(
    data_root: Path,
    source_sha256: str,
    enriched: EnrichedIngestRequest,
    useful_pages: UsefulPagesEnumeration,
    stage3_result: Stage3Result,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    from src.ingestion.output_writer import (
        _atomic_copy_or_skip,
        _atomic_write_bytes,
        _manifest_core,
        _page_filename,
        _utc_now_iso,
    )

    slug = str(manifest.get("slug") or _slugify(enriched.request.reicat.title))
    output_dir = data_root / "output" / source_sha256.strip().lower()
    pages_dir = output_dir / "pages"
    manifest_path = output_dir / "manifest.json"
    merged_entries: dict[int, dict[str, object]] = {}
    existing_pages = manifest.get("pages")
    if isinstance(existing_pages, list):
        for entry in existing_pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            if isinstance(aligned, int):
                merged_entries[aligned] = dict(entry)
    written_aligned: list[int] = []
    for page in stage3_result.pages:
        filename = _page_filename(page.aligned_page, slug)
        rel_path = f"pages/{filename}"
        dest = pages_dir / filename
        source = Path(page.md_path)
        if not source.is_file():
            raise PageRepairError(
                f"stage3 output missing for aligned page {page.aligned_page}"
            )
        _atomic_copy_or_skip(source, dest)
        merged_entries[page.aligned_page] = {
            "aligned": page.aligned_page,
            "original": page.original_page,
            "file": rel_path,
        }
        written_aligned.append(page.aligned_page)
    manifest_data: dict[str, object] = dict(manifest)
    manifest_data.update(
        {
            "source_sha256": source_sha256.strip().lower(),
            "slug": slug,
            "original_page_count": useful_pages.original_page_count,
            "aligned_page_count": useful_pages.aligned_page_count,
            "pages": sorted(merged_entries.values(), key=lambda item: int(item["aligned"])),
            "reicat": enriched.request.reicat.model_dump(by_alias=True),
            "pipeline_version": enriched.request.schema_version,
            "generated_at": _utc_now_iso(),
        }
    )
    for key in ("excluded_aligned_pages", "pages_to_remove"):
        if key in manifest:
            manifest_data[key] = manifest[key]
    new_core = _manifest_core(manifest_data)
    old_core = _manifest_core(manifest)
    if new_core != old_core or written_aligned:
        manifest_bytes = json.dumps(manifest_data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(manifest_path, manifest_bytes)
    Log(
        INFO_LOG_LEVEL,
        "page_repair output merged",
        {
            "source_sha256": source_sha256[:16],
            "aligned_pages_written": written_aligned,
            "manifest_path": str(manifest_path),
        },
    )
    return {
        "aligned_pages_written": sorted(written_aligned),
        "manifest_path": str(manifest_path),
    }


async def _run_repair_async(
    settings: Settings,
    enriched: EnrichedIngestRequest,
    useful_pages: UsefulPagesEnumeration,
    manifest: dict[str, Any],
    aligned_pages: list[int],
    *,
    request_id: str,
    entry_stage: str,
    progress: ProgressReporter | None,
    single_page: bool,
    pipeline_mode: RepairPipelineMode = "classic",
) -> dict[str, Any]:
    slug = str(manifest.get("slug") or _slugify(enriched.request.reicat.title))
    data_root = Path(settings.data_root)
    tmp_root = data_root / "tmp" / enriched.source_sha256
    pipeline_settings = (
        settings.model_copy(update={"max_parallel_request": 1})
        if single_page
        else settings
    )
    registry = NullOrchestratorRegistry()
    ctx = _build_pipeline_context(
        enriched,
        None,
        useful_pages,
        pipeline_settings,
        registry,
        request_id,
        slug=slug,
        data_root=data_root,
        tmp_root=tmp_root,
        progress=progress,
        skip_vision_editor=False,
        counters={"completed": 0, "failed": 0},
    )
    _run_render_phase(ctx)
    page_jobs = _prepare_page_jobs(ctx)
    try:
        if pipeline_mode == "glm_ocr":
            stage1_result, stage2_result = await _run_glm_ocr_phase(ctx, page_jobs)
            stage3_result = await _run_editor_phase_only(ctx, stage2_result, page_jobs)
        else:
            stage1_result = await _run_stage1_phase(ctx, page_jobs)
            stage2_result, stage3_result = await _run_vision_editor_phases(
                ctx, stage1_result, page_jobs
            )
    except OrchestratorStageError as exc:
        raise PageRepairError(str(exc.cause)) from exc
    if not stage3_result.pages:
        if len(aligned_pages) == 1:
            raise PageRepairError(f"pipeline produced no output for page {aligned_pages[0]}")
        raise PageRepairError("pipeline produced no output for selected pages")
    merge_result = merge_repaired_page_into_output(
        data_root,
        enriched.source_sha256,
        enriched,
        useful_pages,
        stage3_result,
        manifest,
    )
    if progress is not None:
        progress(
            make_event(
                PHASE_STAGE3_EDITOR,
                STATUS_DONE,
                aligned_page=aligned_pages[0] if len(aligned_pages) == 1 else None,
                aligned_pages=aligned_pages,
                page_count=len(aligned_pages),
                entry_stage=entry_stage,
                result=merge_result,
            )
        )
    if not single_page:
        await _run_repair_index_phases(
            ctx,
            enriched,
            data_root,
            enriched.source_sha256,
        )
    result: dict[str, Any] = {
        "source_sha256": enriched.source_sha256,
        "aligned_pages": aligned_pages,
        "entry_stage": entry_stage,
        "pipeline_mode": pipeline_mode,
        "stage1_pages": len(stage1_result.pages),
        "stage2_pages": len(stage2_result.pages),
        "stage3_pages": len(stage3_result.pages),
        "index_phases": not single_page,
        **merge_result,
    }
    if len(aligned_pages) == 1:
        result["aligned_page"] = aligned_pages[0]
    return result


def run_book_page_repair(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    aligned_page: int,
    *,
    missing_in: list[str] | None = None,
    request_id: str = "",
    progress: ProgressReporter | None = None,
    pipeline_mode: RepairPipelineMode | object = "classic",
) -> dict[str, Any]:
    if aligned_page < 1:
        raise PageRepairError("aligned_page must be positive")
    sha = source_sha256.strip().lower()
    manifest = load_manifest_for_repair(
        data_root, sha, getattr(settings, "sqlite_path", None)
    )
    excluded, _ = load_book_exclusions(data_root, sha, manifest=manifest)
    if aligned_page in set(excluded):
        raise PageRepairError(f"page {aligned_page} is excluded")
    enriched = build_enriched_from_manifest(data_root, manifest)
    useful_full = build_useful_pages_enumeration(enriched, None)
    original_page = resolve_original_page(manifest, useful_full, aligned_page)
    useful_pages = filter_useful_pages_to_single(useful_full, original_page)
    entry_stage = infer_repair_entry_stage(missing_in or list(STAGE_DIRS))
    resolved_mode = normalize_repair_pipeline_mode(pipeline_mode)
    require_gpu_vram_at_pipeline_start(
        settings,
        skip_vision_editor=False,
        single_page=True,
        entry_stage=entry_stage,
        ocr_backend="glm" if resolved_mode == "glm_ocr" else "easyocr",
    )
    repair_request_id = request_id or str(uuid4())
    if progress is not None:
        progress(
            make_event(
                "page_repair",
                STATUS_STARTED,
                source_sha256=sha[:16],
                aligned_page=aligned_page,
                original_page=original_page,
                entry_stage=entry_stage,
            )
        )
    result = asyncio.run(
        _run_repair_async(
            settings,
            enriched,
            useful_pages,
            manifest,
            [aligned_page],
            request_id=repair_request_id,
            entry_stage=entry_stage,
            progress=progress,
            single_page=True,
            pipeline_mode=resolved_mode,
        )
    )
    if progress is not None:
        progress(
            make_event(
                "page_repair",
                STATUS_COMPLETED,
                source_sha256=sha[:16],
                aligned_page=aligned_page,
                result=result,
            )
        )
    mark_page_pending_review(Path(settings.data_root), sha, aligned_page)
    return result


def run_book_gaps_repair(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    gap_pages: list[dict[str, Any]],
    *,
    request_id: str = "",
    progress: ProgressReporter | None = None,
    pipeline_mode: RepairPipelineMode | object = "classic",
) -> dict[str, Any]:
    if not gap_pages:
        raise PageRepairError("gap_pages must not be empty")
    sha = source_sha256.strip().lower()
    aligned_pages: list[int] = []
    for entry in gap_pages:
        if not isinstance(entry, dict):
            raise PageRepairError("gap_pages entries must be objects")
        aligned = entry.get("aligned")
        if not isinstance(aligned, int) or aligned < 1:
            raise PageRepairError("each gap page must have a positive aligned integer")
        aligned_pages.append(aligned)
    aligned_pages = sorted(set(aligned_pages))
    manifest = load_manifest_for_repair(
        data_root, sha, getattr(settings, "sqlite_path", None)
    )
    excluded, _ = load_book_exclusions(data_root, sha, manifest=manifest)
    excluded_set = set(excluded)
    for aligned_page in aligned_pages:
        if aligned_page in excluded_set:
            raise PageRepairError(f"page {aligned_page} is excluded")
    enriched = build_enriched_from_manifest(data_root, manifest)
    useful_full = build_useful_pages_enumeration(enriched, None)
    useful_pages = filter_useful_pages_to_aligned(useful_full, aligned_pages)
    entry_stage = infer_gaps_repair_entry_stage(gap_pages)
    resolved_mode = normalize_repair_pipeline_mode(pipeline_mode)
    require_gpu_vram_at_pipeline_start(
        settings,
        skip_vision_editor=False,
        single_page=False,
        entry_stage=entry_stage,
        ocr_backend="glm" if resolved_mode == "glm_ocr" else "easyocr",
    )
    repair_request_id = request_id or str(uuid4())
    if progress is not None:
        progress(
            make_event(
                "gaps_repair",
                STATUS_STARTED,
                source_sha256=sha[:16],
                aligned_pages=aligned_pages,
                page_count=len(aligned_pages),
            )
        )
    result = asyncio.run(
        _run_repair_async(
            settings,
            enriched,
            useful_pages,
            manifest,
            aligned_pages,
            request_id=repair_request_id,
            entry_stage=entry_stage,
            progress=progress,
            single_page=False,
            pipeline_mode=resolved_mode,
        )
    )
    data_root_path = Path(settings.data_root)
    for aligned_page in aligned_pages:
        mark_page_pending_review(data_root_path, sha, aligned_page)
    if progress is not None:
        progress(
            make_event(
                "gaps_repair",
                STATUS_COMPLETED,
                source_sha256=sha[:16],
                aligned_pages=aligned_pages,
                page_count=len(aligned_pages),
                entry_stage=entry_stage,
                result=result,
            )
        )
    return result

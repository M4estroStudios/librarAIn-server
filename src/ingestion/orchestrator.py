from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from src.core.errors import ShutdownRequested, raise_if_shutdown
from src.core.log import (
    INFO_LOG_LEVEL,
    Log,
    bind_log_context,
    log_stage_block_async,
    reset_log_context,
)
from src.core.lmstudio_models import (
    swap_lmstudio_model_to_editor,
    swap_lmstudio_vision_to_editor,
    unload_lmstudio_model,
)
from src.core.openai_client import build_openai_client
from src.core.text import slugify as _slugify
from src.ingestion.pipeline.glm_ocr_stage import resolve_glm_ocr_model, run_glm_ocr_combined_stage
from src.ingestion.pipeline.stage1 import Stage1Result, run_stage1_ingest_step
from src.ingestion.pipeline.stage2 import Stage2Result, run_stage2_vision
from src.ingestion.book_md_builder import build_book_md
from src.ingestion.index_builder import build_index_md
from src.ingestion.index_cross_links import apply_index_cross_links
from src.ingestion.polyindex.biblio_json import sync_polyindex_biblio_from_book
from src.ingestion.polyindex.index_json import sync_polyindex_index_from_book
from src.ingestion.polyindex.time_index import sync_time_index_from_book_async
from src.ingestion.polyindex.toc_json import sync_polyindex_toc_from_book
from src.ingestion.toc_builder import build_toc_md
from src.ingestion.tmp_cleanup import cleanup_tmp_after_success
from src.ingestion.toc_index_refine import refine_index_md, refine_toc_md
from src.ingestion.output_writer import BookOutput, materialize_book_pages
from src.ingestion.pipeline.stage3 import Stage3Result, run_stage3_editor
from src.ingestion.progress import (
    PHASE_POLYINDEX_BIBLIO,
    PHASE_POLYINDEX_INDEX,
    PHASE_POLYINDEX_TOC,
    PHASE_RENDER,
    PHASE_TIME_INDEX,
    STATUS_COMPLETED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.models.request import (
    EnrichedIngestRequest,
    PdfAlignmentResult,
    UsefulPagesEnumeration,
)
from src.models.settings import Settings
from src.persistence.pipeline_runs import create_pipeline_run, mark_pipeline_run_finished

PAGE_STATUS_PENDING = "pending"
PAGE_STATUS_STAGE1 = "stage1"
PAGE_STATUS_STAGE2 = "stage2"
PAGE_STATUS_STAGE3 = "stage3"
PAGE_STATUS_COMPLETED = "completed"
PAGE_STATUS_FAILED = "failed"

_TMP_SUBDIRS = ("stage1OCR", "stage2Vision", "stage3Editor", "stage4TocIndexRefine")


class OrchestratorStageError(Exception):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(str(cause))


@dataclass
class PageJob:
    aligned_page: int
    original_page: int
    png_path: str
    txt_path: str
    stage2_md_path: str
    stage3_md_path: str
    status: str
    last_error: str | None = None


@dataclass
class IngestJobEvent:
    at: str
    level: str
    stage: str
    message: str
    request_id: str
    payload: dict[str, Any] | None = None


@runtime_checkable
class OrchestratorRegistry(Protocol):
    def append_event(self, request_id: str, event: IngestJobEvent) -> None: ...


class NullOrchestratorRegistry:
    def append_event(self, request_id: str, event: IngestJobEvent) -> None:
        del request_id, event


@dataclass
class OrchestratorResult:
    page_jobs: list[PageJob]
    rendered_page_count: int
    stage1_result: Stage1Result
    stage2_result: Stage2Result | None = None
    stage3_result: Stage3Result | None = None
    book_output: BookOutput | None = None
    completed_count: int = 0
    failed_count: int = 0


@dataclass
class PipelineContext:
    enriched: EnrichedIngestRequest
    alignment: PdfAlignmentResult | None
    useful_pages: UsefulPagesEnumeration
    settings: Settings
    registry: OrchestratorRegistry
    request_id: str
    slug: str
    data_root: Path
    tmp_root: Path
    progress: ProgressReporter | None
    skip_vision_editor: bool
    counters: dict[str, int]
    source_sha256: str
    prompt_notes: str | None
    page_prompt_notes: str | None
    index_prompt_notes: str | None
    render_page_total: int
    polyindex_dir: Path
    openai_client: Any | None = field(default=None, repr=False)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publish_event(
    registry: OrchestratorRegistry,
    request_id: str,
    *,
    stage: str,
    message: str,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    registry.append_event(
        request_id,
        IngestJobEvent(
            at=_utc_now_iso(),
            level=level,
            stage=stage,
            message=message,
            request_id=request_id,
            payload=payload,
        ),
    )


def _progress_started(ctx: PipelineContext, phase: str, **kwargs: Any) -> None:
    if ctx.progress is not None:
        ctx.progress(make_event(phase, STATUS_STARTED, **kwargs))


def _progress_completed(ctx: PipelineContext, phase: str, **kwargs: Any) -> None:
    if ctx.progress is not None:
        ctx.progress(make_event(phase, STATUS_COMPLETED, **kwargs))


def _build_page_jobs(
    useful_pages: UsefulPagesEnumeration,
    tmp_root: Path,
    slug: str,
) -> list[PageJob]:
    jobs: list[PageJob] = []
    for original_page in sorted(useful_pages.useful_original_pages):
        aligned_page = useful_pages.original_page_to_aligned_page.get(original_page)
        if aligned_page is None:
            continue
        png_path = tmp_root / "render" / f"p.{aligned_page:04d}.png"
        jobs.append(
            PageJob(
                aligned_page=aligned_page,
                original_page=original_page,
                png_path=str(png_path),
                txt_path=str(tmp_root / "stage1OCR" / f"p.{aligned_page:04d}.{slug}.txt"),
                stage2_md_path=str(
                    tmp_root / "stage2Vision" / f"p.{aligned_page:04d}.{slug}.md"
                ),
                stage3_md_path=str(
                    tmp_root / "stage3Editor" / f"p.{aligned_page:04d}.{slug}.md"
                ),
                status=PAGE_STATUS_PENDING,
            )
        )
    return jobs


def _sync_page_jobs_from_stage1(
    page_jobs: list[PageJob],
    stage1_result: Stage1Result,
) -> None:
    succeeded = {p.aligned_page for p in stage1_result.pages}
    for job in page_jobs:
        if job.aligned_page in succeeded:
            job.status = PAGE_STATUS_STAGE2
            job.last_error = None
        elif job.status != PAGE_STATUS_FAILED:
            job.status = PAGE_STATUS_FAILED
            job.last_error = stage1_result.last_error


def _sync_page_jobs_from_stage3(
    page_jobs: list[PageJob],
    stage3_result: Stage3Result,
) -> None:
    completed_aligned = {p.aligned_page for p in stage3_result.pages}
    for job in page_jobs:
        if job.aligned_page in completed_aligned:
            job.status = PAGE_STATUS_COMPLETED
            job.last_error = None


def _build_pipeline_context(
    enriched: EnrichedIngestRequest,
    alignment: PdfAlignmentResult | None,
    useful_pages: UsefulPagesEnumeration,
    settings: Settings,
    registry: OrchestratorRegistry,
    request_id: str,
    *,
    slug: str,
    data_root: Path,
    tmp_root: Path,
    progress: ProgressReporter | None,
    skip_vision_editor: bool,
    counters: dict[str, int],
) -> PipelineContext:
    guidance = (enriched.request.ai_page_guidance or "").strip() or None
    return PipelineContext(
        enriched=enriched,
        alignment=alignment,
        useful_pages=useful_pages,
        settings=settings,
        registry=registry,
        request_id=request_id,
        slug=slug,
        data_root=data_root,
        tmp_root=tmp_root,
        progress=progress,
        skip_vision_editor=skip_vision_editor,
        counters=counters,
        source_sha256=enriched.source_sha256,
        prompt_notes=guidance,
        page_prompt_notes=guidance,
        index_prompt_notes=guidance,
        render_page_total=len(useful_pages.useful_original_pages),
        polyindex_dir=data_root / "polyindex",
    )


def _run_render_phase(ctx: PipelineContext) -> None:
    _progress_started(ctx, PHASE_RENDER, page_total=ctx.render_page_total)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="render",
        message="render deferred to stage1 (useful pages only)",
    )


def _prepare_page_jobs(ctx: PipelineContext) -> list[PageJob]:
    for subdir in _TMP_SUBDIRS:
        (ctx.tmp_root / subdir).mkdir(parents=True, exist_ok=True)
    page_jobs = _build_page_jobs(ctx.useful_pages, ctx.tmp_root, ctx.slug)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="pipeline",
        message="page pipeline started",
        payload={
            "page_count": len(page_jobs),
            "max_parallel": ctx.settings.max_parallel_request,
        },
    )
    return page_jobs


PipelineMode = Literal["classic", "glm_ocr"]


async def _run_glm_ocr_phase(
    ctx: PipelineContext,
    page_jobs: list[PageJob],
) -> tuple[Stage1Result, Stage2Result]:
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="stage1_glm_ocr",
        message="glm ocr combined batch started",
        payload={"page_count": len(page_jobs)},
    )
    ctx.openai_client = build_openai_client(ctx.settings)
    combined = await run_glm_ocr_combined_stage(
        ctx.enriched,
        ctx.alignment,
        ctx.useful_pages,
        ctx.settings,
        ctx.openai_client,
        request_id=ctx.request_id,
        progress=ctx.progress,
        prompt_notes=ctx.page_prompt_notes,
    )
    _sync_page_jobs_from_stage1(page_jobs, combined.stage1)
    for job in page_jobs:
        if job.status == PAGE_STATUS_STAGE1:
            job.status = PAGE_STATUS_STAGE2
    ctx.counters["completed"] = len(combined.stage1.pages)
    ctx.counters["failed"] = len(page_jobs) - ctx.counters["completed"]
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="stage1_glm_ocr",
        message="glm ocr combined batch completed",
        payload={
            "pages_written": len(combined.stage1.pages),
            "failed": len(page_jobs) - len(combined.stage1.pages),
        },
    )
    return combined.stage1, combined.stage2


def _orchestrator_result_skip_after_glm_ocr(
    ctx: PipelineContext,
    page_jobs: list[PageJob],
    stage1_result: Stage1Result,
    stage2_result: Stage2Result,
) -> OrchestratorResult:
    glm_model = resolve_glm_ocr_model(ctx.settings)
    if glm_model:
        unload_lmstudio_model(ctx.settings, glm_model)
    completed_count = ctx.counters["completed"]
    failed_count = ctx.counters["failed"]
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="pipeline",
        message="page pipeline completed (editor skipped after glm ocr)",
        payload={
            "completed_count": completed_count,
            "failed_count": failed_count,
            "rendered_page_count": ctx.render_page_total,
        },
    )
    return OrchestratorResult(
        page_jobs=page_jobs,
        rendered_page_count=ctx.render_page_total,
        stage1_result=stage1_result,
        stage2_result=stage2_result,
        completed_count=completed_count,
        failed_count=failed_count,
    )


async def _run_editor_phase_only(
    ctx: PipelineContext,
    stage2_result: Stage2Result,
    page_jobs: list[PageJob],
) -> Stage3Result:
    if ctx.openai_client is None:
        ctx.openai_client = build_openai_client(ctx.settings)
    glm_model = resolve_glm_ocr_model(ctx.settings)
    swap_lmstudio_model_to_editor(ctx.settings, from_model=glm_model)
    try:
        stage3_result = await run_stage3_editor(
            stage2_result,
            ctx.source_sha256,
            ctx.settings,
            ctx.openai_client,
            request_id=ctx.request_id,
            progress=ctx.progress,
            prompt_notes=ctx.page_prompt_notes,
        )
    except Exception as exc:
        raise OrchestratorStageError("stage3_editor", exc) from exc
    _sync_page_jobs_from_stage3(page_jobs, stage3_result)
    return stage3_result


async def _run_stage1_phase(
    ctx: PipelineContext,
    page_jobs: list[PageJob],
) -> Stage1Result:
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="stage1",
        message="stage1 batch started",
        payload={"page_count": len(page_jobs)},
    )
    stage1_result = await run_stage1_ingest_step(
        ctx.enriched,
        ctx.alignment,
        ctx.useful_pages,
        ctx.settings,
        request_id=ctx.request_id,
        progress=ctx.progress,
    )
    _sync_page_jobs_from_stage1(page_jobs, stage1_result)
    ctx.counters["completed"] = len(stage1_result.pages)
    ctx.counters["failed"] = len(page_jobs) - ctx.counters["completed"]
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="stage1",
        message="stage1 batch completed",
        payload={
            "pages_written": len(stage1_result.pages),
            "failed": len(page_jobs) - len(stage1_result.pages),
        },
    )
    return stage1_result


def _orchestrator_result_skip_vision_editor(
    ctx: PipelineContext,
    page_jobs: list[PageJob],
    stage1_result: Stage1Result,
) -> OrchestratorResult:
    completed_count = ctx.counters["completed"]
    failed_count = ctx.counters["failed"]
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="pipeline",
        message="page pipeline completed (vision/editor skipped)",
        payload={
            "completed_count": completed_count,
            "failed_count": failed_count,
            "rendered_page_count": ctx.render_page_total,
        },
    )
    return OrchestratorResult(
        page_jobs=page_jobs,
        rendered_page_count=ctx.render_page_total,
        stage1_result=stage1_result,
        completed_count=completed_count,
        failed_count=failed_count,
    )


async def _run_vision_editor_phases(
    ctx: PipelineContext,
    stage1_result: Stage1Result,
    page_jobs: list[PageJob],
) -> tuple[Stage2Result, Stage3Result]:
    raise_if_shutdown()
    ctx.openai_client = build_openai_client(ctx.settings)
    try:
        stage2_result = await run_stage2_vision(
            stage1_result,
            ctx.source_sha256,
            ctx.settings,
            ctx.openai_client,
            request_id=ctx.request_id,
            progress=ctx.progress,
            prompt_notes=ctx.page_prompt_notes,
        )
    except ShutdownRequested:
        raise
    except Exception as exc:
        raise OrchestratorStageError("stage2_vision", exc) from exc
    for job in page_jobs:
        if job.status == PAGE_STATUS_STAGE2:
            job.status = PAGE_STATUS_STAGE3

    raise_if_shutdown()
    swap_lmstudio_vision_to_editor(ctx.settings)

    try:
        stage3_result = await run_stage3_editor(
            stage2_result,
            ctx.source_sha256,
            ctx.settings,
            ctx.openai_client,
            request_id=ctx.request_id,
            progress=ctx.progress,
            prompt_notes=ctx.page_prompt_notes,
        )
    except ShutdownRequested:
        raise
    except Exception as exc:
        raise OrchestratorStageError("stage3_editor", exc) from exc
    _sync_page_jobs_from_stage3(page_jobs, stage3_result)
    return stage2_result, stage3_result


def _run_output_writer_phase(
    ctx: PipelineContext,
    stage3_result: Stage3Result,
) -> BookOutput:
    book_output = materialize_book_pages(
        stage3_result,
        ctx.enriched,
        ctx.source_sha256,
        ctx.useful_pages,
        ctx.settings,
        request_id=ctx.request_id,
    )
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="output_writer",
        message="output_writer completed",
        payload={
            "page_count": len(book_output.pages),
            "manifest_path": str(book_output.manifest_path),
        },
    )
    return book_output


def _run_book_md_builder(ctx: PipelineContext, book_output: BookOutput) -> None:
    book_md_path = build_book_md(book_output, ctx.useful_pages)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="book_md_builder",
        message="book_md_builder completed",
        payload={"book_md_path": str(book_md_path)},
    )


def _run_toc_md_builder(ctx: PipelineContext, book_output: BookOutput) -> Path:
    toc_md_path = build_toc_md(book_output, ctx.useful_pages)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="toc_builder",
        message="toc_builder completed",
        payload={"toc_md_path": str(toc_md_path)},
    )
    return toc_md_path


def _run_index_md_builder(ctx: PipelineContext, book_output: BookOutput) -> Path:
    index_md_path = build_index_md(book_output, ctx.useful_pages)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="index_builder",
        message="index_builder completed",
        payload={"index_md_path": str(index_md_path)},
    )
    return index_md_path


async def _run_toc_refine_phase(ctx: PipelineContext, toc_md_path: Path) -> Path:
    toc_refine_cache = ctx.tmp_root / "stage4TocIndexRefine"
    toc_refine_stats: dict[str, int] = {}
    try:
        toc_md_path = await refine_toc_md(
            toc_md_path,
            ctx.openai_client,
            ctx.settings,
            source_sha256=ctx.source_sha256,
            request_id=ctx.request_id,
            cache_dir=toc_refine_cache,
            prompt_notes=ctx.prompt_notes,
            stats=toc_refine_stats,
        )
    except Exception as exc:
        raise OrchestratorStageError("toc_refine", exc) from exc
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="toc_refine",
        message="toc_refine completed",
        payload={
            "toc_md_path": str(toc_md_path),
            "fallback_sections": toc_refine_stats.get("fallback_sections", 0),
        },
    )
    return toc_md_path


async def _run_index_refine_phase(ctx: PipelineContext, index_md_path: Path) -> Path:
    toc_refine_cache = ctx.tmp_root / "stage4TocIndexRefine"
    index_refine_stats: dict[str, int] = {}
    try:
        index_md_path = await refine_index_md(
            index_md_path,
            ctx.openai_client,
            ctx.settings,
            source_sha256=ctx.source_sha256,
            request_id=ctx.request_id,
            cache_dir=toc_refine_cache,
            prompt_notes=ctx.index_prompt_notes,
            stats=index_refine_stats,
        )
    except Exception as exc:
        raise OrchestratorStageError("index_refine", exc) from exc
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="index_refine",
        message="index_refine completed",
        payload={
            "index_md_path": str(index_md_path),
            "fallback_sections": index_refine_stats.get("fallback_sections", 0),
        },
    )
    return index_md_path


async def _run_index_cross_links_phase(
    ctx: PipelineContext,
    book_output: BookOutput,
    index_md_path: Path,
) -> Path:
    try:
        stats = await apply_index_cross_links(
            index_md_path,
            book_output,
            ctx.useful_pages,
            client=ctx.openai_client,
            settings=ctx.settings,
            request_id=ctx.request_id,
        )
    except Exception as exc:
        raise OrchestratorStageError("index_cross_links", exc) from exc
    _run_book_md_builder(ctx, book_output)
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="index_cross_links",
        message="index_cross_links completed",
        payload=stats,
    )
    return index_md_path


async def _run_book_artifact_phases(
    ctx: PipelineContext,
    book_output: BookOutput,
) -> tuple[Path, Path]:
    _run_book_md_builder(ctx, book_output)
    toc_md_path = _run_toc_md_builder(ctx, book_output)
    toc_md_path = await _run_toc_refine_phase(ctx, toc_md_path)
    index_md_path = _run_index_md_builder(ctx, book_output)
    index_md_path = await _run_index_refine_phase(ctx, index_md_path)
    index_md_path = await _run_index_cross_links_phase(ctx, book_output, index_md_path)
    return toc_md_path, index_md_path


async def _run_polyindex_phases(
    ctx: PipelineContext,
    book_output: BookOutput,
    toc_md_path: Path,
    index_md_path: Path,
) -> None:
    _progress_started(ctx, PHASE_POLYINDEX_TOC)
    toc_json_path = sync_polyindex_toc_from_book(
        ctx.polyindex_dir,
        ctx.source_sha256,
        book_output,
        toc_md_path,
        ctx.useful_pages,
    )
    _progress_completed(ctx, PHASE_POLYINDEX_TOC, toc_json_path=str(toc_json_path))
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="polyindex_toc",
        message="polyindex_toc completed",
        payload={"toc_json_path": str(toc_json_path)},
    )

    _progress_started(ctx, PHASE_POLYINDEX_INDEX)
    index_json_path, index_stats = sync_polyindex_index_from_book(
        ctx.polyindex_dir,
        ctx.source_sha256,
        index_md_path,
        ctx.useful_pages,
        ctx.openai_client,
        ctx.settings.sqlite_path,
        ctx.settings,
        ctx.request_id,
        prompt_notes=ctx.index_prompt_notes,
        book_title=ctx.enriched.request.reicat.title,
        book_slug=book_output.slug,
    )
    _progress_completed(
        ctx,
        PHASE_POLYINDEX_INDEX,
        index_json_path=str(index_json_path),
        n_new=index_stats["n_new"],
        n_match=index_stats["n_match"],
        n_alias=index_stats["n_alias"],
    )
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="polyindex_index",
        message="polyindex_index completed",
        payload={
            "index_json_path": str(index_json_path),
            "n_new": index_stats["n_new"],
            "n_match": index_stats["n_match"],
            "n_alias": index_stats["n_alias"],
        },
    )

    _progress_started(ctx, PHASE_TIME_INDEX)
    time_index_path, time_index_stats = await sync_time_index_from_book_async(
        ctx.polyindex_dir,
        ctx.source_sha256,
        book_output,
        book_title=ctx.enriched.request.reicat.title,
        request_id=ctx.request_id,
        client=ctx.openai_client,
        settings=ctx.settings,
        prompt_notes=ctx.page_prompt_notes,
    )
    _progress_completed(
        ctx,
        PHASE_TIME_INDEX,
        time_index_path=str(time_index_path),
        n_years=time_index_stats["n_years"],
        n_dates=time_index_stats["n_dates"],
    )
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="time_index",
        message="time_index completed",
        payload={
            "time_index_path": str(time_index_path),
            "n_years": time_index_stats["n_years"],
            "n_dates": time_index_stats["n_dates"],
        },
    )

    _progress_started(ctx, PHASE_POLYINDEX_BIBLIO)
    biblio_json_path, biblio_stats, biblio_payload = await sync_polyindex_biblio_from_book(
        ctx.polyindex_dir,
        ctx.source_sha256,
        book_output,
        ctx.useful_pages,
        client=ctx.openai_client,
        settings=ctx.settings,
        reicat=ctx.enriched.request.reicat,
        request_id=ctx.request_id,
        prompt_notes=ctx.page_prompt_notes,
        biblio_range_original=ctx.enriched.request.biblio_range,
    )
    _progress_completed(
        ctx,
        PHASE_POLYINDEX_BIBLIO,
        biblio_json_path=str(biblio_json_path),
        n_entries=biblio_stats["n_entries"],
        n_review=biblio_stats["n_review"],
        empty=bool(biblio_payload.get("empty")),
    )
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="polyindex_biblio",
        message="polyindex_biblio completed",
        payload={
            "biblio_json_path": str(biblio_json_path),
            "n_entries": biblio_stats["n_entries"],
            "n_review": biblio_stats["n_review"],
            "empty": bool(biblio_payload.get("empty")),
        },
    )


def _finalize_pipeline_result(
    ctx: PipelineContext,
    page_jobs: list[PageJob],
    stage1_result: Stage1Result,
    stage2_result: Stage2Result,
    stage3_result: Stage3Result,
    book_output: BookOutput,
) -> OrchestratorResult:
    completed_count = sum(1 for job in page_jobs if job.status == PAGE_STATUS_COMPLETED)
    failed_count = sum(1 for job in page_jobs if job.status == PAGE_STATUS_FAILED)
    ctx.counters["completed"] = completed_count
    ctx.counters["failed"] = failed_count
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="pipeline",
        message="page pipeline completed",
        payload={
            "completed_count": completed_count,
            "failed_count": failed_count,
            "rendered_page_count": ctx.render_page_total,
        },
    )
    return OrchestratorResult(
        page_jobs=page_jobs,
        rendered_page_count=ctx.render_page_total,
        stage1_result=stage1_result,
        stage2_result=stage2_result,
        stage3_result=stage3_result,
        book_output=book_output,
        completed_count=completed_count,
        failed_count=failed_count,
    )


async def run_pipeline(
    enriched: EnrichedIngestRequest,
    alignment: PdfAlignmentResult | None,
    useful_pages: UsefulPagesEnumeration,
    settings: Settings,
    sqlite_path: str | Path,
    registry: OrchestratorRegistry,
    request_id: str,
    *,
    progress: ProgressReporter | None = None,
    skip_vision_editor: bool = False,
    pipeline_mode: PipelineMode = "classic",
) -> OrchestratorResult:
    sqlite_path_str = str(sqlite_path)
    source_sha256 = enriched.source_sha256
    slug = _slugify(enriched.request.reicat.title)
    data_root = Path(settings.data_root)
    tmp_root = data_root / "tmp" / source_sha256
    counters = {"completed": 0, "failed": 0}
    request_token, sha_token = bind_log_context(
        request_id=request_id,
        source_sha256=source_sha256,
    )

    create_pipeline_run(
        sqlite_path_str,
        request_id=request_id,
        source_sha256=source_sha256,
        pipeline_version=enriched.request.schema_version,
        total_pages=len(useful_pages.useful_original_pages),
    )

    try:
        async with log_stage_block_async("pipeline"):
            Log(
                INFO_LOG_LEVEL,
                "orchestrator run_pipeline begin",
                {
                    "stage": "orchestrator",
                    "event": "begin",
                    "max_parallel": settings.max_parallel_request,
                    "skip_vision_editor": skip_vision_editor,
                    "pipeline_mode": pipeline_mode,
                },
            )
            try:
                result = await _run_pipeline_body(
                    enriched,
                    alignment,
                    useful_pages,
                    settings,
                    registry,
                    request_id,
                    slug=slug,
                    data_root=data_root,
                    tmp_root=tmp_root,
                    progress=progress,
                    skip_vision_editor=skip_vision_editor,
                    counters=counters,
                    pipeline_mode=pipeline_mode,
                )
            except ShutdownRequested:
                Log(
                    INFO_LOG_LEVEL,
                    "orchestrator interrupted by shutdown",
                    {"request_id": request_id, "source_sha256": source_sha256[:16]},
                )
                raise
            except (OrchestratorStageError, Exception) as exc:
                mark_pipeline_run_finished(
                    sqlite_path_str,
                    request_id=request_id,
                    status="failed",
                    succeeded_pages=counters["completed"],
                    failed_pages=counters["failed"],
                    last_error=str(exc),
                )
                raise

            cleanup_result = cleanup_tmp_after_success(source_sha256, settings)
            _publish_event(
                registry,
                request_id,
                stage="tmp_cleanup",
                message=(
                    "tmp_cleanup completed"
                    if not cleanup_result.skipped
                    else "tmp_cleanup skipped"
                ),
                payload={
                    "skipped": cleanup_result.skipped,
                    "reason": cleanup_result.reason,
                    "files_removed": cleanup_result.files_removed,
                    "bytes_freed": cleanup_result.bytes_freed,
                },
            )

            mark_pipeline_run_finished(
                sqlite_path_str,
                request_id=request_id,
                status="succeeded",
                succeeded_pages=result.completed_count,
                failed_pages=result.failed_count,
            )
            Log(
                INFO_LOG_LEVEL,
                "orchestrator run_pipeline done",
                {
                    "stage": "orchestrator",
                    "event": "done",
                    "completed_count": result.completed_count,
                    "failed_count": result.failed_count,
                },
            )
            if result.stage3_result is not None:
                editor_model = (settings.editor_model or "").strip()
                if editor_model:
                    unload_lmstudio_model(settings, editor_model)
            return result
    finally:
        reset_log_context(request_token, sha_token)


async def _run_pipeline_body(
    enriched: EnrichedIngestRequest,
    alignment: PdfAlignmentResult | None,
    useful_pages: UsefulPagesEnumeration,
    settings: Settings,
    registry: OrchestratorRegistry,
    request_id: str,
    *,
    slug: str,
    data_root: Path,
    tmp_root: Path,
    progress: ProgressReporter | None,
    skip_vision_editor: bool,
    counters: dict[str, int],
    pipeline_mode: PipelineMode = "classic",
) -> OrchestratorResult:
    ctx = _build_pipeline_context(
        enriched,
        alignment,
        useful_pages,
        settings,
        registry,
        request_id,
        slug=slug,
        data_root=data_root,
        tmp_root=tmp_root,
        progress=progress,
        skip_vision_editor=skip_vision_editor,
        counters=counters,
    )
    _run_render_phase(ctx)
    page_jobs = _prepare_page_jobs(ctx)
    if pipeline_mode == "glm_ocr":
        stage1_result, stage2_result = await _run_glm_ocr_phase(ctx, page_jobs)
        if ctx.skip_vision_editor:
            return _orchestrator_result_skip_after_glm_ocr(
                ctx, page_jobs, stage1_result, stage2_result
            )
        stage3_result = await _run_editor_phase_only(ctx, stage2_result, page_jobs)
        book_output = _run_output_writer_phase(ctx, stage3_result)
        toc_md_path, index_md_path = await _run_book_artifact_phases(ctx, book_output)
        await _run_polyindex_phases(ctx, book_output, toc_md_path, index_md_path)
        return _finalize_pipeline_result(
            ctx,
            page_jobs,
            stage1_result,
            stage2_result,
            stage3_result,
            book_output,
        )

    stage1_result = await _run_stage1_phase(ctx, page_jobs)
    if ctx.skip_vision_editor:
        return _orchestrator_result_skip_vision_editor(ctx, page_jobs, stage1_result)

    stage2_result, stage3_result = await _run_vision_editor_phases(
        ctx,
        stage1_result,
        page_jobs,
    )
    book_output = _run_output_writer_phase(ctx, stage3_result)
    toc_md_path, index_md_path = await _run_book_artifact_phases(ctx, book_output)
    await _run_polyindex_phases(ctx, book_output, toc_md_path, index_md_path)
    return _finalize_pipeline_result(
        ctx,
        page_jobs,
        stage1_result,
        stage2_result,
        stage3_result,
        book_output,
    )

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
    transition_lmstudio_models,
    unload_lmstudio_model,
)
from src.core.openai_client import build_openai_client, get_compute_mode
from src.models.ingest_compute import (
    IngestComputePlan,
    apply_step_compute,
    overlay_settings_for_step,
    resolved_model_for_step,
)
from src.core.text import slugify as _slugify
from src.ingestion.pipeline.glm_ocr_stage import resolve_glm_ocr_model, run_glm_ocr_combined_stage
from src.ingestion.pipeline.stage1 import Stage1Result, run_stage1_ingest_step
from src.ingestion.pipeline.stage2 import Stage2Result, run_stage2_vision
from src.ingestion.book_md_builder import build_book_md
from src.ingestion.index_builder import build_index_md
from src.ingestion.index_cross_links import book_index_json_path
from src.ingestion.polyindex.biblio_json import sync_polyindex_biblio_from_book
from src.ingestion.polyindex.gallery_index import write_gallery_index
from src.ingestion.polyindex.toc_json import parse_chapters_from_toc_md, sync_polyindex_toc_from_book
from src.ingestion.toc_builder import build_toc_md
from src.ingestion.tmp_cleanup import cleanup_tmp_after_success
from src.ingestion.toc_index_refine import refine_index_md, refine_toc_md
from src.ingestion.output_writer import (
    BookOutput,
    load_book_index_document,
    materialize_book_pages,
    stamp_page_metadata,
)
from src.ingestion.pipeline.stage3 import Stage3Result, run_stage3_editor
from src.ingestion.progress import (
    PHASE_POLYINDEX_BIBLIO,
    PHASE_POLYINDEX_TOC,
    STATUS_COMPLETED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.models.request import (
    EnrichedIngestRequest,
    PdfAlignmentResult,
    UsefulPagesEnumeration,
    build_md_formatting_block,
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
    md_formatting_block: str
    render_page_total: int
    polyindex_dir: Path
    openai_client: Any | None = field(default=None, repr=False)
    compute_plan: IngestComputePlan | None = None
    last_llm_step: str | None = None


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
    compute_plan: IngestComputePlan | None = None,
) -> PipelineContext:
    raw_guidance = getattr(enriched.request, "ai_page_guidance", None)
    guidance = raw_guidance.strip() if isinstance(raw_guidance, str) else None
    if not guidance:
        guidance = None
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
        md_formatting_block=build_md_formatting_block(enriched.request.md_formatting),
        render_page_total=len(useful_pages.useful_original_pages),
        polyindex_dir=data_root / "polyindex",
        compute_plan=compute_plan,
    )


def _run_render_phase(ctx: PipelineContext) -> None:
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


def _step_settings(ctx: PipelineContext, step_id: str) -> Settings:
    if ctx.compute_plan is None:
        return ctx.settings
    return overlay_settings_for_step(ctx.settings, ctx.compute_plan, step_id)


def _transition_to_llm_step(ctx: PipelineContext, step_id: str) -> None:
    if ctx.compute_plan is None:
        return
    to_settings = _step_settings(ctx, step_id)
    to_mode = ctx.compute_plan.choice(step_id).compute_mode
    to_model = resolved_model_for_step(to_settings, step_id)
    from_mode: str | None = None
    from_model: str | None = None
    if ctx.last_llm_step:
        from_settings = _step_settings(ctx, ctx.last_llm_step)
        from_mode = ctx.compute_plan.choice(ctx.last_llm_step).compute_mode
        from_model = resolved_model_for_step(from_settings, ctx.last_llm_step)
    transition_lmstudio_models(
        ctx.settings,
        from_mode=from_mode,
        from_model=from_model,
        to_mode=to_mode,
        to_model=to_model,
        to_settings=to_settings,
    )
    ctx.last_llm_step = step_id


def _unload_last_local_llm(ctx: PipelineContext) -> None:
    if ctx.compute_plan is None:
        return
    if not ctx.last_llm_step:
        return
    last_settings = _step_settings(ctx, ctx.last_llm_step)
    last_mode = ctx.compute_plan.choice(ctx.last_llm_step).compute_mode
    last_model = resolved_model_for_step(last_settings, ctx.last_llm_step)
    if last_mode != "local" or not last_model:
        return
    unload_lmstudio_model(ctx.settings, last_model, force=True)


class _LlmStep:
    def __init__(self, ctx: PipelineContext, step_id: str) -> None:
        self.ctx = ctx
        self.step_id = step_id
        self._cm: Any = None
        self.settings = ctx.settings

    def __enter__(self) -> Settings:
        ctx = self.ctx
        if ctx.compute_plan is None:
            if ctx.openai_client is None:
                ctx.openai_client = build_openai_client(ctx.settings)
            self.settings = ctx.settings
            return self.settings
        _transition_to_llm_step(ctx, self.step_id)
        self._cm = apply_step_compute(ctx.settings, ctx.compute_plan, self.step_id)
        self.settings = self._cm.__enter__()
        ctx.openai_client = build_openai_client(self.settings)
        return self.settings

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)


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
    with _LlmStep(ctx, "stage1_glm_ocr") as step_settings:
        combined = await run_glm_ocr_combined_stage(
            ctx.enriched,
            ctx.alignment,
            ctx.useful_pages,
            step_settings,
            ctx.openai_client,
            request_id=ctx.request_id,
            progress=ctx.progress,
            prompt_notes=ctx.page_prompt_notes,
            md_formatting=ctx.md_formatting_block,
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
    if ctx.compute_plan is not None:
        _unload_last_local_llm(ctx)
    else:
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
    if ctx.compute_plan is None:
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
                md_formatting=ctx.md_formatting_block,
            )
        except Exception as exc:
            raise OrchestratorStageError("stage3_editor", exc) from exc
        _sync_page_jobs_from_stage3(page_jobs, stage3_result)
        return stage3_result
    with _LlmStep(ctx, "stage3_editor") as step_settings:
        try:
            stage3_result = await run_stage3_editor(
                stage2_result,
                ctx.source_sha256,
                step_settings,
                ctx.openai_client,
                request_id=ctx.request_id,
                progress=ctx.progress,
                prompt_notes=ctx.page_prompt_notes,
                md_formatting=ctx.md_formatting_block,
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
    if ctx.compute_plan is None:
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
                md_formatting=ctx.md_formatting_block,
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
                md_formatting=ctx.md_formatting_block,
            )
        except ShutdownRequested:
            raise
        except Exception as exc:
            raise OrchestratorStageError("stage3_editor", exc) from exc
        _sync_page_jobs_from_stage3(page_jobs, stage3_result)
        return stage2_result, stage3_result

    with _LlmStep(ctx, "stage2_vision") as vision_settings:
        try:
            stage2_result = await run_stage2_vision(
                stage1_result,
                ctx.source_sha256,
                vision_settings,
                ctx.openai_client,
                request_id=ctx.request_id,
                progress=ctx.progress,
                prompt_notes=ctx.page_prompt_notes,
                md_formatting=ctx.md_formatting_block,
            )
        except ShutdownRequested:
            raise
        except Exception as exc:
            raise OrchestratorStageError("stage2_vision", exc) from exc
    for job in page_jobs:
        if job.status == PAGE_STATUS_STAGE2:
            job.status = PAGE_STATUS_STAGE3

    raise_if_shutdown()
    with _LlmStep(ctx, "stage3_editor") as editor_settings:
        try:
            stage3_result = await run_stage3_editor(
                stage2_result,
                ctx.source_sha256,
                editor_settings,
                ctx.openai_client,
                request_id=ctx.request_id,
                progress=ctx.progress,
                prompt_notes=ctx.page_prompt_notes,
                md_formatting=ctx.md_formatting_block,
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
    gallery_path, gallery_stats = write_gallery_index(
        book_output.output_dir,
        book_output.slug,
        [
            (page.aligned_page, page.original_page, page.gallery_captions)
            for page in stage3_result.pages
        ],
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
            "gallery_index_path": str(gallery_path),
            "gallery_n_entries": gallery_stats["n_entries"],
            "gallery_n_pages": gallery_stats["n_pages"],
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
        with _LlmStep(ctx, "toc_refine") as step_settings:
            toc_md_path = await refine_toc_md(
                toc_md_path,
                ctx.openai_client,
                step_settings,
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
        with _LlmStep(ctx, "index_refine") as step_settings:
            index_md_path = await refine_index_md(
                index_md_path,
                ctx.openai_client,
                step_settings,
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


def _run_page_metadata_phase(
    ctx: PipelineContext,
    book_output: BookOutput,
    toc_md_path: Path,
) -> None:
    chapters = parse_chapters_from_toc_md(toc_md_path, ctx.useful_pages)
    index_document = load_book_index_document(
        book_index_json_path(book_output.output_dir, book_output.slug)
    )
    stats = stamp_page_metadata(
        book_output,
        chapters,
        index_document,
        request_id=ctx.request_id,
    )
    _publish_event(
        ctx.registry,
        ctx.request_id,
        stage="page_metadata",
        message="page_metadata completed",
        payload=stats,
    )


async def _run_book_artifact_phases(
    ctx: PipelineContext,
    book_output: BookOutput,
) -> tuple[Path, Path]:
    _run_book_md_builder(ctx, book_output)
    toc_md_path = _run_toc_md_builder(ctx, book_output)
    toc_md_path = await _run_toc_refine_phase(ctx, toc_md_path)
    index_md_path = _run_index_md_builder(ctx, book_output)
    index_md_path = await _run_index_refine_phase(ctx, index_md_path)
    # INDEX_{slug}.json + hyperlink e TIME_INDEX restano alla pagina Indice.
    _run_page_metadata_phase(ctx, book_output, toc_md_path)
    return toc_md_path, index_md_path


async def _run_polyindex_phases(
    ctx: PipelineContext,
    book_output: BookOutput,
    toc_md_path: Path,
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

    with _LlmStep(ctx, "biblio") as step_settings:
        biblio_json_path, biblio_stats, biblio_payload = await sync_polyindex_biblio_from_book(
            ctx.polyindex_dir,
            ctx.source_sha256,
            book_output,
            ctx.useful_pages,
            client=ctx.openai_client,
            settings=step_settings,
            reicat=ctx.enriched.request.reicat,
            request_id=ctx.request_id,
            prompt_notes=ctx.page_prompt_notes,
            biblio_range_original=ctx.enriched.request.biblio_range,
            progress=ctx.progress,
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
    compute_plan: IngestComputePlan | None = None,
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
        compute_mode=(
            compute_plan.summary_mode() if compute_plan is not None else get_compute_mode()
        ),
        compute_plan=compute_plan.to_dict() if compute_plan is not None else None,
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
                    compute_plan=compute_plan,
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
            if result.stage3_result is not None and compute_plan is None:
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
    compute_plan: IngestComputePlan | None = None,
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
        compute_plan=compute_plan,
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
        toc_md_path, _index_md_path = await _run_book_artifact_phases(ctx, book_output)
        await _run_polyindex_phases(ctx, book_output, toc_md_path)
        _unload_last_local_llm(ctx)
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
    toc_md_path, _index_md_path = await _run_book_artifact_phases(ctx, book_output)
    await _run_polyindex_phases(ctx, book_output, toc_md_path)
    _unload_last_local_llm(ctx)
    return _finalize_pipeline_result(
        ctx,
        page_jobs,
        stage1_result,
        stage2_result,
        stage3_result,
        book_output,
    )

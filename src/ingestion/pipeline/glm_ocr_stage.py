from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

from src.core.errors import PermanentError, TransientError
from src.core.hashing import compute_file_sha256
from src.core.log import ERROR_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.core.retry import retry_async
from src.core.text import slugify
from src.ingestion.markdown_artifacts import finalize_vision_page_output
from src.ingestion.pdf_alignment import resolve_aligned_pdf_path_for_stage1
from src.ingestion.pipeline.md_cache import read_stage_md, write_stage_md
from src.ingestion.pipeline.stage1 import (
    Stage1PageResult,
    Stage1Result,
    _render_stage1_pages_sequential,
)
from src.ingestion.pipeline.stage2 import Stage2PageResult, Stage2Result
from src.ingestion.progress import (
    PHASE_STAGE1_GLM_OCR,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAGE_FAILED,
    STATUS_PAGE_PROGRESS,
    STATUS_PAGE_SKIPPED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    IngestInputValidationException,
    PdfAlignmentResult,
    UsefulPagesEnumeration,
)
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_GLM_OCR_PROMPT_FILE = _PROMPTS_DIR / "glm_ocr_prompt.md"
_MAX_COMPLETION_TOKENS = 4096


@dataclass
class _GlmOcrWork:
    page_index: int
    orig: int
    aligned: int
    txt_path: Path
    md_path: Path
    png_path: Path


@dataclass
class _GlmOcrOutcome:
    page_index: int
    stage1_page: Stage1PageResult | None = None
    stage2_page: Stage2PageResult | None = None
    skipped: bool = False
    missing_original: int | None = None
    failed: bool = False
    error: str | None = None


class GlmOcrCombinedResult(BaseModel):
    stage1: Stage1Result
    stage2: Stage2Result


def resolve_glm_ocr_model(settings: Settings) -> str:
    explicit = (settings.glm_ocr_model or "").strip()
    if explicit:
        return explicit
    return (settings.vision_model or "").strip()


def _load_glm_ocr_prompt() -> str:
    return _GLM_OCR_PROMPT_FILE.read_text(encoding="utf-8").strip()


async def transcribe_with_glm_ocr(
    client: openai.OpenAI,
    *,
    model: str,
    page_image_path: Path,
    request_id: str,
    page: int,
    settings: Settings,
    prompt_notes: str | None = None,
) -> str:
    system_text = build_system_prompt(_load_glm_ocr_prompt(), prompt_notes)
    image_bytes = Path(page_image_path).read_bytes()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        },
    ]
    return await chat_completion_with_retry(
        client,
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="stage1_glm_ocr",
        page=page,
        reasoning_effort=settings.reasoning_effort_vision,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_vision,
    )


def _resolve_glm_pages(
    sorted_pages: list[int],
    useful_pages_enumeration: UsefulPagesEnumeration,
    ocr_dir: Path,
    stage2_dir: Path,
    render_dir: Path,
    slug: str,
    model: str,
    *,
    force_recompute: bool,
    request_id: str,
    page_total: int,
    emit_progress,
) -> tuple[list[_GlmOcrOutcome], list[_GlmOcrWork]]:
    settled: list[_GlmOcrOutcome] = []
    work: list[_GlmOcrWork] = []

    for page_index, orig in enumerate(sorted_pages, start=1):
        aligned = useful_pages_enumeration.original_page_to_aligned_page.get(orig)
        if aligned is None:
            Log(
                WARNING_LOG_LEVEL,
                "glm ocr missing aligned page mapping",
                {"request_id": request_id, "original_page": orig},
            )
            settled.append(_GlmOcrOutcome(page_index=page_index, missing_original=orig))
            continue

        stem = f"p.{aligned:04d}.{slug}"
        txt_path = ocr_dir / f"{stem}.txt"
        md_path = stage2_dir / f"{stem}.md"
        if not force_recompute:
            cached = read_stage_md(md_path, model)
            if cached is not None:
                if not txt_path.is_file() or txt_path.stat().st_size == 0:
                    txt_path.write_text(cached, encoding="utf-8")
                emit_progress(make_event(
                    PHASE_STAGE1_GLM_OCR,
                    STATUS_PAGE_SKIPPED,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=aligned,
                    original_page=orig,
                    char_count=len(cached),
                ))
                settled.append(
                    _GlmOcrOutcome(
                        page_index=page_index,
                        stage1_page=Stage1PageResult(
                            aligned_page=aligned,
                            original_page=orig,
                            txt_path=str(txt_path),
                            char_count=len(cached),
                        ),
                        stage2_page=Stage2PageResult(
                            aligned_page=aligned,
                            original_page=orig,
                            md_path=str(md_path),
                            char_count=len(cached),
                        ),
                        skipped=True,
                    )
                )
                continue

        work.append(
            _GlmOcrWork(
                page_index=page_index,
                orig=orig,
                aligned=aligned,
                txt_path=txt_path,
                md_path=md_path,
                png_path=render_dir / f"p.{aligned:04d}.png",
            )
        )

    return settled, work


async def _glm_ocr_pages_parallel(
    work: list[_GlmOcrWork],
    render_failures: dict[int, _GlmOcrOutcome],
    client: openai.OpenAI,
    model: str,
    settings: Settings,
    sem: asyncio.Semaphore,
    *,
    request_id: str,
    page_total: int,
    prompt_notes: str | None,
    emit_progress,
) -> list[_GlmOcrOutcome]:
    pending = [item for item in work if item.page_index not in render_failures]
    if not pending:
        return []

    async def _process_one(item: _GlmOcrWork) -> _GlmOcrOutcome:
        async with sem:
            async def _call_model() -> str:
                try:
                    return await transcribe_with_glm_ocr(
                        client,
                        model=model,
                        page_image_path=item.png_path,
                        request_id=request_id,
                        page=item.aligned,
                        settings=settings,
                        prompt_notes=prompt_notes,
                    )
                except PermanentError:
                    raise
                except Exception as exc:
                    raise TransientError(str(exc)) from exc

            try:
                raw = await retry_async(
                    _call_model,
                    max_attempts=settings.retry_attempts,
                    retry_on=(TransientError,),
                    giveup_on=(PermanentError,),
                )
            except Exception as exc:
                Log(
                    WARNING_LOG_LEVEL,
                    "glm ocr page failed",
                    {
                        "request_id": request_id,
                        "aligned_page": item.aligned,
                        "original_page": item.orig,
                        "error": str(exc),
                    },
                )
                emit_progress(make_event(
                    PHASE_STAGE1_GLM_OCR,
                    STATUS_PAGE_FAILED,
                    counts_as_step=True,
                    page_index=item.page_index,
                    page_total=page_total,
                    aligned_page=item.aligned,
                    original_page=item.orig,
                    error=str(exc),
                    failure="glm_ocr_failed",
                ))
                return _GlmOcrOutcome(page_index=item.page_index, failed=True, error=str(exc))

            finalized = finalize_vision_page_output(raw, prompt_notes)
            write_stage_md(item.md_path, model, finalized)
            item.txt_path.write_text(finalized, encoding="utf-8")
            emit_progress(make_event(
                PHASE_STAGE1_GLM_OCR,
                STATUS_PAGE_PROGRESS,
                counts_as_step=True,
                page_index=item.page_index,
                page_total=page_total,
                aligned_page=item.aligned,
                original_page=item.orig,
                char_count=len(finalized),
            ))
            return _GlmOcrOutcome(
                page_index=item.page_index,
                stage1_page=Stage1PageResult(
                    aligned_page=item.aligned,
                    original_page=item.orig,
                    txt_path=str(item.txt_path),
                    char_count=len(finalized),
                ),
                stage2_page=Stage2PageResult(
                    aligned_page=item.aligned,
                    original_page=item.orig,
                    md_path=str(item.md_path),
                    char_count=len(finalized),
                ),
            )

    return list(await asyncio.gather(*(_process_one(item) for item in pending)))


def _aggregate_glm_outcomes(outcomes: list[_GlmOcrOutcome]) -> GlmOcrCombinedResult:
    stage1_pages: list[Stage1PageResult] = []
    stage2_pages: list[Stage2PageResult] = []
    skipped_existing = 0
    missing: list[int] = []
    last_error: str | None = None
    total_attempted = 0
    failed_count = 0

    for outcome in outcomes:
        if outcome.missing_original is not None:
            missing.append(outcome.missing_original)
            continue
        if outcome.failed:
            total_attempted += 1
            failed_count += 1
            last_error = outcome.error
            continue
        if outcome.stage1_page is not None and outcome.stage2_page is not None:
            stage1_pages.append(outcome.stage1_page)
            stage2_pages.append(outcome.stage2_page)
            if outcome.skipped:
                skipped_existing += 1
            else:
                total_attempted += 1

    stage1_pages.sort(key=lambda p: p.aligned_page)
    stage2_pages.sort(key=lambda p: p.aligned_page)
    return GlmOcrCombinedResult(
        stage1=Stage1Result(
            pages=stage1_pages,
            skipped_existing=skipped_existing,
            missing=missing,
            last_error=last_error,
        ),
        stage2=Stage2Result(
            pages=stage2_pages,
            skipped_existing=skipped_existing,
            missing=[],
            last_error=last_error,
        ),
    ), total_attempted, failed_count


async def run_glm_ocr_combined_stage(
    enriched: EnrichedIngestRequest,
    pdf_alignment: PdfAlignmentResult | None,
    useful_pages_enumeration: UsefulPagesEnumeration,
    settings: Settings,
    client: openai.OpenAI,
    *,
    request_id: str = "",
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
    prompt_notes: str | None = None,
) -> GlmOcrCombinedResult:
    aligned_path = resolve_aligned_pdf_path_for_stage1(
        enriched,
        pdf_alignment,
        settings.processed_pdf_input_dir,
        page_range_per_thread=settings.page_range_per_thread,
    )
    slug = slugify(enriched.request.reicat.title)
    source_sha256 = enriched.source_sha256
    data_root = Path(settings.data_root)
    render_dir = data_root / "tmp" / source_sha256 / "render"
    ocr_dir = data_root / "tmp" / source_sha256 / "stage1OCR"
    stage2_dir = data_root / "tmp" / source_sha256 / "stage2Vision"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    stage2_dir.mkdir(parents=True, exist_ok=True)

    model = resolve_glm_ocr_model(settings)
    if not model:
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                message="GLM_OCR_MODEL or VISION_MODEL must be configured",
            )
        )

    sorted_pages = sorted(useful_pages_enumeration.useful_original_pages)
    page_total = len(sorted_pages)
    sem = asyncio.Semaphore(settings.max_parallel_request)

    def _emit_progress(event: dict) -> None:
        if progress is not None:
            progress(event)

    if progress is not None:
        progress(make_event(PHASE_STAGE1_GLM_OCR, STATUS_STARTED, page_total=page_total))

    render_source_sha256 = compute_file_sha256(aligned_path)
    settled, glm_work = _resolve_glm_pages(
        sorted_pages,
        useful_pages_enumeration,
        ocr_dir,
        stage2_dir,
        render_dir,
        slug,
        model,
        force_recompute=force_recompute,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_progress,
    )
    from src.ingestion.pipeline.stage1 import _Stage1OcrWork

    ocr_work = [
        _Stage1OcrWork(
            page_index=w.page_index,
            orig=w.orig,
            aligned=w.aligned,
            txt_path=w.txt_path,
            png_path=w.png_path,
        )
        for w in glm_work
    ]

    def _emit_render_progress(event: dict) -> None:
        _emit_progress({**event, "phase": PHASE_STAGE1_GLM_OCR})

    render_failures_raw = await _render_stage1_pages_sequential(
        ocr_work,
        aligned_path,
        render_source_sha256,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_render_progress,
    )
    render_failures = {
        idx: _GlmOcrOutcome(page_index=out.page_index, failed=True, error=out.error)
        for idx, out in render_failures_raw.items()
    }
    glm_outcomes = await _glm_ocr_pages_parallel(
        glm_work,
        render_failures,
        client,
        model,
        settings,
        sem,
        request_id=request_id,
        page_total=page_total,
        prompt_notes=prompt_notes,
        emit_progress=_emit_progress,
    )
    outcomes = settled + list(render_failures.values()) + glm_outcomes
    combined, total_attempted, failed_count = _aggregate_glm_outcomes(outcomes)

    if total_attempted > 0 and failed_count / total_attempted >= 0.5:
        Log(
            ERROR_LOG_LEVEL,
            "glm ocr failure threshold exceeded",
            {
                "request_id": request_id,
                "failed": failed_count,
                "attempted": total_attempted,
                "last_error": combined.stage1.last_error,
            },
        )
        if progress is not None:
            progress(make_event(
                PHASE_STAGE1_GLM_OCR,
                STATUS_FAILED,
                failed_count=failed_count,
                attempted=total_attempted,
                error=combined.stage1.last_error,
            ))
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.OCR_STAGE_FAILED,
                message=f"GLM OCR stage failed on {failed_count}/{total_attempted} pages",
            )
        )

    if progress is not None:
        progress(make_event(
            PHASE_STAGE1_GLM_OCR,
            STATUS_COMPLETED,
            pages_written=len(combined.stage1.pages),
            skipped_existing=combined.stage1.skipped_existing,
            missing_count=len(combined.stage1.missing),
            failed_count=failed_count,
        ))

    return combined

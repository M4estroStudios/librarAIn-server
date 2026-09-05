from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

from src.core.errors import PermanentError, ShutdownRequested, TransientError, raise_if_shutdown
from src.core.hashing import compute_file_sha256
from src.core.log import ERROR_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.models.request import build_md_formatting_block
from src.core.parallel import gather_cancellable
from src.core.retry import retry_async
from src.core.text import slugify
from src.ingestion.annotation_rules import (
    OVERLAY_USER_INSTRUCTION,
    compile_page_annotation_block,
    element_display_names,
    notes_cache_hash,
)
from src.ingestion.markdown_artifacts import finalize_vision_page_output
from src.ingestion.pipeline.stage2 import _write_overlay_png
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
    defer_phase_progress,
    elapsed_ms,
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
    page_block: str = ""
    notes_hash: str = ""
    elements: list[dict[str, Any]] | None = None

@dataclass
class _GlmOcrOutcome:
    page_index: int
    stage1_page: Stage1PageResult | None = None
    stage2_page: Stage2PageResult | None = None
    skipped: bool = False
    missing_original: int | None = None
    failed: bool = False
    failed_aligned: int | None = None
    error: str | None = None

class GlmOcrCombinedResult(BaseModel):
    stage1: Stage1Result
    stage2: Stage2Result

def resolve_glm_ocr_model(settings: Settings) -> str:
    for attr in ("ocrvision_model", "glm_ocr_model", "vision_model"):
        value = getattr(settings, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

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
    md_formatting: str | None = None,
    overlay_image_path: Path | None = None,
    page_annotation_block: str | None = None,
) -> str:
    formatting = md_formatting if md_formatting is not None else build_md_formatting_block()
    system_text = build_system_prompt(_load_glm_ocr_prompt(), prompt_notes, md_formatting=formatting)
    image_bytes = Path(page_image_path).read_bytes()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    user_content: list[dict[str, Any]] = []
    block = (page_annotation_block or "").strip()
    if block:
        user_content.append({"type": "text", "text": block})
    user_content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }
    )
    if overlay_image_path is not None and overlay_image_path.is_file():
        overlay_b64 = base64.b64encode(overlay_image_path.read_bytes()).decode("ascii")
        user_content.append({"type": "text", "text": OVERLAY_USER_INSTRUCTION})
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{overlay_b64}"},
            }
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_content},
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
    prompt_notes: str | None = None,
    annotations_by_original: dict[int, list[dict[str, Any]]] | None = None,
    overlay_enabled: bool = True,
) -> tuple[list[_GlmOcrOutcome], list[_GlmOcrWork]]:
    settled: list[_GlmOcrOutcome] = []
    work: list[_GlmOcrWork] = []

    for page_index, orig in enumerate(sorted_pages, start=1):
        aligned = useful_pages_enumeration.original_page_to_aligned_page.get(orig)
        if aligned is None:
            Log(WARNING_LOG_LEVEL, "glm ocr missing aligned page mapping", {"request_id": request_id, "original_page": orig})
            settled.append(_GlmOcrOutcome(page_index=page_index, missing_original=orig))
            continue
        stem = f"p.{aligned:04d}.{slug}"
        txt_path = ocr_dir / f"{stem}.txt"
        md_path = stage2_dir / f"{stem}.md"
        elements = (annotations_by_original or {}).get(orig) or []
        page_block = compile_page_annotation_block(elements) if elements else ""
        page_hash = notes_cache_hash(
            prompt_notes or "", page_block, "overlay" if elements and overlay_enabled else ""
        )
        if not force_recompute:
            cached = read_stage_md(md_path, model, notes_hash=page_hash)
            if cached is not None:
                if not txt_path.is_file() or txt_path.stat().st_size == 0:
                    txt_path.write_text(cached, encoding="utf-8")
                emit_progress(make_event(
                    PHASE_STAGE1_GLM_OCR, STATUS_PAGE_SKIPPED, counts_as_step=True,
                    page_index=page_index, page_total=page_total, aligned_page=aligned,
                    original_page=orig, char_count=len(cached),
                ))
                settled.append(_GlmOcrOutcome(
                    page_index=page_index, skipped=True,
                    stage1_page=Stage1PageResult(aligned_page=aligned, original_page=orig, txt_path=str(txt_path), char_count=len(cached)),
                    stage2_page=Stage2PageResult(aligned_page=aligned, original_page=orig, md_path=str(md_path), char_count=len(cached)),
                ))
                continue
        work.append(_GlmOcrWork(
            page_index=page_index, orig=orig, aligned=aligned,
            txt_path=txt_path, md_path=md_path, png_path=render_dir / f"p.{aligned:04d}.png",
            page_block=page_block, notes_hash=page_hash, elements=elements,
        ))
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
    md_formatting: str | None,
    emit_progress,
) -> list[_GlmOcrOutcome]:
    pending = [item for item in work if item.page_index not in render_failures]
    if not pending:
        return []

    async def _process_one(item: _GlmOcrWork) -> _GlmOcrOutcome:
        async with sem:
            raise_if_shutdown()
            page_started = time.perf_counter()

            async def _call_model() -> str:
                raise_if_shutdown()
                try:
                    overlay_path = None
                    if item.elements and getattr(settings, "annotation_overlay_enabled", True):
                        overlay_path = _write_overlay_png(
                            item.png_path,
                            item.elements,
                            item.png_path.parent / "annotated" / item.png_path.name,
                        )
                    return await transcribe_with_glm_ocr(
                        client,
                        model=model,
                        page_image_path=item.png_path,
                        request_id=request_id,
                        page=item.aligned,
                        settings=settings,
                        prompt_notes=prompt_notes,
                        md_formatting=md_formatting,
                        overlay_image_path=overlay_path,
                        page_annotation_block=item.page_block or None,
                    )
                except PermanentError:
                    raise
                except ShutdownRequested:
                    raise
                except Exception as exc:
                    raise TransientError(str(exc)) from exc

            try:
                raw = await retry_async(
                    _call_model,
                    max_attempts=settings.retry_attempts,
                    retry_on=(TransientError,),
                    giveup_on=(PermanentError, ShutdownRequested),
                )
            except ShutdownRequested:
                raise
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
                    duration_ms=elapsed_ms(page_started),
                ))
                return _GlmOcrOutcome(
                    page_index=item.page_index,
                    failed=True,
                    failed_aligned=item.aligned,
                    error=str(exc),
                )

            finalized = finalize_vision_page_output(
                raw,
                prompt_notes,
                annotation_names=element_display_names(item.elements or []),
            )
            raise_if_shutdown()
            write_stage_md(item.md_path, model, finalized, notes_hash=item.notes_hash)
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
                duration_ms=elapsed_ms(page_started),
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

    return list(await gather_cancellable(*(_process_one(item) for item in pending)))

def _aggregate_glm_outcomes(
    outcomes: list[_GlmOcrOutcome],
) -> tuple[GlmOcrCombinedResult, int, int]:
    stage1_pages: list[Stage1PageResult] = []
    stage2_pages: list[Stage2PageResult] = []
    skipped_existing = 0
    missing_originals: list[int] = []
    failed_aligned: list[int] = []
    last_error: str | None = None
    total_attempted = 0
    failed_count = 0
    for outcome in outcomes:
        if outcome.missing_original is not None:
            missing_originals.append(outcome.missing_original)
            continue
        if outcome.failed:
            total_attempted += 1
            failed_count += 1
            last_error = outcome.error
            if outcome.failed_aligned is not None:
                failed_aligned.append(outcome.failed_aligned)
            continue
        if outcome.stage1_page is not None and outcome.stage2_page is not None:
            stage1_pages.append(outcome.stage1_page)
            stage2_pages.append(outcome.stage2_page)
            skipped_existing += int(outcome.skipped)
            total_attempted += int(not outcome.skipped)
    stage1_pages.sort(key=lambda p: p.aligned_page)
    stage2_pages.sort(key=lambda p: p.aligned_page)
    return (
        GlmOcrCombinedResult(
            stage1=Stage1Result(pages=stage1_pages, skipped_existing=skipped_existing, missing=missing_originals, last_error=last_error),
            stage2=Stage2Result(pages=stage2_pages, skipped_existing=skipped_existing, missing=sorted(set(failed_aligned)), last_error=last_error),
        ),
        total_attempted,
        failed_count,
    )

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
    md_formatting: str | None = None,
    annotations_by_original: dict[int, list[dict[str, Any]]] | None = None,
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
                message="OCRVISION_MODEL or VISION_MODEL must be configured",
            )
        )

    sorted_pages = sorted(useful_pages_enumeration.useful_original_pages)
    page_total = len(sorted_pages)
    sem = asyncio.Semaphore(settings.max_parallel_request)

    def _emit_progress(event: dict) -> None:
        if progress is not None:
            progress(event)

    deferred_ocr_events, defer_ocr_progress = defer_phase_progress(
        PHASE_STAGE1_GLM_OCR, _emit_progress,
    )
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
        emit_progress=defer_ocr_progress,
        prompt_notes=prompt_notes,
        annotations_by_original=annotations_by_original,
        overlay_enabled=bool(getattr(settings, "annotation_overlay_enabled", True)),
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

    render_failures_raw = await _render_stage1_pages_sequential(
        ocr_work,
        aligned_path,
        render_source_sha256,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_progress,
    )
    work_by_index = {item.page_index: item for item in ocr_work}
    render_failures = {
        idx: _GlmOcrOutcome(
            page_index=out.page_index,
            failed=True,
            failed_aligned=(
                work_by_index[idx].aligned if idx in work_by_index else None
            ),
            error=out.error,
        )
        for idx, out in render_failures_raw.items()
    }
    _emit_progress(make_event(PHASE_STAGE1_GLM_OCR, STATUS_STARTED, page_total=page_total))
    for deferred in deferred_ocr_events:
        _emit_progress(deferred)
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
        md_formatting=md_formatting,
        emit_progress=_emit_progress,
    )
    outcomes = settled + list(render_failures.values()) + glm_outcomes
    combined, total_attempted, failed_count = _aggregate_glm_outcomes(outcomes)

    expected_aligned = {
        useful_pages_enumeration.original_page_to_aligned_page[orig]
        for orig in useful_pages_enumeration.useful_original_pages
        if orig in useful_pages_enumeration.original_page_to_aligned_page
    }
    missing_aligned = sorted(
        expected_aligned - {page.aligned_page for page in combined.stage2.pages}
    )
    if missing_aligned:
        combined = GlmOcrCombinedResult(
            stage1=combined.stage1,
            stage2=combined.stage2.model_copy(update={"missing": missing_aligned}),
        )
    if (total_attempted > 0 and failed_count / total_attempted >= 0.5) or missing_aligned:
        sample = ", ".join(str(p) for p in missing_aligned[:12])
        more = f" (+{len(missing_aligned) - 12})" if len(missing_aligned) > 12 else ""
        message = (
            f"GLM OCR incomplete: {len(missing_aligned)} missing page(s) [{sample}{more}]; last_error={combined.stage1.last_error}"
            if missing_aligned
            else f"GLM OCR stage failed on {failed_count}/{total_attempted} pages"
        )
        Log(ERROR_LOG_LEVEL, "glm ocr incomplete or failure threshold exceeded", {
            "request_id": request_id, "failed": failed_count, "attempted": total_attempted,
            "missing_aligned": missing_aligned[:32], "last_error": combined.stage1.last_error,
        })
        if progress is not None:
            progress(make_event(
                PHASE_STAGE1_GLM_OCR, STATUS_FAILED, failed_count=failed_count,
                attempted=total_attempted, missing_aligned=missing_aligned,
                error=combined.stage1.last_error or message,
            ))
        raise IngestInputValidationException(IngestInputValidationError(
            code=IngestInputErrorCode.OCR_STAGE_FAILED, message=message,
        ))

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

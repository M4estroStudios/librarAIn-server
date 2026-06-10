from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from src.ingestion.pdf_alignment import resolve_aligned_pdf_path_for_stage1
from src.ingestion.pipeline.engine import EasyOCRPageEngine, OCRPageEngine
from src.core.hashing import compute_file_sha256
from src.ingestion.pipeline.render import _render_pdf_page_to_png
from src.ingestion.progress import (
    PHASE_STAGE1_OCR,
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
    ReicatMetadata,
    UsefulPagesEnumeration,
)
from src.core.errors import PermanentError, TransientError
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.retry import retry_async
from src.models.settings import Settings

from src.core.text import slugify

# Backwards-compatible alias; new code should import from src.core.text.
_slugify = slugify


class Stage1PageResult(BaseModel):
    aligned_page: int
    original_page: int
    txt_path: str
    char_count: int


class Stage1Result(BaseModel):
    pages: list[Stage1PageResult]
    skipped_existing: int
    missing: list[int]
    last_error: str | None = None


@dataclass
class _Stage1PageOutcome:
    page_index: int
    page: Stage1PageResult | None = None
    skipped: bool = False
    missing_original: int | None = None
    failed: bool = False
    error: str | None = None


@dataclass
class _Stage1OcrWork:
    page_index: int
    orig: int
    aligned: int
    txt_path: Path
    png_path: Path


def _resolve_stage1_pages(
    sorted_pages: list[int],
    useful_pages_enumeration: UsefulPagesEnumeration,
    ocr_dir: Path,
    render_dir: Path,
    slug: str,
    *,
    force_recompute: bool,
    request_id: str,
    page_total: int,
    emit_progress,
) -> tuple[list[_Stage1PageOutcome], list[_Stage1OcrWork]]:
    settled: list[_Stage1PageOutcome] = []
    ocr_work: list[_Stage1OcrWork] = []

    for page_index, orig in enumerate(sorted_pages, start=1):
        aligned = useful_pages_enumeration.original_page_to_aligned_page.get(orig)
        Log(
            INFO_LOG_LEVEL,
            "stage1 page resolve begin",
            {"request_id": request_id, "original_page": orig, "aligned_page": aligned},
        )
        if aligned is None:
            Log(
                WARNING_LOG_LEVEL,
                "stage1 missing aligned page mapping",
                {"request_id": request_id, "original_page": orig},
            )
            settled.append(_Stage1PageOutcome(page_index=page_index, missing_original=orig))
            continue

        txt_path = ocr_dir / f"p.{aligned:04d}.{slug}.txt"
        if not force_recompute and txt_path.is_file() and txt_path.stat().st_size > 0:
            cached_text = txt_path.read_text(encoding="utf-8")
            Log(
                INFO_LOG_LEVEL,
                "stage1 page skip OCR using existing txt",
                {
                    "request_id": request_id,
                    "original_page": orig,
                    "aligned_page": aligned,
                    "txt_path": str(txt_path),
                    "char_count": len(cached_text),
                },
            )
            emit_progress(make_event(
                PHASE_STAGE1_OCR,
                STATUS_PAGE_SKIPPED,
                counts_as_step=True,
                page_index=page_index,
                page_total=page_total,
                aligned_page=aligned,
                original_page=orig,
                char_count=len(cached_text),
            ))
            settled.append(
                _Stage1PageOutcome(
                    page_index=page_index,
                    page=Stage1PageResult(
                        aligned_page=aligned,
                        original_page=orig,
                        txt_path=str(txt_path),
                        char_count=len(cached_text),
                    ),
                    skipped=True,
                )
            )
            continue

        ocr_work.append(
            _Stage1OcrWork(
                page_index=page_index,
                orig=orig,
                aligned=aligned,
                txt_path=txt_path,
                png_path=render_dir / f"p.{aligned:04d}.png",
            )
        )

    return settled, ocr_work


async def _render_stage1_pages_sequential(
    ocr_work: list[_Stage1OcrWork],
    aligned_pdf_path: Path,
    render_source_sha256: str,
    *,
    request_id: str,
    page_total: int,
    emit_progress,
) -> dict[int, _Stage1PageOutcome]:
    render_failures: dict[int, _Stage1PageOutcome] = {}
    if not ocr_work:
        return render_failures

    Log(
        INFO_LOG_LEVEL,
        "stage1 PDF render phase begin",
        {"request_id": request_id, "pages_to_render": len(ocr_work)},
    )
    for work in ocr_work:
        try:
            await asyncio.to_thread(
                _render_pdf_page_to_png,
                aligned_pdf_path,
                work.aligned - 1,
                work.png_path,
                dpi=200,
                source_sha256=render_source_sha256,
            )
        except Exception as exc:
            Log(
                WARNING_LOG_LEVEL,
                "stage1 render page failed",
                {
                    "request_id": request_id,
                    "aligned_page": work.aligned,
                    "original_page": work.orig,
                    "error": str(exc),
                },
            )
            emit_progress(make_event(
                PHASE_STAGE1_OCR,
                STATUS_PAGE_FAILED,
                counts_as_step=True,
                page_index=work.page_index,
                page_total=page_total,
                aligned_page=work.aligned,
                original_page=work.orig,
                error=str(exc),
                failure="render_failed",
            ))
            render_failures[work.page_index] = _Stage1PageOutcome(
                page_index=work.page_index,
                failed=True,
                error=str(exc),
            )
    Log(
        INFO_LOG_LEVEL,
        "stage1 PDF render phase done",
        {
            "request_id": request_id,
            "pages_to_render": len(ocr_work),
            "render_failures": len(render_failures),
        },
    )
    return render_failures


async def _ocr_stage1_pages_parallel(
    ocr_work: list[_Stage1OcrWork],
    render_failures: dict[int, _Stage1PageOutcome],
    engine: OCRPageEngine,
    settings: Settings,
    sem: asyncio.Semaphore,
    *,
    request_id: str,
    page_total: int,
    emit_progress,
) -> list[_Stage1PageOutcome]:
    pending = [work for work in ocr_work if work.page_index not in render_failures]
    if not pending:
        return []

    Log(
        INFO_LOG_LEVEL,
        "stage1 OCR phase begin",
        {"request_id": request_id, "pages_to_ocr": len(pending), "max_parallel": settings.max_parallel_request},
    )

    async def _ocr_one(work: _Stage1OcrWork) -> _Stage1PageOutcome:
        async with sem:
            Log(
                INFO_LOG_LEVEL,
                "stage1 page OCR begin",
                {"request_id": request_id, "original_page": work.orig, "aligned_page": work.aligned},
            )

            async def _ocr_page() -> str:
                try:
                    return await asyncio.to_thread(
                        engine.ocr_page,
                        work.png_path,
                        lang=settings.ocr_languages,
                    )
                except PermanentError:
                    raise
                except Exception as exc:
                    raise TransientError(str(exc)) from exc

            try:
                text = await retry_async(
                    _ocr_page,
                    max_attempts=settings.retry_attempts,
                    retry_on=(TransientError,),
                    giveup_on=(PermanentError,),
                )
            except Exception as exc:
                Log(
                    WARNING_LOG_LEVEL,
                    "stage1 OCR page failed",
                    {
                        "request_id": request_id,
                        "aligned_page": work.aligned,
                        "original_page": work.orig,
                        "error": str(exc),
                    },
                )
                emit_progress(make_event(
                    PHASE_STAGE1_OCR,
                    STATUS_PAGE_FAILED,
                    counts_as_step=True,
                    page_index=work.page_index,
                    page_total=page_total,
                    aligned_page=work.aligned,
                    original_page=work.orig,
                    error=str(exc),
                    failure="ocr_failed",
                ))
                return _Stage1PageOutcome(
                    page_index=work.page_index,
                    failed=True,
                    error=str(exc),
                )

            work.txt_path.write_text(text, encoding="utf-8")
            emit_progress(make_event(
                PHASE_STAGE1_OCR,
                STATUS_PAGE_PROGRESS,
                counts_as_step=True,
                page_index=work.page_index,
                page_total=page_total,
                aligned_page=work.aligned,
                original_page=work.orig,
                char_count=len(text),
            ))
            Log(
                INFO_LOG_LEVEL,
                "stage1 page OCR complete",
                {"request_id": request_id, "original_page": work.orig, "aligned_page": work.aligned},
            )
            return _Stage1PageOutcome(
                page_index=work.page_index,
                page=Stage1PageResult(
                    aligned_page=work.aligned,
                    original_page=work.orig,
                    txt_path=str(work.txt_path),
                    char_count=len(text),
                ),
            )

    return list(await asyncio.gather(*(_ocr_one(work) for work in pending)))


def _aggregate_stage1_outcomes(outcomes: list[_Stage1PageOutcome]) -> tuple[
    list[Stage1PageResult],
    int,
    list[int],
    str | None,
    int,
    int,
]:
    pages: list[Stage1PageResult] = []
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
        if outcome.page is not None:
            pages.append(outcome.page)
            if outcome.skipped:
                skipped_existing += 1
            else:
                total_attempted += 1

    pages.sort(key=lambda p: p.aligned_page)
    return pages, skipped_existing, missing, last_error, total_attempted, failed_count


async def run_stage1_ingest_step(
    enriched: EnrichedIngestRequest,
    pdf_alignment: PdfAlignmentResult | None,
    useful_pages_enumeration: UsefulPagesEnumeration,
    settings: Settings,
    *,
    request_id: str = "",
    engine: OCRPageEngine | None = None,
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
) -> Stage1Result:
    Log(INFO_LOG_LEVEL, "stage1 ingest step resolve aligned PDF path begin", {"request_id": request_id})
    aligned_path = resolve_aligned_pdf_path_for_stage1(
        enriched,
        pdf_alignment,
        settings.processed_pdf_input_dir,
        page_range_per_thread=settings.page_range_per_thread,
    )
    Log(
        INFO_LOG_LEVEL,
        "stage1 ingest step resolve aligned PDF path done",
        {"request_id": request_id, "path": str(aligned_path)},
    )
    Log(INFO_LOG_LEVEL, "stage1 ingest step acquire OCR engine", {"request_id": request_id})
    ocr_engine = engine or EasyOCRPageEngine(gpu=settings.ocr_use_gpu, gpu_device=settings.ocr_gpu_device)
    if isinstance(ocr_engine, EasyOCRPageEngine):
        ocr_engine.prepare_parallel_pool(
            settings.ocr_languages,
            pool_size=settings.max_parallel_request,
        )
    Log(INFO_LOG_LEVEL, "stage1 ingest step OCR engine ready", {"request_id": request_id})
    try:
        return await run_stage1_ocr(
            aligned_path,
            enriched.source_sha256,
            useful_pages_enumeration,
            settings,
            ocr_engine,
            reicat=enriched.request.reicat,
            request_id=request_id,
            force_recompute=force_recompute,
            progress=progress,
        )
    finally:
        if engine is None and isinstance(ocr_engine, EasyOCRPageEngine):
            ocr_engine.release_parallel_pool()


async def run_stage1_ocr(
    aligned_pdf_path: Path,
    source_sha256: str,
    useful_pages_enumeration: UsefulPagesEnumeration,
    settings: Settings,
    engine: OCRPageEngine,
    *,
    reicat: ReicatMetadata,
    request_id: str = "",
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
) -> Stage1Result:
    """reicat is required to derive libro_slug from reicat.title;
    UsefulPagesEnumeration does not carry reicat data."""
    slug = _slugify(reicat.title)
    aligned_pdf_path = Path(aligned_pdf_path)
    data_root = Path(settings.data_root)
    render_dir = data_root / "tmp" / source_sha256 / "render"
    ocr_dir = data_root / "tmp" / source_sha256 / "stage1OCR"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    Log(
        INFO_LOG_LEVEL,
        "stage1 working dirs ready",
        {"request_id": request_id, "render_dir": str(render_dir), "ocr_dir": str(ocr_dir)},
    )

    sorted_pages = sorted(useful_pages_enumeration.useful_original_pages)
    page_total = len(sorted_pages)
    sem = asyncio.Semaphore(settings.max_parallel_request)

    def _emit_progress(event: dict) -> None:
        if progress is not None:
            progress(event)

    Log(
        INFO_LOG_LEVEL,
        "stage1 OCR starting",
        {
            "request_id": request_id,
            "slug": slug,
            "useful_pages": page_total,
            "aligned_pdf": str(aligned_pdf_path),
            "max_parallel": settings.max_parallel_request,
        },
    )

    if progress is not None:
        progress(make_event(PHASE_STAGE1_OCR, STATUS_STARTED, page_total=page_total))

    render_source_sha256 = compute_file_sha256(aligned_pdf_path)

    settled, ocr_work = _resolve_stage1_pages(
        sorted_pages,
        useful_pages_enumeration,
        ocr_dir,
        render_dir,
        slug,
        force_recompute=force_recompute,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_progress,
    )
    render_failures = await _render_stage1_pages_sequential(
        ocr_work,
        aligned_pdf_path,
        render_source_sha256,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_progress,
    )
    ocr_outcomes = await _ocr_stage1_pages_parallel(
        ocr_work,
        render_failures,
        engine,
        settings,
        sem,
        request_id=request_id,
        page_total=page_total,
        emit_progress=_emit_progress,
    )
    outcomes = settled + list(render_failures.values()) + ocr_outcomes
    pages, skipped_existing, missing, last_error, total_attempted, failed_count = _aggregate_stage1_outcomes(
        outcomes
    )

    if total_attempted > 0 and failed_count / total_attempted >= 0.5:
        Log(
            ERROR_LOG_LEVEL,
            "stage1 OCR failure threshold exceeded",
            {"request_id": request_id, "failed": failed_count, "attempted": total_attempted, "last_error": last_error},
        )
        if progress is not None:
            progress(make_event(
                PHASE_STAGE1_OCR,
                STATUS_FAILED,
                failed_count=failed_count,
                attempted=total_attempted,
                error=last_error,
            ))
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.OCR_STAGE_FAILED,
                message=f"OCR stage failed on {failed_count}/{total_attempted} pages",
            )
        )

    if progress is not None:
        progress(make_event(
            PHASE_STAGE1_OCR,
            STATUS_COMPLETED,
            pages_written=len(pages),
            skipped_existing=skipped_existing,
            missing_count=len(missing),
            failed_count=failed_count,
        ))

    Log(
        INFO_LOG_LEVEL,
        "stage1 OCR finished",
        {
            "request_id": request_id,
            "pages_written": len(pages),
            "skipped_existing": skipped_existing,
            "missing_mappings": len(missing),
            "failed_count": failed_count,
        },
    )

    return Stage1Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=missing,
        last_error=last_error,
    )

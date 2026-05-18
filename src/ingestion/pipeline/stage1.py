from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

from src.ingestion.pdf_alignment import resolve_aligned_pdf_path_for_stage1
from src.ingestion.pipeline.engine import EasyOCRPageEngine, OCRPageEngine
from src.ingestion.pipeline.render import render_pdf_page_to_png
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
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.models.settings import Settings

_SLUG_MAX = 32


def _slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:_SLUG_MAX].rstrip("-") or "book"


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


def run_stage1_ingest_step(
    enriched: EnrichedIngestRequest,
    pdf_alignment: PdfAlignmentResult | None,
    useful_pages_enumeration: UsefulPagesEnumeration,
    settings: Settings,
    *,
    engine: OCRPageEngine | None = None,
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
) -> Stage1Result:
    Log(INFO_LOG_LEVEL, "stage1 ingest step resolve aligned PDF path begin")
    aligned_path = resolve_aligned_pdf_path_for_stage1(
        enriched,
        pdf_alignment,
        settings.processed_pdf_input_dir,
        page_range_per_thread=settings.page_range_per_thread,
    )
    Log(
        INFO_LOG_LEVEL,
        "stage1 ingest step resolve aligned PDF path done",
        {"path": str(aligned_path)},
    )
    Log(INFO_LOG_LEVEL, "stage1 ingest step acquire OCR engine")
    ocr_engine = engine or EasyOCRPageEngine(gpu=settings.ocr_use_gpu, gpu_device=settings.ocr_gpu_device)
    Log(INFO_LOG_LEVEL, "stage1 ingest step OCR engine ready")
    return run_stage1_ocr(
        aligned_path,
        enriched.source_sha256,
        useful_pages_enumeration,
        settings,
        ocr_engine,
        reicat=enriched.request.reicat,
        force_recompute=force_recompute,
        progress=progress,
    )


def run_stage1_ocr(
    aligned_pdf_path: Path,
    source_sha256: str,
    useful_pages_enumeration: UsefulPagesEnumeration,
    settings: Settings,
    engine: OCRPageEngine,
    *,
    reicat: ReicatMetadata,
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
        {"render_dir": str(render_dir), "ocr_dir": str(ocr_dir)},
    )

    pages: list[Stage1PageResult] = []
    skipped_existing = 0
    missing: list[int] = []
    last_error: str | None = None
    total_attempted = 0
    failed_count = 0

    sorted_pages = sorted(useful_pages_enumeration.useful_original_pages)
    page_total = len(sorted_pages)
    page_index = 0

    Log(
        INFO_LOG_LEVEL,
        "stage1 OCR starting",
        {
            "slug": slug,
            "useful_pages": page_total,
            "aligned_pdf": str(aligned_pdf_path),
        },
    )

    if progress is not None:
        progress(make_event(PHASE_STAGE1_OCR, STATUS_STARTED, page_total=page_total))

    for orig in sorted_pages:
        aligned = useful_pages_enumeration.original_page_to_aligned_page.get(orig)
        page_index += 1
        Log(
            INFO_LOG_LEVEL,
            "stage1 page iteration begin",
            {"original_page": orig, "aligned_page": aligned},
        )
        if aligned is None:
            Log(
                WARNING_LOG_LEVEL,
                "stage1 missing aligned page mapping",
                {"original_page": orig},
            )
            missing.append(orig)
            Log(
                INFO_LOG_LEVEL,
                "stage1 page iteration end",
                {"original_page": orig, "outcome": "missing_aligned_mapping"},
            )
            continue

        txt_path = ocr_dir / f"p.{aligned:04d}.{slug}.txt"

        if not force_recompute and txt_path.is_file() and txt_path.stat().st_size > 0:
            cached_text = txt_path.read_text(encoding="utf-8")
            Log(
                INFO_LOG_LEVEL,
                "stage1 page skip OCR using existing txt",
                {
                    "original_page": orig,
                    "aligned_page": aligned,
                    "txt_path": str(txt_path),
                    "char_count": len(cached_text),
                },
            )
            pages.append(
                Stage1PageResult(
                    aligned_page=aligned,
                    original_page=orig,
                    txt_path=str(txt_path),
                    char_count=len(cached_text),
                )
            )
            skipped_existing += 1
            if progress is not None:
                progress(make_event(
                    PHASE_STAGE1_OCR,
                    STATUS_PAGE_SKIPPED,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=aligned,
                    original_page=orig,
                    char_count=len(cached_text),
                ))
            Log(
                INFO_LOG_LEVEL,
                "stage1 page iteration complete",
                {
                    "original_page": orig,
                    "aligned_page": aligned,
                    "cache_hit": True,
                },
            )
            continue
        png_path = render_dir / f"p.{aligned:04d}.png"
        Log(
            INFO_LOG_LEVEL,
            "stage1 page render PNG begin",
            {
                "original_page": orig,
                "aligned_page": aligned,
                "png_path": str(png_path),
            },
        )
        try:
            render_pdf_page_to_png(aligned_pdf_path, aligned - 1, png_path)
        except Exception as exc:
            last_error = str(exc)
            Log(
                WARNING_LOG_LEVEL,
                "stage1 render page failed",
                {"aligned_page": aligned, "original_page": orig, "error": str(exc)},
            )
            failed_count += 1
            if progress is not None:
                progress(make_event(
                    PHASE_STAGE1_OCR,
                    STATUS_PAGE_FAILED,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=aligned,
                    original_page=orig,
                    error=str(exc),
                    failure="render_failed",
                ))
            Log(
                INFO_LOG_LEVEL,
                "stage1 page iteration end",
                {
                    "original_page": orig,
                    "aligned_page": aligned,
                    "outcome": "render_failed",
                },
            )
            continue

        Log(
            INFO_LOG_LEVEL,
            "stage1 page render PNG done",
            {"original_page": orig, "aligned_page": aligned},
        )
        Log(
            INFO_LOG_LEVEL,
            "stage1 page OCR begin",
            {"original_page": orig, "aligned_page": aligned},
        )
        try:
            text = engine.ocr_page(png_path, lang=settings.ocr_languages)
        except Exception as exc:
            last_error = str(exc)
            Log(
                WARNING_LOG_LEVEL,
                "stage1 OCR page failed",
                {"aligned_page": aligned, "original_page": orig, "error": str(exc)},
            )
            failed_count += 1
            if progress is not None:
                progress(make_event(
                    PHASE_STAGE1_OCR,
                    STATUS_PAGE_FAILED,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=aligned,
                    original_page=orig,
                    error=str(exc),
                    failure="ocr_failed",
                ))
            Log(
                INFO_LOG_LEVEL,
                "stage1 page iteration end",
                {
                    "original_page": orig,
                    "aligned_page": aligned,
                    "outcome": "ocr_failed",
                },
            )
            continue

        Log(
            INFO_LOG_LEVEL,
            "stage1 page OCR done",
            {
                "original_page": orig,
                "aligned_page": aligned,
                "char_count": len(text),
            },
        )
        Log(
            INFO_LOG_LEVEL,
            "stage1 page write txt begin",
            {"txt_path": str(txt_path)},
        )
        txt_path.write_text(text, encoding="utf-8")
        Log(
            INFO_LOG_LEVEL,
            "stage1 page write txt done",
            {"txt_path": str(txt_path), "char_count": len(text)},
        )
        pages.append(
            Stage1PageResult(
                aligned_page=aligned,
                original_page=orig,
                txt_path=str(txt_path),
                char_count=len(text),
            )
        )
        if progress is not None:
            progress(make_event(
                PHASE_STAGE1_OCR,
                STATUS_PAGE_PROGRESS,
                counts_as_step=True,
                page_index=page_index,
                page_total=page_total,
                aligned_page=aligned,
                original_page=orig,
                char_count=len(text),
            ))
        Log(
            INFO_LOG_LEVEL,
            "stage1 page iteration complete",
            {"original_page": orig, "aligned_page": aligned},
        )

    if total_attempted > 0 and failed_count / total_attempted >= 0.5:
        Log(
            ERROR_LOG_LEVEL,
            "stage1 OCR failure threshold exceeded",
            {"failed": failed_count, "attempted": total_attempted, "last_error": last_error},
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

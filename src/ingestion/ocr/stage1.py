from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

from src.ingestion.ocr.engine import EasyOCRPageEngine, OCRPageEngine
from src.ingestion.ocr.render import render_pdf_page_to_png
from src.ingestion.pdf_alignment import resolve_aligned_pdf_path_for_stage1
from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    PdfAlignmentResult,
    ReicatMetadata,
    UsefulPagesEnumeration,
)
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
) -> Stage1Result:
    aligned_path = resolve_aligned_pdf_path_for_stage1(
        enriched,
        pdf_alignment,
        settings.processed_pdf_input_dir,
        page_range_per_thread=settings.page_range_per_thread,
    )
    ocr_engine = engine or EasyOCRPageEngine(gpu=settings.ocr_use_gpu)
    return run_stage1_ocr(
        aligned_path,
        enriched.source_sha256,
        useful_pages_enumeration,
        settings,
        ocr_engine,
        reicat=enriched.request.reicat,
        force_recompute=force_recompute,
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
) -> Stage1Result:
    """reicat is required to derive libro_slug from reicat.title;
    UsefulPagesEnumeration does not carry reicat data."""
    slug = _slugify(reicat.title)
    aligned_pdf_path = Path(aligned_pdf_path)
    data_root = Path(settings.data_root)
    render_dir = data_root / "tmp" / source_sha256 / "render"
    ocr_dir = data_root / "tmp" / source_sha256 / "stage1OCR"
    ocr_dir.mkdir(parents=True, exist_ok=True)

    pages: list[Stage1PageResult] = []
    skipped_existing = 0
    missing: list[int] = []
    last_error: str | None = None
    total_attempted = 0
    failed_count = 0

    for orig in sorted(useful_pages_enumeration.useful_original_pages):
        aligned = useful_pages_enumeration.original_page_to_aligned_page.get(orig)
        if aligned is None:
            missing.append(orig)
            continue

        txt_path = ocr_dir / f"p.{aligned:04d}.{slug}.txt"

        if not force_recompute and txt_path.is_file() and txt_path.stat().st_size > 0:
            pages.append(
                Stage1PageResult(
                    aligned_page=aligned,
                    original_page=orig,
                    txt_path=str(txt_path),
                    char_count=len(txt_path.read_text(encoding="utf-8")),
                )
            )
            skipped_existing += 1
            continue

        total_attempted += 1
        png_path = render_dir / f"p.{aligned:04d}.png"
        try:
            render_pdf_page_to_png(aligned_pdf_path, aligned - 1, png_path)
        except Exception as exc:
            last_error = str(exc)
            failed_count += 1
            continue

        try:
            text = engine.ocr_page(png_path, lang=settings.ocr_languages)
        except Exception as exc:
            last_error = str(exc)
            failed_count += 1
            continue

        txt_path.write_text(text, encoding="utf-8")
        pages.append(
            Stage1PageResult(
                aligned_page=aligned,
                original_page=orig,
                txt_path=str(txt_path),
                char_count=len(text),
            )
        )

    if total_attempted > 0 and failed_count / total_attempted >= 0.5:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.OCR_STAGE_FAILED,
                message=f"OCR stage failed on {failed_count}/{total_attempted} pages",
            ).model_dump_json()
        )

    return Stage1Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=missing,
        last_error=last_error,
    )

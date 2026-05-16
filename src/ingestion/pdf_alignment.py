from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader, PdfWriter

from src.core.hashing import compute_file_sha256
from src.models.request import (
    EnrichedIngestRequest,
    IngestGatePhaseResult,
    IngestInputErrorCode,
    IngestInputValidationError,
    PdfAlignmentResult,
)

DEFAULT_PAGE_RANGE_PER_THREAD = 10


def _alignment_chunk_specs(
    original_page_count: int, page_range_per_thread: int
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = 0
    while start < original_page_count:
        end = min(start + page_range_per_thread, original_page_count)
        out.append((start, end))
        start = end
    return out


def _write_aligned_pdf_chunk(
    bundle: tuple[str, int, int, tuple[int, ...], int, str],
) -> tuple[int, str | None]:
    (
        source_path_str,
        chunk_start_zero,
        chunk_end_zero,
        removed_sorted,
        chunk_order,
        tmp_root_str,
    ) = bundle
    removed = frozenset(removed_sorted)
    reader = PdfReader(source_path_str, strict=False)
    writer = PdfWriter()
    for zi in range(chunk_start_zero, chunk_end_zero):
        if zi >= len(reader.pages):
            break
        if (zi + 1) not in removed:
            writer.add_page(reader.pages[zi])

    kept = len(writer.pages)
    if kept == 0:
        return chunk_order, None

    chunk_file = Path(tmp_root_str) / f"aligned_chunk_{chunk_order}.pdf"
    with chunk_file.open("wb") as sink:
        writer.write(sink)
    return chunk_order, str(chunk_file)


def _merge_chunk_pdf_paths(chunk_paths_ordered: list[str], target_path: Path) -> None:
    merger = PdfWriter()
    for path_str in chunk_paths_ordered:
        sub_reader = PdfReader(path_str, strict=False)
        for page_index in range(len(sub_reader.pages)):
            merger.add_page(sub_reader.pages[page_index])

    try:
        with target_path.open("wb") as sink:
            merger.write(sink)
    except OSError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message=f"unable to write aligned pdf: {exc}",
                field=None,
            ).model_dump_json()
        ) from exc


def build_page_removal_mapping(
    original_page_count: int, pages_to_remove: list[int]
) -> tuple[int, dict[int, int], dict[int, int]]:
    removed = set(pages_to_remove)
    original_to_aligned: dict[int, int] = {}
    aligned_to_original: dict[int, int] = {}
    aligned_index = 0
    for original in range(1, original_page_count + 1):
        if original in removed:
            continue
        aligned_index += 1
        original_to_aligned[original] = aligned_index
        aligned_to_original[aligned_index] = original
    aligned_count = aligned_index
    return aligned_count, original_to_aligned, aligned_to_original


def build_aligned_pdf(
    enriched: EnrichedIngestRequest,
    processed_pdf_dir: str,
    *,
    page_range_per_thread: int = DEFAULT_PAGE_RANGE_PER_THREAD,
) -> PdfAlignmentResult:
    if page_range_per_thread < 1:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message="page_range_per_thread must be >= 1",
                field=None,
            ).model_dump_json()
        )

    digest = enriched.source_sha256.strip().lower()
    source_path = Path(enriched.source_pdf_path)
    try:
        observed = compute_file_sha256(source_path)
    except OSError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path is not readable for pdf alignment",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc
    if observed != digest:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.SOURCE_DIGEST_MISMATCH,
                message="source pdf changed after enrichment digest was recorded",
                field="source_pdf_path",
            ).model_dump_json()
        )

    try:
        reader = PdfReader(str(source_path), strict=False)
    except Exception as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message="unable to open source pdf for alignment",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc

    original_page_count = len(reader.pages)
    if original_page_count < 1:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message="source pdf has no pages for alignment",
                field="source_pdf_path",
            ).model_dump_json()
        )

    pages_to_remove = enriched.request.pages_to_remove
    removed = set(pages_to_remove)
    for page in removed:
        if page > original_page_count:
            raise ValueError(
                IngestInputValidationError(
                    code=IngestInputErrorCode.PAGES_INVALID,
                    message=(
                        f"page {page} in pages_to_remove exceeds pdf "
                        f"page count ({original_page_count})"
                    ),
                    field="pages_to_remove",
                ).model_dump_json()
            )

    aligned_count, original_to_aligned, aligned_to_original = build_page_removal_mapping(
        original_page_count, pages_to_remove
    )

    target_dir = Path(processed_pdf_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{digest}.pdf"

    chunk_specs = _alignment_chunk_specs(original_page_count, page_range_per_thread)
    removed_sorted = tuple(sorted(set(pages_to_remove)))

    if len(chunk_specs) == 1:
        writer = PdfWriter()
        cs, ce = chunk_specs[0]
        for zero_index in range(cs, ce):
            if (zero_index + 1) not in removed:
                writer.add_page(reader.pages[zero_index])
        try:
            with target_path.open("wb") as sink:
                writer.write(sink)
        except OSError as exc:
            raise ValueError(
                IngestInputValidationError(
                    code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                    message=f"unable to write aligned pdf: {exc}",
                    field=None,
                ).model_dump_json()
            ) from exc
    else:
        del reader
        resolved_source = str(source_path.resolve())

        worker_cap = os.cpu_count() or 4
        max_workers = max(1, min(len(chunk_specs), worker_cap))
        with TemporaryDirectory(prefix="pdfalign_") as tmp_root:
            tmp_root_abs = Path(tmp_root).resolve()

            args_list = [
                (
                    resolved_source,
                    cs,
                    ce,
                    removed_sorted,
                    chunk_order,
                    str(tmp_root_abs),
                )
                for chunk_order, (cs, ce) in enumerate(chunk_specs)
            ]

            try:
                with ProcessPoolExecutor(max_workers=max_workers) as executor:
                    chunk_results = list(executor.map(_write_aligned_pdf_chunk, args_list))
            except Exception as exc:
                raise ValueError(
                    IngestInputValidationError(
                        code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                        message=f"unable to build aligned pdf chunks: {exc}",
                        field=None,
                    ).model_dump_json()
                ) from exc

            chunk_results_sorted = sorted(chunk_results, key=lambda item: item[0])
            ordered_paths = [path for _, path in chunk_results_sorted if path is not None]

            if not ordered_paths:
                raise ValueError(
                    IngestInputValidationError(
                        code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                        message="aligned pdf has no kept pages across chunks",
                        field=None,
                    ).model_dump_json()
                )

            _merge_chunk_pdf_paths(ordered_paths, target_path)

    try:
        check_reader = PdfReader(str(target_path), strict=False)
    except Exception as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message="aligned pdf was written but is not readable",
                field=None,
            ).model_dump_json()
        ) from exc

    written_pages = len(check_reader.pages)
    if written_pages != aligned_count:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_ALIGNMENT_FAILED,
                message=(
                    "aligned pdf page count mismatch "
                    f"(expected {aligned_count}, got {written_pages})"
                ),
                field=None,
            ).model_dump_json()
        )

    return PdfAlignmentResult(
        aligned_pdf_path=str(target_path.resolve()),
        source_sha256=digest,
        original_page_count=original_page_count,
        aligned_page_count=aligned_count,
        original_page_to_aligned_page=original_to_aligned,
        aligned_page_to_original_page=aligned_to_original,
    )


def maybe_run_pdf_alignment(
    enriched: EnrichedIngestRequest,
    gate_phase: IngestGatePhaseResult,
    processed_pdf_dir: str,
    *,
    page_range_per_thread: int = DEFAULT_PAGE_RANGE_PER_THREAD,
) -> PdfAlignmentResult | None:
    if gate_phase.pipeline_skipped:
        return None
    return build_aligned_pdf(
        enriched,
        processed_pdf_dir,
        page_range_per_thread=page_range_per_thread,
    )


def resolve_aligned_pdf_path_for_stage1(
    enriched: EnrichedIngestRequest,
    pdf_alignment: PdfAlignmentResult | None,
    processed_pdf_dir: str,
    *,
    page_range_per_thread: int = DEFAULT_PAGE_RANGE_PER_THREAD,
) -> Path:
    if pdf_alignment is not None:
        return Path(pdf_alignment.aligned_pdf_path)
    digest = enriched.source_sha256.strip().lower()
    candidate = Path(processed_pdf_dir) / f"{digest}.pdf"
    if candidate.is_file():
        return candidate
    built = build_aligned_pdf(
        enriched,
        processed_pdf_dir,
        page_range_per_thread=page_range_per_thread,
    )
    return Path(built.aligned_pdf_path)

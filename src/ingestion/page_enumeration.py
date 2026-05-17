from __future__ import annotations

from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.core.log import INFO_LOG_LEVEL, Log
from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    PageRange,
    PdfAlignmentResult,
    UsefulPagesEnumeration,
)


def _project_interval_to_aligned(
    original_range: PageRange, original_to_aligned: dict[int, int]
) -> PageRange:
    try:
        start_aligned = original_to_aligned[original_range.start]
        end_aligned = original_to_aligned[original_range.end]
    except KeyError as exc:
        missing = exc.args[0]
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGES_INVALID,
                message=(
                    f"cannot map original page {missing} to aligned space "
                    "(page missing from useful-page map)"
                ),
                field="payload",
            ).model_dump_json()
        ) from exc
    return PageRange(start=start_aligned, end=end_aligned)


def _enforce_alignment_maps_equal(
    analytic_o2a: dict[int, int],
    analytic_a2o: dict[int, int],
    alignment: PdfAlignmentResult,
) -> None:
    if alignment.original_page_to_aligned_page != analytic_o2a:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH,
                message="pdf alignment forward map differs from analytic enumeration",
                field=None,
            ).model_dump_json()
        )
    if alignment.aligned_page_to_original_page != analytic_a2o:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH,
                message="pdf alignment reverse map differs from analytic enumeration",
                field=None,
            ).model_dump_json()
        )
    if alignment.aligned_page_count != len(analytic_a2o):
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH,
                message="pdf alignment aligned_page_count mismatches enumerated map size",
                field=None,
            ).model_dump_json()
        )


def build_useful_pages_enumeration(
    enriched: EnrichedIngestRequest,
    alignment: PdfAlignmentResult | None,
) -> UsefulPagesEnumeration:
    normalized_digest = enriched.source_sha256.strip().lower()
    original_total = enriched.source_pdf_page_count
    removal_list = enriched.request.pages_to_remove

    aligned_total, analytic_o2a, analytic_a2o = build_page_removal_mapping(
        original_total, removal_list
    )

    if alignment is not None:
        if normalized_digest != alignment.source_sha256.strip().lower():
            raise ValueError(
                IngestInputValidationError(
                    code=IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH,
                    message="pdf alignment digest disagrees with enriched digest",
                    field=None,
                ).model_dump_json()
            )
        if alignment.original_page_count != original_total:
            raise ValueError(
                IngestInputValidationError(
                    code=IngestInputErrorCode.PAGE_ENUMERATION_MISMATCH,
                    message=(
                        "pdf alignment original_page_count disagrees "
                        "with enrichment page count"
                    ),
                    field=None,
                ).model_dump_json()
            )
        _enforce_alignment_maps_equal(analytic_o2a, analytic_a2o, alignment)

    toc_aligned = _project_interval_to_aligned(
        enriched.request.toc_range, analytic_o2a
    )
    index_aligned = _project_interval_to_aligned(
        enriched.request.index_range, analytic_o2a
    )

    useful_original_sorted = sorted(analytic_o2a.keys())

    result = UsefulPagesEnumeration(
        source_sha256=normalized_digest,
        original_page_count=original_total,
        aligned_page_count=aligned_total,
        useful_original_pages=useful_original_sorted,
        original_page_to_aligned_page=analytic_o2a,
        aligned_page_to_original_page=analytic_a2o,
        toc_range_aligned=toc_aligned,
        index_range_aligned=index_aligned,
    )
    Log(
        INFO_LOG_LEVEL,
        "useful pages enumeration built",
        {
            "source_sha256": normalized_digest[:16],
            "useful_pages": len(useful_original_sorted),
            "aligned_total": aligned_total,
        },
    )
    return result

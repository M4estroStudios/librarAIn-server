from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from src.core.hashing import compute_file_sha256
from src.core.log import INFO_LOG_LEVEL, Log
from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    IngestRequest,
)


_compute_file_sha256 = compute_file_sha256


def _count_pdf_pages(file_path: Path) -> int:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(file_path), strict=False)
    except Exception as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="unable to read pdf for page count verification",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc
    num_pages = len(reader.pages)
    if num_pages < 1:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="pdf has no pages",
                field="source_pdf_path",
            ).model_dump_json()
        )
    return num_pages


def _validate_page_refs_within_pdf(request: IngestRequest, pdf_page_count: int) -> None:
    for page in request.pages_to_remove:
        if page > pdf_page_count:
            raise ValueError(
                IngestInputValidationError(
                    code=IngestInputErrorCode.PAGES_INVALID,
                    message=(
                        f"page {page} in pages_to_remove exceeds pdf "
                        f"page count ({pdf_page_count})"
                    ),
                    field="pages_to_remove",
                ).model_dump_json()
            )
    toc = request.toc_range
    if toc.start > pdf_page_count:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGES_INVALID,
                message=(
                    f"toc_range start {toc.start} exceeds pdf page count ({pdf_page_count})"
                ),
                field="toc_range",
            ).model_dump_json()
        )
    if toc.end > pdf_page_count:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGES_INVALID,
                message=(
                    f"toc_range end {toc.end} exceeds pdf page count ({pdf_page_count})"
                ),
                field="toc_range",
            ).model_dump_json()
        )
    ir = request.index_range
    if ir.start > pdf_page_count:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGES_INVALID,
                message=(
                    f"index_range start {ir.start} exceeds pdf "
                    f"page count ({pdf_page_count})"
                ),
                field="index_range",
            ).model_dump_json()
        )
    if ir.end > pdf_page_count:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PAGES_INVALID,
                message=(
                    f"index_range end {ir.end} exceeds pdf "
                    f"page count ({pdf_page_count})"
                ),
                field="index_range",
            ).model_dump_json()
        )


def validate_and_enrich_request(payload: dict) -> EnrichedIngestRequest:
    Log(
        INFO_LOG_LEVEL,
        "ingest request validation starting",
        {"source_pdf_path": str(payload.get("source_pdf_path", ""))[:120]},
    )
    try:
        request = IngestRequest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.INPUT_SCHEMA_INVALID,
                message="input payload does not match IngestRequest schema",
                field="payload",
            ).model_dump_json()
        ) from exc
    Log(INFO_LOG_LEVEL, "ingest validation IngestRequest model_validate done")

    Log(INFO_LOG_LEVEL, "ingest validation check source path exists begin")
    source_path = Path(request.source_pdf_path).expanduser()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path does not point to an existing file",
                field="source_pdf_path",
            ).model_dump_json()
        )

    Log(INFO_LOG_LEVEL, "ingest validation source path exists")
    try:
        source_sha256 = compute_file_sha256(source_path)
    except OSError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path is not readable",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc

    Log(INFO_LOG_LEVEL, "ingest validation source sha256 computed")

    Log(INFO_LOG_LEVEL, "ingest validation count PDF pages begin")
    pdf_page_count = _count_pdf_pages(source_path)
    Log(
        INFO_LOG_LEVEL,
        "ingest validation count PDF pages done",
        {"pdf_pages": pdf_page_count},
    )
    Log(INFO_LOG_LEVEL, "ingest validation page refs within PDF begin")
    _validate_page_refs_within_pdf(request, pdf_page_count)
    Log(INFO_LOG_LEVEL, "ingest validation page refs within PDF done")

    Log(
        INFO_LOG_LEVEL,
        "ingest request validation completed",
        {"source_sha256": source_sha256[:16], "pdf_pages": pdf_page_count},
    )

    return EnrichedIngestRequest(
        request=request,
        source_sha256=source_sha256,
        source_pdf_path=str(source_path),
        source_pdf_page_count=pdf_page_count,
    )

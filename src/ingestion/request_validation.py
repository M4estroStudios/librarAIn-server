from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import ValidationError

from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    IngestRequest,
)


def _compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as source_file:
        while True:
            chunk = source_file.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_and_enrich_request(payload: dict) -> EnrichedIngestRequest:
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

    source_path = Path(request.source_pdf_path).expanduser()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path does not point to an existing file",
                field="source_pdf_path",
            ).model_dump_json()
        )

    try:
        source_sha256 = _compute_file_sha256(source_path)
    except OSError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path is not readable",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc

    return EnrichedIngestRequest(
        request=request,
        source_sha256=source_sha256,
        source_pdf_path=str(source_path),
    )

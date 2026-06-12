from __future__ import annotations

from pydantic import ValidationError

from src.core.log import INFO_LOG_LEVEL, Log
from src.search.request_schema import (
    ResearchInputErrorCode,
    ResearchInputValidationError,
    ResearchRequest,
)


def _first_error_field(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "payload"
    loc = errors[0].get("loc") or ()
    if not loc:
        return "payload"
    return ".".join(str(part) for part in loc)


def validate_research_request(payload: dict) -> ResearchRequest:
    preview = str(payload.get("query", ""))[:80]
    Log(
        INFO_LOG_LEVEL,
        "research request validation starting",
        {"query_preview": preview},
    )
    try:
        request = ResearchRequest.model_validate(payload)
    except ValidationError as exc:
        field = _first_error_field(exc)
        code = ResearchInputErrorCode.INPUT_SCHEMA_INVALID
        if field == "query" or field.startswith("query"):
            code = ResearchInputErrorCode.QUERY_INVALID
        elif field.startswith("poh"):
            code = ResearchInputErrorCode.POH_INVALID
        elif field.startswith("options"):
            code = ResearchInputErrorCode.OPTIONS_INVALID
        raise ValueError(
            ResearchInputValidationError(
                code=code,
                message="input payload does not match ResearchRequest schema",
                field=field,
            ).model_dump_json()
        ) from exc

    Log(
        INFO_LOG_LEVEL,
        "research request validation completed",
        {
            "query_length": len(request.query),
            "has_poh": request.poh is not None,
            "max_books": request.options.max_books,
            "max_pages_per_book": request.options.max_pages_per_book,
            "dedup": request.options.dedup,
        },
    )
    return request

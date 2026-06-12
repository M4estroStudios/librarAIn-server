from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

QUERY_MIN_LENGTH = 3
QUERY_MAX_LENGTH = 2000
DEFAULT_MAX_BOOKS = 5
DEFAULT_MAX_PAGES_PER_BOOK = 8
MAX_BOOKS_CAP = 50
MAX_PAGES_PER_BOOK_CAP = 100


class ResearchInputErrorCode(str, Enum):
    INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
    QUERY_INVALID = "QUERY_INVALID"
    POH_INVALID = "POH_INVALID"
    OPTIONS_INVALID = "OPTIONS_INVALID"


class ResearchInputValidationError(BaseModel):
    code: ResearchInputErrorCode
    message: str
    field: str | None = None


class ResearchInputValidationException(ValueError):
    def __init__(self, detail: ResearchInputValidationError) -> None:
        self.detail = detail
        super().__init__(detail.model_dump_json())


def _validation_error(
    *,
    code: ResearchInputErrorCode,
    message: str,
    field: str | None = None,
) -> ValueError:
    return ValueError(
        ResearchInputValidationError(
            code=code,
            message=message,
            field=field,
        ).model_dump_json()
    )


class ResearchPoh(BaseModel):
    id: str | None = None
    label: str = Field(min_length=1)
    time_range: str | None = None

    @field_validator("id", "time_range", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("label", mode="before")
    @classmethod
    def strip_label(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def validate_label(self) -> ResearchPoh:
        if not self.label:
            raise _validation_error(
                code=ResearchInputErrorCode.POH_INVALID,
                message="poh.label must be a non-empty string",
                field="poh.label",
            )
        return self


class ResearchOptions(BaseModel):
    max_books: int = Field(default=DEFAULT_MAX_BOOKS, ge=1, le=MAX_BOOKS_CAP)
    max_pages_per_book: int = Field(
        default=DEFAULT_MAX_PAGES_PER_BOOK,
        ge=1,
        le=MAX_PAGES_PER_BOOK_CAP,
    )
    dedup: bool = True


class ResearchRequest(BaseModel):
    query: str = Field(min_length=QUERY_MIN_LENGTH, max_length=QUERY_MAX_LENGTH)
    poh: ResearchPoh | None = None
    options: ResearchOptions = Field(default_factory=ResearchOptions)

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: object) -> object:
        if value is None:
            return value
        return str(value).strip()

    @model_validator(mode="after")
    def validate_query_bounds(self) -> ResearchRequest:
        if len(self.query) < QUERY_MIN_LENGTH:
            raise _validation_error(
                code=ResearchInputErrorCode.QUERY_INVALID,
                message=(
                    f"query must be between {QUERY_MIN_LENGTH} and "
                    f"{QUERY_MAX_LENGTH} characters after trimming"
                ),
                field="query",
            )
        if len(self.query) > QUERY_MAX_LENGTH:
            raise _validation_error(
                code=ResearchInputErrorCode.QUERY_INVALID,
                message=(
                    f"query must be between {QUERY_MIN_LENGTH} and "
                    f"{QUERY_MAX_LENGTH} characters after trimming"
                ),
                field="query",
            )
        return self

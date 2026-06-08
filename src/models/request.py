from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngestInputErrorCode(str, Enum):
    INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
    PDF_NOT_FOUND = "PDF_NOT_FOUND"
    SOURCE_DIGEST_MISMATCH = "SOURCE_DIGEST_MISMATCH"
    RANGE_INVALID = "RANGE_INVALID"
    PAGES_INVALID = "PAGES_INVALID"
    RANGE_INTERSECTS_REMOVED_PAGES = "RANGE_INTERSECTS_REMOVED_PAGES"
    REICAT_MISSING_REQUIRED_FIELDS = "REICAT_MISSING_REQUIRED_FIELDS"
    PDF_ALIGNMENT_FAILED = "PDF_ALIGNMENT_FAILED"
    PAGE_ENUMERATION_MISMATCH = "PAGE_ENUMERATION_MISMATCH"
    OCR_STAGE_FAILED = "OCR_STAGE_FAILED"


class SourceHashGateStatus(str, Enum):
    NEW_HASH = "new_hash"
    DUPLICATE_SOURCE_HASH = "duplicate_source_hash"
    ALREADY_PROCESSED = "already_processed"


class IngestInputValidationError(BaseModel):
    code: IngestInputErrorCode
    message: str
    field: str | None = None


class IngestInputValidationException(ValueError):
    def __init__(self, detail: IngestInputValidationError) -> None:
        self.detail = detail
        super().__init__(detail.model_dump_json())


class PageRange(BaseModel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> "PageRange":
        if self.start > self.end:
            raise ValueError("range start must be <= end")
        return self

    def as_set(self) -> set[int]:
        return set(range(self.start, self.end + 1))


class ReicatMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(min_length=1, alias="titolo")
    subtitle: str | None = Field(default=None, alias="sottotitolo")
    title_complements: str | None = Field(default=None, alias="complementi_del_titolo")
    authors: list[str] = Field(min_length=1, alias="autore")
    editors: list[str] | None = Field(default=None, alias="curatore")
    translators: list[str] | None = Field(default=None, alias="traduttore")
    edition_number: str | None = Field(default=None, alias="numero_edizione")
    publication_year: int | None = Field(default=None, alias="anno_di_pubblicazione")
    publication_type: str | None = Field(default=None, alias="tipo_di_pubblicazione")
    publication_place: str | None = Field(default=None, alias="luogo_di_pubblicazione")
    publisher: str | None = Field(default=None, alias="editore")
    page_count: int | None = Field(default=None, ge=1, alias="numero_pagine")
    series_title: str | None = Field(default=None, alias="titolo_collana")
    series_number: str | None = Field(default=None, alias="numero_nella_collana")
    isbn: str | None = None

    @model_validator(mode="after")
    def validate_reicat_fields(self) -> "ReicatMetadata":
        cleaned_authors = [author.strip() for author in self.authors if author.strip()]
        if not cleaned_authors:
            raise ValueError("at least one non-empty author is required")
        self.authors = cleaned_authors
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("title must be non-empty")

        if self.subtitle is not None:
            self.subtitle = self.subtitle.strip() or None
        if self.title_complements is not None:
            self.title_complements = self.title_complements.strip() or None
        if self.editors is not None:
            cleaned_editors = [name.strip() for name in self.editors if name.strip()]
            self.editors = cleaned_editors or None
        if self.translators is not None:
            cleaned_translators = [name.strip() for name in self.translators if name.strip()]
            self.translators = cleaned_translators or None
        if self.edition_number is not None:
            self.edition_number = self.edition_number.strip() or None
        if self.publication_type is not None:
            self.publication_type = self.publication_type.strip() or None
        if self.publication_place is not None:
            self.publication_place = self.publication_place.strip() or None
        if self.publisher is not None:
            self.publisher = self.publisher.strip() or None
        if self.series_title is not None:
            self.series_title = self.series_title.strip() or None
        if self.series_number is not None:
            self.series_number = self.series_number.strip() or None
        if self.isbn is not None:
            self.isbn = self.isbn.strip() or None
        return self


class IngestOptions(BaseModel):
    force_metadata_update_on_duplicate_hash: bool = True


class IngestRequest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    source_pdf_path: str = Field(min_length=1)
    book_id_hint: str | None = None
    notes: str | None = None
    pages_to_remove: list[int]
    toc_range: PageRange
    index_range: PageRange
    reicat: ReicatMetadata
    options: IngestOptions = Field(default_factory=IngestOptions)

    @model_validator(mode="after")
    def normalize_and_validate(self) -> "IngestRequest":
        self.source_pdf_path = self.source_pdf_path.strip()
        if not self.source_pdf_path:
            raise ValueError("source_pdf_path must be non-empty")
        if self.book_id_hint is not None:
            self.book_id_hint = self.book_id_hint.strip() or None
        if self.notes is not None:
            self.notes = self.notes.strip() or None

        normalized_pages = sorted(set(self.pages_to_remove))
        if any(page < 1 for page in normalized_pages):
            raise ValueError("pages_to_remove must contain only positive 1-based pages")
        self.pages_to_remove = normalized_pages

        removed_pages = set(self.pages_to_remove)
        toc_overlap = removed_pages.intersection(self.toc_range.as_set())
        if toc_overlap:
            raise ValueError("pages_to_remove intersects toc_range")
        index_overlap = removed_pages.intersection(self.index_range.as_set())
        if index_overlap:
            raise ValueError("pages_to_remove intersects index_range")
        return self


class EnrichedIngestRequest(BaseModel):
    request: IngestRequest
    source_sha256: str
    source_pdf_path: str
    source_pdf_page_count: int


class SourceHashGateResult(BaseModel):
    status: SourceHashGateStatus
    source_sha256: str
    should_skip_pipeline: bool


class BookUpsertResult(BaseModel):
    source_sha256: str
    was_inserted: bool
    metadata_audit_row_id: int


class IngestGatePhaseResult(BaseModel):
    gate: SourceHashGateResult
    pipeline_skipped: bool
    book_upsert: BookUpsertResult | None = None
    duplicate_skip_audit_row_id: int | None = None


class PdfAlignmentResult(BaseModel):
    aligned_pdf_path: str
    source_sha256: str
    original_page_count: int
    aligned_page_count: int
    original_page_to_aligned_page: dict[int, int]
    aligned_page_to_original_page: dict[int, int]


class UsefulPagesEnumeration(BaseModel):
    source_sha256: str
    original_page_count: int
    aligned_page_count: int
    useful_original_pages: list[int]
    original_page_to_aligned_page: dict[int, int]
    aligned_page_to_original_page: dict[int, int]
    toc_range_aligned: PageRange
    index_range_aligned: PageRange

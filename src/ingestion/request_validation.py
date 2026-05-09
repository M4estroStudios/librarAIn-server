from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from src.models.request import (
    EnrichedIngestRequest,
    IngestInputErrorCode,
    IngestInputValidationError,
    IngestRequest,
    SourceHashGateResult,
    SourceHashGateStatus,
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

    pdf_page_count = _count_pdf_pages(source_path)
    _validate_page_refs_within_pdf(request, pdf_page_count)

    return EnrichedIngestRequest(
        request=request,
        source_sha256=source_sha256,
        source_pdf_path=str(source_path),
    )


def _validate_source_sha256(source_sha256: str) -> str:
    normalized = source_sha256.strip().lower()
    if len(normalized) != 64:
        raise ValueError("source_sha256 must be a 64-char hex digest")
    hex_chars = set("0123456789abcdef")
    if any(char not in hex_chars for char in normalized):
        raise ValueError("source_sha256 must be a valid hex digest")
    return normalized


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_books_schema(sqlite_path: str) -> None:
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS books (
                    source_sha256 TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    title TEXT NOT NULL,
                    subtitle TEXT,
                    authors_json TEXT NOT NULL,
                    publisher TEXT,
                    publication_year INTEGER,
                    isbn TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_error TEXT
                )
                """
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to initialize books schema") from exc


def insert_book_minimal(
    sqlite_path: str,
    source_sha256: str,
    schema_version: str,
    title: str,
    authors_json: str,
    subtitle: str | None = None,
    publisher: str | None = None,
    publication_year: int | None = None,
    isbn: str | None = None,
    last_error: str | None = None,
) -> None:
    digest = _validate_source_sha256(source_sha256)
    now_iso = _utc_now_iso()
    try:
        with sqlite3.connect(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO books (
                    source_sha256,
                    schema_version,
                    title,
                    subtitle,
                    authors_json,
                    publisher,
                    publication_year,
                    isbn,
                    created_at,
                    updated_at,
                    last_seen_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    schema_version,
                    title,
                    subtitle,
                    authors_json,
                    publisher,
                    publication_year,
                    isbn,
                    now_iso,
                    now_iso,
                    now_iso,
                    last_error,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise RuntimeError("book with source_sha256 already exists") from exc
    except sqlite3.Error as exc:
        raise RuntimeError("unable to insert minimal book row") from exc


def source_hash_gate(source_sha256: str, sqlite_path: str) -> SourceHashGateResult:
    digest = _validate_source_sha256(source_sha256)
    try:
        with sqlite3.connect(sqlite_path) as conn:
            row = conn.execute(
                "SELECT source_sha256 FROM books WHERE source_sha256 = ?",
                (digest,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to read source hash from sqlite") from exc

    if row is not None:
        return SourceHashGateResult(
            status=SourceHashGateStatus.DUPLICATE_SOURCE_HASH,
            source_sha256=digest,
            should_skip_pipeline=True,
        )
    return SourceHashGateResult(
        status=SourceHashGateStatus.NEW_HASH,
        source_sha256=digest,
        should_skip_pipeline=False,
    )

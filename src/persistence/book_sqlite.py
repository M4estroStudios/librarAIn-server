from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.hashing import compute_file_sha256
from src.persistence.pipeline_runs import (
    _sqlite_connection,
    create_pipeline_run,
    ensure_pipeline_runs_table,
    get_pipeline_run_by_request_id,
    mark_pipeline_run_finished,
    reicat_alias_snapshot,
    reicat_alias_snapshot_from_row,
)
from src.persistence.subject_matcher_sqlite import ensure_subject_matcher_tables
from src.models.request import (
    BookUpsertResult,
    EnrichedIngestRequest,
    IngestGatePhaseResult,
    IngestInputErrorCode,
    IngestInputValidationError,
    SourceHashGateResult,
    SourceHashGateStatus,
)

_MIGRATIONS: list[tuple[str, str]] = [
    ("001", "initial schema baseline"),
    ("003", "pipeline_runs table"),
    ("004", "subject_embeddings and subject_match_audit tables"),
]

_BOOK_OPTIONAL_DDL: list[tuple[str, str]] = [
    ("title_complements", "TEXT"),
    ("editors_json", "TEXT"),
    ("translators_json", "TEXT"),
    ("edition_number", "TEXT"),
    ("publication_type", "TEXT"),
    ("publication_place", "TEXT"),
    ("page_count", "INTEGER"),
    ("series_title", "TEXT"),
    ("series_number", "TEXT"),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_source_sha256(source_sha256: str) -> str:
    normalized = source_sha256.strip().lower()
    if len(normalized) != 64:
        raise ValueError("source_sha256 must be a 64-char hex digest")
    hex_chars = set("0123456789abcdef")
    if any(char not in hex_chars for char in normalized):
        raise ValueError("source_sha256 must be a valid hex digest")
    return normalized


def _existing_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _apply_pending_migrations(conn: sqlite3.Connection) -> None:
    applied = {row[0] for row in conn.execute("SELECT id FROM _schema_migrations").fetchall()}
    for mid, desc in _MIGRATIONS:
        if mid not in applied:
            conn.execute(
                "INSERT INTO _schema_migrations (id, applied_at, description) VALUES (?, ?, ?)",
                (mid, _utc_now_iso(), desc),
            )


def _ensure_books_legacy_columns(conn: sqlite3.Connection) -> None:
    present = _existing_table_columns(conn, "books")
    for column_name, column_type in _BOOK_OPTIONAL_DDL:
        if column_name not in present:
            conn.execute(f'ALTER TABLE books ADD COLUMN {column_name} {column_type}')


def init_books_schema(sqlite_path: str) -> None:
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
                source_sha256 TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT,
                title_complements TEXT,
                authors_json TEXT NOT NULL,
                editors_json TEXT,
                translators_json TEXT,
                edition_number TEXT,
                publication_year INTEGER,
                publication_type TEXT,
                publication_place TEXT,
                publisher TEXT,
                page_count INTEGER,
                series_title TEXT,
                series_number TEXT,
                isbn TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )
        _ensure_books_legacy_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_metadata_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_sha256 TEXT NOT NULL,
                event_at TEXT NOT NULL,
                operation TEXT NOT NULL,
                prior_snapshot_json TEXT,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        ensure_pipeline_runs_table(conn)
        ensure_subject_matcher_tables(conn)
        _apply_pending_migrations(conn)
        conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to initialize books schema") from exc
    finally:
        conn.close()


def verify_source_pdf_digest_matches(enriched: EnrichedIngestRequest) -> None:
    pdf_path = Path(enriched.source_pdf_path)
    try:
        current_hex = compute_file_sha256(pdf_path).lower()
    except OSError as exc:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.PDF_NOT_FOUND,
                message="source_pdf_path is not readable for digest verification",
                field="source_pdf_path",
            ).model_dump_json()
        ) from exc
    expected = enriched.source_sha256.strip().lower()
    if current_hex != expected:
        raise ValueError(
            IngestInputValidationError(
                code=IngestInputErrorCode.SOURCE_DIGEST_MISMATCH,
                message=(
                    "source_pdf_path bytes changed since validation; "
                    "digest no longer matches"
                ),
                field="source_pdf_path",
            ).model_dump_json()
        )


def upsert_book_reicat(
    enriched: EnrichedIngestRequest,
    sqlite_path: str,
    *,
    last_error: str | None = None,
    skip_digest_verification: bool = False,
) -> BookUpsertResult:
    init_books_schema(sqlite_path)
    if not skip_digest_verification:
        verify_source_pdf_digest_matches(enriched)
    digest = _validate_source_sha256(enriched.source_sha256)
    request = enriched.request
    meta = request.reicat
    now_iso = _utc_now_iso()
    authors_json = json.dumps(meta.authors, ensure_ascii=False)
    editors_json = (
        json.dumps(meta.editors, ensure_ascii=False) if meta.editors else None
    )
    translators_json = (
        json.dumps(meta.translators, ensure_ascii=False) if meta.translators else None
    )
    new_snapshot = reicat_alias_snapshot(meta)
    new_snapshot_json = json.dumps(new_snapshot, ensure_ascii=False)
    insert_params = (
        digest,
        request.schema_version,
        meta.title,
        meta.subtitle,
        meta.title_complements,
        authors_json,
        editors_json,
        translators_json,
        meta.edition_number,
        meta.publication_year,
        meta.publication_type,
        meta.publication_place,
        meta.publisher,
        meta.page_count,
        meta.series_title,
        meta.series_number,
        meta.isbn,
        now_iso,
        now_iso,
        now_iso,
        last_error,
    )
    upsert_sql = """
        INSERT INTO books (
            source_sha256,
            schema_version,
            title,
            subtitle,
            title_complements,
            authors_json,
            editors_json,
            translators_json,
            edition_number,
            publication_year,
            publication_type,
            publication_place,
            publisher,
            page_count,
            series_title,
            series_number,
            isbn,
            created_at,
            updated_at,
            last_seen_at,
            last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_sha256) DO UPDATE SET
            schema_version = excluded.schema_version,
            title = excluded.title,
            subtitle = excluded.subtitle,
            title_complements = excluded.title_complements,
            authors_json = excluded.authors_json,
            editors_json = excluded.editors_json,
            translators_json = excluded.translators_json,
            edition_number = excluded.edition_number,
            publication_year = excluded.publication_year,
            publication_type = excluded.publication_type,
            publication_place = excluded.publication_place,
            publisher = excluded.publisher,
            page_count = excluded.page_count,
            series_title = excluded.series_title,
            series_number = excluded.series_number,
            isbn = excluded.isbn,
            updated_at = excluded.updated_at,
            last_seen_at = excluded.last_seen_at,
            last_error = excluded.last_error
    """
    audit_sql = """
        INSERT INTO book_metadata_audit (
            source_sha256,
            event_at,
            operation,
            prior_snapshot_json,
            snapshot_json
        ) VALUES (?, ?, ?, ?, ?)
    """
    try:
        db_path = Path(sqlite_path)
        with _sqlite_connection(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            prior_row = conn.execute(
                "SELECT * FROM books WHERE source_sha256 = ?", (digest,)
            ).fetchone()
            conn.execute(upsert_sql, insert_params)
            was_inserted = prior_row is None
            prior_snap_json_text = (
                None
                if prior_row is None
                else json.dumps(
                    reicat_alias_snapshot_from_row(prior_row),
                    ensure_ascii=False,
                )
            )
            operation = "insert" if prior_row is None else "update"
            conn.execute(
                audit_sql,
                (digest, now_iso, operation, prior_snap_json_text, new_snapshot_json),
            )
            audit_identity = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.Error as exc:
        raise RuntimeError("unable to upsert book metadata") from exc
    assert isinstance(audit_identity, int)
    return BookUpsertResult(
        source_sha256=digest,
        was_inserted=was_inserted,
        metadata_audit_row_id=int(audit_identity),
    )


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
        with _sqlite_connection(sqlite_path) as conn:
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
        with _sqlite_connection(sqlite_path) as conn:
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


def _duplicate_skip_no_metadata_audit(
    enriched: EnrichedIngestRequest, sqlite_path: str
) -> int:
    init_books_schema(sqlite_path)
    verify_source_pdf_digest_matches(enriched)
    digest = _validate_source_sha256(enriched.source_sha256)
    now_iso = _utc_now_iso()
    audit_sql = """
        INSERT INTO book_metadata_audit (
            source_sha256,
            event_at,
            operation,
            prior_snapshot_json,
            snapshot_json
        )
        VALUES (?, ?, ?, ?, ?)
    """
    skip_snapshot_json = json.dumps(
        {"duplicate_ingest": True, "metadata_refresh": False},
        ensure_ascii=False,
    )
    try:
        with _sqlite_connection(sqlite_path) as conn:
            row = conn.execute(
                "SELECT source_sha256 FROM books WHERE source_sha256 = ?", (digest,)
            ).fetchone()
            if row is None:
                raise RuntimeError("duplicate ingest without existing book row")
            conn.execute(
                """
                UPDATE books
                SET last_seen_at = ?, updated_at = ?
                WHERE source_sha256 = ?
                """,
                (now_iso, now_iso, digest),
            )
            conn.execute(
                audit_sql,
                (
                    digest,
                    now_iso,
                    "duplicate_skip_no_metadata",
                    None,
                    skip_snapshot_json,
                ),
            )
            audit_identity = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.Error as exc:
        raise RuntimeError("unable to record duplicate skip touch") from exc
    assert isinstance(audit_identity, int)
    return int(audit_identity)


def run_ingest_gate_phase(
    enriched: EnrichedIngestRequest, sqlite_path: str
) -> IngestGatePhaseResult:
    init_books_schema(sqlite_path)
    gate = source_hash_gate(enriched.source_sha256, sqlite_path)
    if gate.status == SourceHashGateStatus.NEW_HASH:
        upsert_result = upsert_book_reicat(enriched, sqlite_path)
        return IngestGatePhaseResult(
            gate=gate,
            pipeline_skipped=False,
            book_upsert=upsert_result,
        )
    force_meta = enriched.request.options.force_metadata_update_on_duplicate_hash
    if force_meta:
        upsert_result = upsert_book_reicat(enriched, sqlite_path)
        return IngestGatePhaseResult(
            gate=gate,
            pipeline_skipped=True,
            book_upsert=upsert_result,
        )
    audit_id = _duplicate_skip_no_metadata_audit(enriched, sqlite_path)
    return IngestGatePhaseResult(
        gate=gate,
        pipeline_skipped=True,
        duplicate_skip_audit_row_id=audit_id,
    )

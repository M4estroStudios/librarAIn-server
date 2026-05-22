from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from src.models.request import ReicatMetadata


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _sqlite_connection(path: str):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_pipeline_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            source_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            pipeline_version TEXT NOT NULL,
            last_error TEXT,
            total_pages INTEGER,
            succeeded_pages INTEGER,
            failed_pages INTEGER
        )
        """
    )


def create_pipeline_run(
    sqlite_path: str,
    *,
    request_id: str,
    source_sha256: str,
    pipeline_version: str,
    total_pages: int,
) -> int:
    from src.persistence.book_sqlite import init_books_schema

    init_books_schema(sqlite_path)
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO pipeline_runs (
                    request_id,
                    source_sha256,
                    status,
                    started_at,
                    finished_at,
                    pipeline_version,
                    last_error,
                    total_pages,
                    succeeded_pages,
                    failed_pages
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    source_sha256,
                    "running",
                    now_iso,
                    None,
                    pipeline_version,
                    None,
                    total_pages,
                    0,
                    0,
                ),
            )
            row_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise RuntimeError("pipeline run with request_id already exists") from exc
    except sqlite3.Error as exc:
        raise RuntimeError("unable to create pipeline run") from exc
    assert isinstance(row_id, int)
    return row_id


def mark_pipeline_run_finished(
    sqlite_path: str,
    *,
    request_id: str,
    status: str,
    succeeded_pages: int,
    failed_pages: int,
    last_error: str | None = None,
) -> None:
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                UPDATE pipeline_runs
                SET finished_at = ?,
                    status = ?,
                    succeeded_pages = ?,
                    failed_pages = ?,
                    last_error = ?
                WHERE request_id = ?
                """,
                (
                    now_iso,
                    status,
                    succeeded_pages,
                    failed_pages,
                    last_error,
                    request_id,
                ),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark pipeline run finished") from exc


def get_pipeline_run_by_request_id(
    sqlite_path: str,
    request_id: str,
) -> dict[str, Any] | None:
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to read pipeline run") from exc
    if row is None:
        return None
    return dict(row)


def reicat_alias_snapshot(meta: ReicatMetadata) -> dict[str, Any]:
    return meta.model_dump(mode="json", exclude_none=True, by_alias=True)


def reicat_alias_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    def _maybe_json_list(raw: str | None) -> list[str] | None:
        if raw is None or raw == "":
            return None
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None
        return [str(x) for x in parsed]

    editors = _maybe_json_list(row["editors_json"])
    translators = _maybe_json_list(row["translators_json"])
    out: dict[str, Any] = {
        "titolo": row["title"],
        "autore": json.loads(row["authors_json"]),
    }
    subtitle = row["subtitle"]
    if subtitle:
        out["sottotitolo"] = subtitle
    tcomp = row["title_complements"]
    if tcomp:
        out["complementi_del_titolo"] = tcomp
    if editors:
        out["curatore"] = editors
    if translators:
        out["traduttore"] = translators
    en = row["edition_number"]
    if en:
        out["numero_edizione"] = en
    pub_y = row["publication_year"]
    if pub_y is not None:
        out["anno_di_pubblicazione"] = pub_y
    ptype = row["publication_type"]
    if ptype:
        out["tipo_di_pubblicazione"] = ptype
    pplace = row["publication_place"]
    if pplace:
        out["luogo_di_pubblicazione"] = pplace
    publisher = row["publisher"]
    if publisher:
        out["editore"] = publisher
    pc = row["page_count"]
    if pc is not None:
        out["numero_pagine"] = pc
    stitle = row["series_title"]
    if stitle:
        out["titolo_collana"] = stitle
    snum = row["series_number"]
    if snum:
        out["numero_nella_collana"] = snum
    if row["isbn"]:
        out["isbn"] = row["isbn"]
    return out

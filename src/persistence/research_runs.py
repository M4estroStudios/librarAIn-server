from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.persistence.pipeline_runs import _sqlite_connection
from src.search.article_llm import query_log_fields
from src.search.request_schema import ResearchPoh


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_research_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_runs (
            request_id TEXT PRIMARY KEY,
            query_hash TEXT NOT NULL,
            query_preview TEXT NOT NULL,
            poh_id TEXT,
            status TEXT NOT NULL,
            context_books_json TEXT NOT NULL,
            subjects_matched_json TEXT NOT NULL,
            citations_count INTEGER,
            pipeline_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            last_error TEXT
        )
        """
    )


def create_research_run_accepted(
    sqlite_path: str,
    *,
    request_id: str,
    query: str,
    poh: ResearchPoh | None,
    pipeline_version: str,
) -> None:
    from src.persistence.book_sqlite import init_books_schema

    fields = query_log_fields(query, poh)
    init_books_schema(sqlite_path)
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO research_runs (
                    request_id,
                    query_hash,
                    query_preview,
                    poh_id,
                    status,
                    context_books_json,
                    subjects_matched_json,
                    citations_count,
                    pipeline_version,
                    started_at,
                    finished_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    fields["query_hash"],
                    fields["query_preview"],
                    fields.get("poh_id"),
                    "accepted",
                    _json_dumps({}),
                    _json_dumps([]),
                    None,
                    pipeline_version,
                    now_iso,
                    None,
                    None,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise RuntimeError("research run with request_id already exists") from exc
    except sqlite3.Error as exc:
        raise RuntimeError("unable to create research run") from exc


def mark_research_run_running(sqlite_path: str, *, request_id: str) -> None:
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?
                WHERE request_id = ?
                """,
                ("running", request_id),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark research run running") from exc


def mark_research_run_succeeded(
    sqlite_path: str,
    *,
    request_id: str,
    context_books: dict[str, list[int]],
    subjects_matched: list[dict[str, Any]],
    citations_count: int,
) -> None:
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?,
                    context_books_json = ?,
                    subjects_matched_json = ?,
                    citations_count = ?,
                    finished_at = ?,
                    last_error = ?
                WHERE request_id = ?
                """,
                (
                    "succeeded",
                    _json_dumps(context_books),
                    _json_dumps(subjects_matched),
                    citations_count,
                    now_iso,
                    None,
                    request_id,
                ),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark research run succeeded") from exc


def mark_research_run_failed(
    sqlite_path: str,
    *,
    request_id: str,
    last_error: str,
) -> None:
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?,
                    finished_at = ?,
                    last_error = ?
                WHERE request_id = ?
                """,
                ("failed", now_iso, last_error, request_id),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark research run failed") from exc


def get_research_run_by_request_id(
    sqlite_path: str,
    request_id: str,
) -> dict[str, Any] | None:
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM research_runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to read research run") from exc
    if row is None:
        return None
    return dict(row)

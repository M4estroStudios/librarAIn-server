from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.persistence.pipeline_runs import _sqlite_connection

BIBLIO_STAGE_LABELS: dict[str, str] = {
    "polyindex_toc": "Polyindex TOC",
    "polyindex_index": "Indice BOOKs",
    "library_index": "Indice LIBRARY",
    "time_index": "Indice TIME_INDEX",
    "polyindex_biblio": "Polyindex BIBLIO",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_biblio_stage_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS biblio_stage_runs (
            request_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            stage TEXT NOT NULL,
            compute_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            last_error TEXT,
            result_json TEXT
        )
        """
    )


def create_biblio_stage_run(
    sqlite_path: str,
    *,
    request_id: str,
    source_sha256: str,
    stage: str,
    compute_mode: str = "local",
) -> None:
    from src.persistence.book_sqlite import init_books_schema

    init_books_schema(sqlite_path)
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO biblio_stage_runs (
                    request_id,
                    source_sha256,
                    stage,
                    compute_mode,
                    status,
                    started_at,
                    finished_at,
                    last_error,
                    result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    source_sha256,
                    stage,
                    compute_mode,
                    "running",
                    now_iso,
                    None,
                    None,
                    None,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise RuntimeError("biblio stage run with request_id already exists") from exc
    except sqlite3.Error as exc:
        raise RuntimeError("unable to create biblio stage run") from exc


def mark_biblio_stage_run_done(
    sqlite_path: str,
    *,
    request_id: str,
    result: dict[str, Any] | None = None,
) -> None:
    now_iso = _utc_now_iso()
    payload = _json_dumps(result) if isinstance(result, dict) else None
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                UPDATE biblio_stage_runs
                SET status = ?,
                    finished_at = ?,
                    last_error = ?,
                    result_json = ?
                WHERE request_id = ?
                """,
                ("done", now_iso, None, payload, request_id),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark biblio stage run done") from exc


def mark_biblio_stage_run_failed(
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
                UPDATE biblio_stage_runs
                SET status = ?,
                    finished_at = ?,
                    last_error = ?
                WHERE request_id = ?
                """,
                ("error", now_iso, last_error, request_id),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to mark biblio stage run failed") from exc


def _decode_biblio_stage_run_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("result_json", None)
    result: dict[str, Any] | None = None
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            result = parsed
    data["result"] = result
    return data


def list_biblio_stage_runs(
    sqlite_path: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    from src.persistence.book_sqlite import init_books_schema

    init_books_schema(sqlite_path)
    cap = max(1, min(limit, 500))
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    bsr.*,
                    b.title AS book_title
                FROM biblio_stage_runs bsr
                LEFT JOIN books b ON b.source_sha256 = bsr.source_sha256
                ORDER BY bsr.started_at DESC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to list biblio stage runs") from exc
    return [_decode_biblio_stage_run_row(row) for row in rows]

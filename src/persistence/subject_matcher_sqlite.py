from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from src.persistence.pipeline_runs import _sqlite_connection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_subject_matcher_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subject_embeddings (
            canonical_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            embedding BLOB NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subject_match_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            raw_label TEXT NOT NULL,
            normalized TEXT NOT NULL,
            decision TEXT NOT NULL,
            canonical_id TEXT,
            similarity REAL,
            ai_used INTEGER NOT NULL,
            ai_reason TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def init_subject_matcher_schema(sqlite_path: str) -> None:
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _sqlite_connection(str(db_path)) as conn:
            ensure_subject_matcher_tables(conn)
    except sqlite3.Error as exc:
        raise RuntimeError("unable to initialize subject matcher schema") from exc


def pack_embedding_vector(values: list[float]) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def unpack_embedding_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"{count}f", blob))


def get_subject_embedding(
    sqlite_path: str, canonical_id: str, model: str
) -> list[float] | None:
    init_subject_matcher_schema(sqlite_path)
    try:
        with _sqlite_connection(sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT embedding, model FROM subject_embeddings
                WHERE canonical_id = ?
                """,
                (canonical_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to read subject embedding") from exc
    if row is None:
        return None
    stored_model = str(row[1])
    if stored_model != model:
        return None
    return unpack_embedding_vector(bytes(row[0]))


def set_subject_embedding(
    sqlite_path: str,
    canonical_id: str,
    label: str,
    embedding: list[float],
    model: str,
) -> None:
    init_subject_matcher_schema(sqlite_path)
    blob = pack_embedding_vector(embedding)
    now_iso = _utc_now_iso()
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO subject_embeddings (
                    canonical_id, label, embedding, model, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    label = excluded.label,
                    embedding = excluded.embedding,
                    model = excluded.model,
                    created_at = excluded.created_at
                """,
                (canonical_id, label, blob, model, now_iso),
            )
    except sqlite3.Error as exc:
        raise RuntimeError("unable to store subject embedding") from exc


def insert_subject_match_audit(
    sqlite_path: str,
    *,
    request_id: str,
    raw_label: str,
    normalized: str,
    decision: str,
    canonical_id: str | None,
    similarity: float | None,
    ai_used: bool,
    ai_reason: str | None,
) -> int:
    init_subject_matcher_schema(sqlite_path)
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO subject_match_audit (
                    request_id,
                    raw_label,
                    normalized,
                    decision,
                    canonical_id,
                    similarity,
                    ai_used,
                    ai_reason,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    raw_label,
                    normalized,
                    decision,
                    canonical_id,
                    similarity,
                    1 if ai_used else 0,
                    ai_reason,
                    _utc_now_iso(),
                ),
            )
            row = conn.execute("SELECT last_insert_rowid()").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("unable to insert subject match audit") from exc
    assert row is not None
    return int(row[0])

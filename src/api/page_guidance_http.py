from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.api.appendix_types import (
    dump_appendix_sections_json,
    normalize_appendix_sections,
    normalize_appendix_splits,
    pages_to_spec,
)
from src.api.page_guidance_suggest import normalize_annotations, suggest_page_guidance
from src.core.hashing import compute_file_sha256, validate_source_sha256
from src.api.pdf_upload_storage import (
    delete_draft_pdf,
    find_draft_pdf_by_sha256,
    find_raw_pdf_by_sha256,
    find_source_pdf_by_sha256,
    save_draft_pdf,
    upload_staging_path,
)
from src.search.article_catalog import list_ingested_books
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.pdf_alignment import merge_pdf_paths
from src.models.settings import Settings
from src.models.request import MD_FORMATTING_FIELD_DEFAULTS
from src.models.ingest_compute import (
    apply_step_compute,
    missing_cloud_config_for_plan,
    resolve_ingest_compute_plan,
    with_step_override,
)
from src.persistence.pipeline_runs import _sqlite_connection



def _ingest_notes_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(ingest_notes)").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_ingest_notes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_notes (
            source_sha256 TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_draft INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Idempotent migration for DBs created before is_draft existed.
    if "is_draft" not in _ingest_notes_columns(conn):
        conn.execute(
            "ALTER TABLE ingest_notes ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0"
        )


_RANGE_FIELD_NAMES = (
    "pages_to_remove",
    "toc_range",
    "index_range",
    "biblio_range",
    "reicat_pages",
    "appendix_pages",
    "appendix_sections_json",
)

_REICAT_FIELD_NAMES = (
    "titolo",
    "sottotitolo",
    "complementi_del_titolo",
    "autore",
    "curatore",
    "traduttore",
    "numero_edizione",
    "anno_di_pubblicazione",
    "tipo_di_pubblicazione",
    "luogo_di_pubblicazione",
    "editore",
    "numero_pagine",
    "titolo_collana",
    "numero_nella_collana",
    "isbn",
)

_MD_FORMATTING_FIELD_NAMES = tuple(MD_FORMATTING_FIELD_DEFAULTS)


def _str_fields_from_mapping(src: dict[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    return {name: str(src.get(name) or "").strip() for name in names}


def _range_fields_from_mapping(src: dict[str, Any]) -> dict[str, str]:
    return _str_fields_from_mapping(src, _RANGE_FIELD_NAMES)


def _reicat_fields_from_mapping(src: dict[str, Any]) -> dict[str, str]:
    return _str_fields_from_mapping(src, _REICAT_FIELD_NAMES)


def _md_formatting_fields_from_mapping(src: dict[str, Any]) -> dict[str, str]:
    merged = dict(src)
    nested = src.get("md_formatting")
    if isinstance(nested, dict):
        for name in _MD_FORMATTING_FIELD_NAMES:
            if not str(merged.get(name) or "").strip() and nested.get(name):
                merged[name] = nested[name]
    return _str_fields_from_mapping(merged, _MD_FORMATTING_FIELD_NAMES)


def _book_id_hint_from_mapping(src: dict[str, Any]) -> str:
    return str(src.get("book_id_hint") or "").strip()


def _json_list_to_csv(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if not isinstance(parsed, list):
        return raw.strip()
    parts = [str(item).strip() for item in parsed if str(item).strip()]
    return ", ".join(parts)


def load_book_reicat_form_fields(
    sqlite_path: str,
    source_sha256: str,
) -> dict[str, str] | None:
    digest = validate_source_sha256(source_sha256)
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return None
    try:
        with _sqlite_connection(str(db_path)) as conn:
            row = conn.execute(
                """
                SELECT title, subtitle, title_complements, authors_json, editors_json,
                       translators_json, edition_number, publication_year, publication_type,
                       publication_place, publisher, page_count, series_title, series_number, isbn
                FROM books WHERE source_sha256 = ?
                """,
                (digest,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "titolo": str(row[0] or "").strip(),
        "sottotitolo": str(row[1] or "").strip(),
        "complementi_del_titolo": str(row[2] or "").strip(),
        "autore": _json_list_to_csv(row[3]),
        "curatore": _json_list_to_csv(row[4]),
        "traduttore": _json_list_to_csv(row[5]),
        "numero_edizione": str(row[6] or "").strip(),
        "anno_di_pubblicazione": "" if row[7] is None else str(row[7]).strip(),
        "tipo_di_pubblicazione": str(row[8] or "").strip(),
        "luogo_di_pubblicazione": str(row[9] or "").strip(),
        "editore": str(row[10] or "").strip(),
        "numero_pagine": "" if row[11] is None else str(row[11]).strip(),
        "titolo_collana": str(row[12] or "").strip(),
        "numero_nella_collana": str(row[13] or "").strip(),
        "isbn": str(row[14] or "").strip(),
    }


def _merge_reicat_defaults(
    state: dict[str, Any],
    reicat_fields: dict[str, str] | None,
) -> dict[str, Any]:
    if not reicat_fields:
        return state
    for name in _REICAT_FIELD_NAMES:
        current = str(state.get(name) or "").strip()
        if current:
            continue
        fallback = str(reicat_fields.get(name) or "").strip()
        if fallback:
            state[name] = fallback
    return state


def _normalize_appendix_sections_field(state: dict[str, Any]) -> str:
    raw = state.get("appendix_sections_json")
    sections = normalize_appendix_sections(raw)
    if not sections and str(state.get("appendix_pages") or "").strip():
        # Migrazione soft: un blocco legacy senza tipologia resta solo in appendix_pages.
        return "[]"
    splits = normalize_appendix_splits(raw, sections)
    return dump_appendix_sections_json(sections, splits)


def _normalize_ingest_notes_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "notes": str(state.get("notes") or "").strip(),
        "index_notes": str(state.get("index_notes") or "").strip(),
        "page_notes": str(state.get("page_notes") or "").strip(),
        "ai_page_guidance": str(state.get("ai_page_guidance") or "").strip(),
        "annotations": normalize_annotations(state.get("annotations") or []),
        "file_name": str(state.get("file_name") or "").strip(),
    }
    payload.update(_range_fields_from_mapping(state))
    payload.update(_reicat_fields_from_mapping(state))
    payload.update(_md_formatting_fields_from_mapping(state))
    payload["book_id_hint"] = _book_id_hint_from_mapping(state)
    compute_plan = state.get("compute_plan")
    if isinstance(compute_plan, dict):
        payload["compute_plan"] = json.dumps(compute_plan, ensure_ascii=False, sort_keys=True)
    else:
        payload["compute_plan"] = str(compute_plan or "").strip()
    sections_json = _normalize_appendix_sections_field(state)
    payload["appendix_sections_json"] = sections_json
    sections = normalize_appendix_sections(sections_json)
    if sections:
        all_pages: list[int] = []
        for item in sections:
            all_pages.extend(int(p) for p in item["pages"])
        payload["appendix_pages"] = pages_to_spec(all_pages)
    return payload


def build_ingest_notes_state(
    text_fields: dict[str, str],
    ingest_payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        annotations = json.loads(text_fields.get("annotations_json") or "[]")
    except json.JSONDecodeError:
        annotations = []
    if not isinstance(annotations, list):
        annotations = []
    state = {
        "notes": (text_fields.get("notes") or "").strip(),
        "index_notes": (text_fields.get("index_notes") or "").strip(),
        "page_notes": (text_fields.get("page_notes") or "").strip(),
        "ai_page_guidance": str(ingest_payload.get("ai_page_guidance") or "").strip(),
        "annotations": normalize_annotations(annotations),
        "file_name": str(text_fields.get("file_name") or "").strip(),
    }
    state.update(_range_fields_from_mapping(text_fields))
    state.update(_reicat_fields_from_mapping(text_fields))
    merged_fields = dict(text_fields)
    nested_md = ingest_payload.get("md_formatting")
    if isinstance(nested_md, dict):
        merged_fields["md_formatting"] = nested_md
    if ingest_payload.get("book_id_hint"):
        merged_fields["book_id_hint"] = ingest_payload["book_id_hint"]
    state.update(_md_formatting_fields_from_mapping(merged_fields))
    state["book_id_hint"] = _book_id_hint_from_mapping(merged_fields)
    compute_plan = ingest_payload.get("compute_plan")
    if compute_plan is None:
        compute_plan = text_fields.get("compute_plan")
    if isinstance(compute_plan, dict):
        state["compute_plan"] = json.dumps(compute_plan, ensure_ascii=False, sort_keys=True)
    else:
        state["compute_plan"] = str(compute_plan or "").strip()
    return state


def save_ingest_notes_state(
    sqlite_path: str,
    source_sha256: str,
    state: dict[str, Any],
    *,
    is_draft: bool | None = None,
) -> str:
    digest = validate_source_sha256(source_sha256)
    payload = _normalize_ingest_notes_payload(state)
    now_iso = datetime.now(timezone.utc).isoformat()
    state_json = json.dumps(payload, ensure_ascii=False)
    draft_flag: int | None = None if is_draft is None else (1 if is_draft else 0)
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        conn.execute(
            """
            INSERT INTO ingest_notes (source_sha256, state_json, updated_at, is_draft)
            VALUES (?, ?, ?, COALESCE(?, 0))
            ON CONFLICT(source_sha256) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at,
                is_draft = COALESCE(?, ingest_notes.is_draft)
            """,
            (digest, state_json, now_iso, draft_flag, draft_flag),
        )
    return digest


def load_ingest_notes_state(
    sqlite_path: str,
    source_sha256: str,
) -> dict[str, Any] | None:
    digest = validate_source_sha256(source_sha256)
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return None
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        row = conn.execute(
            "SELECT state_json FROM ingest_notes WHERE source_sha256 = ?",
            (digest,),
        ).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(str(row[0] or ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _normalize_ingest_notes_payload(parsed)


def resolve_ingest_ui_state(
    sqlite_path: str,
    source_sha256: str,
) -> dict[str, Any] | None:
    digest = validate_source_sha256(source_sha256)
    notes_state = load_ingest_notes_state(sqlite_path, digest)
    reicat_fields = load_book_reicat_form_fields(sqlite_path, digest)
    if notes_state is None and reicat_fields is None:
        return None
    state = notes_state or {
        "notes": "",
        "index_notes": "",
        "page_notes": "",
        "ai_page_guidance": "",
        "annotations": [],
        "file_name": "",
        "book_id_hint": "",
        **{name: "" for name in _RANGE_FIELD_NAMES},
        **{name: "" for name in _REICAT_FIELD_NAMES},
        **{name: "" for name in _MD_FORMATTING_FIELD_NAMES},
    }
    return _merge_reicat_defaults(dict(state), reicat_fields)


def persist_ingest_notes_for_pdf(
    sqlite_path: str,
    pdf_path: Path,
    text_fields: dict[str, str],
    ingest_payload: dict[str, Any],
    *,
    alias_shas: list[str] | None = None,
) -> str | None:
    try:
        digest = compute_file_sha256(pdf_path)
        state = build_ingest_notes_state(text_fields, ingest_payload)
        # Submit riuscito: state resta, bozza esce dalla lista attiva.
        save_ingest_notes_state(sqlite_path, digest, state, is_draft=False)
        aliases: list[str] = []
        for raw_alias in alias_shas or []:
            try:
                alias = validate_source_sha256(raw_alias)
            except ValueError:
                continue
            if alias == digest:
                continue
            save_ingest_notes_state(sqlite_path, alias, state, is_draft=False)
            aliases.append(alias)
        Log(INFO_LOG_LEVEL, "ingest notes persisted", {"source_sha256": digest[:16], "alias_count": len(aliases)})
        return digest
    except Exception as exc:
        Log(
            WARNING_LOG_LEVEL,
            "ingest notes persist failed",
            {"error": str(exc)},
        )
        return None


def list_ingest_drafts(
    sqlite_path: str,
    *,
    data_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return []
    root = Path(data_root) if data_root else None
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        rows = conn.execute(
            """
            SELECT source_sha256, state_json, updated_at
            FROM ingest_notes
            WHERE is_draft = 1
            ORDER BY updated_at DESC
            """
        ).fetchall()
    drafts: list[dict[str, Any]] = []
    for row in rows:
        title = ""
        file_name = ""
        try:
            parsed = json.loads(str(row[1] or ""))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            title = str(parsed.get("titolo") or "").strip()
            file_name = str(parsed.get("file_name") or "").strip()
        digest = str(row[0])
        has_pdf = bool(root and find_draft_pdf_by_sha256(root, digest) is not None)
        drafts.append(
            {
                "source_sha256": digest,
                "title": title,
                "file_name": file_name,
                "updated_at": str(row[2] or ""),
                "has_pdf": has_pdf,
            }
        )
    return drafts


def save_ingest_draft(
    sqlite_path: str,
    source_sha256: str,
    state: dict[str, Any],
    *,
    file_name: str | None = None,
    data_root: Path | str | None = None,
) -> str:
    payload = dict(state)
    if file_name is not None:
        payload["file_name"] = str(file_name).strip()
    digest = save_ingest_notes_state(sqlite_path, source_sha256, payload, is_draft=True)
    Log(INFO_LOG_LEVEL, "ingest draft saved", {"source_sha256": digest[:16]})
    if data_root is not None:
        from src.persistence.draft_sync import sync_on_draft_saved

        sync_on_draft_saved(sqlite_path, data_root, digest)
    return digest


def _book_exists_in_biblioteca(conn: sqlite3.Connection, digest: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM books WHERE source_sha256 = ? LIMIT 1",
            (digest,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def delete_ingest_draft(
    sqlite_path: str,
    source_sha256: str,
    *,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    """Rimuove bozza attiva.

    Se solo bozza mai ingestita (nessuna riga in books) → DELETE della riga.
    Se libro già in Biblioteca → solo is_draft=0, state/REICAT restano.
    In entrambi i casi rimuove il PDF bozza da input/drafts (se presente).
    """
    digest = validate_source_sha256(source_sha256)
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        if data_root:
            delete_draft_pdf(Path(data_root), digest)
        return {"ok": True, "found": False, "removed": False, "cleared": False, "source_sha256": digest}
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        row = conn.execute(
            "SELECT is_draft FROM ingest_notes WHERE source_sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None:
            if data_root:
                delete_draft_pdf(Path(data_root), digest)
            return {"ok": True, "found": False, "removed": False, "cleared": False, "source_sha256": digest}
        if _book_exists_in_biblioteca(conn, digest):
            conn.execute(
                """
                UPDATE ingest_notes
                SET is_draft = 0, updated_at = ?
                WHERE source_sha256 = ?
                """,
                (datetime.now(timezone.utc).isoformat(), digest),
            )
            Log(INFO_LOG_LEVEL, "ingest draft cleared (book kept)", {"source_sha256": digest[:16]})
            if data_root:
                delete_draft_pdf(Path(data_root), digest)
                from src.persistence.draft_sync import sync_on_draft_deleted

                sync_on_draft_deleted(data_root, digest)
            return {"ok": True, "found": True, "removed": False, "cleared": True, "source_sha256": digest}
        conn.execute("DELETE FROM ingest_notes WHERE source_sha256 = ?", (digest,))
        Log(INFO_LOG_LEVEL, "ingest draft row removed", {"source_sha256": digest[:16]})
        if data_root:
            delete_draft_pdf(Path(data_root), digest)
            from src.persistence.draft_sync import sync_on_draft_deleted

            sync_on_draft_deleted(data_root, digest)
        return {"ok": True, "found": True, "removed": True, "cleared": False, "source_sha256": digest}


def _annotation_counts(state: dict[str, Any] | None) -> tuple[int, int]:
    if not state:
        return 0, 0
    annotations = state.get("annotations")
    if not isinstance(annotations, list):
        return 0, 0
    pages = 0
    elements = 0
    for entry in annotations:
        if not isinstance(entry, dict):
            continue
        elems = entry.get("elements")
        if not isinstance(elems, list) or not elems:
            continue
        pages += 1
        elements += len(elems)
    return pages, elements


def _load_books_catalog_rows(sqlite_path: str) -> dict[str, dict[str, Any]]:
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with _sqlite_connection(str(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT source_sha256, title, updated_at, last_seen_at
                FROM books
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        digest = str(row[0] or "").strip().lower()
        if not digest:
            continue
        out[digest] = {
            "source_sha256": digest,
            "title": str(row[1] or "").strip(),
            "updated_at": str(row[3] or row[2] or "").strip(),
        }
    return out


def _load_ingest_notes_index(
    sqlite_path: str,
) -> dict[str, dict[str, Any]]:
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with _sqlite_connection(str(db_path)) as conn:
            _ensure_ingest_notes_table(conn)
            rows = conn.execute(
                """
                SELECT source_sha256, state_json, updated_at, is_draft
                FROM ingest_notes
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        digest = str(row[0] or "").strip().lower()
        if not digest:
            continue
        try:
            parsed = json.loads(str(row[1] or ""))
        except json.JSONDecodeError:
            parsed = None
        state = parsed if isinstance(parsed, dict) else {}
        ann_pages, ann_elements = _annotation_counts(state)
        out[digest] = {
            "title": str(state.get("titolo") or "").strip(),
            "file_name": str(state.get("file_name") or "").strip(),
            "updated_at": str(row[2] or "").strip(),
            "is_draft": bool(int(row[3] or 0)),
            "annotation_pages": ann_pages,
            "annotation_elements": ann_elements,
            "has_notes": bool(state),
        }
    return out


def list_ingested_books_for_reingest(
    sqlite_path: str,
    data_root: Path | str,
) -> list[dict[str, Any]]:
    """Libri già ingeriti (output e/o riga books), con riepilogo annotazioni."""
    root = Path(data_root)
    by_sha: dict[str, dict[str, Any]] = {}

    for book in list_ingested_books(root):
        digest = str(book.get("source_sha256") or "").strip().lower()
        if not digest:
            continue
        by_sha[digest] = {
            "source_sha256": digest,
            "title": str(book.get("title") or "").strip(),
            "slug": str(book.get("slug") or "").strip(),
            "page_count": int(book.get("page_count") or 0),
            "has_output": True,
            "updated_at": "",
        }

    for digest, row in _load_books_catalog_rows(sqlite_path).items():
        entry = by_sha.get(digest)
        if entry is None:
            by_sha[digest] = {
                "source_sha256": digest,
                "title": row["title"],
                "slug": "",
                "page_count": 0,
                "has_output": False,
                "updated_at": row["updated_at"],
            }
        else:
            if not entry["title"] and row["title"]:
                entry["title"] = row["title"]
            if row["updated_at"] and (
                not entry["updated_at"] or row["updated_at"] > entry["updated_at"]
            ):
                entry["updated_at"] = row["updated_at"]

    notes_index = _load_ingest_notes_index(sqlite_path)
    books: list[dict[str, Any]] = []
    for digest, entry in by_sha.items():
        notes = notes_index.get(digest) or {}
        title = str(notes.get("title") or entry.get("title") or "").strip()
        file_name = str(notes.get("file_name") or "").strip()
        updated_at = str(notes.get("updated_at") or entry.get("updated_at") or "").strip()
        pdf_path = find_source_pdf_by_sha256(root, digest)
        books.append(
            {
                "source_sha256": digest,
                "title": title,
                "file_name": file_name,
                "slug": str(entry.get("slug") or "").strip(),
                "page_count": int(entry.get("page_count") or 0),
                "updated_at": updated_at,
                "has_output": bool(entry.get("has_output")),
                "has_pdf": pdf_path is not None,
                "has_notes": bool(notes.get("has_notes")),
                "annotation_pages": int(notes.get("annotation_pages") or 0),
                "annotation_elements": int(notes.get("annotation_elements") or 0),
                "is_draft": bool(notes.get("is_draft")),
            }
        )

    books.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("title") or "").casefold(),
        ),
        reverse=True,
    )
    return books


def prepare_book_reingest_from_zero(
    data_root: Path | str,
    source_sha256: str,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Cancella cache tmp + output del libro, lasciando note/annotazioni e PDF.

    Se esistono note UI, le rimette in bozza attiva così il libro resta recuperabile
    dalla home anche dopo la cancellazione di ``output/``.
    """
    digest = validate_source_sha256(source_sha256)
    root = Path(data_root)
    removed: list[str] = []
    for rel in (f"tmp/{digest}", f"output/{digest}"):
        target = root / rel
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink(missing_ok=True)
        removed.append(rel)
    restored_as_draft = False
    if sqlite_path:
        existing = load_ingest_notes_state(sqlite_path, digest)
        if existing is not None:
            save_ingest_notes_state(sqlite_path, digest, existing, is_draft=True)
            restored_as_draft = True
    Log(
        INFO_LOG_LEVEL,
        "ingest reingest prepared from zero",
        {
            "source_sha256": digest[:16],
            "removed": removed,
            "restored_as_draft": restored_as_draft,
        },
    )
    pdf_path = find_source_pdf_by_sha256(root, digest)
    return {
        "ok": True,
        "source_sha256": digest,
        "removed": removed,
        "has_pdf": pdf_path is not None,
        "file_name": pdf_path.name if pdf_path is not None else "",
        "restored_as_draft": restored_as_draft,
    }


def _ingest_notes_response(sqlite_path: str, digest: str) -> dict[str, Any]:
    state = resolve_ingest_ui_state(sqlite_path, digest)
    return {
        "ok": True,
        "found": state is not None,
        "source_sha256": digest,
        "state": state,
    }


def try_handle_ingest_notes_get(
    path: str,
    handler: Any,
    query: dict[str, list[str]],
    *,
    settings: Settings,
    send_json: Callable[..., None],
) -> bool:
    if path != "/api/ingest/notes-state":
        return False
    raw_sha = (query.get("source_sha256") or [""])[0].strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        digest = validate_source_sha256(raw_sha)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    send_json(handler, 200, _ingest_notes_response(settings.sqlite_path, digest))
    return True


def try_handle_ingest_drafts_get(
    path: str,
    handler: Any,
    *,
    settings: Settings,
    send_json: Callable[..., None],
) -> bool:
    if path != "/api/ingest/drafts":
        return False
    drafts = list_ingest_drafts(settings.sqlite_path, data_root=settings.data_root)
    send_json(handler, 200, {"ok": True, "drafts": drafts})
    return True


def try_handle_ingest_ingested_get(
    path: str,
    handler: Any,
    *,
    settings: Settings,
    send_json: Callable[..., None],
) -> bool:
    if path != "/api/ingest/ingested":
        return False
    books = list_ingested_books_for_reingest(settings.sqlite_path, settings.data_root)
    send_json(handler, 200, {"ok": True, "books": books})
    return True


def try_handle_ingest_ingested_pdf_get(
    path: str,
    handler: Any,
    query: dict[str, list[str]],
    *,
    settings: Settings,
    send_json: Callable[..., None],
    send_bytes: Callable[..., None],
) -> bool:
    """GET /api/ingest/ingested/pdf?source_sha256=… — PDF sorgente (draft/processed/raw)."""
    if path != "/api/ingest/ingested/pdf":
        return False
    raw_sha = (query.get("source_sha256") or [""])[0].strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        digest = validate_source_sha256(raw_sha)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    pdf_path = find_source_pdf_by_sha256(Path(settings.data_root), digest)
    if pdf_path is None:
        send_json(handler, 404, {"ok": False, "error": "source PDF not found"})
        return True
    try:
        content = pdf_path.read_bytes()
    except OSError as exc:
        send_json(handler, 500, {"ok": False, "error": f"cannot read source PDF: {exc}"})
        return True
    send_bytes(handler, 200, content, "application/pdf")
    return True


def try_handle_ingest_reingest_prepare_post(
    path: str,
    handler: Any,
    *,
    settings: Settings,
    send_json: Callable[..., None],
    read_body: Callable[..., bytes],
    max_bytes: int = 64 * 1024,
) -> bool:
    """POST /api/ingest/ingested/prepare-reingest — wipe tmp+output per re-ingest da zero."""
    if path != "/api/ingest/ingested/prepare-reingest":
        return False
    try:
        raw = read_body(handler, max_bytes)
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid json: {exc}"})
        return True
    if not isinstance(payload, dict):
        send_json(handler, 400, {"ok": False, "error": "body must be an object"})
        return True
    raw_sha = str(payload.get("source_sha256") or "").strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        result = prepare_book_reingest_from_zero(
            settings.data_root,
            raw_sha,
            sqlite_path=settings.sqlite_path,
        )
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    except OSError as exc:
        Log(ERROR_LOG_LEVEL, "reingest prepare failed", {"error": str(exc)})
        send_json(handler, 500, {"ok": False, "error": f"cannot clear book artifacts: {exc}"})
        return True
    send_json(handler, 200, result)
    return True


def _state_from_draft_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("state")
    if isinstance(nested, dict):
        base = dict(nested)
    else:
        base = dict(payload)
    if "annotations_json" in base and "annotations" not in base:
        try:
            annotations = json.loads(str(base.get("annotations_json") or "[]"))
        except json.JSONDecodeError:
            annotations = []
        if isinstance(annotations, list):
            base["annotations"] = annotations
    if "file_name" in payload and "file_name" not in base:
        base["file_name"] = payload.get("file_name")
    return base


def try_handle_ingest_notes_state_put(
    path: str,
    handler: Any,
    *,
    settings: Settings,
    send_json: Callable[..., None],
    read_body: Callable[..., bytes],
    max_bytes: int = 2 * 1024 * 1024,
) -> bool:
    """Salva Bozza: PUT /api/ingest/notes-state (JSON, PDF non richiesto)."""
    if path != "/api/ingest/notes-state":
        return False
    try:
        raw = read_body(handler, max_bytes)
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid json: {exc}"})
        return True
    if not isinstance(payload, dict):
        send_json(handler, 400, {"ok": False, "error": "body must be an object"})
        return True
    raw_sha = str(payload.get("source_sha256") or "").strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        digest = validate_source_sha256(raw_sha)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    state = _state_from_draft_payload(payload)
    file_name = payload.get("file_name")
    if file_name is not None and not isinstance(file_name, str):
        file_name = str(file_name)
    digest = save_ingest_draft(
        settings.sqlite_path,
        digest,
        state,
        file_name=file_name if isinstance(file_name, str) else None,
        data_root=settings.data_root,
    )
    send_json(
        handler,
        200,
        {
            "ok": True,
            "source_sha256": digest,
            "is_draft": True,
            "state": resolve_ingest_ui_state(settings.sqlite_path, digest),
        },
    )
    return True


def try_handle_ingest_drafts_delete(
    path: str,
    handler: Any,
    query: dict[str, list[str]],
    *,
    settings: Settings,
    send_json: Callable[..., None],
) -> bool:
    if path != "/api/ingest/drafts":
        return False
    raw_sha = (query.get("source_sha256") or [""])[0].strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        digest = validate_source_sha256(raw_sha)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    result = delete_ingest_draft(
        settings.sqlite_path,
        digest,
        data_root=settings.data_root,
    )
    send_json(handler, 200, result)
    return True


def try_handle_ingest_drafts_pdf_get(
    path: str,
    handler: Any,
    query: dict[str, list[str]],
    *,
    settings: Settings,
    send_json: Callable[..., None],
    send_bytes: Callable[..., None],
) -> bool:
    """GET /api/ingest/drafts/pdf?source_sha256=… — PDF bozza salvato."""
    if path != "/api/ingest/drafts/pdf":
        return False
    raw_sha = (query.get("source_sha256") or [""])[0].strip().lower()
    if not raw_sha:
        send_json(handler, 400, {"ok": False, "error": "source_sha256 is required"})
        return True
    try:
        digest = validate_source_sha256(raw_sha)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    pdf_path = find_draft_pdf_by_sha256(Path(settings.data_root), digest)
    if pdf_path is None:
        send_json(handler, 404, {"ok": False, "error": "draft PDF not found"})
        return True
    try:
        content = pdf_path.read_bytes()
    except OSError as exc:
        send_json(handler, 500, {"ok": False, "error": f"cannot read draft PDF: {exc}"})
        return True
    send_bytes(handler, 200, content, "application/pdf")
    return True


def try_handle_ingest_drafts_post(
    path: str,
    handler: Any,
    *,
    data_root: Path,
    settings: Settings,
    send_json: Callable[..., None],
    parse_multipart: Callable[..., Any],
    request_content_length: Callable[[Any], int],
    max_upload: int,
    safe_filename: Callable[[str], str],
) -> bool:
    """POST /api/ingest/drafts — salva bozza + PDF (merge volumi se presenti)."""
    if path != "/api/ingest/drafts":
        return False

    content_type = handler.headers.get("Content-Type") or ""
    part_path = upload_staging_path(data_root, "draft")
    volume_paths: list[Path] = []
    merged_tmp: Path | None = None
    try:
        content_length = request_content_length(handler)
        parsed = parse_multipart(
            handler.rfile,
            content_type,
            content_length=content_length,
            max_bytes=max_upload,
            pdf_part_path=part_path,
        )
    except (ValueError, OSError) as exc:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": f"multipart parse failed: {exc}"})
        return True

    uploaded = parsed.pdf
    if uploaded is None:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "pdf_file is required"})
        return True
    if uploaded.size == 0:
        uploaded.path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "empty PDF upload"})
        return True

    text_fields = dict(parsed.text_fields or {})
    volume_merge = str(text_fields.get("volume_merge") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    for volume_upload in getattr(parsed, "volume_pdfs", ()) or ():
        volume_saved = volume_upload.path.with_name(
            f"{secrets.token_hex(6)}_{safe_filename(volume_upload.filename or 'volume.pdf')}"
        )
        volume_upload.path.rename(volume_saved)
        volume_paths.append(volume_saved)

    saved_path = uploaded.path
    try:
        with saved_path.open("rb") as handle:
            magic = handle.read(4)
        if magic != b"%PDF":
            send_json(handler, 400, {"ok": False, "error": "uploaded file is not a PDF"})
            return True

        if volume_merge and volume_paths:
            merged_tmp = saved_path.with_name(
                f"merged_{secrets.token_hex(6)}_{safe_filename(uploaded.filename or 'draft.pdf')}"
            )
            merge_pdf_paths([saved_path, *volume_paths], merged_tmp)
            saved_path.unlink(missing_ok=True)
            for volume_path in volume_paths:
                volume_path.unlink(missing_ok=True)
            volume_paths = []
            saved_path = merged_tmp
            merged_tmp = None
        elif volume_paths:
            send_json(
                handler,
                400,
                {"ok": False, "error": "extra volume PDFs require volume_merge=1"},
            )
            return True

        digest = compute_file_sha256(saved_path)
        save_draft_pdf(data_root, digest, saved_path)

        raw_state = text_fields.get("state_json") or text_fields.get("state") or "{}"
        try:
            state_obj = json.loads(raw_state) if isinstance(raw_state, str) else {}
        except json.JSONDecodeError:
            state_obj = {}
        if not isinstance(state_obj, dict):
            state_obj = {}
        # Allow flat text fields to fill missing state keys.
        for key, value in text_fields.items():
            if key in ("state_json", "state", "volume_merge", "source_sha256"):
                continue
            if key not in state_obj and str(value or "").strip():
                state_obj[key] = value
        file_name = str(
            text_fields.get("file_name")
            or state_obj.get("file_name")
            or uploaded.filename
            or "draft.pdf"
        ).strip()
        state_obj["file_name"] = file_name
        save_ingest_draft(
            settings.sqlite_path,
            digest,
            state_obj,
            file_name=file_name,
            data_root=settings.data_root,
        )
        send_json(
            handler,
            200,
            {
                "ok": True,
                "source_sha256": digest,
                "is_draft": True,
                "has_pdf": True,
                "file_name": file_name,
                "state": resolve_ingest_ui_state(settings.sqlite_path, digest),
            },
        )
        return True
    except Exception as exc:
        Log(ERROR_LOG_LEVEL, "ingest draft save with PDF failed", {"error": str(exc)})
        send_json(handler, 500, {"ok": False, "error": f"draft save failed: {exc}"})
        return True
    finally:
        if merged_tmp is not None:
            merged_tmp.unlink(missing_ok=True)
        drafts_dir = Path(data_root) / "input" / "drafts"
        try:
            if saved_path.is_file() and saved_path.parent.resolve() != drafts_dir.resolve():
                saved_path.unlink(missing_ok=True)
        except OSError:
            pass
        for volume_path in volume_paths:
            volume_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)


def ensure_ingest_ai_page_guidance(
    pdf_path: Path,
    settings: Settings,
    ingest_payload: dict[str, Any],
    text_fields: dict[str, str],
) -> None:
    existing = ingest_payload.get("ai_page_guidance")
    if isinstance(existing, str) and existing.strip():
        ingest_payload["ai_page_guidance"] = existing.strip()
        return

    try:
        annotations = json.loads(text_fields.get("annotations_json") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("annotations_json must be valid JSON") from exc
    if not isinstance(annotations, list):
        raise ValueError("annotations_json must be a JSON array")

    result = suggest_page_guidance(
        pdf_path,
        settings,
        notes=(text_fields.get("notes") or "").strip(),
        index_notes=(text_fields.get("index_notes") or "").strip(),
        page_notes=(text_fields.get("page_notes") or "").strip(),
        annotations=annotations,
    )
    guidance = str(result.get("guidance") or "").strip()
    if not guidance:
        raise ValueError("page guidance suggestion returned empty text")
    ingest_payload["ai_page_guidance"] = guidance


def _parse_page_list(raw: str) -> list[int]:
    text = (raw or "").strip()
    if not text:
        return []
    pages: list[int] = []
    seen: set[int] = set()
    for part in text.replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start = int(left.strip())
            end = int(right.strip())
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                if page >= 1 and page not in seen:
                    seen.add(page)
                    pages.append(page)
            continue
        page = int(token)
        if page >= 1 and page not in seen:
            seen.add(page)
            pages.append(page)
    return pages


def try_handle_page_guidance_post(
    path: str,
    handler: Any,
    *,
    data_root: Path,
    settings: Settings,
    send_json: Callable[..., None],
    parse_multipart: Callable[..., Any],
    request_content_length: Callable[[Any], int],
    max_upload: int,
    safe_filename: Callable[[str], str],
) -> bool:
    if path != "/api/ingest/page-guidance-suggest":
        return False

    content_type = handler.headers.get("Content-Type") or ""
    part_path = upload_staging_path(data_root)
    try:
        content_length = request_content_length(handler)
        parsed = parse_multipart(
            handler.rfile,
            content_type,
            content_length=content_length,
            max_bytes=max_upload,
            pdf_part_path=part_path,
        )
    except (ValueError, OSError) as exc:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": f"multipart form could not be parsed: {exc}"})
        return True

    uploaded = parsed.pdf
    if uploaded is None:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "pdf_file upload is required"})
        return True
    if uploaded.size == 0:
        uploaded.path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "empty PDF upload"})
        return True
    with uploaded.path.open("rb") as pdf_handle:
        magic = pdf_handle.read(4)
    if magic != b"%PDF":
        uploaded.path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "uploaded file is not a PDF"})
        return True

    digest = compute_file_sha256(uploaded.path)
    existing_raw_path = find_raw_pdf_by_sha256(data_root, digest)
    if existing_raw_path is None:
        saved_path = data_root / "input" / "raw" / (
            f"{secrets.token_hex(6)}_{safe_filename(uploaded.filename or 'upload.pdf')}"
        )
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        uploaded.path.rename(saved_path)
    else:
        # Keep the duplicate in staging; do not touch input/raw.
        saved_path = uploaded.path
    fields = parsed.text_fields

    try:
        compute_plan = resolve_ingest_compute_plan(
            compute_mode=fields.get("compute_mode"),
            compute_plan=fields.get("compute_plan"),
        )
        compute_plan = with_step_override(
            compute_plan,
            "page_guidance",
            model=fields.get("model"),
        )
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": str(exc), "field": "compute_mode"})
        return True

    missing_cloud = missing_cloud_config_for_plan(
        settings, compute_plan, step_ids=("page_guidance",)
    )
    if missing_cloud:
        saved_path.unlink(missing_ok=True)
        send_json(
            handler,
            400,
            {
                "ok": False,
                "error": "cloud compute requires: " + ", ".join(missing_cloud),
                "field": "compute_mode",
            },
        )
        return True

    try:
        annotations = json.loads(fields.get("annotations_json") or "[]")
    except json.JSONDecodeError:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "annotations_json must be valid JSON"})
        return True

    try:
        sample_pages = _parse_page_list(fields.get("sample_pages") or "")
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": f"invalid sample_pages: {exc}"})
        return True

    try:
        with apply_step_compute(settings, compute_plan, "page_guidance") as job_settings:
            result = suggest_page_guidance(
                saved_path,
                job_settings,
                notes=(fields.get("notes") or "").strip(),
                index_notes=(fields.get("index_notes") or "").strip(),
                page_notes=(fields.get("page_notes") or "").strip(),
                annotations=annotations if isinstance(annotations, list) else [],
                sample_pages=sample_pages or None,
            )
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        Log(ERROR_LOG_LEVEL, "page guidance suggest failed", {"error": str(exc)})
        send_json(handler, 500, {"ok": False, "error": str(exc)})
        return True
    finally:
        saved_path.unlink(missing_ok=True)

    send_json(handler, 200, {"ok": True, **result})
    return True

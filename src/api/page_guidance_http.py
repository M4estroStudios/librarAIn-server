from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.api.page_guidance_suggest import normalize_annotations, suggest_page_guidance
from src.core.hashing import compute_file_sha256, validate_source_sha256
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import use_compute_mode
from src.models.settings import Settings, normalize_compute_mode
from src.persistence.pipeline_runs import _sqlite_connection


def _ensure_ingest_notes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_notes (
            source_sha256 TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


_RANGE_FIELD_NAMES = (
    "pages_to_remove",
    "toc_range",
    "index_range",
    "biblio_range",
    "reicat_pages",
    "appendix_pages",
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


def _str_fields_from_mapping(src: dict[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    return {name: str(src.get(name) or "").strip() for name in names}


def _range_fields_from_mapping(src: dict[str, Any]) -> dict[str, str]:
    return _str_fields_from_mapping(src, _RANGE_FIELD_NAMES)


def _reicat_fields_from_mapping(src: dict[str, Any]) -> dict[str, str]:
    return _str_fields_from_mapping(src, _REICAT_FIELD_NAMES)


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
    }
    state.update(_range_fields_from_mapping(text_fields))
    state.update(_reicat_fields_from_mapping(text_fields))
    return state


def save_ingest_notes_state(
    sqlite_path: str,
    source_sha256: str,
    state: dict[str, Any],
) -> str:
    digest = validate_source_sha256(source_sha256)
    payload = {
        "notes": str(state.get("notes") or "").strip(),
        "index_notes": str(state.get("index_notes") or "").strip(),
        "page_notes": str(state.get("page_notes") or "").strip(),
        "ai_page_guidance": str(state.get("ai_page_guidance") or "").strip(),
        "annotations": normalize_annotations(state.get("annotations") or []),
    }
    payload.update(_range_fields_from_mapping(state))
    payload.update(_reicat_fields_from_mapping(state))
    now_iso = datetime.now(timezone.utc).isoformat()
    state_json = json.dumps(payload, ensure_ascii=False)
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        conn.execute(
            """
            INSERT INTO ingest_notes (source_sha256, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source_sha256) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (digest, state_json, now_iso),
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
    state = {
        "notes": str(parsed.get("notes") or "").strip(),
        "index_notes": str(parsed.get("index_notes") or "").strip(),
        "page_notes": str(parsed.get("page_notes") or "").strip(),
        "ai_page_guidance": str(parsed.get("ai_page_guidance") or "").strip(),
        "annotations": normalize_annotations(parsed.get("annotations") or []),
    }
    state.update(_range_fields_from_mapping(parsed))
    state.update(_reicat_fields_from_mapping(parsed))
    return state


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
        **{name: "" for name in _RANGE_FIELD_NAMES},
        **{name: "" for name in _REICAT_FIELD_NAMES},
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
        save_ingest_notes_state(sqlite_path, digest, state)
        aliases: list[str] = []
        for raw_alias in alias_shas or []:
            try:
                alias = validate_source_sha256(raw_alias)
            except ValueError:
                continue
            if alias == digest:
                continue
            save_ingest_notes_state(sqlite_path, alias, state)
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
    part_path = data_root / "input" / "raw" / f".upload_{secrets.token_hex(8)}.part"
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

    saved_path = uploaded.path.with_name(
        f"{secrets.token_hex(6)}_{safe_filename(uploaded.filename or 'upload.pdf')}"
    )
    uploaded.path.rename(saved_path)
    fields = parsed.text_fields

    try:
        compute_mode = normalize_compute_mode(fields.get("compute_mode"))
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": str(exc), "field": "compute_mode"})
        return True

    if compute_mode == "cloud":
        missing_cloud = settings.missing_cloud_config(job_kind="reicat")
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
        with use_compute_mode(compute_mode, settings):
            job_settings = settings.for_compute_mode(compute_mode)
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

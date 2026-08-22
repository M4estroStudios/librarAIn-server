"""Catalogo globale tipologies appendice + parsing sezioni tipizzate."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.api.ingest_form import InvalidPagesSpec, _parse_pages_spec
from src.ingestion.pdf_alignment import extract_pages_to_pdf
from src.persistence.pipeline_runs import _sqlite_connection

_TYPE_NAME_RE = re.compile(r"\s+")


def normalize_appendix_type_name(raw: str) -> str:
    cleaned = _TYPE_NAME_RE.sub(" ", str(raw or "").strip())
    return cleaned[:80]


def slugify_appendix_type(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", normalize_appendix_type_name(name))
    ascii_only = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only.lower()).strip("-")
    return (slug or "appendice")[:64]


def _ensure_appendix_types_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appendix_types (
            name TEXT NOT NULL COLLATE NOCASE,
            slug TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (name)
        )
        """
    )


def list_appendix_types(sqlite_path: str) -> list[dict[str, str]]:
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return []
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_appendix_types_table(conn)
        rows = conn.execute(
            "SELECT name, slug, created_at FROM appendix_types ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
    return [
        {
            "name": str(row[0]),
            "slug": str(row[1]),
            "created_at": str(row[2] or ""),
        }
        for row in rows
    ]


def upsert_appendix_type(sqlite_path: str, name: str, *, data_root: Path | str | None = None) -> dict[str, str]:
    cleaned = normalize_appendix_type_name(name)
    if not cleaned:
        raise ValueError("tipologia appendice vuota")
    slug = slugify_appendix_type(cleaned)
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_appendix_types_table(conn)
        existing = conn.execute(
            "SELECT name, slug, created_at FROM appendix_types WHERE name = ? COLLATE NOCASE",
            (cleaned,),
        ).fetchone()
        if existing is not None:
            item = {
                "name": str(existing[0]),
                "slug": str(existing[1]),
                "created_at": str(existing[2] or ""),
            }
        else:
            # Collisioni slug: aggiungi suffisso numerico.
            base_slug = slug
            suffix = 2
            while True:
                taken = conn.execute(
                    "SELECT 1 FROM appendix_types WHERE slug = ?",
                    (slug,),
                ).fetchone()
                if taken is None:
                    break
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            conn.execute(
                "INSERT INTO appendix_types (name, slug, created_at) VALUES (?, ?, ?)",
                (cleaned, slug, now_iso),
            )
            item = {"name": cleaned, "slug": slug, "created_at": now_iso}
    if data_root is not None:
        try:
            from src.persistence.draft_sync import export_appendix_types

            export_appendix_types(sqlite_path, data_root)
        except Exception:
            pass
    return item


def pages_to_spec(pages: list[int]) -> str:
    if not pages:
        return ""
    sorted_pages = sorted({int(p) for p in pages if int(p) >= 1})
    if not sorted_pages:
        return ""
    parts: list[str] = []
    start = prev = sorted_pages[0]
    for cur in sorted_pages[1:]:
        if cur == prev + 1:
            prev = cur
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cur
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def _coerce_appendix_payload(raw: Any) -> tuple[list[Any], dict[str, float]]:
    """Accetta lista legacy oppure {sections, splits}."""
    if raw is None:
        return [], {}
    parsed: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [], {}
    splits_raw: Any = {}
    items: Any = parsed
    if isinstance(parsed, dict):
        items = parsed.get("sections")
        splits_raw = parsed.get("splits") or {}
    if not isinstance(items, list):
        return [], {}
    splits: dict[str, float] = {}
    if isinstance(splits_raw, dict):
        for key, value in splits_raw.items():
            try:
                page = int(key)
                ratio = float(value)
            except (TypeError, ValueError):
                continue
            if page < 1:
                continue
            splits[str(page)] = min(0.92, max(0.08, ratio))
    return items, splits


def normalize_appendix_splits(raw: Any, sections: list[dict[str, Any]] | None = None) -> dict[str, float]:
    _, splits = _coerce_appendix_payload(raw)
    if sections is None:
        return splits
    shared: set[int] = set()
    seen: set[int] = set()
    for item in sections:
        for page in item.get("pages") or []:
            page_i = int(page)
            if page_i in seen:
                shared.add(page_i)
            else:
                seen.add(page_i)
    cleaned: dict[str, float] = {}
    for page in shared:
        key = str(page)
        cleaned[key] = splits.get(key, 0.5)
    return cleaned


def normalize_appendix_sections(raw: Any) -> list[dict[str, Any]]:
    """Normalizza sezioni tipizzate da JSON list, oggetto {sections,splits} o stringa JSON.

    La stessa pagina può appartenere a più sezioni (confine a metà pagina).
    """
    items, _splits = _coerce_appendix_payload(raw)
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        type_name = normalize_appendix_type_name(str(item.get("type") or item.get("tipologia") or ""))
        if not type_name:
            continue
        pages_raw = item.get("pages")
        pages: list[int] = []
        seen_in_section: set[int] = set()
        if isinstance(pages_raw, list):
            for value in pages_raw:
                try:
                    page = int(value)
                except (TypeError, ValueError):
                    continue
                if page >= 1 and page not in seen_in_section:
                    pages.append(page)
                    seen_in_section.add(page)
        else:
            spec = str(pages_raw or item.get("pages_spec") or "").strip()
            if not spec:
                continue
            try:
                for page in _parse_pages_spec(spec):
                    if page not in seen_in_section:
                        pages.append(page)
                        seen_in_section.add(page)
            except InvalidPagesSpec:
                continue
        if not pages:
            continue
        pages.sort()
        out.append(
            {
                "type": type_name,
                "slug": slugify_appendix_type(type_name),
                "pages": pages,
                "pages_spec": pages_to_spec(pages),
            }
        )
    return out


def dump_appendix_sections_json(
    sections: list[dict[str, Any]],
    splits: dict[str, float] | None = None,
) -> str:
    compact = [{"type": item["type"], "pages": item["pages_spec"]} for item in sections]
    cleaned_splits = normalize_appendix_splits(
        {"sections": compact, "splits": splits or {}},
        sections,
    )
    return json.dumps(
        {"sections": compact, "splits": cleaned_splits},
        ensure_ascii=False,
    )

def appendix_sections_from_form(
    text_fields: dict[str, str],
) -> list[dict[str, Any]]:
    """Preferisce appendix_sections_json; fallback legacy appendix_pages senza tipo."""
    sections = normalize_appendix_sections(text_fields.get("appendix_sections_json"))
    if sections:
        return sections
    legacy = (text_fields.get("appendix_pages") or "").strip()
    if not legacy:
        return []
    try:
        pages = _parse_pages_spec(legacy)
    except InvalidPagesSpec:
        raise
    if not pages:
        return []
    # Legacy: un solo blocco senza tipologia → cartella "appendice".
    return [
        {
            "type": "Appendice",
            "slug": "appendice",
            "pages": pages,
            "pages_spec": pages_to_spec(pages),
        }
    ]


def extract_typed_appendix_pdfs(
    source_path: Path,
    sections: list[dict[str, Any]],
    *,
    data_root: Path,
    book_stem: str,
) -> list[dict[str, Any]]:
    """Scrive PDF in data/input/raw_appendix/<stem>/appendici/<slug>/appendix.pdf."""
    stem = (book_stem or "upload").strip() or "upload"
    base = data_root / "input" / "raw_appendix" / stem / "appendici"
    written: list[dict[str, Any]] = []
    # Raggruppa pagine per slug (stessa tipologia → un PDF).
    by_slug: dict[str, dict[str, Any]] = {}
    for section in sections:
        slug = str(section.get("slug") or slugify_appendix_type(str(section.get("type") or "")))
        type_name = str(section.get("type") or "Appendice")
        pages = [int(p) for p in (section.get("pages") or []) if int(p) >= 1]
        if not pages:
            continue
        bucket = by_slug.setdefault(
            slug,
            {"type": type_name, "slug": slug, "pages": []},
        )
        for page in pages:
            if page not in bucket["pages"]:
                bucket["pages"].append(page)
    for slug, bucket in by_slug.items():
        pages = sorted(bucket["pages"])
        target = base / slug / "appendix.pdf"
        count = extract_pages_to_pdf(source_path, pages, target)
        written.append(
            {
                "type": bucket["type"],
                "slug": slug,
                "pages": pages,
                "pages_spec": pages_to_spec(pages),
                "path": str(target),
                "page_count": count,
            }
        )
    return written


def try_handle_appendix_types_get(
    path: str,
    handler,
    *,
    sqlite_path: str,
    send_json,
) -> bool:
    parsed = urlparse(path if "://" in path else f"http://x{path}")
    if parsed.path != "/api/ingest/appendix-types":
        return False
    items = list_appendix_types(sqlite_path)
    send_json(handler, 200, {"ok": True, "items": items, "count": len(items)})
    return True


def try_handle_appendix_types_post(
    path: str,
    handler,
    *,
    sqlite_path: str,
    send_json,
    read_body,
    data_root: Path | str | None = None,
) -> bool:
    parsed = urlparse(path if "://" in path else f"http://x{path}")
    if parsed.path != "/api/ingest/appendix-types":
        return False
    try:
        raw = read_body(handler, 64 * 1024)
        payload = json.loads(raw.decode("utf-8") if raw else b"{}")
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid json: {exc}"})
        return True
    if not isinstance(payload, dict):
        send_json(handler, 400, {"ok": False, "error": "body must be an object"})
        return True
    name = str(payload.get("name") or payload.get("type") or "").strip()
    try:
        item = upsert_appendix_type(sqlite_path, name, data_root=data_root)
    except ValueError as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    send_json(handler, 200, {"ok": True, "item": item})
    return True

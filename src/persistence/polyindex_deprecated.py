from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingestion.polyindex.file_lock import polyindex_dir_lock

SCHEMA_VERSION = "1.0"
INDEX_KINDS = frozenset({"time_index", "polyindex_index", "polyindex_biblio", "polyindex_toc"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def deprecated_path(data_root: Path) -> Path:
    return Path(data_root) / "polyindex" / "DEPRECATED.json"


def _empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": []}


def _atomic_write_json(dest: Path, payload: dict[str, Any]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


def _load_document(data_root: Path) -> dict[str, Any]:
    path = deprecated_path(data_root)
    if not path.is_file():
        return _empty_document()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_document()
    if not isinstance(raw, dict):
        return _empty_document()
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    return {"schema_version": SCHEMA_VERSION, "items": [item for item in items if isinstance(item, dict)]}


def _save_document(data_root: Path, document: dict[str, Any]) -> None:
    polyindex_dir = Path(data_root) / "polyindex"
    with polyindex_dir_lock(polyindex_dir, ".deprecated.lock"):
        _atomic_write_json(deprecated_path(data_root), document)


def _item_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get("source_sha256") or ""),
        str(item.get("index_kind") or ""),
        str(item.get("section") or ""),
        str(item.get("label") or ""),
        int(item.get("aligned_page") or 0),
    )


def list_deprecated_items(
    data_root: Path,
    *,
    source_sha256: str | None = None,
) -> list[dict[str, Any]]:
    document = _load_document(data_root)
    items = list(document.get("items") or [])
    sha = (source_sha256 or "").strip().lower()
    if sha:
        items = [item for item in items if str(item.get("source_sha256") or "").lower() == sha]
    items.sort(
        key=lambda item: (
            str(item.get("index_kind") or ""),
            int(item.get("aligned_page") or 0),
            str(item.get("label") or ""),
        )
    )
    return items


def count_deprecated_for_book(data_root: Path, source_sha256: str) -> int:
    return len(list_deprecated_items(data_root, source_sha256=source_sha256))


def add_deprecated_item(
    data_root: Path,
    *,
    source_sha256: str,
    index_kind: str,
    label: str,
    aligned_page: int,
    original_page: int | None = None,
    section: str = "",
    detail: str = "",
) -> dict[str, Any] | None:
    sha = (source_sha256 or "").strip().lower()
    kind = (index_kind or "").strip()
    text = (label or "").strip()
    if not sha or kind not in INDEX_KINDS or not text or aligned_page < 1:
        return None
    document = _load_document(data_root)
    items = list(document.get("items") or [])
    candidate = {
        "source_sha256": sha,
        "index_kind": kind,
        "section": (section or "").strip(),
        "label": text,
        "aligned_page": int(aligned_page),
        "original_page": int(original_page) if isinstance(original_page, int) and original_page > 0 else None,
        "detail": (detail or "").strip(),
        "status": "deprecated",
    }
    key = _item_key(candidate)
    for existing in items:
        if _item_key(existing) == key:
            return existing
    candidate["id"] = uuid.uuid4().hex
    candidate["created_at"] = _utc_now_iso()
    items.append(candidate)
    document["items"] = items
    _save_document(data_root, document)
    return candidate


def clear_deprecated_item_if_matches(
    data_root: Path,
    *,
    source_sha256: str,
    index_kind: str,
    label: str,
    aligned_page: int,
    section: str = "",
) -> bool:
    sha = (source_sha256 or "").strip().lower()
    document = _load_document(data_root)
    items = list(document.get("items") or [])
    target = {
        "source_sha256": sha,
        "index_kind": (index_kind or "").strip(),
        "section": (section or "").strip(),
        "label": (label or "").strip(),
        "aligned_page": int(aligned_page),
    }
    key = _item_key(target)
    kept = [item for item in items if _item_key(item) != key]
    if len(kept) == len(items):
        return False
    document["items"] = kept
    _save_document(data_root, document)
    return True


def delete_deprecated_item(data_root: Path, item_id: str) -> dict[str, Any]:
    item_id = (item_id or "").strip()
    if not item_id:
        return {"ok": False, "error": "id required"}
    document = _load_document(data_root)
    items = list(document.get("items") or [])
    found = None
    kept: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("id") or "") == item_id:
            found = item
            continue
        kept.append(item)
    if found is None:
        return {"ok": False, "error": "deprecated item not found"}
    document["items"] = kept
    _save_document(data_root, document)
    removed_from_index = _remove_from_live_index(data_root, found)
    return {"ok": True, "deleted": found, "removed_from_index": removed_from_index}


def _drop_page_from_lists(
    entry: dict[str, Any],
    aligned_page: int,
    original_page: int | None,
) -> bool:
    changed = False
    aligned = entry.get("aligned_pages")
    original = entry.get("original_pages")
    if isinstance(aligned, list) and aligned_page in aligned:
        entry["aligned_pages"] = [page for page in aligned if page != aligned_page]
        changed = True
    if isinstance(original, list) and isinstance(original_page, int) and original_page in original:
        entry["original_pages"] = [page for page in original if page != original_page]
        changed = True
    return changed


def _remove_time_label_for_page(
    data_root: Path,
    *,
    source_sha256: str,
    section: str,
    label: str,
    aligned_page: int,
    original_page: int | None,
) -> bool:
    from src.ingestion.polyindex.time_index import (
        _atomic_write_json,
        _sort_book_time_index_document,
        _sort_time_index_document,
        book_time_index_json_path,
    )

    sha = (source_sha256 or "").strip().lower()
    section_name = "years" if section == "years" else "dates"
    text = (label or "").strip()
    if not sha or not text or aligned_page < 1:
        return False
    output_dir = Path(data_root) / "output" / sha
    slug = "book"
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and isinstance(manifest.get("slug"), str):
                slug = manifest["slug"]
        except (json.JSONDecodeError, OSError):
            pass
    changed = False
    book_path = book_time_index_json_path(output_dir, slug)
    if book_path.is_file():
        try:
            book_document = json.loads(book_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            book_document = None
        if isinstance(book_document, dict):
            local_section = book_document.get(section_name)
            if isinstance(local_section, dict) and isinstance(local_section.get(text), dict):
                entry = local_section[text]
                if _drop_page_from_lists(entry, aligned_page, original_page):
                    changed = True
                if isinstance(entry.get("aligned_pages"), list) and not entry["aligned_pages"]:
                    del local_section[text]
                    changed = True
            page_map = book_document.get("page_years" if section_name == "years" else "page_dates")
            if isinstance(page_map, dict):
                key = str(aligned_page)
                labels = page_map.get(key)
                if isinstance(labels, list) and text in labels:
                    page_map[key] = [item for item in labels if item != text]
                    if not page_map[key]:
                        del page_map[key]
                    changed = True
            if changed:
                _atomic_write_json(book_path, _sort_book_time_index_document(book_document))
    polyindex_dir = Path(data_root) / "polyindex"
    global_path = polyindex_dir / "TIME_INDEX.json"
    if global_path.is_file():
        with polyindex_dir_lock(polyindex_dir, ".time_index.lock"):
            try:
                document = json.loads(global_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                document = None
            if isinstance(document, dict):
                global_section = document.get(section_name)
                if isinstance(global_section, dict) and isinstance(global_section.get(text), dict):
                    books = global_section[text].get("books")
                    if isinstance(books, dict) and isinstance(books.get(sha), dict):
                        book = books[sha]
                        if _drop_page_from_lists(book, aligned_page, original_page):
                            changed = True
                        if isinstance(book.get("aligned_pages"), list) and not book["aligned_pages"]:
                            del books[sha]
                            changed = True
                        if not books:
                            del global_section[text]
                            changed = True
                        if changed:
                            _atomic_write_json(global_path, _sort_time_index_document(document))
    return changed


def _remove_from_live_index(data_root: Path, item: dict[str, Any]) -> bool:
    kind = str(item.get("index_kind") or "")
    if kind != "time_index":
        return False
    return _remove_time_label_for_page(
        data_root,
        source_sha256=str(item.get("source_sha256") or ""),
        section=str(item.get("section") or "years"),
        label=str(item.get("label") or ""),
        aligned_page=int(item.get("aligned_page") or 0),
        original_page=item.get("original_page") if isinstance(item.get("original_page"), int) else None,
    )

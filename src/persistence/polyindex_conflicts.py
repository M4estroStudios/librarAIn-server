from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingestion.polyindex.file_lock import polyindex_dir_lock

SCHEMA_VERSION = "1.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def conflicts_path(data_root: Path) -> Path:
    return Path(data_root) / "polyindex" / "CONFLICTS.json"


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
    path = conflicts_path(data_root)
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
    with polyindex_dir_lock(polyindex_dir, ".conflicts.lock"):
        _atomic_write_json(conflicts_path(data_root), document)


def list_conflict_items(
    data_root: Path,
    *,
    source_sha256: str | None = None,
) -> list[dict[str, Any]]:
    items = list(_load_document(data_root).get("items") or [])
    sha = (source_sha256 or "").strip().lower()
    if sha:
        items = [item for item in items if str(item.get("source_sha256") or "").lower() == sha]
    items.sort(
        key=lambda item: (
            int(item.get("aligned_page") or 0),
            str(item.get("created_at") or ""),
        )
    )
    return items


def count_conflicts_for_book(data_root: Path, source_sha256: str) -> int:
    return len(list_conflict_items(data_root, source_sha256=source_sha256))


def add_conflict_item(
    data_root: Path,
    *,
    source_sha256: str,
    aligned_page: int,
    original_page: int | None = None,
    manual_text: str = "",
    ai_text: str = "",
    detail: str = "",
) -> dict[str, Any] | None:
    sha = (source_sha256 or "").strip().lower()
    if not sha or aligned_page < 1:
        return None
    document = _load_document(data_root)
    items = list(document.get("items") or [])
    item = {
        "id": uuid.uuid4().hex,
        "source_sha256": sha,
        "aligned_page": int(aligned_page),
        "original_page": int(original_page) if isinstance(original_page, int) and original_page > 0 else None,
        "manual_text": manual_text or "",
        "ai_text": ai_text or "",
        "detail": (detail or "").strip() or "AI transcript differs from operator edit; operator edit applied",
        "status": "conflict",
        "created_at": _utc_now_iso(),
    }
    items.append(item)
    document["items"] = items
    _save_document(data_root, document)
    return item


def delete_conflict_item(data_root: Path, item_id: str) -> dict[str, Any]:
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
        return {"ok": False, "error": "conflict item not found"}
    document["items"] = kept
    _save_document(data_root, document)
    return {"ok": True, "deleted": found}

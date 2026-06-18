from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.models.polyindex_index import PolyindexIndexDocument
from src.persistence.book_pages_audit import (
    STAGE_DIRS,
    _load_manifest,
    _normalize_int_list,
    _stage_page_path,
)


class PageExcludeError(ValueError):
    pass


def _exclude_config_path(data_root: Path, source_sha256: str) -> Path:
    return data_root / "tmp" / source_sha256 / "exclude_config.json"


def _manifest_path(data_root: Path, source_sha256: str) -> Path:
    return data_root / "output" / source_sha256 / "manifest.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)


def load_book_exclusions(
    data_root: Path,
    source_sha256: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> tuple[list[int], list[int]]:
    if manifest is None:
        manifest = _load_manifest(_manifest_path(data_root, source_sha256))
    aligned = _normalize_int_list(manifest.get("excluded_aligned_pages") if manifest else [])
    original = _normalize_int_list(manifest.get("pages_to_remove") if manifest else [])
    if manifest:
        return aligned, original
    sidecar = _exclude_config_path(data_root, source_sha256)
    if not sidecar.is_file():
        return [], []
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []
    if not isinstance(raw, dict):
        return [], []
    return (
        _normalize_int_list(raw.get("excluded_aligned_pages")),
        _normalize_int_list(raw.get("pages_to_remove")),
    )


def _aligned_to_original_from_manifest(manifest: dict[str, Any]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    pages = manifest.get("pages")
    if isinstance(pages, list):
        for entry in pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            original = entry.get("original")
            if isinstance(aligned, int) and isinstance(original, int):
                mapping[aligned] = original
    return mapping


def _resolve_original_page(
    aligned_page: int,
    *,
    manifest: dict[str, Any] | None,
    pages_to_remove: list[int],
    data_root: Path,
    source_sha256: str,
) -> int | None:
    original_count: int | None = None
    if manifest:
        direct = _aligned_to_original_from_manifest(manifest).get(aligned_page)
        if direct is not None:
            return direct
        count = manifest.get("original_page_count")
        if isinstance(count, int) and count > 0:
            original_count = count
    if original_count is None:
        sidecar = _exclude_config_path(data_root, source_sha256)
        if sidecar.is_file():
            try:
                raw = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    count = raw.get("original_page_count")
                    if isinstance(count, int) and count > 0:
                        original_count = count
            except (json.JSONDecodeError, OSError):
                pass
    if original_count is None:
        return None
    _, _, aligned_to_original = build_page_removal_mapping(
        original_count, pages_to_remove
    )
    return aligned_to_original.get(aligned_page)


def _resolve_slug(
    data_root: Path,
    source_sha256: str,
    manifest: dict[str, Any] | None,
) -> str:
    if manifest:
        slug = str(manifest.get("slug") or "").strip()
        if slug:
            return slug
    tmp_root = data_root / "tmp" / source_sha256
    for stage_name in ("stage3Editor", "stage2Vision", "stage1OCR"):
        stage_dir = tmp_root / stage_name
        if not stage_dir.is_dir():
            continue
        for path in stage_dir.iterdir():
            match = re.match(r"^p\.\d{4}\.(.+)\.(txt|md)$", path.name)
            if match:
                return match.group(1)
    return ""


def _delete_page_artifacts(
    data_root: Path,
    source_sha256: str,
    slug: str,
    aligned_page: int,
) -> list[str]:
    removed: list[str] = []
    tmp_root = data_root / "tmp" / source_sha256
    render_dir = tmp_root / "render"
    for suffix in (".png", ".png.json"):
        render_path = render_dir / f"p.{aligned_page:04d}{suffix}"
        if render_path.is_file():
            render_path.unlink()
            removed.append(str(render_path))
    for stage_key in STAGE_DIRS:
        path = _stage_page_path(data_root, source_sha256, slug, stage_key, aligned_page)
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed


def _save_exclusions(
    data_root: Path,
    source_sha256: str,
    *,
    manifest: dict[str, Any] | None,
    excluded_aligned: list[int],
    pages_to_remove: list[int],
) -> None:
    if manifest is not None:
        manifest["excluded_aligned_pages"] = excluded_aligned
        manifest["pages_to_remove"] = pages_to_remove
        pages = manifest.get("pages")
        if isinstance(pages, list):
            manifest["pages"] = [
                entry
                for entry in pages
                if not (
                    isinstance(entry, dict)
                    and isinstance(entry.get("aligned"), int)
                    and entry["aligned"] in excluded_aligned
                )
            ]
        _atomic_write_json(_manifest_path(data_root, source_sha256), manifest)
        return
    original_count = 0
    manifest_for_count = _load_manifest(_manifest_path(data_root, source_sha256))
    if manifest_for_count:
        count = manifest_for_count.get("original_page_count")
        if isinstance(count, int):
            original_count = count
    sidecar_path = _exclude_config_path(data_root, source_sha256)
    if sidecar_path.is_file():
        try:
            existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                count = existing.get("original_page_count")
                if isinstance(count, int):
                    original_count = count
        except (json.JSONDecodeError, OSError):
            pass
    payload = {
        "source_sha256": source_sha256,
        "original_page_count": original_count,
        "pages_to_remove": pages_to_remove,
        "excluded_aligned_pages": excluded_aligned,
    }
    _atomic_write_json(sidecar_path, payload)


def _remove_page_from_index(
    document: PolyindexIndexDocument,
    source_sha256: str,
    aligned_page: int,
    original_page: int | None,
) -> None:
    empty_subjects: list[str] = []
    for subject_id, entry in document.subjects.items():
        book = entry.books.get(source_sha256)
        if book is None:
            continue
        if aligned_page not in book.aligned_pages:
            continue
        book.aligned_pages = [page for page in book.aligned_pages if page != aligned_page]
        if original_page is not None:
            book.original_pages = [
                page for page in book.original_pages if page != original_page
            ]
        if not book.aligned_pages:
            del entry.books[source_sha256]
        if not entry.books:
            empty_subjects.append(subject_id)
    for subject_id in empty_subjects:
        del document.subjects[subject_id]


def _remove_page_from_time_index_section(
    section: dict[str, Any],
    source_sha256: str,
    aligned_page: int,
    original_page: int | None,
) -> None:
    empty_labels: list[str] = []
    for label, entry in section.items():
        if not isinstance(entry, dict):
            continue
        books = entry.get("books")
        if not isinstance(books, dict):
            continue
        book = books.get(source_sha256)
        if not isinstance(book, dict):
            continue
        aligned = book.get("aligned_pages")
        if isinstance(aligned, list) and aligned_page in aligned:
            book["aligned_pages"] = sorted(
                page for page in aligned if page != aligned_page
            )
        original = book.get("original_pages")
        if (
            original_page is not None
            and isinstance(original, list)
            and original_page in original
        ):
            book["original_pages"] = sorted(
                page for page in original if page != original_page
            )
        if not book.get("aligned_pages"):
            del books[source_sha256]
        if not books:
            empty_labels.append(label)
    for label in empty_labels:
        del section[label]


def _purge_polyindex_page(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
    original_page: int | None,
) -> None:
    polyindex_dir = data_root / "polyindex"
    index_path = polyindex_dir / "INDEX.json"
    if index_path.is_file():
        with polyindex_dir_lock(polyindex_dir, ".index.lock"):
            document = PolyindexIndexDocument.load_file(index_path)
            _remove_page_from_index(
                document, source_sha256, aligned_page, original_page
            )
            document.write_atomic(index_path, sort_document=True)
    time_index_path = polyindex_dir / "TIME_INDEX.json"
    if time_index_path.is_file():
        with polyindex_dir_lock(polyindex_dir, ".time_index.lock"):
            try:
                raw = json.loads(time_index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            if isinstance(raw, dict):
                years = raw.get("years")
                dates = raw.get("dates")
                if isinstance(years, dict):
                    _remove_page_from_time_index_section(
                        years, source_sha256, aligned_page, original_page
                    )
                if isinstance(dates, dict):
                    _remove_page_from_time_index_section(
                        dates, source_sha256, aligned_page, original_page
                    )
                _atomic_write_json(time_index_path, raw)


def exclude_book_page(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> dict[str, Any]:
    sha = source_sha256.strip().lower()
    if aligned_page < 1:
        raise PageExcludeError("aligned_page must be positive")
    manifest_path = _manifest_path(data_root, sha)
    manifest = _load_manifest(manifest_path)
    excluded_aligned, pages_to_remove = load_book_exclusions(data_root, sha, manifest=manifest)
    if aligned_page in excluded_aligned:
        raise PageExcludeError(f"page {aligned_page} is already excluded")
    slug = _resolve_slug(data_root, sha, manifest)
    if not slug:
        raise PageExcludeError("book slug not found")
    original_page = _resolve_original_page(
        aligned_page,
        manifest=manifest,
        pages_to_remove=pages_to_remove,
        data_root=data_root,
        source_sha256=sha,
    )
    excluded_aligned = sorted(set(excluded_aligned + [aligned_page]))
    if original_page is not None:
        pages_to_remove = sorted(set(pages_to_remove + [original_page]))
    _save_exclusions(
        data_root,
        sha,
        manifest=manifest,
        excluded_aligned=excluded_aligned,
        pages_to_remove=pages_to_remove,
    )
    deleted_files = _delete_page_artifacts(data_root, sha, slug, aligned_page)
    _purge_polyindex_page(data_root, sha, aligned_page, original_page)
    return {
        "source_sha256": sha,
        "aligned_page": aligned_page,
        "original_page": original_page,
        "excluded_aligned_pages": excluded_aligned,
        "pages_to_remove": pages_to_remove,
        "deleted_files": deleted_files,
    }

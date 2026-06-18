from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PAGE_NUM_RE = re.compile(r"^p\.(\d{4})\.")

STAGE_DIRS: dict[str, tuple[str, str]] = {
    "stage1OCR": ("tmp", ".txt"),
    "stage2Vision": ("tmp", ".md"),
    "stage3Editor": ("tmp", ".md"),
    "output": ("output", ".md"),
}


def _normalize_int_list(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        if isinstance(value, int) and value > 0:
            out.append(value)
    return sorted(set(out))


def load_excluded_aligned_pages(
    data_root: Path,
    source_sha256: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[int]:
    if manifest is None:
        manifest = _load_manifest(data_root / "output" / source_sha256 / "manifest.json")
    if manifest:
        return _normalize_int_list(manifest.get("excluded_aligned_pages"))
    sidecar = data_root / "tmp" / source_sha256 / "exclude_config.json"
    if not sidecar.is_file():
        return []
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, dict):
        return []
    return _normalize_int_list(raw.get("excluded_aligned_pages"))


def _discover_aligned_from_dir(directory: Path) -> set[int]:
    if not directory.is_dir():
        return set()
    found: set[int] = set()
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _PAGE_NUM_RE.match(path.name)
        if match:
            found.add(int(match.group(1)))
    return found


def _stage_page_path(
    data_root: Path,
    source_sha256: str,
    slug: str,
    stage_key: str,
    aligned: int,
) -> Path:
    root_kind, suffix = STAGE_DIRS[stage_key]
    stem = f"p.{aligned:04d}.{slug}{suffix}"
    if root_kind == "tmp":
        subdir = {
            "stage1OCR": "stage1OCR",
            "stage2Vision": "stage2Vision",
            "stage3Editor": "stage3Editor",
        }[stage_key]
        return data_root / "tmp" / source_sha256 / subdir / stem
    return data_root / "output" / source_sha256 / "pages" / stem


def _load_manifest(manifest_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _book_title(manifest: dict[str, Any] | None, source_sha256: str) -> str:
    if manifest:
        reicat = manifest.get("reicat") if isinstance(manifest.get("reicat"), dict) else {}
        title = reicat.get("titolo") or reicat.get("title") or manifest.get("slug")
        if title:
            return str(title)
    return source_sha256[:16] + "…"


def _expected_aligned_pages(
    data_root: Path,
    source_sha256: str,
    slug: str,
    manifest: dict[str, Any] | None,
) -> set[int]:
    expected: set[int] = set()
    if manifest:
        pages = manifest.get("pages")
        if isinstance(pages, list):
            for entry in pages:
                if isinstance(entry, dict) and isinstance(entry.get("aligned"), int):
                    expected.add(entry["aligned"])
    tmp_root = data_root / "tmp" / source_sha256
    for stage_key in ("stage1OCR", "stage2Vision", "stage3Editor"):
        subdir = tmp_root / stage_key
        expected |= _discover_aligned_from_dir(subdir)
    output_pages = data_root / "output" / source_sha256 / "pages"
    expected |= _discover_aligned_from_dir(output_pages)
    if not expected and manifest:
        aligned_count = manifest.get("aligned_page_count")
        if isinstance(aligned_count, int) and aligned_count > 0:
            expected = set(range(1, aligned_count + 1))
    return expected


def _stage_status(
    data_root: Path,
    source_sha256: str,
    slug: str,
    stage_key: str,
    expected: set[int],
) -> dict[str, Any]:
    present: list[int] = []
    missing: list[int] = []
    for aligned in sorted(expected):
        path = _stage_page_path(data_root, source_sha256, slug, stage_key, aligned)
        if path.is_file() and path.stat().st_size > 0:
            present.append(aligned)
        else:
            missing.append(aligned)
    return {
        "present_count": len(present),
        "missing_count": len(missing),
        "missing": missing,
    }


def _list_viewer_pages(
    data_root: Path,
    source_sha256: str,
    manifest: dict[str, Any] | None,
    excluded_set: set[int],
) -> list[int]:
    if manifest:
        aligned_count = manifest.get("aligned_page_count")
        if isinstance(aligned_count, int) and aligned_count > 0:
            return [
                page for page in range(1, aligned_count + 1) if page not in excluded_set
            ]
        pages = manifest.get("pages")
        if isinstance(pages, list):
            found = sorted(
                entry["aligned"]
                for entry in pages
                if isinstance(entry, dict) and isinstance(entry.get("aligned"), int)
            )
            return [page for page in found if page not in excluded_set]
    tmp_render = data_root / "tmp" / source_sha256 / "render"
    if not tmp_render.is_dir():
        return []
    found_pages: list[int] = []
    for path in tmp_render.glob("p.*.png"):
        stem = path.stem
        if stem.startswith("p.") and len(stem) >= 6:
            try:
                found_pages.append(int(stem[2:6]))
            except ValueError:
                continue
    return sorted(page for page in found_pages if page not in excluded_set)


def audit_book(
    data_root: Path,
    source_sha256: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    output_dir = data_root / "output" / source_sha256
    manifest_path = output_dir / "manifest.json"
    if manifest is None and manifest_path.is_file():
        manifest = _load_manifest(manifest_path)
    slug = str(manifest.get("slug") or "") if manifest else ""
    if not slug:
        tmp_root = data_root / "tmp" / source_sha256
        for stage_name in ("stage3Editor", "stage2Vision", "stage1OCR"):
            stage_dir = tmp_root / stage_name
            if not stage_dir.is_dir():
                continue
            for path in stage_dir.iterdir():
                match = re.match(r"^p\.\d{4}\.(.+)\.(txt|md)$", path.name)
                if match:
                    slug = match.group(1)
                    break
            if slug:
                break
    if not slug and not manifest:
        return None
    excluded_aligned = load_excluded_aligned_pages(
        data_root, source_sha256, manifest=manifest
    )
    excluded_set = set(excluded_aligned)
    expected = _expected_aligned_pages(data_root, source_sha256, slug, manifest)
    expected -= excluded_set
    if not expected:
        return None
    stages: dict[str, Any] = {}
    for stage_key in STAGE_DIRS:
        stages[stage_key] = _stage_status(
            data_root, source_sha256, slug, stage_key, expected
        )
    missing_pages: list[dict[str, Any]] = []
    for aligned in sorted(expected):
        missing_in = [
            stage_key
            for stage_key in STAGE_DIRS
            if aligned in stages[stage_key]["missing"]
        ]
        if missing_in:
            missing_pages.append({"aligned": aligned, "missing_in": missing_in})
    total_gaps = sum(stages[key]["missing_count"] for key in STAGE_DIRS)
    from src.persistence.book_page_preview import list_pending_review_pages

    return {
        "source_sha256": source_sha256,
        "title": _book_title(manifest, source_sha256),
        "slug": slug,
        "expected_page_count": len(expected),
        "has_manifest": manifest is not None,
        "complete": total_gaps == 0,
        "stages": stages,
        "missing_pages": missing_pages,
        "excluded_aligned_pages": sorted(excluded_set),
        "viewer_pages": _list_viewer_pages(
            data_root, source_sha256, manifest, excluded_set
        ),
        "pending_review_pages": list_pending_review_pages(data_root, source_sha256),
    }


def _discover_book_shas(data_root: Path) -> set[str]:
    shas: set[str] = set()
    output_dir = data_root / "output"
    if output_dir.is_dir():
        for book_dir in output_dir.iterdir():
            if book_dir.is_dir():
                shas.add(book_dir.name)
    tmp_dir = data_root / "tmp"
    if tmp_dir.is_dir():
        for book_dir in tmp_dir.iterdir():
            if book_dir.is_dir():
                shas.add(book_dir.name)
    return shas


def audit_all_books(data_root: Path) -> dict[str, Any]:
    books: list[dict[str, Any]] = []
    for source_sha256 in sorted(_discover_book_shas(data_root)):
        entry = audit_book(data_root, source_sha256)
        if entry is not None:
            books.append(entry)
    books.sort(key=lambda item: str(item["title"]).casefold())
    books_with_gaps = sum(1 for book in books if not book["complete"])
    total_missing_entries = sum(
        len(book["missing_pages"]) for book in books
    )
    return {
        "books": books,
        "summary": {
            "book_count": len(books),
            "books_with_gaps": books_with_gaps,
            "books_complete": len(books) - books_with_gaps,
            "total_pages_with_gaps": total_missing_entries,
        },
    }

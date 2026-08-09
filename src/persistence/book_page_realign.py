from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.models.polyindex_index import PolyindexIndexDocument
from src.persistence.book_page_exclude import (
    PageExcludeError,
    _atomic_write_json,
    _delete_page_artifacts,
    _manifest_path,
    _safe_sha,
    _save_exclusions,
    _resolve_slug,
    load_book_exclusions,
)
from src.persistence.book_pages_audit import STAGE_DIRS, _load_manifest, _stage_page_path

def _stage_paths_for_page(
    data_root: Path,
    source_sha256: str,
    slug: str,
    aligned_page: int,
) -> list[Path]:
    paths = [_stage_page_path(data_root, source_sha256, slug, key, aligned_page) for key in STAGE_DIRS]
    tmp_root = data_root / "tmp" / source_sha256
    render_dir = tmp_root / "render"
    paths.append(render_dir / f"p.{aligned_page:04d}.png")
    paths.append(render_dir / f"p.{aligned_page:04d}.png.json")
    paths.append(tmp_root / "stageTimeIndex" / f"p.{aligned_page:04d}.{slug}.json")
    return paths


def _two_phase_rename(moves: list[tuple[Path, Path]]) -> list[str]:
    renamed: list[str] = []
    temps: list[tuple[Path, Path]] = []
    for src, dst in moves:
        if not src.is_file() or src.resolve() == dst.resolve():
            continue
        tmp = src.with_name(src.name + ".realign_tmp")
        if tmp.exists():
            tmp.unlink()
        src.rename(tmp)
        temps.append((tmp, dst))
    for tmp, dst in temps:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        tmp.rename(dst)
        renamed.append(str(dst))
    return renamed


def _remap_page_list(values: object, old_to_new: dict[int, int]) -> list[int]:
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        if isinstance(value, int) and value in old_to_new:
            out.append(old_to_new[value])
    return sorted(set(out))


def _remap_manifest_range(
    manifest: dict[str, Any],
    original_key: str,
    aligned_key: str,
    original_to_aligned: dict[int, int],
) -> None:
    raw = manifest.get(original_key)
    if not isinstance(raw, dict):
        return
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return
    mapped = [
        original_to_aligned[page]
        for page in range(start, end + 1)
        if page in original_to_aligned
    ]
    if not mapped:
        manifest.pop(aligned_key, None)
        return
    manifest[aligned_key] = {"start": min(mapped), "end": max(mapped)}


def _remap_book_index_json(path: Path, old_to_new: dict[int, int]) -> None:
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(raw, dict):
        return
    subjects = raw.get("subjects")
    if isinstance(subjects, dict):
        for entry in subjects.values():
            if not isinstance(entry, dict):
                continue
            entry["aligned_pages"] = _remap_page_list(entry.get("aligned_pages"), old_to_new)
    page_subjects = raw.get("page_subjects")
    if isinstance(page_subjects, dict):
        remapped: dict[str, Any] = {}
        for key, value in page_subjects.items():
            try:
                old_page = int(key)
            except (TypeError, ValueError):
                continue
            new_page = old_to_new.get(old_page)
            if new_page is None:
                continue
            remapped[str(new_page)] = value
        raw["page_subjects"] = remapped
    _atomic_write_json(path, raw)


def _remap_polyindex_book_pages(
    data_root: Path,
    source_sha256: str,
    old_to_new: dict[int, int],
) -> None:
    polyindex_dir = data_root / "polyindex"
    index_path = polyindex_dir / "INDEX.json"
    if index_path.is_file():
        with polyindex_dir_lock(polyindex_dir, ".index.lock"):
            document = PolyindexIndexDocument.load_file(index_path)
            empty_subjects: list[str] = []
            for subject_id, entry in document.subjects.items():
                book = entry.books.get(source_sha256)
                if book is None:
                    continue
                book.aligned_pages = _remap_page_list(book.aligned_pages, old_to_new)
                if not book.aligned_pages:
                    del entry.books[source_sha256]
                if not entry.books:
                    empty_subjects.append(subject_id)
            for subject_id in empty_subjects:
                del document.subjects[subject_id]
            document.write_atomic(index_path, sort_document=True)
    time_index_path = polyindex_dir / "TIME_INDEX.json"
    if time_index_path.is_file():
        with polyindex_dir_lock(polyindex_dir, ".time_index.lock"):
            try:
                raw = json.loads(time_index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            if isinstance(raw, dict):
                for section_key in ("years", "dates"):
                    section = raw.get(section_key)
                    if not isinstance(section, dict):
                        continue
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
                        book["aligned_pages"] = _remap_page_list(
                            book.get("aligned_pages"), old_to_new
                        )
                        if not book.get("aligned_pages"):
                            del books[source_sha256]
                        if not books:
                            empty_labels.append(label)
                    for label in empty_labels:
                        del section[label]
                _atomic_write_json(time_index_path, raw)


def _rebuild_aligned_pdf(data_root: Path, manifest: dict[str, Any]) -> int | None:
    from src.ingestion.pdf_alignment import build_aligned_pdf
    from src.persistence.book_page_repair import (
        _find_raw_pdf_by_digest,
        build_enriched_from_manifest,
    )

    source_sha256 = str(manifest.get("source_sha256") or "").strip().lower()
    if not source_sha256 or _find_raw_pdf_by_digest(data_root, source_sha256) is None:
        return None
    enriched = build_enriched_from_manifest(data_root, manifest)
    result = build_aligned_pdf(enriched, str(data_root / "input" / "processed"))
    return int(result.aligned_page_count)


def compact_book_alignment(data_root: Path, source_sha256: str) -> dict[str, Any]:
    sha = _safe_sha(source_sha256)
    manifest_path = _manifest_path(data_root, sha)
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        raise PageExcludeError("manifest not found for book")
    slug = _resolve_slug(data_root, sha, manifest)
    if not slug:
        raise PageExcludeError("book slug not found")
    original_count = manifest.get("original_page_count")
    if not isinstance(original_count, int) or original_count < 1:
        raise PageExcludeError("invalid original_page_count")
    _excluded, pages_to_remove = load_book_exclusions(data_root, sha, manifest=manifest)
    aligned_count, original_to_aligned, _aligned_to_original = build_page_removal_mapping(
        original_count, pages_to_remove
    )
    pages = manifest.get("pages")
    if not isinstance(pages, list):
        pages = []
    old_to_new: dict[int, int] = {}
    new_pages: list[dict[str, object]] = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        old_aligned = entry.get("aligned")
        original = entry.get("original")
        if not isinstance(old_aligned, int) or not isinstance(original, int):
            continue
        if original in pages_to_remove:
            continue
        new_aligned = original_to_aligned.get(original)
        if new_aligned is None:
            continue
        old_to_new[old_aligned] = new_aligned
        file_name = f"pages/p.{new_aligned:04d}.{slug}.md"
        new_pages.append(
            {"aligned": new_aligned, "original": original, "file": file_name}
        )
    new_pages.sort(key=lambda item: int(item["aligned"]))
    moves: list[tuple[Path, Path]] = []
    for old_aligned, new_aligned in sorted(old_to_new.items()):
        if old_aligned == new_aligned:
            continue
        old_paths = _stage_paths_for_page(data_root, sha, slug, old_aligned)
        new_paths = _stage_paths_for_page(data_root, sha, slug, new_aligned)
        for src, dst in zip(old_paths, new_paths):
            moves.append((src, dst))
    renamed = _two_phase_rename(moves)
    occupied = set(old_to_new.values())
    for old_aligned in sorted(set(_excluded), reverse=True):
        if old_aligned in occupied or old_aligned in old_to_new:
            continue
        _delete_page_artifacts(data_root, sha, slug, old_aligned)
    from src.persistence.book_page_preview import (
        _load_review_pending_set,
        _save_review_pending_set,
    )

    pending = _load_review_pending_set(data_root, sha)
    if pending:
        _save_review_pending_set(
            data_root,
            sha,
            {old_to_new[page] for page in pending if page in old_to_new},
        )
    _remap_polyindex_book_pages(data_root, sha, old_to_new)
    for index_json in (data_root / "output" / sha).glob("INDEX_*.json"):
        _remap_book_index_json(index_json, old_to_new)
    manifest["pages"] = new_pages
    manifest["aligned_page_count"] = aligned_count
    manifest["pages_to_remove"] = pages_to_remove
    manifest["excluded_aligned_pages"] = []
    _remap_manifest_range(manifest, "toc_range", "toc_range_aligned", original_to_aligned)
    _remap_manifest_range(manifest, "index_range", "index_range_aligned", original_to_aligned)
    _remap_manifest_range(manifest, "biblio_range", "biblio_range_aligned", original_to_aligned)
    pdf_count = _rebuild_aligned_pdf(data_root, manifest)
    if pdf_count is not None and pdf_count != aligned_count:
        raise PageExcludeError(
            f"aligned pdf page count mismatch: pdf={pdf_count} expected={aligned_count}"
        )
    _atomic_write_json(manifest_path, manifest)
    _save_exclusions(
        data_root,
        sha,
        manifest=None,
        excluded_aligned=[],
        pages_to_remove=pages_to_remove,
    )
    return {
        "source_sha256": sha,
        "aligned_page_count": aligned_count,
        "pages_to_remove": pages_to_remove,
        "renamed_files": len(renamed),
        "old_to_new_count": len(old_to_new),
    }


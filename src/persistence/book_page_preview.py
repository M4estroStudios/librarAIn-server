from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.hashing import compute_file_sha256
from src.ingestion.pipeline.render import _render_pdf_page_to_png
from src.ingestion.pipeline.md_cache import stage_md_cached_model, write_stage_md
from src.persistence.book_pages_audit import _load_manifest, _stage_page_path

DEFAULT_RENDER_DPI = 200
_TRANSCRIPT_STAGE_ORDER = ("stage3Editor", "output", "stage1OCR")
_REVIEW_PENDING_FILE = "review_pending.json"


class PagePreviewError(ValueError):
    pass


def _aligned_pdf_path(data_root: Path, source_sha256: str) -> Path:
    sha = source_sha256.strip().lower()
    candidate = data_root / "input" / "processed" / f"{sha}.pdf"
    if not candidate.is_file():
        raise PagePreviewError(f"aligned pdf not found for book {sha[:16]}…")
    return candidate


def _render_png_path(data_root: Path, source_sha256: str, aligned_page: int) -> Path:
    sha = source_sha256.strip().lower()
    return data_root / "tmp" / sha / "render" / f"p.{aligned_page:04d}.png"


def ensure_page_render_png(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
    *,
    dpi: int = DEFAULT_RENDER_DPI,
) -> Path:
    if aligned_page < 1:
        raise PagePreviewError("aligned_page must be positive")
    sha = source_sha256.strip().lower()
    pdf_path = _aligned_pdf_path(data_root, sha)
    png_path = _render_png_path(data_root, sha, aligned_page)
    if png_path.is_file() and png_path.stat().st_size > 0:
        return png_path
    render_digest = compute_file_sha256(pdf_path)
    try:
        _render_pdf_page_to_png(
            pdf_path,
            aligned_page - 1,
            png_path,
            dpi=dpi,
            source_sha256=render_digest,
        )
    except Exception as exc:
        raise PagePreviewError(f"unable to render page {aligned_page}: {exc}") from exc
    if not png_path.is_file():
        raise PagePreviewError(f"render output missing for page {aligned_page}")
    return png_path


def _book_slug(data_root: Path, source_sha256: str) -> str:
    sha = source_sha256.strip().lower()
    manifest = _load_manifest(data_root / "output" / sha / "manifest.json")
    if manifest:
        slug = str(manifest.get("slug") or "").strip()
        if slug:
            return slug
    tmp_root = data_root / "tmp" / sha
    for stage_name in ("stage3Editor", "stage2Vision", "stage1OCR"):
        stage_dir = tmp_root / stage_name
        if not stage_dir.is_dir():
            continue
        for path in stage_dir.iterdir():
            stem = path.stem
            if stem.startswith("p.") and len(stem) > 6:
                parts = stem.split(".", 2)
                if len(parts) == 3 and parts[2]:
                    return parts[2]
    raise PagePreviewError(f"book slug not found for {sha[:16]}…")


def _resolve_page_transcript_path(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> tuple[Path, str]:
    if aligned_page < 1:
        raise PagePreviewError("aligned_page must be positive")
    sha = source_sha256.strip().lower()
    slug = _book_slug(data_root, sha)
    for stage_key in _TRANSCRIPT_STAGE_ORDER:
        path = _stage_page_path(data_root, sha, slug, stage_key, aligned_page)
        if path.is_file() and path.stat().st_size > 0:
            return path, stage_key
    raise PagePreviewError(f"transcript not found for page {aligned_page}")


def _default_transcript_path(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> tuple[Path, str]:
    sha = source_sha256.strip().lower()
    slug = _book_slug(data_root, sha)
    return (
        _stage_page_path(data_root, sha, slug, "stage1OCR", aligned_page),
        "stage1OCR",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text if text.endswith("\n") else text + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)


def _split_model_marker(raw: str) -> tuple[str, str | None]:
    if not raw:
        return raw, None
    if "\n" in raw:
        first, body = raw.split("\n", 1)
    else:
        first, body = raw, ""
    model = stage_md_cached_model(first.strip())
    if model is not None:
        return body, model
    return raw, None


def _read_cached_model(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    first = raw.split("\n", 1)[0].strip()
    return stage_md_cached_model(first)


def load_page_transcript(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> tuple[str, str, str | None]:
    path, stage_key = _resolve_page_transcript_path(
        data_root, source_sha256, aligned_page
    )
    raw = path.read_text(encoding="utf-8")
    if stage_key in ("stage3Editor", "output"):
        text, model = _split_model_marker(raw)
        return text, stage_key, model
    return raw, stage_key, None


def save_page_transcript(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
    text: str,
) -> dict[str, str]:
    if aligned_page < 1:
        raise PagePreviewError("aligned_page must be positive")
    if not isinstance(text, str):
        raise PagePreviewError("text must be a string")
    try:
        path, stage_key = _resolve_page_transcript_path(
            data_root, source_sha256, aligned_page
        )
    except PagePreviewError:
        path, stage_key = _default_transcript_path(
            data_root, source_sha256, aligned_page
        )
    _atomic_write_text(path, text)
    return {"stage": stage_key, "path": str(path)}


def _review_pending_path(data_root: Path, source_sha256: str) -> Path:
    sha = source_sha256.strip().lower()
    return data_root / "tmp" / sha / _REVIEW_PENDING_FILE


def _load_review_pending_set(data_root: Path, source_sha256: str) -> set[int]:
    path = _review_pending_path(data_root, source_sha256)
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {value for value in raw if isinstance(value, int) and value > 0}


def _save_review_pending_set(
    data_root: Path,
    source_sha256: str,
    pages: set[int],
) -> None:
    path = _review_pending_path(data_root, source_sha256)
    if not pages:
        if path.is_file():
            path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sorted(pages), ensure_ascii=False, indent=2)
    _atomic_write_text(path, payload)


def mark_page_pending_review(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> None:
    if aligned_page < 1:
        raise PagePreviewError("aligned_page must be positive")
    pending = _load_review_pending_set(data_root, source_sha256)
    pending.add(aligned_page)
    _save_review_pending_set(data_root, source_sha256, pending)


def clear_page_pending_review(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
) -> None:
    pending = _load_review_pending_set(data_root, source_sha256)
    pending.discard(aligned_page)
    _save_review_pending_set(data_root, source_sha256, pending)


def list_pending_review_pages(
    data_root: Path,
    source_sha256: str,
) -> list[int]:
    return sorted(_load_review_pending_set(data_root, source_sha256))


def _resolve_original_page(manifest: dict[str, Any], aligned_page: int) -> int:
    pages = manifest.get("pages")
    if isinstance(pages, list):
        for entry in pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            original = entry.get("original")
            if aligned == aligned_page and isinstance(original, int) and original > 0:
                return original
    return aligned_page


def confirm_page_transcript(
    data_root: Path,
    source_sha256: str,
    aligned_page: int,
    text: str,
) -> dict[str, Any]:
    if aligned_page < 1:
        raise PagePreviewError("aligned_page must be positive")
    if not isinstance(text, str):
        raise PagePreviewError("text must be a string")
    sha = source_sha256.strip().lower()
    manifest_path = data_root / "output" / sha / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        raise PagePreviewError("manifest not found for book")
    slug = _book_slug(data_root, sha)
    stage3_path = _stage_page_path(data_root, sha, slug, "stage3Editor", aligned_page)
    output_path = _stage_page_path(data_root, sha, slug, "output", aligned_page)
    body, pasted_model = _split_model_marker(text)
    model = (
        _read_cached_model(stage3_path)
        or _read_cached_model(output_path)
        or pasted_model
        or "manual-review"
    )
    stage3_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_stage_md(stage3_path, model, body)
    write_stage_md(output_path, model, body)
    from src.ingestion.output_writer import (
        _atomic_write_bytes,
        _manifest_core,
        _page_filename,
        _utc_now_iso,
    )

    filename = _page_filename(aligned_page, slug)
    rel_path = f"pages/{filename}"
    merged_entries: dict[int, dict[str, object]] = {}
    existing_pages = manifest.get("pages")
    if isinstance(existing_pages, list):
        for entry in existing_pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            if isinstance(aligned, int):
                merged_entries[aligned] = dict(entry)
    merged_entries[aligned_page] = {
        "aligned": aligned_page,
        "original": _resolve_original_page(manifest, aligned_page),
        "file": rel_path,
    }
    manifest_data: dict[str, object] = dict(manifest)
    manifest_data.update(
        {
            "source_sha256": sha,
            "slug": slug,
            "pages": sorted(merged_entries.values(), key=lambda item: int(item["aligned"])),
            "generated_at": _utc_now_iso(),
        }
    )
    for key in ("excluded_aligned_pages", "pages_to_remove", "reicat", "pipeline_version"):
        if key in manifest:
            manifest_data[key] = manifest[key]
    new_core = _manifest_core(manifest_data)
    old_core = _manifest_core(manifest)
    if new_core != old_core:
        manifest_bytes = json.dumps(manifest_data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(manifest_path, manifest_bytes)
    clear_page_pending_review(data_root, sha, aligned_page)
    return {
        "stage3_path": str(stage3_path),
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "aligned_page": aligned_page,
        "producer_model": model,
    }

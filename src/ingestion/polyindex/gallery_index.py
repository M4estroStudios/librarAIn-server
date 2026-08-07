from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.output_writer import BookOutput, strip_page_frontmatter

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "book_gallery_index_json_path",
    "extract_gallery_captions",
    "sync_gallery_index_from_book",
    "write_gallery_index",
]


def book_gallery_index_json_path(output_dir: Path, slug: str) -> Path:
    return output_dir / f"GALLERY_INDEX_{slug}.json"


def _atomic_write_json(dest: Path, payload: dict[str, object]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


def _blockquote_line_content(line: str) -> str | None:
    if not line.startswith(">"):
        return None
    content = line[1:]
    if content.startswith(" "):
        content = content[1:]
    return content


def _normalize_caption(lines: list[str]) -> str:
    parts = [line.strip() for line in lines]
    nonempty = [part for part in parts if part]
    if not nonempty:
        return ""
    return " ".join(nonempty)


def extract_gallery_captions(text: str) -> list[str]:
    body = strip_page_frontmatter(text)
    captions: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        content = _blockquote_line_content(line)
        if content is not None:
            current.append(content)
            continue
        if current:
            captions.append(_normalize_caption(current))
            current = []
    if current:
        captions.append(_normalize_caption(current))
    return captions


def _entries_from_page_captions(
    page_captions: Iterable[tuple[int, int, Sequence[str]]],
) -> tuple[list[dict[str, object]], int]:
    entries: list[dict[str, object]] = []
    pages_with_images: set[int] = set()
    for aligned_page, original_page, captions in page_captions:
        if not captions:
            continue
        pages_with_images.add(int(aligned_page))
        for caption in captions:
            entries.append(
                {
                    "aligned_page": int(aligned_page),
                    "original_page": int(original_page),
                    "caption": str(caption),
                }
            )
    entries.sort(
        key=lambda entry: (
            int(entry["aligned_page"]),
            str(entry["caption"]),
        )
    )
    return entries, len(pages_with_images)


def write_gallery_index(
    output_dir: Path,
    slug: str,
    page_captions: Iterable[tuple[int, int, Sequence[str]]],
    *,
    request_id: str = "",
) -> tuple[Path, dict[str, Any]]:
    entries, n_pages = _entries_from_page_captions(page_captions)
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "entries": entries,
    }
    gallery_path = book_gallery_index_json_path(output_dir, slug)
    _atomic_write_json(gallery_path, document)
    stats: dict[str, Any] = {
        "gallery_index_path": str(gallery_path),
        "n_entries": len(entries),
        "n_pages": n_pages,
    }
    Log(
        INFO_LOG_LEVEL,
        "gallery index sync completed",
        {
            "request_id": request_id,
            "book_slug": slug,
            **stats,
        },
    )
    return gallery_path, stats


def sync_gallery_index_from_book(
    book_output: BookOutput,
    *,
    request_id: str = "",
) -> tuple[Path, dict[str, Any]]:
    page_captions: list[tuple[int, int, list[str]]] = []
    for page in book_output.pages:
        if not page.file.is_file():
            continue
        text = page.file.read_text(encoding="utf-8")
        page_captions.append(
            (page.aligned, page.original, extract_gallery_captions(text))
        )
    return write_gallery_index(
        book_output.output_dir,
        book_output.slug,
        page_captions,
        request_id=request_id,
    )

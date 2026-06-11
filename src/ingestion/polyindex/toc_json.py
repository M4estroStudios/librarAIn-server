from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.core.log import Log, WARNING_LOG_LEVEL
from src.ingestion.output_writer import BookOutput
from src.ingestion.polyindex.chapter_patterns import try_match_chapter_line
from src.models.polyindex_toc import (
    PolyindexTocBookEntry,
    PolyindexTocChapter,
    PolyindexTocDocument,
)
from src.models.request import UsefulPagesEnumeration

if sys.platform != "win32":
    import fcntl

ChapterEntry = PolyindexTocChapter


def _is_skippable_toc_line(stripped: str) -> bool:
    if not stripped:
        return True
    if stripped == "---":
        return True
    if stripped.startswith("# TOC"):
        return True
    if stripped.startswith("#"):
        return True
    return False


def parse_chapters_from_toc_md(
    toc_md_path: Path,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> list[PolyindexTocChapter]:
    text = toc_md_path.read_text(encoding="utf-8")
    mapping = useful_pages_enumeration.original_page_to_aligned_page
    useful_original = useful_pages_enumeration.useful_original_pages
    last_useful_original = max(useful_original) if useful_original else 0
    last_useful_aligned = mapping.get(last_useful_original, last_useful_original)

    parsed: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _is_skippable_toc_line(stripped):
            continue

        match = try_match_chapter_line(stripped)
        if match is None:
            continue

        original_page = match.original_page
        if original_page not in mapping:
            Log(
                WARNING_LOG_LEVEL,
                "toc chapter page not in mapping",
                {"line": stripped, "original_page": original_page},
            )
            continue

        parsed.append((match.label, original_page, mapping[original_page]))

    if not parsed:
        return []

    parsed.sort(key=lambda item: (item[1], item[0]))

    entries: list[PolyindexTocChapter] = []
    for index, (label, original_start, aligned_start) in enumerate(parsed):
        if index + 1 < len(parsed):
            next_original_start = parsed[index + 1][1]
            next_aligned_start = parsed[index + 1][2]
            original_end = next_original_start - 1
            aligned_end = next_aligned_start - 1
        else:
            original_end = last_useful_original
            aligned_end = last_useful_aligned

        entries.append(
            PolyindexTocChapter(
                label=label,
                aligned_page_start=aligned_start,
                aligned_page_end=aligned_end,
                original_page_start=original_start,
                original_page_end=original_end,
            )
        )

    return entries


@contextmanager
def _toc_file_lock(polyindex_dir: Path) -> Iterator[None]:
    polyindex_dir.mkdir(parents=True, exist_ok=True)
    lock_path = polyindex_dir / ".toc.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if sys.platform != "win32":
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            pass
        try:
            yield
        finally:
            if sys.platform != "win32":
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def update_polyindex_toc(
    polyindex_dir: Path,
    source_sha256: str,
    book_entry: PolyindexTocBookEntry,
) -> Path:
    toc_path = polyindex_dir / "TOC.json"
    with _toc_file_lock(polyindex_dir):
        document = PolyindexTocDocument.load_file(toc_path)
        document.upsert_book(source_sha256, book_entry)
        document.write_atomic(toc_path)
    return toc_path


def chapter_entries_to_dicts(
    entries: list[PolyindexTocChapter],
) -> list[dict[str, object]]:
    return [entry.model_dump(mode="json") for entry in entries]


def _resolve_book_title(book_output: BookOutput) -> str:
    try:
        data = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
        reicat = data.get("reicat")
        if isinstance(reicat, dict):
            title = reicat.get("titolo") or reicat.get("title")
            if title:
                return str(title).strip()
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        pass
    return book_output.slug


def sync_polyindex_toc_from_book(
    polyindex_dir: Path,
    source_sha256: str,
    book_output: BookOutput,
    toc_md_path: Path,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> Path:
    chapters = parse_chapters_from_toc_md(toc_md_path, useful_pages_enumeration)
    book_entry = PolyindexTocBookEntry(
        title=_resolve_book_title(book_output),
        slug=book_output.slug,
        chapters=chapters,
    )
    return update_polyindex_toc(polyindex_dir, source_sha256, book_entry)

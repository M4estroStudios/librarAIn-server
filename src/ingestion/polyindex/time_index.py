from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.ingestion.polyindex.time_extract import (
    _year_sort_key,
    extract_time_references,
)
from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page
from src.models.settings import Settings

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "book_time_index_json_path",
    "extract_time_references",
    "sync_time_index_from_book",
    "sync_time_index_from_book_async",
]


def book_time_index_json_path(output_dir: Path, slug: str) -> Path:
    return output_dir / f"TIME_INDEX_{slug}.json"


def _empty_time_index_document() -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "years": {}, "dates": {}}


def _empty_book_time_index_document() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "years": {},
        "dates": {},
        "page_years": {},
        "page_dates": {},
    }


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


def _merge_local_entry_pages(
    section: dict[str, Any],
    label: str,
    aligned_page: int,
    original_page: int,
) -> None:
    entry = section.get(label)
    if not isinstance(entry, dict):
        entry = {"aligned_pages": [], "original_pages": []}
        section[label] = entry
    aligned = entry.setdefault("aligned_pages", [])
    original = entry.setdefault("original_pages", [])
    if not isinstance(aligned, list):
        aligned = []
        entry["aligned_pages"] = aligned
    if not isinstance(original, list):
        original = []
        entry["original_pages"] = original
    if aligned_page not in aligned:
        aligned.append(aligned_page)
    if original_page not in original:
        original.append(original_page)


def _append_page_labels(
    page_map: dict[str, list[str]],
    aligned_page: int,
    labels: set[str],
) -> None:
    if not labels:
        return
    key = str(aligned_page)
    existing = page_map.get(key)
    if not isinstance(existing, list):
        existing = []
        page_map[key] = existing
    for label in sorted(labels):
        if label not in existing:
            existing.append(label)


def _sort_book_time_index_document(document: dict[str, object]) -> dict[str, object]:
    years = document.get("years")
    if isinstance(years, dict):
        document["years"] = {
            label: years[label] for label in sorted(years, key=_year_sort_key)
        }
    dates = document.get("dates")
    if isinstance(dates, dict):
        document["dates"] = {label: dates[label] for label in sorted(dates)}
    for section_name in ("years", "dates"):
        section = document.get(section_name)
        if not isinstance(section, dict):
            continue
        for entry in section.values():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("aligned_pages"), list):
                entry["aligned_pages"] = sorted(set(entry["aligned_pages"]))
            if isinstance(entry.get("original_pages"), list):
                entry["original_pages"] = sorted(set(entry["original_pages"]))
    for map_name in ("page_years", "page_dates"):
        page_map = document.get(map_name)
        if not isinstance(page_map, dict):
            continue
        document[map_name] = {
            page: sorted(set(labels)) if isinstance(labels, list) else []
            for page, labels in sorted(
                page_map.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 10**9
            )
        }
    return document


def _build_book_time_index_document(
    page_refs: list[tuple[int, int, set[str], set[str]]],
) -> dict[str, object]:
    document = _empty_book_time_index_document()
    years_section = document["years"]
    dates_section = document["dates"]
    page_years = document["page_years"]
    page_dates = document["page_dates"]
    assert isinstance(years_section, dict)
    assert isinstance(dates_section, dict)
    assert isinstance(page_years, dict)
    assert isinstance(page_dates, dict)
    for aligned_page, original_page, years, dates in page_refs:
        for year_label in years:
            _merge_local_entry_pages(
                years_section, year_label, aligned_page, original_page
            )
        for date_label in dates:
            _merge_local_entry_pages(
                dates_section, date_label, aligned_page, original_page
            )
        _append_page_labels(page_years, aligned_page, years)
        _append_page_labels(page_dates, aligned_page, dates)
    return _sort_book_time_index_document(document)


def _merge_entry_pages(
    section: dict[str, Any],
    label: str,
    source_sha256: str,
    aligned_page: int,
    original_page: int,
    *,
    book_title: str | None,
    book_slug: str | None,
) -> None:
    entry = section.get(label)
    if not isinstance(entry, dict):
        entry = {"books": {}}
        section[label] = entry
    books = entry.get("books")
    if not isinstance(books, dict):
        books = {}
        entry["books"] = books
    book = books.get(source_sha256)
    if not isinstance(book, dict):
        book = {}
        if book_title:
            book["title"] = book_title
        if book_slug:
            book["slug"] = book_slug
        book["aligned_pages"] = []
        book["original_pages"] = []
        books[source_sha256] = book
    aligned = book.setdefault("aligned_pages", [])
    original = book.setdefault("original_pages", [])
    if aligned_page not in aligned:
        aligned.append(aligned_page)
    if original_page not in original:
        original.append(original_page)


def _sort_time_index_document(document: dict[str, object]) -> dict[str, object]:
    years = document.get("years")
    if isinstance(years, dict):
        document["years"] = {
            label: years[label]
            for label in sorted(years, key=_year_sort_key)
        }
    dates = document.get("dates")
    if isinstance(dates, dict):
        document["dates"] = {label: dates[label] for label in sorted(dates)}
    for section in (document.get("years"), document.get("dates")):
        if not isinstance(section, dict):
            continue
        for entry in section.values():
            if not isinstance(entry, dict):
                continue
            books = entry.get("books")
            if not isinstance(books, dict):
                continue
            entry["books"] = dict(sorted(books.items()))
            for book in entry["books"].values():
                if isinstance(book, dict):
                    if isinstance(book.get("aligned_pages"), list):
                        book["aligned_pages"] = sorted(set(book["aligned_pages"]))
                    if isinstance(book.get("original_pages"), list):
                        book["original_pages"] = sorted(set(book["original_pages"]))
    return document


def _purge_book_from_section(section: dict[str, Any], source_sha256: str) -> None:
    empty_labels: list[str] = []
    for label, entry in section.items():
        if not isinstance(entry, dict):
            continue
        books = entry.get("books")
        if isinstance(books, dict) and source_sha256 in books:
            del books[source_sha256]
            if not books:
                empty_labels.append(label)
    for label in empty_labels:
        del section[label]


def sync_time_index_from_book(
    polyindex_dir: Path,
    source_sha256: str,
    book_output: BookOutput,
    *,
    book_title: str | None = None,
    request_id: str = "",
    client: openai.OpenAI | None = None,
    settings: Settings | None = None,
    prompt_notes: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    return asyncio.run(
        sync_time_index_from_book_async(
            polyindex_dir,
            source_sha256,
            book_output,
            book_title=book_title,
            request_id=request_id,
            client=client,
            settings=settings,
            prompt_notes=prompt_notes,
        )
    )


async def sync_time_index_from_book_async(
    polyindex_dir: Path,
    source_sha256: str,
    book_output: BookOutput,
    *,
    book_title: str | None = None,
    request_id: str = "",
    client: openai.OpenAI | None = None,
    settings: Settings | None = None,
    prompt_notes: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    time_index_path = polyindex_dir / "TIME_INDEX.json"
    book_time_index_path = book_time_index_json_path(
        book_output.output_dir, book_output.slug
    )

    page_refs: list[tuple[int, int, set[str], set[str]]] = []
    llm_pages = 0
    sem = (
        asyncio.Semaphore(settings.max_parallel_request)
        if settings is not None
        else asyncio.Semaphore(1)
    )

    async def _scan_page(page: BookPageOutput) -> tuple[int, int, set[str], set[str], bool] | None:
        if not page.file.is_file():
            return None
        text = page.file.read_text(encoding="utf-8")
        async with sem:
            years, dates, used_llm = await extract_time_references_for_page(
                text,
                client=client,
                settings=settings,
                request_id=request_id,
                aligned_page=page.aligned,
                prompt_notes=prompt_notes,
                source_sha256=source_sha256,
                book_slug=book_output.slug,
            )
        if not years and not dates:
            return None
        return page.aligned, page.original, years, dates, used_llm

    scan_results = await asyncio.gather(
        *(_scan_page(page) for page in book_output.pages)
    )
    for result in scan_results:
        if result is None:
            continue
        aligned_page, original_page, years, dates, used_llm = result
        page_refs.append((aligned_page, original_page, years, dates))
        if used_llm:
            llm_pages += 1

    book_document = _build_book_time_index_document(page_refs)
    _atomic_write_json(book_time_index_path, book_document)

    with polyindex_dir_lock(polyindex_dir, ".time_index.lock"):
        if time_index_path.is_file():
            document = json.loads(time_index_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                document = _empty_time_index_document()
        else:
            document = _empty_time_index_document()

        document["schema_version"] = SCHEMA_VERSION
        years_section = document.setdefault("years", {})
        dates_section = document.setdefault("dates", {})
        if not isinstance(years_section, dict):
            years_section = {}
            document["years"] = years_section
        if not isinstance(dates_section, dict):
            dates_section = {}
            document["dates"] = dates_section

        _purge_book_from_section(years_section, source_sha256)
        _purge_book_from_section(dates_section, source_sha256)

        for aligned_page, original_page, years, dates in page_refs:
            for year_label in years:
                _merge_entry_pages(
                    years_section,
                    year_label,
                    source_sha256,
                    aligned_page,
                    original_page,
                    book_title=book_title,
                    book_slug=book_output.slug,
                )
            for date_label in dates:
                _merge_entry_pages(
                    dates_section,
                    date_label,
                    source_sha256,
                    aligned_page,
                    original_page,
                    book_title=book_title,
                    book_slug=book_output.slug,
                )

        _atomic_write_json(time_index_path, _sort_time_index_document(document))

    local_years = book_document.get("years")
    local_dates = book_document.get("dates")
    stats = {
        "n_years": len(local_years) if isinstance(local_years, dict) else 0,
        "n_dates": len(local_dates) if isinstance(local_dates, dict) else 0,
        "n_pages_scanned": len(book_output.pages),
        "n_llm_pages": llm_pages,
        "book_time_index_path": str(book_time_index_path),
    }
    Log(
        INFO_LOG_LEVEL,
        "time index sync completed",
        {
            "time_index_path": str(time_index_path),
            "request_id": request_id,
            **stats,
        },
    )
    return time_index_path, stats

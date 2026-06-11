from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page
from src.models.settings import Settings

SCHEMA_VERSION = "1.0"

_MONTHS = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
)

_ERA_SUFFIX = r"(?:a|d)\.\s*C\."

_DATE_PATTERN = re.compile(
    r"\b(?P<day>[1-9]\d?)\s*°?\s+(?P<month>" + "|".join(_MONTHS) + r")"
    r"(?:\s+(?P<year>\d{1,4})\s*(?P<era>" + _ERA_SUFFIX + r")?)?\b",
    re.IGNORECASE,
)

_YEAR_WITH_ERA_PATTERN = re.compile(
    r"\b(?P<year>\d{1,4})\s*(?P<era>" + _ERA_SUFFIX + r")",
    re.IGNORECASE,
)

_BARE_YEAR_PATTERN = re.compile(
    r"(?<![\d.])\b(?P<year>\d{3,4})\b(?!\s*" + _ERA_SUFFIX + r")"
)
_PAGE_REF_BEFORE = re.compile(
    r"(?:\bpp?\.|\bpagg?\.)\s*[\d\s,.\u2013\u2014-]*$", re.IGNORECASE
)

_BARE_YEAR_MIN = 100
_BARE_YEAR_MAX = 2099


def _normalize_era(era_raw: str | None) -> str | None:
    if not era_raw:
        return None
    compact = era_raw.replace(" ", "").lower()
    return "a.C." if compact.startswith("a") else "d.C."


def _year_label(year: int, era: str | None) -> str:
    if era:
        return f"{year} {era}"
    return str(year)


def _year_sort_key(label: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)(?:\s+(a\.C\.))?$", label)
    if match is None:
        return (1, 10**6)
    value = int(match.group(1))
    if match.group(2):
        return (0, -value)
    return (1, value)


def extract_time_references(text: str) -> tuple[set[str], set[str]]:
    """Extract year labels and date labels from a page of text."""
    years: set[str] = set()
    dates: set[str] = set()

    date_spans: list[tuple[int, int]] = []
    for match in _DATE_PATTERN.finditer(text):
        day = int(match.group("day"))
        if day < 1 or day > 31:
            continue
        month = match.group("month").lower()
        year_raw = match.group("year")
        era = _normalize_era(match.group("era"))
        if year_raw:
            year = int(year_raw)
            label = f"{day} {month} {_year_label(year, era)}"
            years.add(_year_label(year, era))
        else:
            label = f"{day} {month}"
        dates.add(label)
        date_spans.append(match.span())

    def _inside_date(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in date_spans)

    for match in _YEAR_WITH_ERA_PATTERN.finditer(text):
        if _inside_date(*match.span()):
            continue
        year = int(match.group("year"))
        if year < 1:
            continue
        years.add(_year_label(year, _normalize_era(match.group("era"))))

    for match in _BARE_YEAR_PATTERN.finditer(text):
        if _inside_date(*match.span()):
            continue
        year = int(match.group("year"))
        if year < _BARE_YEAR_MIN or year > _BARE_YEAR_MAX:
            continue
        if _PAGE_REF_BEFORE.search(text[: match.start()]):
            continue
        years.add(str(year))

    return years, dates


def _empty_time_index_document() -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "years": {}, "dates": {}}


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
) -> tuple[Path, dict[str, int]]:
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
) -> tuple[Path, dict[str, int]]:
    time_index_path = polyindex_dir / "TIME_INDEX.json"

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

    stats = {
        "n_years": sum(
            1
            for entry in years_section.values()
            if isinstance(entry, dict)
            and isinstance(entry.get("books"), dict)
            and source_sha256 in entry["books"]
        ),
        "n_dates": sum(
            1
            for entry in dates_section.values()
            if isinstance(entry, dict)
            and isinstance(entry.get("books"), dict)
            and source_sha256 in entry["books"]
        ),
        "n_pages_scanned": len(book_output.pages),
        "n_llm_pages": llm_pages,
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

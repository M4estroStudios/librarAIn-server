from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.polyindex.time_index import extract_time_references
from src.search.request_schema import ResearchPoh

_PERIOD_RANGE_PATTERN = re.compile(
    r"\b(?P<start>\d{3,4}(?:\s+(?:a|d)\.\s*C\.)?)"
    r"\s*[\u2013\u2014-]\s*"
    r"(?P<end>\d{3,4}(?:\s+(?:a|d)\.\s*C\.)?)\b",
    re.IGNORECASE,
)
_YEAR_FROM_DATE_PATTERN = re.compile(
    r"(\d{3,4})\s+(?:a|d)\.\s*C\.\s*$|(\d{3,4})\s*$"
)


@dataclass(frozen=True)
class TimelineCandidate:
    label: str
    source_sha256: str
    aligned_pages: list[int]


@dataclass(frozen=True)
class TimeLookupResult:
    pages: dict[str, list[int]]
    timeline_candidates: list[TimelineCandidate]
    matched_labels: int
    fallback_labels: int


def _empty_time_index() -> dict[str, Any]:
    return {"schema_version": "1.0", "years": {}, "dates": {}}


def load_time_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_time_index()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_time_index()
    if not isinstance(raw, dict):
        return _empty_time_index()
    years = raw.get("years")
    dates = raw.get("dates")
    return {
        "schema_version": raw.get("schema_version", "1.0"),
        "years": years if isinstance(years, dict) else {},
        "dates": dates if isinstance(dates, dict) else {},
    }


def _search_text(query: str, poh: ResearchPoh | None) -> str:
    parts = [query]
    if poh is not None and poh.time_range:
        parts.append(poh.time_range)
    return " ".join(parts)


def _extract_period_ranges(text: str) -> list[tuple[str, str, str]]:
    ranges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _PERIOD_RANGE_PATTERN.finditer(text):
        start = match.group("start").strip()
        end = match.group("end").strip()
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        label = f"{start}–{end}"
        ranges.append((label, start, end))
    return ranges


def _year_labels_in_ranges(ranges: list[tuple[str, str, str]]) -> set[str]:
    labels: set[str] = set()
    for _, start, end in ranges:
        labels.add(start)
        labels.add(end)
    return labels


def _pages_for_label(section: dict[str, Any], label: str) -> dict[str, list[int]]:
    entry = section.get(label)
    if not isinstance(entry, dict):
        return {}
    books = entry.get("books")
    if not isinstance(books, dict):
        return {}
    result: dict[str, list[int]] = {}
    for source_sha256, book in books.items():
        if not isinstance(book, dict):
            continue
        pages = book.get("aligned_pages")
        if not isinstance(pages, list):
            continue
        aligned = sorted({int(page) for page in pages if isinstance(page, int)})
        if aligned:
            result[str(source_sha256)] = aligned
    return result


def _merge_book_pages(
    target: dict[str, list[int]],
    source: dict[str, list[int]],
) -> None:
    for source_sha256, pages in source.items():
        existing = set(target.get(source_sha256, []))
        existing.update(pages)
        target[source_sha256] = sorted(existing)


def _resolve_with_fallback(
    section: dict[str, Any],
    label: str,
    fallback_label: str | None,
) -> tuple[dict[str, list[int]], bool]:
    pages = _pages_for_label(section, label)
    if pages:
        return pages, False
    if fallback_label and fallback_label != label:
        fallback_pages = _pages_for_label(section, fallback_label)
        if fallback_pages:
            return fallback_pages, True
    return {}, False


def _year_fallback_for_date(date_label: str) -> str | None:
    match = _YEAR_FROM_DATE_PATTERN.search(date_label)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _years_embedded_in_dates(dates: set[str]) -> set[str]:
    embedded: set[str] = set()
    for date_label in dates:
        year_label = _year_fallback_for_date(date_label)
        if year_label:
            embedded.add(year_label)
    return embedded


def _resolve_date_pages(
    dates_section: dict[str, Any],
    years_section: dict[str, Any],
    date_label: str,
) -> tuple[dict[str, list[int]], bool]:
    pages = _pages_for_label(dates_section, date_label)
    if pages:
        return pages, False
    year_label = _year_fallback_for_date(date_label)
    if year_label:
        pages = _pages_for_label(years_section, year_label)
        if pages:
            return pages, True
    return {}, False


def _append_timeline_candidates(
    timeline: list[TimelineCandidate],
    label: str,
    pages_by_book: dict[str, list[int]],
) -> None:
    for source_sha256 in sorted(pages_by_book):
        aligned_pages = pages_by_book[source_sha256]
        if aligned_pages:
            timeline.append(
                TimelineCandidate(
                    label=label,
                    source_sha256=source_sha256,
                    aligned_pages=aligned_pages,
                )
            )


def _merge_enriched_pages(
    candidate_pages: dict[str, list[int]],
    enriched: dict[str, list[int]],
) -> dict[str, list[int]]:
    merged: dict[str, list[int]] = {
        source_sha256: sorted(set(pages))
        for source_sha256, pages in candidate_pages.items()
    }
    _merge_book_pages(merged, enriched)
    return merged


def lookup_time(
    query: str,
    poh: ResearchPoh | None,
    candidate_pages: dict[str, list[int]],
    time_index: dict[str, Any],
    *,
    request_id: str = "",
) -> TimeLookupResult:
    search_text = _search_text(query, poh)
    period_ranges = _extract_period_ranges(search_text)
    years, dates = extract_time_references(search_text)
    range_year_labels = _year_labels_in_ranges(period_ranges)
    embedded_year_labels = _years_embedded_in_dates(dates)
    standalone_years = sorted(
        years.difference(range_year_labels).difference(embedded_year_labels)
    )
    standalone_dates = sorted(dates)

    if not period_ranges and not standalone_years and not standalone_dates:
        return TimeLookupResult(
            pages=dict(candidate_pages),
            timeline_candidates=[],
            matched_labels=0,
            fallback_labels=0,
        )

    years_section = time_index.get("years")
    dates_section = time_index.get("dates")
    if not isinstance(years_section, dict):
        years_section = {}
    if not isinstance(dates_section, dict):
        dates_section = {}

    timeline: list[TimelineCandidate] = []
    enriched: dict[str, list[int]] = {}
    matched_labels = 0
    fallback_labels = 0

    for label, start, end in period_ranges:
        start_pages, start_fallback = _resolve_with_fallback(years_section, start, None)
        end_pages, end_fallback = _resolve_with_fallback(years_section, end, start)
        combined: dict[str, list[int]] = {}
        _merge_book_pages(combined, start_pages)
        _merge_book_pages(combined, end_pages)
        if combined:
            matched_labels += 1
            if end_fallback:
                fallback_labels += 1
            if start_fallback:
                fallback_labels += 1
            _merge_book_pages(enriched, combined)
            _append_timeline_candidates(timeline, label, combined)

    for year_label in standalone_years:
        pages, used_fallback = _resolve_with_fallback(years_section, year_label, None)
        if pages:
            matched_labels += 1
            if used_fallback:
                fallback_labels += 1
            _merge_book_pages(enriched, pages)
            _append_timeline_candidates(timeline, year_label, pages)

    for date_label in standalone_dates:
        pages, used_fallback = _resolve_date_pages(
            dates_section,
            years_section,
            date_label,
        )
        if pages:
            matched_labels += 1
            if used_fallback:
                fallback_labels += 1
            _merge_book_pages(enriched, pages)
            _append_timeline_candidates(timeline, date_label, pages)

    merged_pages = _merge_enriched_pages(candidate_pages, enriched)

    Log(
        INFO_LOG_LEVEL,
        "research time lookup completed",
        {
            "request_id": request_id,
            "matched_labels": matched_labels,
            "fallback_labels": fallback_labels,
            "timeline_candidates": len(timeline),
            "input_books": len(candidate_pages),
            "output_books": len(merged_pages),
        },
    )

    return TimeLookupResult(
        pages=merged_pages,
        timeline_candidates=timeline,
        matched_labels=matched_labels,
        fallback_labels=fallback_labels,
    )

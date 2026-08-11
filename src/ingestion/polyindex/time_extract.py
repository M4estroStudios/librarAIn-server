from __future__ import annotations

import re

from src.ingestion.polyindex.time_patterns import *
from src.ingestion.polyindex.time_periods import (
    add_inferred,
    expand_italian_century,
    expand_named_period,
    expand_roman_century,
    expand_year_range,
    origin_from_named_period,
    origin_from_qualifier,
    resolve_year_range_end,
)
from src.ingestion.polyindex.time_sort import _date_sort_key, _year_sort_key


_BARE_YEAR_MIN = 100
_BARE_YEAR_MAX = 2099


def _normalize_era(era_raw: str | None) -> str | None:
    if not era_raw:
        return None
    compact = era_raw.replace(" ", "").lower()
    return "a.C." if compact.startswith("a") else "d.C."


def _year_label(year: int, era: str | None) -> str:
    if era == "a.C.":
        return f"{year} a.C."
    return str(year)


def _drop_bare_years_covered_by_ac(years: set[str]) -> set[str]:
    ac_nums = {
        match.group(1)
        for label in years
        if (match := re.fullmatch(r"(\d+) a\.C\.", label))
    }
    if not ac_nums:
        return years
    return {
        label
        for label in years
        if not (label.isdigit() and label in ac_nums)
    }


def _normalize_qualifier(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw.strip().lower()).replace("meta", "metà")
    return cleaned or None


def _period_label(core: str, qualifier: str | None, *, linked: bool) -> str:
    if not qualifier:
        return core
    if linked:
        return f"{qualifier} del {core}"
    return f"{qualifier} {core}"


def _expand_month_range(month1: str, month2: str) -> list[str]:
    start = _MONTH_INDEX[month1]
    end = _MONTH_INDEX[month2]
    if end < start:
        return [month1, month2]
    return [_MONTHS[index - 1] for index in range(start, end + 1)]


def _roman_century_label(roman: str, era: str | None, qualifier: str | None, *, linked: bool) -> str:
    core = f"{roman.lower()} secolo"
    if era == "a.C.":
        core = f"{core} a.C."
    return _period_label(core, qualifier, linked=linked)


def extract_time_references(
    text: str,
) -> tuple[set[str], set[str], dict[str, list[str]]]:
    direct: set[str] = set()
    inferred: dict[str, set[str]] = {}
    dates: set[str] = set()
    spans: list[tuple[int, int]] = []

    def _inside(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in spans)

    def _mark(span: tuple[int, int]) -> None:
        spans.append(span)

    def _add_range(start: int, end: int, era: str | None) -> None:
        start_lbl = _year_label(start, era)
        end_lbl = _year_label(end, era)
        direct.add(start_lbl)
        direct.add(end_lbl)
        add_inferred(
            inferred,
            expand_year_range(start, end, era),
            "range",
            direct=direct,
        )

    for match in _DAY_RANGE_PATTERN.finditer(text):
        day1 = int(match.group("day1"))
        day2 = int(match.group("day2"))
        if day1 < 1 or day1 > 31 or day2 < 1 or day2 > 31 or day2 < day1:
            continue
        month = match.group("month").lower()
        year_raw = match.group("year")
        era = _normalize_era(match.group("era"))
        if year_raw:
            year_lbl = _year_label(int(year_raw), era)
            direct.add(year_lbl)
            dates.add(f"{day1}–{day2} {month} {year_lbl}")
            for day in range(day1, day2 + 1):
                dates.add(f"{day} {month} {year_lbl}")
        else:
            dates.add(f"{day1}–{day2} {month}")
            for day in range(day1, day2 + 1):
                dates.add(f"{day} {month}")
        _mark(match.span())

    for match in _DATE_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        day_num = match.group("day_num")
        day = 1 if match.group("day_word") else int(day_num)
        if day < 1 or day > 31:
            continue
        month = match.group("month").lower()
        year_raw = match.group("year")
        era = _normalize_era(match.group("era"))
        if year_raw:
            year_lbl = _year_label(int(year_raw), era)
            direct.add(year_lbl)
            dates.add(f"{day} {month} {year_lbl}")
        else:
            dates.add(f"{day} {month}")
        _mark(match.span())

    for match in _MONTH_RANGE_YEAR_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        month1 = match.group("month1").lower()
        month2 = match.group("month2").lower()
        year_lbl = _year_label(int(match.group("year")), _normalize_era(match.group("era")))
        direct.add(year_lbl)
        dates.add(f"{month1}–{month2} {year_lbl}")
        for month in _expand_month_range(month1, month2):
            dates.add(f"{month} {year_lbl}")
        _mark(match.span())

    for match in _MONTH_YEAR_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        month = match.group("month").lower()
        year_lbl = _year_label(int(match.group("year")), _normalize_era(match.group("era")))
        direct.add(year_lbl)
        dates.add(f"{month} {year_lbl}")
        _mark(match.span())

    for pattern in (_ITALIAN_YEAR_RANGE_ERA_PATTERN, _DASH_YEAR_RANGE_ERA_PATTERN):
        for match in pattern.finditer(text):
            if _inside(*match.span()):
                continue
            era = _normalize_era(match.group("era"))
            start = int(match.group("start"))
            end = int(match.group("end"))
            if start < 1 or end < 1:
                continue
            _add_range(start, end, era)
            _mark(match.span())

    for match in _YEAR_RANGE_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        if _PAGE_REF_BEFORE.search(text[: match.start()]):
            continue
        if _MEASURE_AFTER.search(text[match.end() :]):
            continue
        start = int(match.group("start"))
        end = resolve_year_range_end(start, match.group("end"))
        if end is None:
            continue
        if start < _BARE_YEAR_MIN or end > _BARE_YEAR_MAX:
            continue
        _add_range(start, end, None)
        _mark(match.span())

    for match in _ITALIAN_CENTURY_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        core = match.group("century").capitalize()
        if core not in _ITALIAN_CENTURY_NAMES:
            core = next(n for n in _ITALIAN_CENTURY_NAMES if n.lower() == core.lower())
        qualifier = _normalize_qualifier(match.group("qual"))
        direct.add(_period_label(core, qualifier, linked=bool(match.group("link"))))
        add_inferred(
            inferred,
            expand_italian_century(core, qualifier),
            origin_from_qualifier(qualifier),
            direct=direct,
        )
        _mark(match.span())

    for pattern in (_ROMAN_CENTURY_PATTERN, _ROMAN_CENTURY_INVERTED_PATTERN):
        for match in pattern.finditer(text):
            if _inside(*match.span()):
                continue
            roman = match.group("roman")
            era = _normalize_era(match.group("era"))
            qualifier = _normalize_qualifier(match.group("qual"))
            direct.add(
                _roman_century_label(
                    roman,
                    era,
                    qualifier,
                    linked=bool(match.group("link")),
                )
            )
            add_inferred(
                inferred,
                expand_roman_century(roman, era=era, qualifier=qualifier),
                origin_from_qualifier(qualifier),
                direct=direct,
            )
            _mark(match.span())

    for match in _DIGIT_CENTURY_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        roman = _DIGIT_TO_ROMAN.get(int(match.group("num")))
        if roman is None:
            continue
        era = _normalize_era(match.group("era"))
        qualifier = _normalize_qualifier(match.group("qual"))
        direct.add(
            _roman_century_label(
                roman,
                era,
                qualifier,
                linked=bool(match.group("link")),
            )
        )
        add_inferred(
            inferred,
            expand_roman_century(roman, era=era, qualifier=qualifier),
            origin_from_qualifier(qualifier),
            direct=direct,
        )
        _mark(match.span())

    for match in _CENTURY_ADJ_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        core = _CENTURY_ADJ_TO_NAME[match.group("adj").lower()]
        direct.add(core)
        add_inferred(
            inferred,
            expand_italian_century(core),
            "secolo",
            direct=direct,
        )
        _mark(match.span())

    for match in _NAMED_PERIOD_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        label = _NAMED_PERIODS[match.group("period").lower()]
        direct.add(label)
        add_inferred(
            inferred,
            expand_named_period(label),
            origin_from_named_period(label),
            direct=direct,
        )
        _mark(match.span())

    for match in _YEAR_WITH_ERA_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        year = int(match.group("year"))
        if year < 1:
            continue
        direct.add(_year_label(year, _normalize_era(match.group("era"))))
        _mark(match.span())

    for match in _BARE_YEAR_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        year = int(match.group("year"))
        if year < _BARE_YEAR_MIN or year > _BARE_YEAR_MAX:
            continue
        if _PAGE_REF_BEFORE.search(text[: match.start()]):
            continue
        if _MEASURE_AFTER.search(text[match.end() :]):
            continue
        direct.add(str(year))

    years = _drop_bare_years_covered_by_ac(direct | set(inferred))
    direct &= years
    via = {
        label: sorted(kinds)
        for label, kinds in inferred.items()
        if label in years and label not in direct
    }
    return years, dates, via



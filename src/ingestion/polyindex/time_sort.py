from __future__ import annotations

import re

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
_MONTH_ALT = "|".join(_MONTHS)
_MONTH_INDEX = {name: index + 1 for index, name in enumerate(_MONTHS)}

_DATE_SORT_PATTERN = re.compile(
    r"^(?:(?P<day1>\d{1,2})(?:[\u2013\u2014-](?P<day2>\d{1,2}))?\s+)?"
    r"(?P<month1>"
    + _MONTH_ALT
    + r")"
    r"(?:\s*[\u2013\u2014-]\s*(?P<month2>"
    + _MONTH_ALT
    + r"))?"
    r"(?:\s+(?P<year>\d{1,4})(?:\s+(?P<era>a\.C\.))?)?$",
    re.IGNORECASE,
)


def _date_sort_key(label: str) -> tuple[int, int, int, int, str]:
    cleaned = label.strip()
    match = _DATE_SORT_PATTERN.fullmatch(cleaned)
    if match is None:
        return (2, 10**9, 99, 99, cleaned.casefold())
    month = _MONTH_INDEX[match.group("month1").lower()]
    day = int(match.group("day1") or 0)
    year_raw = match.group("year")
    if year_raw is None:
        return (1, 10**9, month, day, cleaned.casefold())
    year = int(year_raw)
    year_ord = -year if match.group("era") else year
    return (0, year_ord, month, day, cleaned.casefold())


def _year_sort_key(label: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)(?:\s+(a\.C\.))?$", label)
    if match is None:
        return (1, 10**6)
    value = int(match.group(1))
    if match.group(2):
        return (0, -value)
    return (1, value)

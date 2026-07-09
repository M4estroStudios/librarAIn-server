"""Parse a polyindex ``time_range`` string into E-TALY CSV ``beginning``/``end`` dates.

E-TALY catalog CSVs use ``YYYY/MM/DD`` dates and represent *avanti Cristo* (BCE) years as
**negative** years (e.g. ``509 a.C.`` -> ``-509/01/01``), mirroring the negative timeline
year keys used by :mod:`src.export.etaly_adapter`. Only years are recoverable from the
free-text polyindex range, so the month/day are always ``01/01``.

The parser is deterministic and never raises: when no year can be extracted (e.g. a
purely qualitative range like ``"I secolo a.C."``) both values are returned empty so the
reviewer / E-TALY can fill them in later.
"""

from __future__ import annotations

import re

# A year optionally followed by an era marker (``a.C.``/``d.C.``/``BC``/``AD``).
_YEAR_ERA_RE = re.compile(
    r"(\d{1,4})\s*(a\.?\s*c\.?|d\.?\s*c\.?|b\.?\s*c\.?|a\.?\s*d\.?)?",
    re.IGNORECASE,
)
_BCE_MARKER_RE = re.compile(r"\b(?:a\.?\s*c\.?|b\.?\s*c\.?)", re.IGNORECASE)


def _signed_years(text: str) -> list[int]:
    years: list[int] = []
    for match in _YEAR_ERA_RE.finditer(text):
        year = int(match.group(1))
        era = match.group(2) or ""
        is_bce = _BCE_MARKER_RE.fullmatch(era.strip()) is not None
        years.append(-year if is_bce else year)
    return years


def _format_year(year: int) -> str:
    return f"{year}/01/01"


def parse_time_range(time_range: str | None) -> tuple[str, str]:
    """Return ``(beginning, end)`` ``YYYY/MM/DD`` strings derived from ``time_range``.

    Both values are empty when no year is parseable. When a single year is present it is
    used for both bounds; otherwise the min/max signed years become begin/end.
    """
    if not time_range:
        return "", ""
    years = _signed_years(time_range)
    if not years:
        return "", ""
    return _format_year(min(years)), _format_year(max(years))

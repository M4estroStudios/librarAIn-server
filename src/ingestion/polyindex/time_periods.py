from __future__ import annotations

_ITALIAN_CENTURY_START = {
    "Duecento": 1200,
    "Trecento": 1300,
    "Quattrocento": 1400,
    "Cinquecento": 1500,
    "Seicento": 1600,
    "Settecento": 1700,
    "Ottocento": 1800,
    "Novecento": 1900,
}

_ROMAN_TO_NUM = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
    "xiii": 13,
    "xiv": 14,
    "xv": 15,
    "xvi": 16,
    "xvii": 17,
    "xviii": 18,
    "xix": 19,
    "xx": 20,
    "xxi": 21,
}

_NAMED_PERIOD_SIGNED_RANGES = {
    "alto Medioevo": (476, 1000),
    "Medioevo": (476, 1492),
    "Rinascimento": (1400, 1600),
    "Manierismo": (1520, 1600),
    "Barocco": (1600, 1750),
    "Neoclassicismo": (1750, 1830),
    "dopoguerra": (1945, 1968),
    "epoca moderna": (1492, 1815),
    "età imperiale": (-27, 476),
    "età romana": (-753, 476),
    "età repubblicana": (-509, -27),
    "età moderna": (1492, 1815),
    "antichità": (-753, 476),
}


def _year_label(year: int, era: str | None) -> str:
    if era == "a.C.":
        return f"{year} a.C."
    return str(year)


def _signed_to_label(signed: int) -> str:
    if signed < 0:
        return _year_label(-signed, "a.C.")
    return _year_label(signed, None)


def _iter_signed(lo: int, hi: int) -> list[int]:
    if lo > hi:
        lo, hi = hi, lo
    return [year for year in range(lo, hi + 1) if year != 0]


def _century_signed_bounds(century_num: int, *, era: str | None) -> tuple[int, int]:
    if century_num < 1 or century_num > 21:
        raise ValueError(f"unsupported century: {century_num}")
    if era == "a.C.":
        return -century_num * 100, -((century_num - 1) * 100 + 1)
    if century_num == 1:
        return 1, 100
    start = (century_num - 1) * 100
    return start, start + 99


def _apply_qualifier(years: list[int], qualifier: str | None) -> list[int]:
    if not years or not qualifier:
        return list(years)
    qual = qualifier.strip().lower().replace("meta", "metà")
    count = len(years)
    if qual in {"inizio", "inizi", "primi"}:
        return years[: min(25, count)]
    if qual in {"fine", "tardo", "tarda"}:
        return years[max(0, count - 25) :]
    if qual == "prima metà":
        return years[: (count + 1) // 2]
    if qual == "seconda metà":
        return years[(count + 1) // 2 :]
    if qual == "metà":
        mid = count // 2
        return years[max(0, mid - 10) : mid + 10]
    return list(years)


def expand_italian_century(name: str, qualifier: str | None = None) -> list[str]:
    start = _ITALIAN_CENTURY_START[name]
    signed = _iter_signed(start, start + 99)
    return [_signed_to_label(year) for year in _apply_qualifier(signed, qualifier)]


def expand_roman_century(
    roman: str,
    *,
    era: str | None = None,
    qualifier: str | None = None,
) -> list[str]:
    century_num = _ROMAN_TO_NUM[roman.lower()]
    lo, hi = _century_signed_bounds(century_num, era=era)
    signed = _iter_signed(lo, hi)
    return [_signed_to_label(year) for year in _apply_qualifier(signed, qualifier)]


def expand_named_period(label: str) -> list[str]:
    bounds = _NAMED_PERIOD_SIGNED_RANGES.get(label)
    if bounds is None:
        return []
    lo, hi = bounds
    return [_signed_to_label(year) for year in _iter_signed(lo, hi)]


_YEAR_RANGE_EXPAND_MAX = 120


def resolve_year_range_end(start: int, end_raw: str) -> int | None:
    end = int(end_raw)
    if len(end_raw) <= 2:
        century = start // 100 * 100
        end = century + end
        if end < start:
            end += 100
    if end < start:
        return None
    return end


def expand_year_range(start: int, end: int, era: str | None = None) -> list[str]:
    lo, hi = (start, end) if start <= end else (end, start)
    if hi - lo > _YEAR_RANGE_EXPAND_MAX:
        if start == end:
            return [_year_label(start, era)]
        return [_year_label(start, era), _year_label(end, era)]
    return [_year_label(year, era) for year in range(lo, hi + 1)]


def origin_from_qualifier(qualifier: str | None) -> str:
    if not qualifier:
        return "secolo"
    qual = qualifier.strip().lower().replace("meta", "metà")
    if qual in {"inizio", "inizi", "primi"}:
        return "inizi"
    if qual in {"fine", "tardo", "tarda"}:
        return "fine"
    if "metà" in qual:
        return "metà"
    return "secolo"


def origin_from_named_period(label: str) -> str:
    lowered = label.casefold()
    if lowered.startswith("età") or lowered.startswith("eta "):
        return "età"
    return "periodo"


def add_inferred(
    inferred: dict[str, set[str]],
    labels: list[str] | set[str],
    kind: str,
    *,
    direct: set[str],
) -> None:
    for label in labels:
        if label in direct:
            continue
        inferred.setdefault(label, set()).add(kind)

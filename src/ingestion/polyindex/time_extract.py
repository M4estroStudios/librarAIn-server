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

_ERA_SUFFIX = r"(?:a|d)\.\s*C\."
_YEAR_ERA_GROUP = r"(?P<year>\d{1,4})\s*(?P<era>" + _ERA_SUFFIX + r")?"

_ITALIAN_CENTURY_NAMES = (
    "Duecento",
    "Trecento",
    "Quattrocento",
    "Cinquecento",
    "Seicento",
    "Settecento",
    "Ottocento",
    "Novecento",
)
_ITALIAN_CENTURY_ALT = "|".join(_ITALIAN_CENTURY_NAMES)
_CENTURY_ADJ_TO_NAME = {
    "duecentesco": "Duecento",
    "duecentesca": "Duecento",
    "trecentesco": "Trecento",
    "trecentesca": "Trecento",
    "quattrocentesco": "Quattrocento",
    "quattrocentesca": "Quattrocento",
    "cinquecentesco": "Cinquecento",
    "cinquecentesca": "Cinquecento",
    "seicentesco": "Seicento",
    "seicentesca": "Seicento",
    "secentesco": "Seicento",
    "secentesca": "Seicento",
    "settecentesco": "Settecento",
    "settecentesca": "Settecento",
    "ottocentesco": "Ottocento",
    "ottocentesca": "Ottocento",
    "novecentesco": "Novecento",
    "novecentesca": "Novecento",
}
_CENTURY_ADJ_ALT = "|".join(sorted(_CENTURY_ADJ_TO_NAME, key=len, reverse=True))
_ROMAN_CENTURY_ALT = (
    "xxi|xx|xix|xviii|xvii|xvi|xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i"
)
_PERIOD_QUALIFIER = (
    r"(?:tardo|tarda|inizio|inizi|fine|met[aà]|prima\s+met[aà]|seconda\s+met[aà]|primi)"
)
_NAMED_PERIODS = {
    "alto medioevo": "alto Medioevo",
    "medioevo": "Medioevo",
    "rinascimento": "Rinascimento",
    "manierismo": "Manierismo",
    "barocco": "Barocco",
    "neoclassicismo": "Neoclassicismo",
    "dopoguerra": "dopoguerra",
    "epoca moderna": "epoca moderna",
    "età imperiale": "età imperiale",
    "eta imperiale": "età imperiale",
    "età romana": "età romana",
    "eta romana": "età romana",
    "età repubblicana": "età repubblicana",
    "eta repubblicana": "età repubblicana",
    "età moderna": "età moderna",
    "eta moderna": "età moderna",
    "antichità": "antichità",
    "antichita": "antichità",
}
_NAMED_PERIOD_ALT = "|".join(
    sorted((re.escape(k) for k in _NAMED_PERIODS), key=len, reverse=True)
)
_DIGIT_TO_ROMAN = {
    1: "i",
    2: "ii",
    3: "iii",
    4: "iv",
    5: "v",
    6: "vi",
    7: "vii",
    8: "viii",
    9: "ix",
    10: "x",
    11: "xi",
    12: "xii",
    13: "xiii",
    14: "xiv",
    15: "xv",
    16: "xvi",
    17: "xvii",
    18: "xviii",
    19: "xix",
    20: "xx",
    21: "xxi",
}

_DAY_RANGE_PATTERN = re.compile(
    r"\b(?P<day1>[1-9]\d?)\s+al\s+(?P<day2>[1-9]\d?)\s+(?P<month>"
    + _MONTH_ALT
    + r")(?:\s+"
    + _YEAR_ERA_GROUP
    + r")?\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"\b(?:(?P<day_num>[1-9]\d?)\s*°?|(?P<day_word>prim[oa]))\s+(?P<month>"
    + _MONTH_ALT
    + r")(?:\s+"
    + _YEAR_ERA_GROUP
    + r")?\b",
    re.IGNORECASE,
)
_MONTH_RANGE_YEAR_PATTERN = re.compile(
    r"\b(?P<month1>"
    + _MONTH_ALT
    + r")\s*[\u2013\u2014/-]\s*(?P<month2>"
    + _MONTH_ALT
    + r")\s+(?:"
    + _YEAR_ERA_GROUP
    + r")\b",
    re.IGNORECASE,
)
_MONTH_YEAR_PATTERN = re.compile(
    r"\b(?:nel\s+)?(?P<month>"
    + _MONTH_ALT
    + r")(?:\s+del)?\s+(?:"
    + _YEAR_ERA_GROUP
    + r")\b",
    re.IGNORECASE,
)
_YEAR_RANGE_PATTERN = re.compile(
    r"(?<![\d.])\b(?P<start>\d{3,4})\s*[\u2013\u2014/-]\s*(?P<end>\d{2,4})\b"
)
_ITALIAN_CENTURY_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?(?P<century>"
    + _ITALIAN_CENTURY_ALT
    + r")\b",
    re.IGNORECASE,
)
_ROMAN_CENTURY_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?(?P<roman>"
    + _ROMAN_CENTURY_ALT
    + r")\s+secolo(?:\s*(?P<era>"
    + _ERA_SUFFIX
    + r"))?\b",
    re.IGNORECASE,
)
_ROMAN_CENTURY_INVERTED_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?secolo\s+(?P<roman>"
    + _ROMAN_CENTURY_ALT
    + r")(?:\s*(?P<era>"
    + _ERA_SUFFIX
    + r"))?\b",
    re.IGNORECASE,
)
_DIGIT_CENTURY_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?(?P<num>[1-9]|1\d|2[01])\s+secolo(?:\s*(?P<era>"
    + _ERA_SUFFIX
    + r"))?\b",
    re.IGNORECASE,
)
_CENTURY_ADJ_PATTERN = re.compile(
    r"\b(?P<adj>" + _CENTURY_ADJ_ALT + r")\b",
    re.IGNORECASE,
)
_NAMED_PERIOD_PATTERN = re.compile(
    r"\b(?P<period>" + _NAMED_PERIOD_ALT + r")\b",
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
    r"(?:(?:\bpp?\.|\bpagg?\.|\bn{1,2}\.|n[º°])\s*[\d\s,.\u2013\u2014-]*$)",
    re.IGNORECASE,
)
_MEASURE_AFTER = re.compile(
    r"^\s*(?:metri|metro|chilometri|km\b|posti|ettari|kg\b|m\b)",
    re.IGNORECASE,
)

_BARE_YEAR_MIN = 100
_BARE_YEAR_MAX = 2099
_YEAR_RANGE_EXPAND_MAX = 120


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


def _resolve_year_range_end(start: int, end_raw: str) -> int | None:
    end = int(end_raw)
    if len(end_raw) <= 2:
        century = start // 100 * 100
        end = century + end
        if end < start:
            end += 100
    if end < start:
        return None
    return end


def _expand_year_range(start: int, end: int) -> list[str]:
    if end - start > _YEAR_RANGE_EXPAND_MAX:
        return [str(start), str(end)]
    return [str(year) for year in range(start, end + 1)]


def _expand_month_range(month1: str, month2: str) -> list[str]:
    start = _MONTH_INDEX[month1]
    end = _MONTH_INDEX[month2]
    if end < start:
        return [month1, month2]
    return [_MONTHS[index - 1] for index in range(start, end + 1)]


def _roman_century_label(roman: str, era: str | None, qualifier: str | None, *, linked: bool) -> str:
    core = f"{roman.lower()} secolo"
    if era:
        core = f"{core} {era}"
    return _period_label(core, qualifier, linked=linked)


def extract_time_references(text: str) -> tuple[set[str], set[str]]:
    """Extract year labels and date labels from a page of text."""
    years: set[str] = set()
    dates: set[str] = set()
    spans: list[tuple[int, int]] = []

    def _inside(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in spans)

    def _mark(span: tuple[int, int]) -> None:
        spans.append(span)

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
            years.add(year_lbl)
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
            years.add(year_lbl)
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
        years.add(year_lbl)
        dates.add(f"{month1}–{month2} {year_lbl}")
        for month in _expand_month_range(month1, month2):
            dates.add(f"{month} {year_lbl}")
        _mark(match.span())

    for match in _MONTH_YEAR_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        month = match.group("month").lower()
        year_lbl = _year_label(int(match.group("year")), _normalize_era(match.group("era")))
        years.add(year_lbl)
        dates.add(f"{month} {year_lbl}")
        _mark(match.span())

    for match in _YEAR_RANGE_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        if _PAGE_REF_BEFORE.search(text[: match.start()]):
            continue
        if _MEASURE_AFTER.search(text[match.end() :]):
            continue
        start = int(match.group("start"))
        end = _resolve_year_range_end(start, match.group("end"))
        if end is None:
            continue
        if start < _BARE_YEAR_MIN or end > _BARE_YEAR_MAX:
            continue
        years.update(_expand_year_range(start, end))
        _mark(match.span())

    for match in _ITALIAN_CENTURY_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        core = match.group("century").capitalize()
        if core not in _ITALIAN_CENTURY_NAMES:
            core = next(n for n in _ITALIAN_CENTURY_NAMES if n.lower() == core.lower())
        years.add(
            _period_label(
                core,
                _normalize_qualifier(match.group("qual")),
                linked=bool(match.group("link")),
            )
        )
        _mark(match.span())

    for pattern in (_ROMAN_CENTURY_PATTERN, _ROMAN_CENTURY_INVERTED_PATTERN):
        for match in pattern.finditer(text):
            if _inside(*match.span()):
                continue
            years.add(
                _roman_century_label(
                    match.group("roman"),
                    _normalize_era(match.group("era")),
                    _normalize_qualifier(match.group("qual")),
                    linked=bool(match.group("link")),
                )
            )
            _mark(match.span())

    for match in _DIGIT_CENTURY_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        roman = _DIGIT_TO_ROMAN.get(int(match.group("num")))
        if roman is None:
            continue
        years.add(
            _roman_century_label(
                roman,
                _normalize_era(match.group("era")),
                _normalize_qualifier(match.group("qual")),
                linked=bool(match.group("link")),
            )
        )
        _mark(match.span())

    for match in _CENTURY_ADJ_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        years.add(_CENTURY_ADJ_TO_NAME[match.group("adj").lower()])
        _mark(match.span())

    for match in _NAMED_PERIOD_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        years.add(_NAMED_PERIODS[match.group("period").lower()])
        _mark(match.span())

    for match in _YEAR_WITH_ERA_PATTERN.finditer(text):
        if _inside(*match.span()):
            continue
        year = int(match.group("year"))
        if year < 1:
            continue
        years.add(_year_label(year, _normalize_era(match.group("era"))))
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
        years.add(str(year))

    return years, dates



from __future__ import annotations

import re

__all__ = [
    "_BARE_YEAR_PATTERN",
    "_CENTURY_ADJ_ALT",
    "_CENTURY_ADJ_PATTERN",
    "_CENTURY_ADJ_TO_NAME",
    "_DASH_YEAR_RANGE_ERA_PATTERN",
    "_DATE_PATTERN",
    "_DAY_RANGE_PATTERN",
    "_DIGIT_CENTURY_PATTERN",
    "_DIGIT_TO_ROMAN",
    "_END_BOUND",
    "_ERA_SUFFIX",
    "_ITALIAN_CENTURY_ALT",
    "_ITALIAN_CENTURY_NAMES",
    "_ITALIAN_CENTURY_PATTERN",
    "_ITALIAN_YEAR_RANGE_ERA_PATTERN",
    "_MEASURE_AFTER",
    "_MONTHS",
    "_MONTH_ALT",
    "_MONTH_INDEX",
    "_MONTH_RANGE_YEAR_PATTERN",
    "_MONTH_YEAR_PATTERN",
    "_NAMED_PERIODS",
    "_NAMED_PERIOD_ALT",
    "_NAMED_PERIOD_PATTERN",
    "_PAGE_REF_BEFORE",
    "_PERIOD_QUALIFIER",
    "_ROMAN_CENTURY_ALT",
    "_ROMAN_CENTURY_INVERTED_PATTERN",
    "_ROMAN_CENTURY_PATTERN",
    "_YEAR_ERA_GROUP",
    "_YEAR_RANGE_PATTERN",
    "_YEAR_WITH_ERA_PATTERN",
]

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
_END_BOUND = r"(?!\w)"

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
    + r")?"
    + _END_BOUND,
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"\b(?:(?P<day_num>[1-9]\d?)\s*°?|(?P<day_word>prim[oa]))\s+(?P<month>"
    + _MONTH_ALT
    + r")(?:(?:\s+del)?\s+"
    + _YEAR_ERA_GROUP
    + r")?"
    + _END_BOUND,
    re.IGNORECASE,
)
_MONTH_RANGE_YEAR_PATTERN = re.compile(
    r"\b(?P<month1>"
    + _MONTH_ALT
    + r")\s*[\u2013\u2014/-]\s*(?P<month2>"
    + _MONTH_ALT
    + r")\s+(?:"
    + _YEAR_ERA_GROUP
    + r")"
    + _END_BOUND,
    re.IGNORECASE,
)
_MONTH_YEAR_PATTERN = re.compile(
    r"\b(?:nel\s+)?(?P<month>"
    + _MONTH_ALT
    + r")(?:\s+del)?\s+(?:"
    + _YEAR_ERA_GROUP
    + r")"
    + _END_BOUND,
    re.IGNORECASE,
)
_YEAR_RANGE_PATTERN = re.compile(
    r"(?<![\d.])\b(?P<start>\d{3,4})\s*[\u2013\u2014/-]\s*(?P<end>\d{2,4})\b"
)
_ITALIAN_YEAR_RANGE_ERA_PATTERN = re.compile(
    r"\b(?:tra\s+(?:il\s+)?|dal\s+|da\s+)?(?P<start>\d{1,4})\s+"
    r"(?:e\s+(?:il\s+)?|al\s+|a\s+)(?P<end>\d{1,4})\s*(?P<era>"
    + _ERA_SUFFIX
    + r")"
    + _END_BOUND,
    re.IGNORECASE,
)
_DASH_YEAR_RANGE_ERA_PATTERN = re.compile(
    r"(?<![\d.])\b(?P<start>\d{1,4})\s*[\u2013\u2014/-]\s*(?P<end>\d{1,4})\s*(?P<era>"
    + _ERA_SUFFIX
    + r")"
    + _END_BOUND,
    re.IGNORECASE,
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
    + r"))?"
    + _END_BOUND,
    re.IGNORECASE,
)
_ROMAN_CENTURY_INVERTED_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?secolo\s+(?P<roman>"
    + _ROMAN_CENTURY_ALT
    + r")(?:\s*(?P<era>"
    + _ERA_SUFFIX
    + r"))?"
    + _END_BOUND,
    re.IGNORECASE,
)
_DIGIT_CENTURY_PATTERN = re.compile(
    r"\b(?:(?P<qual>"
    + _PERIOD_QUALIFIER
    + r")(?P<link>\s+(?:del|della|dei))?\s+)?(?P<num>[1-9]|1\d|2[01])\s+secolo(?:\s*(?P<era>"
    + _ERA_SUFFIX
    + r"))?"
    + _END_BOUND,
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

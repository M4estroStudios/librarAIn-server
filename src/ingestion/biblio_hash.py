from __future__ import annotations

import hashlib
import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_text_for_hash(raw: str) -> str:
    text = unicodedata.normalize("NFC", (raw or "").strip().lower())
    text = _PUNCT_RE.sub("", text)
    return "".join(text.split())


def normalize_authors_for_hash(authors: str | list[str] | None) -> str:
    if authors is None:
        return "unknown"
    if isinstance(authors, list):
        parts = [part.strip() for part in authors if str(part).strip()]
        joined = ",".join(parts)
    else:
        joined = str(authors).strip()
    if not joined:
        return "unknown"
    return normalize_text_for_hash(joined) or "unknown"


def normalize_title_for_hash(title: str | None) -> str:
    normalized = normalize_text_for_hash(title or "")
    return normalized or "unknown"


def normalize_year_for_hash(year: object) -> str:
    if year is None:
        return "unknown"
    if isinstance(year, int):
        return str(year) if year > 0 else "unknown"
    text = str(year).strip()
    if not text:
        return "unknown"
    match = re.search(r"(\d{4})", text)
    if match is None:
        match = re.search(r"(\d{3})", text)
    if match is None:
        return "unknown"
    return match.group(1)


def is_all_unknown(authors_norm: str, title_norm: str, year_norm: str) -> bool:
    return authors_norm == "unknown" and title_norm == "unknown" and year_norm == "unknown"


def compute_biblio_id(
    authors: str | list[str] | None,
    title: str | None,
    year: object,
) -> tuple[str, str, str, str]:
    authors_norm = normalize_authors_for_hash(authors)
    title_norm = normalize_title_for_hash(title)
    year_norm = normalize_year_for_hash(year)
    digest_input = f"{authors_norm}{title_norm}{year_norm}"
    entry_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    return entry_id, authors_norm, title_norm, year_norm

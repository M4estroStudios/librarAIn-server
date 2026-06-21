from __future__ import annotations

import re

from src.ingestion.polyindex.index_md_parser import normalize_label
from src.models.polyindex_index import PolyindexIndexDocument
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh

_MIN_TERM_LEN = 2


def _contains_term(haystack_norm: str, term_norm: str) -> bool:
    if len(term_norm) < _MIN_TERM_LEN:
        return False
    pattern = r"(?<!\w)" + re.escape(term_norm) + r"(?!\w)"
    return re.search(pattern, haystack_norm) is not None


def collect_subject_terms(
    query: str,
    poh: ResearchPoh | None,
    document: PolyindexIndexDocument,
) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        normalized = normalize_label(raw.strip())
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        terms.append(normalized)

    for part in query.strip().split():
        add(part)

    if poh is not None and poh.label.strip():
        add(poh.label)
        for part in poh.label.split():
            add(part)

    if poh is not None and poh.id and poh.id in document.subjects:
        entry = document.subjects[poh.id]
        add(entry.canonical_label)
        for part in entry.canonical_label.split():
            add(part)
        for alias in entry.aliases:
            add(alias)
            for part in alias.split():
                add(part)

    return terms


def page_mentions_subject(page_text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    haystack = normalize_label(page_text)
    return any(_contains_term(haystack, term) for term in terms)


def filter_relevant_pages(
    pages: list[LoadedPage],
    *,
    query: str,
    poh: ResearchPoh | None,
    document: PolyindexIndexDocument,
) -> list[LoadedPage]:
    terms = collect_subject_terms(query, poh, document)
    if not terms:
        return list(pages)
    return [page for page in pages if page_mentions_subject(page.markdown, terms)]

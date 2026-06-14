from __future__ import annotations

import math
import re
from dataclasses import dataclass

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.core.openai_client_sync import embedding_with_retry_sync
from src.ingestion.polyindex.index_md_parser import normalize_label
from src.models.polyindex_index import PolyindexIndexDocument
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import (
    get_subject_embedding,
    set_subject_embedding,
)
from src.search.request_schema import ResearchPoh

_STAGE_EMBEDDING = "research_subject_lookup_embedding"

METHOD_EXACT = "exact"
METHOD_ALIAS = "alias"
METHOD_POH_ID = "poh_id"
METHOD_SEMANTIC = "semantic"


@dataclass(frozen=True)
class SubjectMatch:
    canonical_id: str
    canonical_label: str
    method: str
    similarity: float | None = None


@dataclass(frozen=True)
class SubjectLookupResult:
    matches: list[SubjectMatch]
    pages: dict[str, list[int]]
    ai_used: bool
    degraded: bool


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _search_text(query: str, poh: ResearchPoh | None) -> str:
    parts = [query]
    if poh is not None and poh.label:
        parts.append(poh.label)
    return " ".join(parts)


def _contains_normalized(haystack_norm: str, needle_norm: str) -> bool:
    if not needle_norm:
        return False
    pattern = r"(?<!\w)" + re.escape(needle_norm) + r"(?!\w)"
    return re.search(pattern, haystack_norm) is not None


def _deterministic_matches(
    search_text_norm: str,
    poh: ResearchPoh | None,
    document: PolyindexIndexDocument,
) -> dict[str, SubjectMatch]:
    found: dict[str, SubjectMatch] = {}

    if poh is not None and poh.id and poh.id in document.subjects:
        entry = document.subjects[poh.id]
        found[poh.id] = SubjectMatch(
            canonical_id=poh.id,
            canonical_label=entry.canonical_label,
            method=METHOD_POH_ID,
        )

    for canonical_id, entry in document.subjects.items():
        if canonical_id in found:
            continue
        if _contains_normalized(search_text_norm, normalize_label(entry.canonical_label)):
            found[canonical_id] = SubjectMatch(
                canonical_id=canonical_id,
                canonical_label=entry.canonical_label,
                method=METHOD_EXACT,
            )
            continue
        for alias in entry.aliases:
            if _contains_normalized(search_text_norm, normalize_label(alias)):
                found[canonical_id] = SubjectMatch(
                    canonical_id=canonical_id,
                    canonical_label=entry.canonical_label,
                    method=METHOD_ALIAS,
                )
                break

    return found


def _canonical_embedding(
    client: object,
    sqlite_path: str,
    model: str,
    canonical_id: str,
    label: str,
    *,
    request_id: str,
) -> list[float]:
    cached = get_subject_embedding(sqlite_path, canonical_id, model)
    if cached is not None:
        return cached
    vector = embedding_with_retry_sync(
        client,  # type: ignore[arg-type]
        model=model,
        text=label,
        request_id=request_id,
        stage=_STAGE_EMBEDDING,
    )
    set_subject_embedding(sqlite_path, canonical_id, label, vector, model)
    return vector


def _semantic_matches(
    search_text: str,
    document: PolyindexIndexDocument,
    client: object,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    already_matched: set[str],
) -> tuple[list[SubjectMatch], bool]:
    model = settings.matcher_embedding_model
    threshold = settings.matcher_similarity_threshold
    try:
        query_vector = embedding_with_retry_sync(
            client,  # type: ignore[arg-type]
            model=model,
            text=search_text,
            request_id=request_id,
            stage=_STAGE_EMBEDDING,
        )
    except Exception as exc:  # noqa: BLE001
        Log(
            WARNING_LOG_LEVEL,
            "research subject lookup degraded to deterministic only",
            {"request_id": request_id, "error": repr(exc)},
        )
        return [], True

    matches: list[SubjectMatch] = []
    degraded = False
    for canonical_id, entry in document.subjects.items():
        if canonical_id in already_matched:
            continue
        try:
            candidate_vector = _canonical_embedding(
                client,
                sqlite_path,
                model,
                canonical_id,
                entry.canonical_label,
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001
            Log(
                WARNING_LOG_LEVEL,
                "research subject lookup canonical embedding failed",
                {
                    "request_id": request_id,
                    "canonical_id": canonical_id,
                    "error": repr(exc),
                },
            )
            degraded = True
            break
        similarity = _cosine_similarity(query_vector, candidate_vector)
        if similarity >= threshold:
            matches.append(
                SubjectMatch(
                    canonical_id=canonical_id,
                    canonical_label=entry.canonical_label,
                    method=METHOD_SEMANTIC,
                    similarity=similarity,
                )
            )
    matches.sort(key=lambda item: (-(item.similarity or 0.0), item.canonical_id))
    return matches, degraded


def _aggregate_pages(
    document: PolyindexIndexDocument,
    matches: list[SubjectMatch],
) -> dict[str, list[int]]:
    pages: dict[str, set[int]] = {}
    for match in matches:
        entry = document.subjects.get(match.canonical_id)
        if entry is None:
            continue
        for source_sha256, book in entry.books.items():
            pages.setdefault(source_sha256, set()).update(book.aligned_pages)
    return {sha: sorted(page_set) for sha, page_set in sorted(pages.items())}


def lookup_subjects(
    query: str,
    poh: ResearchPoh | None,
    document: PolyindexIndexDocument,
    client: object,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
) -> SubjectLookupResult:
    search_text = _search_text(query, poh)
    search_text_norm = normalize_label(search_text)

    deterministic = _deterministic_matches(search_text_norm, poh, document)
    matches: list[SubjectMatch] = list(deterministic.values())

    ai_used = False
    degraded = False
    if settings.matcher_use_ai and document.subjects:
        ai_used = True
        semantic, degraded = _semantic_matches(
            search_text,
            document,
            client,
            sqlite_path,
            settings,
            request_id,
            already_matched=set(deterministic.keys()),
        )
        matches.extend(semantic)

    pages = _aggregate_pages(document, matches)

    Log(
        INFO_LOG_LEVEL,
        "research subject lookup completed",
        {
            "request_id": request_id,
            "matched_subjects": len(matches),
            "deterministic_subjects": len(deterministic),
            "books_in_context": len(pages),
            "ai_used": ai_used,
            "degraded": degraded,
        },
    )

    return SubjectLookupResult(
        matches=matches,
        pages=pages,
        ai_used=ai_used,
        degraded=degraded,
    )

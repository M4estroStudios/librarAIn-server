from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from src.core.openai_client_sync import embeddings_batch_with_retry_sync
from src.ingestion.polyindex.index_md_parser import RawSubject, normalize_label
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import get_subject_embedding, set_subject_embedding

_EMBEDDING_BATCH_CHUNK_SIZE = 64
_STAGE_EMBEDDING = "subject_matcher_embedding"
SubjectsMap = dict[str, dict[str, Any]]
FindExactCanonical = Callable[[SubjectsMap, str], str | None]
FuzzyBorderlineCandidates = Callable[[SubjectsMap, str], list[tuple[str, int]]]
LexicalTopK = Callable[[SubjectsMap, str, int], list[str]]
CanonicalLabelForId = Callable[[SubjectsMap, str], str]
SubjectsMapFn = Callable[[dict[str, Any]], SubjectsMap]


def match_path_requires_embeddings(
    raw_subject: RawSubject,
    subjects: SubjectsMap,
    settings: Settings,
    *,
    find_exact_canonical: FindExactCanonical,
    fuzzy_borderline_candidates: FuzzyBorderlineCandidates,
) -> bool:
    if raw_subject.alias_of:
        return False
    normalized = normalize_label(raw_subject.raw_label)
    if find_exact_canonical(subjects, normalized) is not None:
        return False
    borderline = fuzzy_borderline_candidates(subjects, normalized)
    if borderline and not settings.matcher_use_ai:
        return False
    return bool(borderline or settings.matcher_use_ai)


def fetch_embeddings_parallel(
    client: object,
    model: str,
    texts: list[str],
    *,
    request_id: str,
    max_parallel: int,
) -> list[list[float]]:
    if not texts:
        return []
    workers = max(1, min(max_parallel, len(texts)))
    chunks = [
        texts[index : index + _EMBEDDING_BATCH_CHUNK_SIZE]
        for index in range(0, len(texts), _EMBEDDING_BATCH_CHUNK_SIZE)
    ]
    if len(chunks) == 1:
        return embeddings_batch_with_retry_sync(
            client,  # type: ignore[arg-type]
            model=model,
            texts=chunks[0],
            request_id=request_id,
            stage=_STAGE_EMBEDDING,
        )

    def _fetch_chunk(chunk: list[str]) -> list[list[float]]:
        return embeddings_batch_with_retry_sync(
            client,  # type: ignore[arg-type]
            model=model,
            texts=chunk,
            request_id=request_id,
            stage=_STAGE_EMBEDDING,
        )

    with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
        chunk_vectors = list(pool.map(_fetch_chunk, chunks))
    return [vector for batch in chunk_vectors for vector in batch]


def prefetch_matcher_embedding_vectors(
    raw_subjects: list[RawSubject],
    polyindex_state: dict[str, Any],
    client: object,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    subjects_map: SubjectsMapFn,
    find_exact_canonical: FindExactCanonical,
    fuzzy_borderline_candidates: FuzzyBorderlineCandidates,
    lexical_top_k: LexicalTopK,
    canonical_label_for_id: CanonicalLabelForId,
    top_k: int,
) -> dict[str, list[float]]:
    subjects = subjects_map(polyindex_state)
    model = settings.matcher_embedding_model
    cache: dict[str, list[float]] = {}
    texts_to_fetch: list[str] = []
    scheduled: set[str] = set()
    canonical_by_label: dict[str, str] = {}

    def remember_canonical(canonical_id: str, label: str) -> None:
        canonical_by_label[label] = canonical_id
        cached = get_subject_embedding(sqlite_path, canonical_id, model)
        if cached is not None:
            cache[label] = cached

    def schedule_text(text: str) -> None:
        if text in cache or text in scheduled:
            return
        scheduled.add(text)
        texts_to_fetch.append(text)

    for raw_subject in raw_subjects:
        if not match_path_requires_embeddings(
            raw_subject,
            subjects,
            settings,
            find_exact_canonical=find_exact_canonical,
            fuzzy_borderline_candidates=fuzzy_borderline_candidates,
        ):
            continue
        normalized = normalize_label(raw_subject.raw_label)
        schedule_text(raw_subject.raw_label)
        for canonical_id in lexical_top_k(subjects, normalized, top_k):
            if canonical_id not in subjects:
                continue
            label = canonical_label_for_id(subjects, canonical_id)
            remember_canonical(canonical_id, label)
            if label not in cache:
                schedule_text(label)

    if not texts_to_fetch:
        return cache

    vectors = fetch_embeddings_parallel(
        client,
        model,
        texts_to_fetch,
        request_id=request_id,
        max_parallel=settings.max_parallel_request,
    )
    for text, vector in zip(texts_to_fetch, vectors):
        cache[text] = vector
        canonical_id = canonical_by_label.get(text)
        if canonical_id is not None:
            set_subject_embedding(sqlite_path, canonical_id, text, vector, model)
    return cache


def resolve_subject_embedding_vectors(
    client: object,
    sqlite_path: str,
    settings: Settings,
    subjects: SubjectsMap,
    raw_label: str,
    candidate_ids: list[str],
    *,
    request_id: str,
    embedding_cache: dict[str, list[float]] | None,
    canonical_label_for_id: CanonicalLabelForId,
) -> tuple[list[float], dict[str, list[float]]]:
    model = settings.matcher_embedding_model
    candidate_vectors: dict[str, list[float]] = {}
    texts_to_fetch: list[str] = []
    fetch_labels: list[str] = []
    canonical_by_label: dict[str, str] = {}

    def schedule_label(label: str, canonical_id: str | None = None) -> None:
        if embedding_cache is not None and label in embedding_cache:
            if canonical_id is not None:
                candidate_vectors[canonical_id] = embedding_cache[label]
            return
        if canonical_id is not None:
            canonical_by_label[label] = canonical_id
            cached = get_subject_embedding(sqlite_path, canonical_id, model)
            if cached is not None:
                candidate_vectors[canonical_id] = cached
                return
        if label in fetch_labels:
            return
        fetch_labels.append(label)
        texts_to_fetch.append(label)

    schedule_label(raw_label)
    for canonical_id in candidate_ids:
        if canonical_id not in subjects:
            continue
        schedule_label(canonical_label_for_id(subjects, canonical_id), canonical_id)

    fetched_by_label: dict[str, list[float]] = {}
    if texts_to_fetch:
        vectors = fetch_embeddings_parallel(
            client,
            model,
            texts_to_fetch,
            request_id=request_id,
            max_parallel=settings.max_parallel_request,
        )
        for label, vector in zip(texts_to_fetch, vectors):
            fetched_by_label[label] = vector
            canonical_id = canonical_by_label.get(label)
            if canonical_id is not None:
                set_subject_embedding(sqlite_path, canonical_id, label, vector, model)
                candidate_vectors[canonical_id] = vector

    raw_vector = fetched_by_label.get(raw_label)
    if raw_vector is None and embedding_cache is not None:
        raw_vector = embedding_cache.get(raw_label)
    if raw_vector is None:
        raise RuntimeError("missing embedding vector for raw subject label")

    for canonical_id in candidate_ids:
        if canonical_id in candidate_vectors:
            continue
        label = canonical_label_for_id(subjects, canonical_id)
        vector = fetched_by_label.get(label)
        if vector is None and embedding_cache is not None:
            vector = embedding_cache.get(label)
        if vector is not None:
            candidate_vectors[canonical_id] = vector

    return raw_vector, candidate_vectors

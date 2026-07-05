from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from src.models.settings import Settings
from src.search.article_catalog import list_index_subjects, _article_is_complete, _load_catalog


def _normalize(text: str) -> str:
    lowered = " ".join(text.strip().split()).lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _best_label_similarity(
    label: str,
    aliases: list[str],
    other_label: str,
    other_aliases: list[str],
) -> float:
    needles = [_normalize(label)] + [_normalize(a) for a in aliases if a]
    haystacks = [_normalize(other_label)] + [_normalize(a) for a in other_aliases if a]
    best = 0.0
    for n in needles:
        if not n:
            continue
        for h in haystacks:
            if not h:
                continue
            if n == h:
                return 1.0
            score = fuzz.ratio(n, h) / 100.0
            if score > best:
                best = score
    return best


def list_poh_overlaps(
    data_root: Path,
    book_sha: str,
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    book_sha_norm = book_sha.strip().lower()
    if not book_sha_norm:
        return []
    subjects = list_index_subjects(data_root)
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    if not isinstance(articles, dict):
        articles = {}
    threshold = float(settings.matcher_similarity_threshold)
    book_poh_ids = [
        poh_id
        for poh_id, entry in subjects.items()
        if book_sha_norm in entry.books and entry.books[book_sha_norm].aligned_pages
    ]
    overlaps: list[dict[str, Any]] = []
    for poh_id in book_poh_ids:
        entry = subjects[poh_id]
        candidates: list[dict[str, Any]] = []
        for other_id, other in subjects.items():
            if other_id == poh_id:
                continue
            sim = _best_label_similarity(
                entry.canonical_label,
                list(entry.aliases),
                other.canonical_label,
                list(other.aliases),
            )
            if sim < threshold:
                continue
            meta = articles.get(other_id)
            has_article = _article_is_complete(data_root, other_id, meta)
            if not has_article:
                continue
            candidates.append(
                {
                    "poh_id": other_id,
                    "label": other.canonical_label,
                    "similarity": round(sim, 3),
                    "has_article": True,
                    "url": meta.get("url") if isinstance(meta, dict) else None,
                }
            )
        candidates.sort(key=lambda c: (-c["similarity"], c["label"].casefold()))
        item: dict[str, Any] = {
            "poh_id": poh_id,
            "label": entry.canonical_label,
            "has_article": _article_is_complete(data_root, poh_id, articles.get(poh_id)),
        }
        if candidates:
            item["similar_to"] = candidates
            overlaps.append(item)
    overlaps.sort(key=lambda x: str(x["label"]).casefold())
    return overlaps

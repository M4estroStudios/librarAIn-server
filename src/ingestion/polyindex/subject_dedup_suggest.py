from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_openai_client
from src.ingestion.polyindex.index_md_parser import normalize_label
from src.ingestion.polyindex.subject_matcher import (
    _LLM_HIGH_SIM,
    _LLM_LOW_SIM,
    _llm_arbitrate,
)
from src.ingestion.progress import STATUS_DONE, STATUS_ERROR, STATUS_PROGRESS, STATUS_STARTED, make_event
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import list_subject_embeddings
from src.search.article_catalog import _article_is_complete, _load_catalog

ProgressReporter = Callable[[dict[str, Any]], None]

_DISMISSED_FILENAME = "admin_dedup_dismissed.json"
_SUGGESTIONS_FILENAME = "admin_dedup_suggestions.json"
_SCHEMA_VERSION = "1.0"
_PHASE = "subject_dedup"
_DEFAULT_CLUSTER_LIMIT = 200
_PAIR_PROGRESS_EVERY = 500


@dataclass(frozen=True)
class DedupEdge:
    left_id: str
    right_id: str
    score: float
    method: str
    llm_reason: str | None = None


class UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self._parent = {item: item for item in ids}

    def find(self, item: str) -> str:
        parent = self._parent[item]
        if parent != item:
            self._parent[item] = self.find(parent)
        return self._parent[item]

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left


def pair_key(left_id: str, right_id: str) -> str:
    if left_id <= right_id:
        return f"{left_id}|{right_id}"
    return f"{right_id}|{left_id}"


def cluster_key(member_ids: list[str]) -> str:
    return "|".join(sorted(member_ids))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


def dismissed_path(polyindex_dir: Path) -> Path:
    return polyindex_dir / _DISMISSED_FILENAME


def suggestions_path(polyindex_dir: Path) -> Path:
    return polyindex_dir / _SUGGESTIONS_FILENAME


def load_dismissed_pairs(polyindex_dir: Path) -> set[str]:
    path = dismissed_path(polyindex_dir)
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(raw, dict):
        return set()
    pairs = raw.get("pairs")
    if not isinstance(pairs, list):
        return set()
    return {str(item) for item in pairs if isinstance(item, str) and item}


def save_dismissed_pairs(polyindex_dir: Path, pairs: set[str]) -> None:
    _atomic_write_json(
        dismissed_path(polyindex_dir),
        {
            "schema_version": _SCHEMA_VERSION,
            "pairs": sorted(pairs),
            "updated_at": _utc_now_iso(),
        },
    )


def dismiss_pairs(polyindex_dir: Path, pair_ids: list[str]) -> list[str]:
    cleaned = [pid for pid in dict.fromkeys(pair_ids) if isinstance(pid, str) and "|" in pid]
    if not cleaned:
        return []
    current = load_dismissed_pairs(polyindex_dir)
    current.update(cleaned)
    save_dismissed_pairs(polyindex_dir, current)
    return cleaned


def dismiss_cluster(polyindex_dir: Path, member_ids: list[str]) -> list[str]:
    members = [mid for mid in dict.fromkeys(member_ids) if mid]
    if len(members) < 2:
        return []
    pairs: list[str] = []
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            pairs.append(pair_key(left, right))
    return dismiss_pairs(polyindex_dir, pairs)


def load_suggestions(polyindex_dir: Path) -> dict[str, Any]:
    path = suggestions_path(polyindex_dir)
    if not path.is_file():
        return {
            "schema_version": _SCHEMA_VERSION,
            "scanned_at": None,
            "clusters": [],
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "schema_version": _SCHEMA_VERSION,
            "scanned_at": None,
            "clusters": [],
        }
    if not isinstance(raw, dict):
        return {
            "schema_version": _SCHEMA_VERSION,
            "scanned_at": None,
            "clusters": [],
        }
    clusters = raw.get("clusters")
    if not isinstance(clusters, list):
        clusters = []
    return {
        "schema_version": str(raw.get("schema_version") or _SCHEMA_VERSION),
        "scanned_at": raw.get("scanned_at"),
        "clusters": clusters,
        "stats": raw.get("stats") if isinstance(raw.get("stats"), dict) else {},
    }


def save_suggestions(polyindex_dir: Path, payload: dict[str, Any]) -> None:
    _atomic_write_json(suggestions_path(polyindex_dir), payload)


def _label_keys(entry: PolyindexIndexSubjectEntry) -> list[str]:
    keys = [normalize_label(entry.canonical_label)]
    for alias in entry.aliases:
        if alias.strip():
            keys.append(normalize_label(alias))
    return [key for key in keys if key]


def _fuzzy_similarity(
    left: PolyindexIndexSubjectEntry,
    right: PolyindexIndexSubjectEntry,
) -> float:
    left_keys = _label_keys(left)
    right_keys = _label_keys(right)
    best = 0.0
    for left_key in left_keys:
        for right_key in right_keys:
            if left_key == right_key:
                return 1.0
            score = fuzz.token_sort_ratio(left_key, right_key) / 100.0
            if score > best:
                best = score
    return best


def _book_count(entry: PolyindexIndexSubjectEntry) -> int:
    return sum(1 for book in entry.books.values() if book.aligned_pages)


def _pick_suggested_target(
    member_ids: list[str],
    document: PolyindexIndexDocument,
    has_article: dict[str, bool],
) -> str:
    def sort_key(canonical_id: str) -> tuple[int, int, int, str]:
        entry = document.subjects[canonical_id]
        return (
            -_book_count(entry),
            -int(has_article.get(canonical_id, False)),
            len(entry.canonical_label.strip()),
            canonical_id,
        )

    return sorted(member_ids, key=sort_key)[0]


def _pair_decision(
    score: float,
    *,
    threshold: float,
    use_llm: bool,
    client: object | None,
    settings: Settings,
    left_label: str,
    right_label: str,
    request_id: str,
) -> tuple[bool, str | None]:
    if score >= _LLM_HIGH_SIM:
        return True, None
    if score < _LLM_LOW_SIM:
        return score >= threshold, None
    if use_llm and client is not None:
        same, reason = _llm_arbitrate(
            client,
            settings,
            left_label,
            right_label,
            request_id=request_id,
        )
        return same, reason
    return score >= threshold, None


def list_open_suggestions(polyindex_dir: Path) -> dict[str, Any]:
    payload = load_suggestions(polyindex_dir)
    dismissed = load_dismissed_pairs(polyindex_dir)
    open_clusters: list[dict[str, Any]] = []
    for cluster in payload.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        members = cluster.get("members")
        if not isinstance(members, list) or len(members) < 2:
            continue
        member_ids = [
            str(item.get("canonical_id"))
            for item in members
            if isinstance(item, dict) and item.get("canonical_id")
        ]
        if len(member_ids) < 2:
            continue
        blocked = False
        for index, left in enumerate(member_ids):
            for right in member_ids[index + 1 :]:
                if pair_key(left, right) in dismissed:
                    blocked = True
                    break
            if blocked:
                break
        if blocked:
            continue
        open_clusters.append(cluster)
    return {
        "schema_version": payload.get("schema_version") or _SCHEMA_VERSION,
        "scanned_at": payload.get("scanned_at"),
        "clusters": open_clusters,
        "stats": payload.get("stats") or {},
        "dismissed_pair_count": len(dismissed),
    }


def run_subject_dedup_scan(
    polyindex_dir: Path,
    settings: Settings,
    *,
    data_root: Path | None = None,
    request_id: str,
    min_similarity: float | None = None,
    use_llm: bool | None = None,
    limit: int | None = None,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    threshold = (
        float(min_similarity)
        if min_similarity is not None
        else float(settings.matcher_similarity_threshold)
    )
    llm_enabled = settings.matcher_use_ai if use_llm is None else bool(use_llm)
    cluster_limit = max(1, int(limit) if limit is not None else _DEFAULT_CLUSTER_LIMIT)
    root = data_root if data_root is not None else polyindex_dir.parent

    document = PolyindexIndexDocument.load_file(polyindex_dir / "INDEX.json")
    subject_ids = sorted(document.subjects.keys())
    total_subjects = len(subject_ids)
    if progress is not None:
        progress(
            make_event(
                _PHASE,
                STATUS_STARTED,
                total=total_subjects,
                message=f"Scansione dedup su {total_subjects} soggetti",
            )
        )

    if total_subjects < 2:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "scanned_at": _utc_now_iso(),
            "clusters": [],
            "stats": {
                "total_subjects": total_subjects,
                "pair_comparisons": 0,
                "edges": 0,
                "clusters": 0,
                "llm_calls": 0,
                "threshold": threshold,
                "use_llm": llm_enabled,
            },
        }
        save_suggestions(polyindex_dir, payload)
        if progress is not None:
            progress(make_event(_PHASE, STATUS_DONE, clusters=0, message="Nessun soggetto da confrontare"))
        return payload

    catalog = _load_catalog(root)
    articles = catalog.get("articles", {})
    if not isinstance(articles, dict):
        articles = {}
    has_article = {
        canonical_id: _article_is_complete(root, canonical_id, articles.get(canonical_id))
        for canonical_id in subject_ids
    }

    model = settings.matcher_embedding_model
    embedding_map = {
        canonical_id: vector
        for canonical_id, _label, vector in list_subject_embeddings(settings.sqlite_path, model)
        if canonical_id in document.subjects
    }

    dismissed = load_dismissed_pairs(polyindex_dir)
    client: object | None = None
    if llm_enabled:
        try:
            client = build_openai_client(settings)
        except Exception as exc:
            Log(
                WARNING_LOG_LEVEL,
                "subject dedup LLM client unavailable; continuing without LLM",
                {"request_id": request_id, "error": str(exc)},
            )
            llm_enabled = False
            client = None

    edges: list[DedupEdge] = []
    llm_calls = 0
    pair_comparisons = 0
    total_pairs = total_subjects * (total_subjects - 1) // 2

    for i, left_id in enumerate(subject_ids):
        left_entry = document.subjects[left_id]
        left_vector = embedding_map.get(left_id)
        for right_id in subject_ids[i + 1 :]:
            pair_comparisons += 1
            key = pair_key(left_id, right_id)
            if key in dismissed:
                continue
            right_entry = document.subjects[right_id]
            right_vector = embedding_map.get(right_id)
            method = "fuzzy"
            if left_vector is not None and right_vector is not None:
                score = _cosine_similarity(left_vector, right_vector)
                method = "embedding"
            else:
                score = _fuzzy_similarity(left_entry, right_entry)
            if score < _LLM_LOW_SIM and score < threshold:
                continue
            accept, reason = _pair_decision(
                score,
                threshold=threshold,
                use_llm=llm_enabled,
                client=client,
                settings=settings,
                left_label=left_entry.canonical_label,
                right_label=right_entry.canonical_label,
                request_id=request_id,
            )
            if reason is not None:
                llm_calls += 1
            if not accept:
                continue
            edges.append(
                DedupEdge(
                    left_id=left_id,
                    right_id=right_id,
                    score=score,
                    method=method,
                    llm_reason=reason,
                )
            )
            if progress is not None and pair_comparisons % _PAIR_PROGRESS_EVERY == 0:
                progress(
                    make_event(
                        _PHASE,
                        STATUS_PROGRESS,
                        done=pair_comparisons,
                        total=total_pairs,
                        edges=len(edges),
                        message=f"Confronti {pair_comparisons}/{total_pairs}",
                    )
                )

    uf = UnionFind(subject_ids)
    edge_by_pair = {pair_key(edge.left_id, edge.right_id): edge for edge in edges}
    for edge in edges:
        uf.union(edge.left_id, edge.right_id)

    groups: dict[str, list[str]] = {}
    for subject_id in subject_ids:
        root_id = uf.find(subject_id)
        groups.setdefault(root_id, []).append(subject_id)

    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_ids = sorted(members)
        pair_scores: list[float] = []
        methods: set[str] = set()
        reasons: list[str] = []
        for index, left in enumerate(member_ids):
            for right in member_ids[index + 1 :]:
                edge = edge_by_pair.get(pair_key(left, right))
                if edge is None:
                    continue
                pair_scores.append(edge.score)
                methods.add(edge.method)
                if edge.llm_reason:
                    reasons.append(edge.llm_reason)
        if not pair_scores:
            continue
        suggested = _pick_suggested_target(member_ids, document, has_article)
        clusters.append(
            {
                "cluster_key": cluster_key(member_ids),
                "suggested_target_id": suggested,
                "score": round(max(pair_scores), 4),
                "methods": sorted(methods),
                "llm_reasons": reasons[:5],
                "members": [
                    {
                        "canonical_id": member_id,
                        "canonical_label": document.subjects[member_id].canonical_label,
                        "aliases": list(document.subjects[member_id].aliases),
                        "book_count": _book_count(document.subjects[member_id]),
                        "has_article": bool(has_article.get(member_id)),
                        "time_range": document.subjects[member_id].time_range,
                    }
                    for member_id in member_ids
                ],
            }
        )

    clusters.sort(key=lambda item: (-float(item["score"]), str(item["cluster_key"])))
    if len(clusters) > cluster_limit:
        clusters = clusters[:cluster_limit]

    payload = {
        "schema_version": _SCHEMA_VERSION,
        "scanned_at": _utc_now_iso(),
        "clusters": clusters,
        "stats": {
            "total_subjects": total_subjects,
            "pair_comparisons": pair_comparisons,
            "edges": len(edges),
            "clusters": len(clusters),
            "llm_calls": llm_calls,
            "threshold": threshold,
            "use_llm": llm_enabled,
            "embedded_subjects": len(embedding_map),
        },
    }
    save_suggestions(polyindex_dir, payload)
    Log(
        INFO_LOG_LEVEL,
        "subject dedup scan completed",
        {
            "request_id": request_id,
            "clusters": len(clusters),
            "edges": len(edges),
            "llm_calls": llm_calls,
        },
    )
    if progress is not None:
        progress(
            make_event(
                _PHASE,
                STATUS_DONE,
                clusters=len(clusters),
                edges=len(edges),
                message=f"Trovati {len(clusters)} cluster",
            )
        )
    return payload


def emit_dedup_error(progress: ProgressReporter | None, message: str) -> None:
    if progress is None:
        return
    progress(make_event(_PHASE, STATUS_ERROR, message=message))

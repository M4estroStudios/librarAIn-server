from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai
from rapidfuzz import fuzz

from src.core.errors import classify_openai_exception
from src.core.log import Log, WARNING_LOG_LEVEL
from src.core.retry import retry_sync
from src.core.text import slugify as _slugify
from src.ingestion.polyindex.index_md_parser import RawSubject, normalize_label
from src.models.settings import Settings
from src.core.openai_client import build_system_prompt
from src.persistence.subject_matcher_sqlite import (
    get_subject_embedding,
    insert_subject_match_audit,
    set_subject_embedding,
)

_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "subject_matcher_prompt.md"
)
_FUZZY_BORDERLINE_SCORE = 90
_LLM_LOW_SIM = 0.82
_LLM_HIGH_SIM = 0.92
_TOP_K = 10
_EMBEDDING_DIM_FALLBACK = 64


@dataclass(frozen=True)
class MatchDecision:
    action: str
    canonical_id: str
    similarity: float | None = None
    ai_used: bool = False
    ai_reason: str | None = None


def _subjects_map(polyindex_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    subjects = polyindex_state.get("subjects")
    if isinstance(subjects, dict):
        return subjects
    return {}


def _canonical_normalized_keys(entry: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    label = entry.get("canonical_label")
    if isinstance(label, str) and label.strip():
        keys.append(normalize_label(label))
    aliases = entry.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                keys.append(normalize_label(alias))
    return keys


def _find_exact_canonical(
    subjects: dict[str, dict[str, Any]], normalized: str
) -> str | None:
    for canonical_id, entry in subjects.items():
        if normalized in _canonical_normalized_keys(entry):
            return canonical_id
    return None


def _fuzzy_borderline_candidates(
    subjects: dict[str, dict[str, Any]], normalized: str
) -> list[tuple[str, int]]:
    scored: list[tuple[str, int]] = []
    for canonical_id, entry in subjects.items():
        best = 0
        for key in _canonical_normalized_keys(entry):
            score = fuzz.token_sort_ratio(normalized, key)
            if score > best:
                best = score
        if best >= _FUZZY_BORDERLINE_SCORE:
            scored.append((canonical_id, best))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _lexical_top_k(
    subjects: dict[str, dict[str, Any]], normalized: str, limit: int
) -> list[str]:
    scored: list[tuple[str, int]] = []
    for canonical_id, entry in subjects.items():
        best = 0
        for key in _canonical_normalized_keys(entry):
            score = fuzz.token_sort_ratio(normalized, key)
            if score > best:
                best = score
        scored.append((canonical_id, best))
    scored.sort(key=lambda item: (-item[1], item[0]))
    if len(scored) <= limit:
        return [canonical_id for canonical_id, _ in scored]
    return [canonical_id for canonical_id, _ in scored[:limit]]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embedding_vector_from_response(data: Any) -> list[float]:
    if hasattr(data, "__iter__") and not isinstance(data, (str, bytes)):
        return [float(x) for x in data]
    raise ValueError("unexpected embedding payload")


_MATCHER_RETRY_ATTEMPTS = 3


def _openai_call_with_retry(fn: Any) -> Any:
    """Run a blocking OpenAI call with transient-error classification + retry."""

    def attempt() -> Any:
        try:
            return fn()
        except openai.OpenAIError as exc:
            wrapped = classify_openai_exception(exc)
            raise wrapped(str(exc)) from exc

    return retry_sync(attempt, max_attempts=_MATCHER_RETRY_ATTEMPTS)


def _fetch_embedding(
    client: openai.OpenAI, model: str, text: str
) -> list[float]:
    response = _openai_call_with_retry(
        lambda: client.embeddings.create(model=model, input=text)
    )
    return _embedding_vector_from_response(response.data[0].embedding)


def _load_matcher_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _matcher_llm_model(settings: Settings) -> str:
    if settings.matcher_llm_model:
        return settings.matcher_llm_model
    if settings.editor_model:
        return settings.editor_model
    return "gpt-4.1-mini"


def _coerce_same_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "si", "sì")
    return bool(value)


def _brace_delimited_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r'\{\s*"same"\s*:', text, re.IGNORECASE):
        start = match.start()
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    return candidates


def _json_candidates_from_llm_content(content: str) -> list[str]:
    stripped = content.strip()
    if not stripped:
        return []
    candidates = [stripped]
    if stripped.startswith("```"):
        unfenced = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        unfenced = re.sub(r"\s*```$", "", unfenced)
        candidates.append(unfenced.strip())
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.extend(_brace_delimited_json_candidates(stripped))
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _parse_llm_same_response(content: str) -> tuple[bool, str] | None:
    for candidate in _json_candidates_from_llm_content(content):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "same" not in payload:
            continue
        same = _coerce_same_flag(payload.get("same"))
        reason = str(payload.get("reason", "")).strip()
        return same, reason
    return None


def _llm_arbitrate(
    client: openai.OpenAI,
    settings: Settings,
    raw_label: str,
    candidate_label: str,
    *,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> tuple[bool, str]:
    system_prompt = build_system_prompt(_load_matcher_system_prompt(), prompt_notes)
    user_content = json.dumps(
        {"label_a": raw_label, "label_b": candidate_label},
        ensure_ascii=False,
    )
    response = _openai_call_with_retry(
        lambda: client.chat.completions.create(
            model=_matcher_llm_model(settings),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
        )
    )
    content = response.choices[0].message.content or ""
    parsed = _parse_llm_same_response(content)
    if parsed is not None:
        return parsed
    Log(
        WARNING_LOG_LEVEL,
        "subject matcher LLM response not parseable as JSON; treating as different entities",
        {
            "request_id": request_id,
            "raw_label": raw_label,
            "candidate_label": candidate_label,
            "content_preview": content[:200],
        },
    )
    return False, "llm_response_unparseable"


def _canonical_label_for_id(
    subjects: dict[str, dict[str, Any]], canonical_id: str
) -> str:
    entry = subjects.get(canonical_id, {})
    label = entry.get("canonical_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return canonical_id


def _get_or_create_canonical_embedding(
    client: openai.OpenAI,
    sqlite_path: str,
    settings: Settings,
    subjects: dict[str, dict[str, Any]],
    canonical_id: str,
) -> list[float]:
    model = settings.matcher_embedding_model
    cached = get_subject_embedding(sqlite_path, canonical_id, model)
    if cached is not None:
        return cached
    label = _canonical_label_for_id(subjects, canonical_id)
    vector = _fetch_embedding(client, model, label)
    set_subject_embedding(sqlite_path, canonical_id, label, vector, model)
    return vector


def _allocate_canonical_id(
    subjects: dict[str, dict[str, Any]], normalized: str
) -> str:
    base = _slugify(normalized)
    if base not in subjects:
        return base
    counter = 2
    while True:
        candidate = f"{base}-{counter}"
        if candidate not in subjects:
            return candidate
        counter += 1


def _resolve_alias_target(
    subjects: dict[str, dict[str, Any]], alias_of: str
) -> MatchDecision | None:
    target_norm = normalize_label(alias_of)
    canonical_id = _find_exact_canonical(subjects, target_norm)
    if canonical_id is not None:
        return MatchDecision(action="alias", canonical_id=canonical_id, ai_used=False)
    return None


def _stage2_match(
    raw_subject: RawSubject,
    subjects: dict[str, dict[str, Any]],
    client: openai.OpenAI,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    prompt_notes: str | None = None,
) -> MatchDecision:
    normalized = normalize_label(raw_subject.raw_label)
    model = settings.matcher_embedding_model
    candidate_ids = _lexical_top_k(subjects, normalized, _TOP_K)
    if not candidate_ids:
        return MatchDecision(
            action="new",
            canonical_id=_allocate_canonical_id(subjects, normalized),
            ai_used=False,
        )

    raw_vector = _fetch_embedding(client, model, raw_subject.raw_label)
    best_id: str | None = None
    best_sim = -1.0
    for canonical_id in candidate_ids:
        if canonical_id not in subjects:
            continue
        candidate_vector = _get_or_create_canonical_embedding(
            client, sqlite_path, settings, subjects, canonical_id
        )
        sim = _cosine_similarity(raw_vector, candidate_vector)
        if sim > best_sim:
            best_sim = sim
            best_id = canonical_id

    if best_id is None:
        return MatchDecision(
            action="new",
            canonical_id=_allocate_canonical_id(subjects, normalized),
            ai_used=False,
        )

    threshold = settings.matcher_similarity_threshold
    if best_sim >= _LLM_HIGH_SIM:
        return MatchDecision(
            action="match",
            canonical_id=best_id,
            similarity=best_sim,
            ai_used=True,
        )

    if best_sim >= threshold or best_sim >= _LLM_LOW_SIM:
        if best_sim < _LLM_HIGH_SIM:
            candidate_label = _canonical_label_for_id(subjects, best_id)
            same, reason = _llm_arbitrate(
                client,
                settings,
                raw_subject.raw_label,
                candidate_label,
                request_id=request_id,
                prompt_notes=prompt_notes,
            )
            if same:
                return MatchDecision(
                    action="match",
                    canonical_id=best_id,
                    similarity=best_sim,
                    ai_used=True,
                    ai_reason=reason,
                )
            return MatchDecision(
                action="new",
                canonical_id=_allocate_canonical_id(subjects, normalized),
                similarity=best_sim,
                ai_used=True,
                ai_reason=reason,
            )
        return MatchDecision(
            action="match",
            canonical_id=best_id,
            similarity=best_sim,
            ai_used=True,
        )

    return MatchDecision(
        action="new",
        canonical_id=_allocate_canonical_id(subjects, normalized),
        similarity=best_sim,
        ai_used=True,
    )


def find_exact_canonical(
    subjects: dict[str, dict[str, Any]], normalized: str
) -> str | None:
    """Public lock-safe exact lookup by normalized label/alias."""
    return _find_exact_canonical(subjects, normalized)


def allocate_canonical_id(
    subjects: dict[str, dict[str, Any]], normalized: str
) -> str:
    """Public allocator for a unique canonical id from a normalized label."""
    return _allocate_canonical_id(subjects, normalized)


def match_subject(
    raw_subject: RawSubject,
    polyindex_state: dict[str, Any],
    client: openai.OpenAI,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    prompt_notes: str | None = None,
) -> MatchDecision:
    subjects = _subjects_map(polyindex_state)
    normalized = normalize_label(raw_subject.raw_label)

    if raw_subject.alias_of:
        alias_decision = _resolve_alias_target(subjects, raw_subject.alias_of)
        if alias_decision is not None:
            decision = alias_decision
        else:
            target_norm = normalize_label(raw_subject.alias_of)
            decision = MatchDecision(
                action="new",
                canonical_id=_allocate_canonical_id(subjects, target_norm),
                ai_used=False,
            )
    else:
        exact_id = _find_exact_canonical(subjects, normalized)
        if exact_id is not None:
            entry = subjects.get(exact_id, {})
            canonical_norm = normalize_label(
                str(entry.get("canonical_label", raw_subject.raw_label))
            )
            if normalized == canonical_norm:
                decision = MatchDecision(action="match", canonical_id=exact_id, ai_used=False)
            else:
                decision = MatchDecision(action="alias", canonical_id=exact_id, ai_used=False)
        else:
            borderline = _fuzzy_borderline_candidates(subjects, normalized)
            if borderline and not settings.matcher_use_ai:
                decision = MatchDecision(
                    action="match",
                    canonical_id=borderline[0][0],
                    ai_used=False,
                )
            elif borderline or settings.matcher_use_ai:
                decision = _stage2_match(
                    raw_subject,
                    subjects,
                    client,
                    sqlite_path,
                    settings,
                    request_id,
                    prompt_notes=prompt_notes,
                )
            else:
                decision = MatchDecision(
                    action="new",
                    canonical_id=_allocate_canonical_id(subjects, normalized),
                    ai_used=False,
                )

    insert_subject_match_audit(
        sqlite_path,
        request_id=request_id,
        raw_label=raw_subject.raw_label,
        normalized=normalized,
        decision=decision.action,
        canonical_id=decision.canonical_id,
        similarity=decision.similarity,
        ai_used=decision.ai_used,
        ai_reason=decision.ai_reason,
    )
    return decision

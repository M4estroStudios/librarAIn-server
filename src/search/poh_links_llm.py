from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log, safe_text
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.polyindex.index_md_parser import normalize_label
from src.models.polyindex_index import PolyindexIndexDocument
from src.models.settings import Settings
from src.search.article_llm import (
    is_no_material_article,
    query_log_fields,
    research_model,
    strip_article_markdown_fences,
)
from src.search.request_schema import ResearchPoh
from src.search.subject_lookup import SubjectMatch

_STAGE = "research_poh_links"
_MAX_COMPLETION_TOKENS = 8192
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "poh_links_prompt.md"


@dataclass(frozen=True)
class PohCandidate:
    poh_id: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PohLinksResult:
    markdown: str
    skipped_llm: bool
    model: str | None = None


def load_poh_links_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _contains_normalized(haystack_norm: str, needle_norm: str) -> bool:
    if not needle_norm:
        return False
    pattern = r"(?<!\w)" + re.escape(needle_norm) + r"(?!\w)"
    return re.search(pattern, haystack_norm) is not None


def _subjects_in_text(
    text: str,
    document: PolyindexIndexDocument,
) -> dict[str, SubjectMatch]:
    text_norm = normalize_label(text)
    found: dict[str, SubjectMatch] = {}
    for canonical_id, entry in document.subjects.items():
        if _contains_normalized(text_norm, normalize_label(entry.canonical_label)):
            found[canonical_id] = SubjectMatch(
                canonical_id=canonical_id,
                canonical_label=entry.canonical_label,
                method="article_exact",
            )
            continue
        for alias in entry.aliases:
            if _contains_normalized(text_norm, normalize_label(alias)):
                found[canonical_id] = SubjectMatch(
                    canonical_id=canonical_id,
                    canonical_label=entry.canonical_label,
                    method="article_alias",
                )
                break
    return found


def build_poh_candidates(
    *,
    document: PolyindexIndexDocument,
    subject_matches: list[SubjectMatch],
    article_markdown: str,
    query: str,
) -> list[PohCandidate]:
    matched_ids: dict[str, SubjectMatch] = {
        match.canonical_id: match for match in subject_matches
    }
    for source in (query, article_markdown):
        matched_ids.update(_subjects_in_text(source, document))

    candidates: list[PohCandidate] = []
    for canonical_id in sorted(matched_ids):
        entry = document.subjects.get(canonical_id)
        if entry is None:
            continue
        candidates.append(
            PohCandidate(
                poh_id=canonical_id,
                label=entry.canonical_label,
                aliases=tuple(entry.aliases),
            )
        )
    return candidates


def _primary_poh_payload(poh: ResearchPoh | None) -> dict[str, str] | None:
    if poh is None:
        return None
    payload: dict[str, str] = {"label": poh.label}
    if poh.id:
        payload["id"] = poh.id
    if poh.time_range:
        payload["time_range"] = poh.time_range
    return payload


def build_poh_links_user_payload(
    *,
    query: str,
    article_markdown: str,
    poh_candidates: list[PohCandidate],
    poh: ResearchPoh | None,
) -> dict[str, Any]:
    return {
        "query": query.strip(),
        "primary_poh": _primary_poh_payload(poh),
        "poh_candidates": [
            {
                "id": candidate.poh_id,
                "label": candidate.label,
                "aliases": list(candidate.aliases),
            }
            for candidate in poh_candidates
        ],
        "article_markdown": article_markdown,
    }


def build_poh_links_user_message(
    *,
    query: str,
    article_markdown: str,
    poh_candidates: list[PohCandidate],
    poh: ResearchPoh | None,
) -> str:
    payload = build_poh_links_user_payload(
        query=query,
        article_markdown=article_markdown,
        poh_candidates=poh_candidates,
        poh=poh,
    )
    return json.dumps(payload, ensure_ascii=False)


async def add_poh_links(
    *,
    query: str,
    article_markdown: str,
    poh_candidates: list[PohCandidate],
    client: openai.OpenAI,
    settings: Settings,
    poh: ResearchPoh | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> PohLinksResult:
    log_fields = query_log_fields(query)
    if is_no_material_article(article_markdown):
        Log(
            INFO_LOG_LEVEL,
            "research poh links skipped: no-material article",
            {
                "request_id": request_id,
                "stage": _STAGE,
                **log_fields,
            },
        )
        return PohLinksResult(
            markdown=article_markdown,
            skipped_llm=True,
            model=None,
        )
    if not poh_candidates:
        Log(
            INFO_LOG_LEVEL,
            "research poh links skipped: no poh candidates",
            {
                "request_id": request_id,
                "stage": _STAGE,
                **log_fields,
            },
        )
        return PohLinksResult(
            markdown=article_markdown,
            skipped_llm=True,
            model=None,
        )

    model = research_model(settings)
    system_prompt = build_system_prompt(load_poh_links_prompt(), prompt_notes)
    user_message = build_poh_links_user_message(
        query=query,
        article_markdown=article_markdown,
        poh_candidates=poh_candidates,
        poh=poh,
    )
    Log(
        INFO_LOG_LEVEL,
        "research poh links begin",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "candidate_count": len(poh_candidates),
            "article_chars": len(article_markdown),
            "user_message_preview": safe_text(user_message),
            **log_fields,
        },
    )
    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=settings.research_temperature,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage=_STAGE,
        page=0,
        reasoning_effort=settings.reasoning_effort_research,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_research,
    )
    markdown = strip_article_markdown_fences(content)
    Log(
        INFO_LOG_LEVEL,
        "research poh links completed",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "markdown_chars": len(markdown),
            "markdown_preview": safe_text(markdown),
            **log_fields,
        },
    )
    return PohLinksResult(
        markdown=markdown,
        skipped_llm=False,
        model=model,
    )

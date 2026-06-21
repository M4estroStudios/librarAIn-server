from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log, safe_text
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.models.settings import Settings
from src.search.article_llm import (
    is_no_material_article,
    query_log_fields,
    research_model,
    strip_article_markdown_fences,
)
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh
from src.search.time_lookup import TimelineCandidate

_STAGE = "research_timeline"
_MAX_COMPLETION_TOKENS = 8192
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "timeline_prompt.md"


@dataclass(frozen=True)
class TimelineResult:
    markdown: str
    skipped_llm: bool
    model: str | None = None


def load_timeline_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _primary_poh_payload(poh: ResearchPoh | None) -> dict[str, str] | None:
    if poh is None:
        return None
    payload: dict[str, str] = {"label": poh.label}
    if poh.id:
        payload["id"] = poh.id
    if poh.time_range:
        payload["time_range"] = poh.time_range
    return payload


def build_timeline_user_payload(
    *,
    query: str,
    article_markdown: str,
    timeline_candidates: list[TimelineCandidate],
    pages: list[LoadedPage],
    poh: ResearchPoh | None,
) -> dict[str, Any]:
    return {
        "query": query.strip(),
        "primary_poh": _primary_poh_payload(poh),
        "article_markdown": article_markdown,
        "timeline_candidates": [
            {
                "label": candidate.label,
                "source_sha256": candidate.source_sha256,
                "aligned_pages": list(candidate.aligned_pages),
            }
            for candidate in timeline_candidates
        ],
        "pages": [
            {
                "source_sha256": page.source_sha256,
                "aligned_page": page.aligned_page,
                "book_title": page.book_title,
                "text": page.markdown,
            }
            for page in pages
        ],
    }


def build_timeline_user_message(
    *,
    query: str,
    article_markdown: str,
    timeline_candidates: list[TimelineCandidate],
    pages: list[LoadedPage],
    poh: ResearchPoh | None,
) -> str:
    payload = build_timeline_user_payload(
        query=query,
        article_markdown=article_markdown,
        timeline_candidates=timeline_candidates,
        pages=pages,
        poh=poh,
    )
    return json.dumps(payload, ensure_ascii=False)


async def add_timeline(
    *,
    query: str,
    article_markdown: str,
    timeline_candidates: list[TimelineCandidate],
    pages: list[LoadedPage],
    client: openai.OpenAI,
    settings: Settings,
    poh: ResearchPoh | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> TimelineResult:
    log_fields = query_log_fields(query, poh)
    subject = log_fields["research_subject"]
    if is_no_material_article(article_markdown):
        Log(
            INFO_LOG_LEVEL,
            f"research timeline skipped (no material): {subject}",
            {
                "request_id": request_id,
                "stage": _STAGE,
                **log_fields,
            },
        )
        return TimelineResult(
            markdown=article_markdown,
            skipped_llm=True,
            model=None,
        )

    model = research_model(settings)
    system_prompt = build_system_prompt(load_timeline_prompt(), prompt_notes)
    user_message = build_timeline_user_message(
        query=query,
        article_markdown=article_markdown,
        timeline_candidates=timeline_candidates,
        pages=pages,
        poh=poh,
    )
    Log(
        INFO_LOG_LEVEL,
        f"research timeline begin: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "candidate_count": len(timeline_candidates),
            "page_count": len(pages),
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
        f"research timeline completed: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "markdown_chars": len(markdown),
            "markdown_preview": safe_text(markdown),
            **log_fields,
        },
    )
    return TimelineResult(
        markdown=markdown,
        skipped_llm=False,
        model=model,
    )

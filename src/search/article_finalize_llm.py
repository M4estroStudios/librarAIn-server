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
from src.search.request_schema import ResearchPoh

_STAGE = "research_finalize"
_MAX_COMPLETION_TOKENS = 8192
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "article_finalize_prompt.md"


@dataclass(frozen=True)
class ArticleFinalizeResult:
    markdown: str
    skipped_llm: bool
    model: str | None = None


def load_article_finalize_prompt() -> str:
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


def build_article_finalize_user_payload(
    *,
    query: str,
    draft_markdown: str,
    enriched_markdown: str,
    poh: ResearchPoh | None,
) -> dict[str, Any]:
    return {
        "query": query.strip(),
        "primary_poh": _primary_poh_payload(poh),
        "draft_markdown": draft_markdown,
        "enriched_markdown": enriched_markdown,
    }


def build_article_finalize_user_message(
    *,
    query: str,
    draft_markdown: str,
    enriched_markdown: str,
    poh: ResearchPoh | None,
) -> str:
    payload = build_article_finalize_user_payload(
        query=query,
        draft_markdown=draft_markdown,
        enriched_markdown=enriched_markdown,
        poh=poh,
    )
    return json.dumps(payload, ensure_ascii=False)


async def finalize_article(
    *,
    query: str,
    draft_markdown: str,
    enriched_markdown: str,
    client: openai.OpenAI,
    settings: Settings,
    poh: ResearchPoh | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> ArticleFinalizeResult:
    log_fields = query_log_fields(query, poh)
    subject = log_fields["research_subject"]
    if is_no_material_article(draft_markdown) or is_no_material_article(enriched_markdown):
        Log(
            INFO_LOG_LEVEL,
            f"research finalize skipped (no material): {subject}",
            {
                "request_id": request_id,
                "stage": _STAGE,
                **log_fields,
            },
        )
        return ArticleFinalizeResult(
            markdown=enriched_markdown,
            skipped_llm=True,
            model=None,
        )

    model = research_model(settings)
    system_prompt = build_system_prompt(load_article_finalize_prompt(), prompt_notes)
    user_message = build_article_finalize_user_message(
        query=query,
        draft_markdown=draft_markdown,
        enriched_markdown=enriched_markdown,
        poh=poh,
    )
    Log(
        INFO_LOG_LEVEL,
        f"research finalize begin: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "draft_chars": len(draft_markdown),
            "enriched_chars": len(enriched_markdown),
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
        f"research finalize completed: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "markdown_chars": len(markdown),
            "markdown_preview": safe_text(markdown),
            **log_fields,
        },
    )
    return ArticleFinalizeResult(
        markdown=markdown,
        skipped_llm=False,
        model=model,
    )

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log, safe_text
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.models.settings import Settings
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh

_STAGE = "research_article"
_MAX_COMPLETION_TOKENS = 8192
_QUERY_PREVIEW_LEN = 80
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "article_prompt.md"

_NO_MATERIAL_TITLE = "# Materiale insufficiente"
_NO_MATERIAL_BODY = (
    "La biblioteca indicizzata non contiene pagine candidate sufficienti per "
    "rispondere alla query con fonti verificabili."
)


@dataclass(frozen=True)
class ArticleGenerationResult:
    markdown: str
    skipped_llm: bool
    model: str | None = None


def load_article_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def is_no_material_article(markdown: str) -> bool:
    return markdown.lstrip().startswith(_NO_MATERIAL_TITLE)


def build_no_material_article(query: str) -> str:
    preview = query.strip()
    if len(preview) > _QUERY_PREVIEW_LEN:
        preview = preview[: _QUERY_PREVIEW_LEN - 1].rstrip() + "…"
    lines = [
        _NO_MATERIAL_TITLE,
        "",
        _NO_MATERIAL_BODY,
        "",
        f"**Query:** {preview}",
    ]
    return "\n".join(lines)


def query_log_fields(query: str) -> dict[str, str]:
    normalized = query.strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    preview = normalized
    if len(preview) > _QUERY_PREVIEW_LEN:
        preview = preview[: _QUERY_PREVIEW_LEN - 1].rstrip() + "…"
    return {"query_hash": digest, "query_preview": preview}


def strip_article_markdown_fences(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    unfenced = re.sub(r"^```(?:markdown|md)?\s*", "", stripped, flags=re.IGNORECASE)
    unfenced = re.sub(r"\s*```$", "", unfenced)
    return unfenced.strip()


def _optional_model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def research_model(settings: Settings) -> str:
    for attr in ("research_model", "editor_model", "matcher_llm_model"):
        model = _optional_model_name(getattr(settings, attr, None))
        if model:
            return model
    return "gpt-4.1-mini"


def _poh_payload(poh: ResearchPoh | None) -> dict[str, str] | None:
    if poh is None:
        return None
    payload: dict[str, str] = {"label": poh.label}
    if poh.id:
        payload["id"] = poh.id
    if poh.time_range:
        payload["time_range"] = poh.time_range
    return payload


def build_article_user_payload(
    *,
    query: str,
    poh: ResearchPoh | None,
    pages: list[LoadedPage],
) -> dict[str, Any]:
    return {
        "query": query.strip(),
        "poh": _poh_payload(poh),
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


def build_article_user_message(
    *,
    query: str,
    poh: ResearchPoh | None,
    pages: list[LoadedPage],
) -> str:
    payload = build_article_user_payload(query=query, poh=poh, pages=pages)
    return json.dumps(payload, ensure_ascii=False)


async def generate_article(
    *,
    query: str,
    pages: list[LoadedPage],
    client: openai.OpenAI,
    settings: Settings,
    poh: ResearchPoh | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> ArticleGenerationResult:
    log_fields = query_log_fields(query)
    if not pages:
        Log(
            INFO_LOG_LEVEL,
            "research article generation skipped: no context pages",
            {
                "request_id": request_id,
                "stage": _STAGE,
                **log_fields,
            },
        )
        return ArticleGenerationResult(
            markdown=build_no_material_article(query),
            skipped_llm=True,
            model=None,
        )

    model = research_model(settings)
    system_prompt = build_system_prompt(load_article_prompt(), prompt_notes)
    user_message = build_article_user_message(query=query, poh=poh, pages=pages)
    Log(
        INFO_LOG_LEVEL,
        "research article generation begin",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "page_count": len(pages),
            "context_chars": sum(len(page.markdown) for page in pages),
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
        "research article generation completed",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "markdown_chars": len(markdown),
            "markdown_preview": safe_text(markdown),
            **log_fields,
        },
    )
    return ArticleGenerationResult(
        markdown=markdown,
        skipped_llm=False,
        model=model,
    )

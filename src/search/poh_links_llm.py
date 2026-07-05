from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ProgressReporter = Callable[[dict[str, Any]], None]

import openai

from src.core.log import INFO_LOG_LEVEL, Log, safe_text
from src.core.openai_client import (
    build_system_prompt,
    chat_completion_with_retry,
    run_in_client_thread_pool,
)
from src.core.openai_client_sync import embedding_with_retry_sync
from src.models.polyindex_index import PolyindexIndexDocument
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import get_subject_embedding, set_subject_embedding
from src.search.article_llm import (
    is_no_material_article,
    query_log_fields,
    research_model,
    strip_article_markdown_fences,
)
from src.search.request_schema import ResearchPoh
from src.search.subject_lookup import _cosine_similarity

_STAGE = "research_poh_links"
_STAGE_EMBEDDING = "research_poh_links_embedding"
_MAX_COMPLETION_TOKENS = 8192
_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 100
_TOP_K_PER_CHUNK = 10
POH_LINK_CHUNK_STEPS = 1 + _TOP_K_PER_CHUNK
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "poh_links_prompt.md"
_WORD_CHAR = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True)
class PohCandidate:
    poh_id: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleParagraph:
    index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class PohLinkTask:
    poh_id: str
    label: str
    aliases: tuple[str, ...]
    paragraph_index: int
    first_offset: int
    similarity: float


@dataclass(frozen=True)
class PohLinksResult:
    markdown: str
    skipped_llm: bool
    model: str | None = None


def load_poh_links_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def poh_link_phase_total(chunk_count: int, link_task_count: int) -> int:
    return max(1, chunk_count * POH_LINK_CHUNK_STEPS + link_task_count)


def _trim_partial_word_start(text: str, start: int, end: int) -> int:
    if start <= 0 or start >= end:
        return start
    if _WORD_CHAR.match(text[start]) and _WORD_CHAR.match(text[start - 1]):
        while start < end and _WORD_CHAR.match(text[start]):
            start += 1
        while start < end and not _WORD_CHAR.match(text[start]):
            start += 1
    return start


def _trim_partial_word_end(text: str, start: int, end: int) -> int:
    if end >= len(text) or start >= end:
        return end
    if _WORD_CHAR.match(text[end - 1]) and _WORD_CHAR.match(text[end]):
        while end > start and _WORD_CHAR.match(text[end - 1]):
            end -= 1
    return end


def chunk_article_text(
    text: str,
    *,
    size: int = _CHUNK_SIZE,
    overlap: int = _CHUNK_OVERLAP,
) -> list[tuple[int, int, str]]:
    if not text.strip():
        return []
    step = max(1, size - overlap)
    chunks: list[tuple[int, int, str]] = []
    pos = 0
    while pos < len(text):
        raw_end = min(pos + size, len(text))
        start = _trim_partial_word_start(text, pos, raw_end)
        end = _trim_partial_word_end(text, start, raw_end)
        if start < end and text[start:end].strip():
            chunks.append((start, end, text[start:end]))
        if raw_end >= len(text):
            break
        pos += step
    return chunks


def split_article_paragraphs(text: str) -> list[ArticleParagraph]:
    if not text:
        return []
    paragraphs: list[ArticleParagraph] = []
    pattern = re.compile(r"\n\n+")
    cursor = 0
    para_index = 0
    for match in pattern.finditer(text):
        block_end = match.start()
        if block_end > cursor:
            paragraphs.append(
                ArticleParagraph(
                    index=para_index,
                    start=cursor,
                    end=block_end,
                    text=text[cursor:block_end],
                )
            )
            para_index += 1
        cursor = match.end()
    if cursor < len(text):
        paragraphs.append(
            ArticleParagraph(
                index=para_index,
                start=cursor,
                end=len(text),
                text=text[cursor:],
            )
        )
    return paragraphs


def paragraph_for_offset(
    paragraphs: list[ArticleParagraph],
    offset: int,
) -> ArticleParagraph | None:
    for paragraph in paragraphs:
        if paragraph.start <= offset < paragraph.end:
            return paragraph
    if paragraphs and offset >= paragraphs[-1].start:
        return paragraphs[-1]
    return None


def dedupe_vector_hits(
    hits: list[tuple[str, int, float]],
) -> list[tuple[str, int, float]]:
    ordered = sorted(hits, key=lambda item: (item[1], -item[2], item[0]))
    seen: set[str] = set()
    deduped: list[tuple[str, int, float]] = []
    for poh_id, offset, similarity in ordered:
        if poh_id in seen:
            continue
        seen.add(poh_id)
        deduped.append((poh_id, offset, similarity))
    return deduped


def apply_paragraph_updates(
    article: str,
    paragraphs: list[ArticleParagraph],
    updates: dict[int, str],
) -> str:
    if not updates:
        return article
    pieces: list[str] = []
    cursor = 0
    for paragraph in paragraphs:
        pieces.append(article[cursor:paragraph.start])
        pieces.append(updates.get(paragraph.index, paragraph.text))
        cursor = paragraph.end
    pieces.append(article[cursor:])
    return "".join(pieces)


def _primary_poh_payload(poh: ResearchPoh | None) -> dict[str, str] | None:
    if poh is None:
        return None
    payload: dict[str, str] = {"label": poh.label}
    if poh.id:
        payload["id"] = poh.id
    if poh.time_range:
        payload["time_range"] = poh.time_range
    return payload


def _subject_embedding(
    client: openai.OpenAI,
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
        client,
        model=model,
        text=label,
        request_id=request_id,
        stage=_STAGE_EMBEDDING,
    )
    set_subject_embedding(sqlite_path, canonical_id, label, vector, model)
    return vector


def _load_poh_embedding_index(
    document: PolyindexIndexDocument,
    client: openai.OpenAI,
    settings: Settings,
    sqlite_path: str,
    *,
    request_id: str,
) -> list[tuple[str, list[float]]]:
    model = settings.matcher_embedding_model
    indexed: list[tuple[str, list[float]]] = []
    for canonical_id in sorted(document.subjects):
        entry = document.subjects[canonical_id]
        vector = _subject_embedding(
            client,
            sqlite_path,
            model,
            canonical_id,
            entry.canonical_label,
            request_id=request_id,
        )
        indexed.append((canonical_id, vector))
    return indexed


def _top_poh_matches(
    chunk_vector: list[float],
    embedding_index: list[tuple[str, list[float]]],
    *,
    top_k: int,
) -> list[tuple[str, float]]:
    scored = [
        (poh_id, _cosine_similarity(chunk_vector, vector))
        for poh_id, vector in embedding_index
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:top_k]


def build_poh_link_tasks(
    *,
    document: PolyindexIndexDocument,
    article_markdown: str,
    hits: list[tuple[str, int, float]],
) -> list[PohLinkTask]:
    paragraphs = split_article_paragraphs(article_markdown)
    tasks: list[PohLinkTask] = []
    for poh_id, offset, similarity in dedupe_vector_hits(hits):
        entry = document.subjects.get(poh_id)
        paragraph = paragraph_for_offset(paragraphs, offset)
        if entry is None or paragraph is None:
            continue
        tasks.append(
            PohLinkTask(
                poh_id=poh_id,
                label=entry.canonical_label,
                aliases=tuple(entry.aliases),
                paragraph_index=paragraph.index,
                first_offset=offset,
                similarity=similarity,
            )
        )
    tasks.sort(key=lambda item: (item.paragraph_index, item.first_offset, item.poh_id))
    return tasks


def build_poh_links_paragraph_payload(
    *,
    query: str,
    subject: PohLinkTask,
    paragraph_markdown: str,
    poh: ResearchPoh | None,
    is_lead_paragraph: bool,
) -> dict[str, Any]:
    return {
        "query": query.strip(),
        "primary_poh": _primary_poh_payload(poh),
        "subject": {
            "id": subject.poh_id,
            "label": subject.label,
            "aliases": list(subject.aliases),
        },
        "paragraph_markdown": paragraph_markdown,
        "is_lead_paragraph": is_lead_paragraph,
    }


async def discover_poh_link_tasks(
    *,
    article_markdown: str,
    document: PolyindexIndexDocument,
    client: openai.OpenAI,
    settings: Settings,
    sqlite_path: str,
    request_id: str = "",
    reporter: ProgressReporter | None = None,
) -> list[PohLinkTask]:
    chunks = chunk_article_text(article_markdown)
    if not chunks or not document.subjects:
        return []

    embedding_index = await run_in_client_thread_pool(
        client,
        _load_poh_embedding_index,
        document,
        client,
        settings,
        sqlite_path,
        request_id=request_id,
    )
    model = settings.matcher_embedding_model
    sem = asyncio.Semaphore(settings.max_parallel_request)
    hits: list[tuple[str, int, float]] = []

    async def _process_chunk(
        start: int,
        _end: int,
        chunk_text: str,
    ) -> list[tuple[str, int, float]]:
        async with sem:
            chunk_vector = await run_in_client_thread_pool(
                client,
                embedding_with_retry_sync,
                client,
                model=model,
                text=chunk_text,
                request_id=request_id,
                stage=_STAGE_EMBEDDING,
            )
        if reporter is not None:
            reporter(
                {
                    "phase": "research_poh_links",
                    "status": "progress",
                    "counts_as_step": True,
                    "poh_step": "chunk_embedding",
                }
            )
        top_matches = _top_poh_matches(
            chunk_vector,
            embedding_index,
            top_k=_TOP_K_PER_CHUNK,
        )
        for _poh_id, _similarity in top_matches:
            if reporter is not None:
                reporter(
                    {
                        "phase": "research_poh_links",
                        "status": "progress",
                        "counts_as_step": True,
                        "poh_step": "chunk_hit",
                    }
                )
        return [(poh_id, start, similarity) for poh_id, similarity in top_matches]

    for chunk_hits in await asyncio.gather(
        *[_process_chunk(start, end, text) for start, end, text in chunks]
    ):
        hits.extend(chunk_hits)

    tasks = build_poh_link_tasks(
        document=document,
        article_markdown=article_markdown,
        hits=hits,
    )
    Log(
        INFO_LOG_LEVEL,
        "research poh links discovery completed",
        {
            "request_id": request_id,
            "chunk_count": len(chunks),
            "max_parallel": settings.max_parallel_request,
            "raw_hits": len(hits),
            "task_count": len(tasks),
        },
    )
    return tasks


async def _link_subject_in_paragraph(
    *,
    query: str,
    task: PohLinkTask,
    paragraph_markdown: str,
    poh: ResearchPoh | None,
    is_lead_paragraph: bool,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str,
    prompt_notes: str | None,
    model: str,
    reporter: ProgressReporter | None = None,
) -> str:
    system_prompt = build_system_prompt(load_poh_links_prompt(), prompt_notes)
    user_message = json.dumps(
        build_poh_links_paragraph_payload(
            query=query,
            subject=task,
            paragraph_markdown=paragraph_markdown,
            poh=poh,
            is_lead_paragraph=is_lead_paragraph,
        ),
        ensure_ascii=False,
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
        page=task.paragraph_index,
        reasoning_effort=settings.reasoning_effort_research,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_research,
    )
    linked = strip_article_markdown_fences(content)
    if reporter is not None:
        reporter(
            {
                "phase": "research_poh_links",
                "status": "progress",
                "counts_as_step": True,
                "poh_step": "link_apply",
            }
        )
    return linked


def _lead_paragraph_index(paragraphs: list[ArticleParagraph]) -> int | None:
    for paragraph in paragraphs:
        stripped = paragraph.text.lstrip()
        if stripped.startswith("# "):
            continue
        if stripped:
            return paragraph.index
    return None


def group_link_tasks_by_paragraph(
    tasks: list[PohLinkTask],
) -> dict[int, list[PohLinkTask]]:
    grouped: dict[int, list[PohLinkTask]] = {}
    for task in tasks:
        grouped.setdefault(task.paragraph_index, []).append(task)
    return grouped


async def _link_subjects_in_paragraph(
    *,
    query: str,
    paragraph_tasks: list[PohLinkTask],
    initial_paragraph: str,
    paragraph_index: int,
    lead_index: int | None,
    poh: ResearchPoh | None,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str,
    prompt_notes: str | None,
    model: str,
    reporter: ProgressReporter | None = None,
) -> tuple[int, str]:
    current = initial_paragraph
    is_lead = lead_index is not None and paragraph_index == lead_index
    for task in paragraph_tasks:
        current = await _link_subject_in_paragraph(
            query=query,
            task=task,
            paragraph_markdown=current,
            poh=poh,
            is_lead_paragraph=is_lead,
            client=client,
            settings=settings,
            request_id=request_id,
            prompt_notes=prompt_notes,
            model=model,
            reporter=reporter,
        )
    return paragraph_index, current


async def _apply_link_tasks_by_paragraph(
    *,
    tasks: list[PohLinkTask],
    paragraph_text: dict[int, str],
    query: str,
    lead_index: int | None,
    poh: ResearchPoh | None,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str,
    prompt_notes: str | None,
    model: str,
    reporter: ProgressReporter | None = None,
) -> dict[int, str]:
    grouped = group_link_tasks_by_paragraph(tasks)
    sem = asyncio.Semaphore(settings.max_parallel_request)

    async def _process_paragraph(
        paragraph_index: int,
        paragraph_tasks: list[PohLinkTask],
    ) -> tuple[int, str]:
        async with sem:
            return await _link_subjects_in_paragraph(
                query=query,
                paragraph_tasks=paragraph_tasks,
                initial_paragraph=paragraph_text.get(paragraph_index, ""),
                paragraph_index=paragraph_index,
                lead_index=lead_index,
                poh=poh,
                client=client,
                settings=settings,
                request_id=request_id,
                prompt_notes=prompt_notes,
                model=model,
                reporter=reporter,
            )

    results = await asyncio.gather(
        *[
            _process_paragraph(paragraph_index, paragraph_tasks)
            for paragraph_index, paragraph_tasks in sorted(grouped.items())
        ]
    )
    updated = dict(paragraph_text)
    for paragraph_index, text in results:
        updated[paragraph_index] = text
    return updated


async def add_poh_links(
    *,
    query: str,
    article_markdown: str,
    document: PolyindexIndexDocument,
    client: openai.OpenAI,
    settings: Settings,
    link_tasks: list[PohLinkTask] | None = None,
    poh: ResearchPoh | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
    sqlite_path: str = "",
    reporter: ProgressReporter | None = None,
    poh_phase_total: int | None = None,
) -> PohLinksResult:
    log_fields = query_log_fields(query, poh)
    subject = log_fields["research_subject"]
    if is_no_material_article(article_markdown):
        Log(
            INFO_LOG_LEVEL,
            f"research poh links skipped (no material): {subject}",
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

    tasks = link_tasks
    if tasks is None:
        tasks = await discover_poh_link_tasks(
            article_markdown=article_markdown,
            document=document,
            client=client,
            settings=settings,
            sqlite_path=sqlite_path,
            request_id=request_id,
            reporter=reporter,
        )

    if not tasks:
        Log(
            INFO_LOG_LEVEL,
            f"research poh links skipped (no candidates): {subject}",
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

    paragraphs = split_article_paragraphs(article_markdown)
    lead_index = _lead_paragraph_index(paragraphs)
    paragraph_text = {paragraph.index: paragraph.text for paragraph in paragraphs}
    model = research_model(settings)

    Log(
        INFO_LOG_LEVEL,
        f"research poh links begin: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "task_count": len(tasks),
            "paragraph_count": len(paragraphs),
            "max_parallel": settings.max_parallel_request,
            **log_fields,
        },
    )

    paragraph_text = await _apply_link_tasks_by_paragraph(
        tasks=tasks,
        paragraph_text=paragraph_text,
        query=query,
        lead_index=lead_index,
        poh=poh,
        client=client,
        settings=settings,
        request_id=request_id,
        prompt_notes=prompt_notes,
        model=model,
        reporter=reporter,
    )

    markdown = apply_paragraph_updates(article_markdown, paragraphs, paragraph_text)
    Log(
        INFO_LOG_LEVEL,
        f"research poh links completed: {subject}",
        {
            "request_id": request_id,
            "stage": _STAGE,
            "model": model,
            "task_count": len(tasks),
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

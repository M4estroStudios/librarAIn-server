from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.markdown_artifacts import (
    clean_markdown_channel_artifacts,
    strip_lmstudio_channel_artifacts,
)
from src.ingestion.output_writer import _atomic_write_bytes
from src.ingestion.polyindex.index_md_parser import sort_index_md_body
from src.ingestion.pipeline.md_cache import (
    read_stage_md as _read_stage_md,
    write_stage_md as _write_stage_md,
)
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parent / "pipeline" / "prompts"
TOC_REFINE_PROMPT_FILE = _PROMPTS_DIR / "toc_aggregate_refine_prompt.md"
INDEX_REFINE_PROMPT_FILE = _PROMPTS_DIR / "index_aggregate_refine_prompt.md"
_SECTION_SEPARATOR = "\n\n---\n\n"
_MAX_COMPLETION_TOKENS = 8192


class AggregateKind(str, Enum):
    TOC = "toc"
    INDEX = "index"


def _load_prompt(kind: AggregateKind) -> str:
    path = TOC_REFINE_PROMPT_FILE if kind is AggregateKind.TOC else INDEX_REFINE_PROMPT_FILE
    return path.read_text(encoding="utf-8").strip()


def _header_marker(kind: AggregateKind) -> str:
    return "# TOC — " if kind is AggregateKind.TOC else "# INDEX — "


def _split_header_and_body(text: str, kind: AggregateKind) -> tuple[str, str]:
    marker = _header_marker(kind)
    if not text.startswith(marker):
        return "", text
    newline = text.find("\n")
    if newline < 0:
        return text, ""
    first_line = text[:newline]
    rest = text[newline + 1 :]
    if rest.startswith("\n"):
        return first_line + "\n\n", rest.lstrip("\n")
    return first_line + "\n", rest.lstrip("\n")


def _strip_obvious_artifacts(text: str) -> str:
    return strip_lmstudio_channel_artifacts(text)


def _body_has_refineable_content(body: str) -> bool:
    stripped = _strip_obvious_artifacts(body).strip()
    if not stripped:
        return False
    for line in stripped.splitlines():
        if line.strip():
            return True
    return False


def _cache_path(cache_dir: Path, kind: AggregateKind, section_index: int) -> Path:
    return cache_dir / f"{kind.value}.section.{section_index:04d}.md"


async def _refine_section(
    client: openai.OpenAI,
    *,
    model: str,
    kind: AggregateKind,
    section_text: str,
    request_id: str,
    section_index: int,
    settings: Settings,
    prompt_notes: str | None = None,
) -> str:
    system_text = build_system_prompt(_load_prompt(kind), prompt_notes)
    cleaned_input = _strip_obvious_artifacts(section_text)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": cleaned_input},
    ]
    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage=f"toc_index_refine_{kind.value}",
        page=section_index,
        reasoning_effort=settings.reasoning_effort_editor,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_editor,
    )
    return content.strip()


async def refine_aggregate_markdown_file(
    md_path: Path,
    kind: AggregateKind,
    client: openai.OpenAI,
    settings: Settings,
    *,
    source_sha256: str,
    request_id: str = "",
    cache_dir: Path | None = None,
    force_recompute: bool = False,
    prompt_notes: str | None = None,
    stats: dict[str, int] | None = None,
) -> Path:
    raw = md_path.read_text(encoding="utf-8")
    header, body = _split_header_and_body(raw, kind)
    if not _body_has_refineable_content(body):
        Log(
            INFO_LOG_LEVEL,
            "toc_index_refine skip empty body",
            {"path": str(md_path), "kind": kind.value, "request_id": request_id},
        )
        return md_path

    model = settings.editor_model or ""
    if not model:
        raise ValueError("EDITOR_MODEL is required for toc/index refine")

    work_cache = cache_dir
    if work_cache is None:
        work_cache = Path(settings.data_root) / "tmp" / source_sha256 / "stage4TocIndexRefine"
    work_cache.mkdir(parents=True, exist_ok=True)

    sections = body.split(_SECTION_SEPARATOR) if body.strip() else []
    if not sections:
        sections = [body]

    sem = asyncio.Semaphore(settings.max_parallel_request)

    async def _process_section(section_index: int, section_text: str) -> str:
        cache_file = _cache_path(work_cache, kind, section_index)
        if not force_recompute:
            cached = _read_stage_md(cache_file, model)
            if cached is not None:
                Log(
                    INFO_LOG_LEVEL,
                    "toc_index_refine section cache hit",
                    {
                        "kind": kind.value,
                        "section_index": section_index,
                        "request_id": request_id,
                    },
                )
                return cached

        async with sem:
            try:
                refined = await _refine_section(
                    client,
                    model=model,
                    kind=kind,
                    section_text=section_text,
                    request_id=request_id,
                    section_index=section_index,
                    settings=settings,
                    prompt_notes=prompt_notes,
                )
            except Exception as exc:
                Log(
                    WARNING_LOG_LEVEL,
                    "toc_index_refine section failed using input",
                    {
                        "kind": kind.value,
                        "section_index": section_index,
                        "request_id": request_id,
                        "error": str(exc),
                    },
                )
                if stats is not None:
                    stats["fallback_sections"] = stats.get("fallback_sections", 0) + 1
                return _strip_obvious_artifacts(section_text)

        _write_stage_md(cache_file, model, refined)
        return refined

    refined_sections = await asyncio.gather(
        *(_process_section(index, section) for index, section in enumerate(sections))
    )
    new_body = clean_markdown_channel_artifacts(
        _SECTION_SEPARATOR.join(refined_sections)
    )
    if kind is AggregateKind.INDEX:
        new_body = sort_index_md_body(new_body)
    if header:
        output = f"{header}{new_body}"
    else:
        output = new_body
    if not output.endswith("\n"):
        output += "\n"
    _atomic_write_bytes(md_path, output.encode("utf-8"))
    Log(
        INFO_LOG_LEVEL,
        "toc_index_refine completed",
        {
            "path": str(md_path),
            "kind": kind.value,
            "sections": len(sections),
            "request_id": request_id,
        },
    )
    return md_path


async def refine_toc_md(
    toc_md_path: Path,
    client: openai.OpenAI,
    settings: Settings,
    *,
    source_sha256: str,
    request_id: str = "",
    cache_dir: Path | None = None,
    force_recompute: bool = False,
    prompt_notes: str | None = None,
    stats: dict[str, int] | None = None,
) -> Path:
    return await refine_aggregate_markdown_file(
        toc_md_path,
        AggregateKind.TOC,
        client,
        settings,
        source_sha256=source_sha256,
        request_id=request_id,
        cache_dir=cache_dir,
        force_recompute=force_recompute,
        prompt_notes=prompt_notes,
        stats=stats,
    )


async def refine_index_md(
    index_md_path: Path,
    client: openai.OpenAI,
    settings: Settings,
    *,
    source_sha256: str,
    request_id: str = "",
    cache_dir: Path | None = None,
    force_recompute: bool = False,
    prompt_notes: str | None = None,
    stats: dict[str, int] | None = None,
) -> Path:
    return await refine_aggregate_markdown_file(
        index_md_path,
        AggregateKind.INDEX,
        client,
        settings,
        source_sha256=source_sha256,
        request_id=request_id,
        cache_dir=cache_dir,
        force_recompute=force_recompute,
        prompt_notes=prompt_notes,
        stats=stats,
    )


def sorted_index_md_text(raw: str) -> str:
    header, body = _split_header_and_body(raw, AggregateKind.INDEX)
    sorted_body = sort_index_md_body(body)
    if header:
        output = f"{header}{sorted_body}"
    else:
        output = sorted_body
    if not output.endswith("\n"):
        output += "\n"
    return output


def sort_index_md_file(index_md_path: Path) -> bool:
    raw = index_md_path.read_text(encoding="utf-8")
    output = sorted_index_md_text(raw)
    if output == raw:
        return False
    _atomic_write_bytes(index_md_path, output.encode("utf-8"))
    return True

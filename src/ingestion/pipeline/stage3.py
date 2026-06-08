from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.pipeline.stage2 import (
    Stage2Result,
    _read_stage_md,
    _write_stage_md,
)
from src.ingestion.progress import (
    PHASE_STAGE3_EDITOR,
    STATUS_COMPLETED,
    STATUS_PAGE_FAILED,
    STATUS_PAGE_PROGRESS,
    STATUS_PAGE_SKIPPED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.ingestion.markdown_artifacts import clean_markdown_channel_artifacts
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
EDITOR_PROMPT_FILE = _PROMPTS_DIR / "editor_prompt.md"
_MAX_COMPLETION_TOKENS = 4096


def _load_editor_prompt() -> str:
    return EDITOR_PROMPT_FILE.read_text(encoding="utf-8").strip()


def _stage2_body(s2_page: Stage2PageResult, vision_model: str) -> str:
    cached = _read_stage_md(Path(s2_page.md_path), vision_model)
    if cached is not None:
        return cached
    return Path(s2_page.md_path).read_text(encoding="utf-8")


async def refine_with_editor(
    client: openai.OpenAI,
    *,
    model: str,
    stage2_md: str,
    request_id: str,
    page: int,
    settings: Settings,
    temperature: float = 0.1,
    prompt_notes: str | None = None,
) -> str:
    Log(INFO_LOG_LEVEL, "stage3 refine_with_editor load prompt file begin", {"request_id": request_id})
    system_text = build_system_prompt(_load_editor_prompt(), prompt_notes)
    Log(
        INFO_LOG_LEVEL,
        "stage3 refine_with_editor load prompt file done",
        {"request_id": request_id, "chars": len(system_text)},
    )
    Log(INFO_LOG_LEVEL, "stage3 refine_with_editor build messages begin", {"request_id": request_id, "page": page})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": stage2_md},
    ]
    Log(INFO_LOG_LEVEL, "stage3 refine_with_editor build messages done", {"request_id": request_id, "page": page})
    Log(
        INFO_LOG_LEVEL,
        "stage3 refine_with_editor chat completion begin",
        {"page": page, "model": model, "request_id": request_id},
    )
    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="stage3_editor",
        page=page,
        reasoning_effort=settings.reasoning_effort_editor,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_editor,
    )
    Log(
        INFO_LOG_LEVEL,
        "stage3 refine_with_editor chat completion done",
        {"request_id": request_id, "page": page, "char_count": len(content)},
    )
    return content


class Stage3PageResult(BaseModel):
    aligned_page: int
    original_page: int
    md_path: str
    char_count: int
    stage2_char_count: int
    char_delta: int


class Stage3Result(BaseModel):
    pages: list[Stage3PageResult]
    skipped_existing: int
    missing: list[int]
    last_error: str | None = None


async def run_stage3_editor(
    stage2_result: Stage2Result,
    source_sha256: str,
    settings: Settings,
    client: openai.OpenAI,
    *,
    request_id: str = "",
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
    prompt_notes: str | None = None,
) -> Stage3Result:
    data_root = Path(settings.data_root)
    stage3_dir = data_root / "tmp" / source_sha256 / "stage3Editor"
    stage3_dir.mkdir(parents=True, exist_ok=True)

    Log(
        INFO_LOG_LEVEL,
        "stage3 working dirs ready",
        {"request_id": request_id, "stage3_dir": str(stage3_dir)},
    )

    model: str = settings.editor_model or ""
    page_total = len(stage2_result.pages)
    sem = asyncio.Semaphore(settings.max_parallel_request)
    last_error: str | None = None

    Log(
        INFO_LOG_LEVEL,
        "stage3 editor starting",
        {
            "request_id": request_id,
            "pages_from_stage2": page_total,
            "model": model,
            "max_parallel": settings.max_parallel_request,
        },
    )

    if progress is not None:
        progress(make_event(PHASE_STAGE3_EDITOR, STATUS_STARTED, page_total=page_total))

    async def _process_page(page_index: int, s2_page: Stage2PageResult) -> tuple[Stage3PageResult | None, bool]:
        nonlocal last_error
        async with sem:
            Log(
                INFO_LOG_LEVEL,
                "stage3 page iteration begin",
                {
                    "request_id": request_id,
                    "aligned_page": s2_page.aligned_page,
                    "original_page": s2_page.original_page,
                },
            )
            stem = Path(s2_page.md_path).stem
            md_path = stage3_dir / f"{stem}.md"

            if not force_recompute:
                cached = _read_stage_md(md_path, model)
                if cached is not None:
                    stage2_char_count = len(_stage2_body(s2_page, settings.vision_model or ""))
                    char_delta = len(cached) - stage2_char_count
                    Log(
                        INFO_LOG_LEVEL,
                        "stage3 page skip editor using existing md",
                        {
                            "request_id": request_id,
                            "aligned_page": s2_page.aligned_page,
                            "original_page": s2_page.original_page,
                            "md_path": str(md_path),
                            "char_count": len(cached),
                        },
                    )
                    if progress is not None:
                        progress(make_event(
                            PHASE_STAGE3_EDITOR,
                            STATUS_PAGE_SKIPPED,
                            counts_as_step=True,
                            page_index=page_index,
                            page_total=page_total,
                            aligned_page=s2_page.aligned_page,
                            original_page=s2_page.original_page,
                            char_count=len(cached),
                        ))
                    return (
                        Stage3PageResult(
                            aligned_page=s2_page.aligned_page,
                            original_page=s2_page.original_page,
                            md_path=str(md_path),
                            char_count=len(cached),
                            stage2_char_count=stage2_char_count,
                            char_delta=char_delta,
                        ),
                        True,
                    )

            stage2_md = _stage2_body(s2_page, settings.vision_model or "")
            stage2_char_count = len(stage2_md)

            try:
                refined = await refine_with_editor(
                    client,
                    model=model,
                    stage2_md=stage2_md,
                    request_id=request_id,
                    page=s2_page.aligned_page,
                    settings=settings,
                    prompt_notes=prompt_notes,
                )
            except Exception as exc:
                last_error = str(exc)
                Log(
                    WARNING_LOG_LEVEL,
                    "stage3 editor page failed",
                    {
                        "request_id": request_id,
                        "aligned_page": s2_page.aligned_page,
                        "original_page": s2_page.original_page,
                        "error": str(exc),
                    },
                )
                if progress is not None:
                    progress(make_event(
                        PHASE_STAGE3_EDITOR,
                        STATUS_PAGE_FAILED,
                        counts_as_step=True,
                        page_index=page_index,
                        page_total=page_total,
                        aligned_page=s2_page.aligned_page,
                        original_page=s2_page.original_page,
                        error=str(exc),
                    ))
                return None, False

            stage3_char_count = len(refined)
            char_delta = stage3_char_count - stage2_char_count
            _write_stage_md(md_path, model, clean_markdown_channel_artifacts(refined))
            if progress is not None:
                progress(make_event(
                    PHASE_STAGE3_EDITOR,
                    STATUS_PAGE_PROGRESS,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=s2_page.aligned_page,
                    original_page=s2_page.original_page,
                    char_count=stage3_char_count,
                ))
            return (
                Stage3PageResult(
                    aligned_page=s2_page.aligned_page,
                    original_page=s2_page.original_page,
                    md_path=str(md_path),
                    char_count=stage3_char_count,
                    stage2_char_count=stage2_char_count,
                    char_delta=char_delta,
                ),
                False,
            )

    outcomes = await asyncio.gather(
        *(
            _process_page(page_index, s2_page)
            for page_index, s2_page in enumerate(stage2_result.pages, start=1)
        )
    )

    pages: list[Stage3PageResult] = []
    skipped_existing = 0
    for result, skipped in outcomes:
        if result is not None:
            pages.append(result)
            if skipped:
                skipped_existing += 1

    pages.sort(key=lambda p: p.aligned_page)

    Log(
        INFO_LOG_LEVEL,
        "stage3 editor finished",
        {
            "request_id": request_id,
            "pages_written": len(pages),
            "skipped_existing": skipped_existing,
        },
    )

    if progress is not None:
        progress(make_event(
            PHASE_STAGE3_EDITOR,
            STATUS_COMPLETED,
            pages_written=len(pages),
            skipped_existing=skipped_existing,
        ))

    return Stage3Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=[],
        last_error=last_error,
    )

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result
from src.ingestion.progress import (
    PHASE_STAGE2_VISION,
    STATUS_COMPLETED,
    STATUS_PAGE_FAILED,
    STATUS_PAGE_PROGRESS,
    STATUS_PAGE_SKIPPED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.ingestion.pipeline.md_cache import read_stage_md, write_stage_md
from src.models.settings import Settings
from src.ingestion.markdown_artifacts import finalize_vision_page_output

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
VISION_PROMPT_FILE = _PROMPTS_DIR / "vision_prompt.md"
_MAX_COMPLETION_TOKENS = 4096

# Backwards-compatible aliases; new code should import from md_cache.
_read_stage_md = read_stage_md
_write_stage_md = write_stage_md


def _load_vision_prompt() -> str:
    return VISION_PROMPT_FILE.read_text(encoding="utf-8").strip()


class Stage2PageResult(BaseModel):
    aligned_page: int
    original_page: int
    md_path: str
    char_count: int


class Stage2Result(BaseModel):
    pages: list[Stage2PageResult]
    skipped_existing: int
    missing: list[int]
    last_error: str | None = None


async def refine_with_vision(
    client: openai.OpenAI,
    *,
    model: str,
    page_image_path: Path,
    raw_ocr_text: str,
    request_id: str,
    page: int,
    settings: Settings,
    temperature: float = 0.1,
    prompt_notes: str | None = None,
) -> str:
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision load prompt file begin", {"request_id": request_id})
    system_text = build_system_prompt(_load_vision_prompt(), prompt_notes)
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision load prompt file done",
        {"request_id": request_id, "chars": len(system_text)},
    )
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision read image begin",
        {"request_id": request_id, "path": str(page_image_path)},
    )
    image_bytes = Path(page_image_path).read_bytes()
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision read image done",
        {"request_id": request_id, "bytes": len(image_bytes)},
    )
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision encode image base64 begin", {"request_id": request_id})
    b64 = base64.b64encode(image_bytes).decode("ascii")
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision encode image base64 done",
        {"request_id": request_id, "b64_chars": len(b64)},
    )
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision build messages begin", {"request_id": request_id, "page": page})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": raw_ocr_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        },
    ]
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision build messages done", {"request_id": request_id, "page": page})
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision chat completion begin",
        {"page": page, "model": model, "request_id": request_id},
    )
    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="stage2_vision",
        page=page,
        reasoning_effort=settings.reasoning_effort_vision,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_vision,
    )
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision chat completion done",
        {"request_id": request_id, "page": page, "char_count": len(content)},
    )
    return content


async def run_stage2_vision(
    stage1_result: Stage1Result,
    source_sha256: str,
    settings: Settings,
    client: openai.OpenAI,
    *,
    request_id: str = "",
    force_recompute: bool = False,
    progress: ProgressReporter | None = None,
    prompt_notes: str | None = None,
) -> Stage2Result:
    data_root = Path(settings.data_root)
    stage2_dir = data_root / "tmp" / source_sha256 / "stage2Vision"
    render_dir = data_root / "tmp" / source_sha256 / "render"
    stage2_dir.mkdir(parents=True, exist_ok=True)

    Log(
        INFO_LOG_LEVEL,
        "stage2 working dirs ready",
        {"request_id": request_id, "stage2_dir": str(stage2_dir), "render_dir": str(render_dir)},
    )

    model: str = settings.vision_model or ""
    page_total = len(stage1_result.pages)
    sem = asyncio.Semaphore(settings.max_parallel_request)
    last_error: str | None = None

    Log(
        INFO_LOG_LEVEL,
        "stage2 vision starting",
        {"request_id": request_id, "pages_from_stage1": page_total, "model": model, "max_parallel": settings.max_parallel_request},
    )

    if progress is not None:
        progress(make_event(PHASE_STAGE2_VISION, STATUS_STARTED, page_total=page_total))

    async def _process_page(page_index: int, s1_page: Stage1PageResult) -> tuple[Stage2PageResult | None, bool]:
        nonlocal last_error
        async with sem:
            Log(
                INFO_LOG_LEVEL,
                "stage2 page iteration begin",
                {
                    "request_id": request_id,
                    "aligned_page": s1_page.aligned_page,
                    "original_page": s1_page.original_page,
                },
            )
            stem = Path(s1_page.txt_path).stem
            md_path = stage2_dir / f"{stem}.md"

            if not force_recompute:
                cached = _read_stage_md(md_path, model)
                if cached is not None:
                    Log(
                        INFO_LOG_LEVEL,
                        "stage2 page skip vision using existing md",
                        {
                            "request_id": request_id,
                            "aligned_page": s1_page.aligned_page,
                            "original_page": s1_page.original_page,
                            "md_path": str(md_path),
                            "char_count": len(cached),
                        },
                    )
                    if progress is not None:
                        progress(make_event(
                            PHASE_STAGE2_VISION,
                            STATUS_PAGE_SKIPPED,
                            counts_as_step=True,
                            page_index=page_index,
                            page_total=page_total,
                            aligned_page=s1_page.aligned_page,
                            original_page=s1_page.original_page,
                            char_count=len(cached),
                        ))
                    return (
                        Stage2PageResult(
                            aligned_page=s1_page.aligned_page,
                            original_page=s1_page.original_page,
                            md_path=str(md_path),
                            char_count=len(cached),
                        ),
                        True,
                    )

            png_path = render_dir / f"p.{s1_page.aligned_page:04d}.png"
            raw_ocr_text = Path(s1_page.txt_path).read_text(encoding="utf-8")

            try:
                refined = await refine_with_vision(
                    client,
                    model=model,
                    page_image_path=png_path,
                    raw_ocr_text=raw_ocr_text,
                    request_id=request_id,
                    page=s1_page.aligned_page,
                    settings=settings,
                    prompt_notes=prompt_notes,
                )
            except Exception as exc:
                last_error = str(exc)
                Log(
                    WARNING_LOG_LEVEL,
                    "stage2 vision page failed",
                    {
                        "request_id": request_id,
                        "aligned_page": s1_page.aligned_page,
                        "original_page": s1_page.original_page,
                        "error": str(exc),
                    },
                )
                if progress is not None:
                    progress(make_event(
                        PHASE_STAGE2_VISION,
                        STATUS_PAGE_FAILED,
                        counts_as_step=True,
                        page_index=page_index,
                        page_total=page_total,
                        aligned_page=s1_page.aligned_page,
                        original_page=s1_page.original_page,
                        error=str(exc),
                    ))
                return None, False

            finalized = finalize_vision_page_output(refined, prompt_notes)
            _write_stage_md(md_path, model, finalized)
            if progress is not None:
                progress(make_event(
                    PHASE_STAGE2_VISION,
                    STATUS_PAGE_PROGRESS,
                    counts_as_step=True,
                    page_index=page_index,
                    page_total=page_total,
                    aligned_page=s1_page.aligned_page,
                    original_page=s1_page.original_page,
                    char_count=len(finalized),
                ))
            return (
                Stage2PageResult(
                    aligned_page=s1_page.aligned_page,
                    original_page=s1_page.original_page,
                    md_path=str(md_path),
                    char_count=len(finalized),
                ),
                False,
            )

    outcomes = await asyncio.gather(
        *(
            _process_page(page_index, s1_page)
            for page_index, s1_page in enumerate(stage1_result.pages, start=1)
        )
    )

    pages: list[Stage2PageResult] = []
    skipped_existing = 0
    for result, skipped in outcomes:
        if result is not None:
            pages.append(result)
            if skipped:
                skipped_existing += 1

    pages.sort(key=lambda p: p.aligned_page)

    Log(
        INFO_LOG_LEVEL,
        "stage2 vision finished",
        {
            "request_id": request_id,
            "pages_written": len(pages),
            "skipped_existing": skipped_existing,
        },
    )

    if progress is not None:
        progress(make_event(
            PHASE_STAGE2_VISION,
            STATUS_COMPLETED,
            pages_written=len(pages),
            skipped_existing=skipped_existing,
        ))

    return Stage2Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=[],
        last_error=last_error,
    )

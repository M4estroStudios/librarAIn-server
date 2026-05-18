from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import chat_completion_with_retry
from src.ingestion.pipeline.stage1 import Stage1Result
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
VISION_PROMPT_FILE = _PROMPTS_DIR / "vision_prompt.md"
_MAX_COMPLETION_TOKENS = 4096


def _load_vision_prompt() -> str:
    return VISION_PROMPT_FILE.read_text(encoding="utf-8").strip()


class Stage2PageResult(BaseModel):
    aligned_page: int
    original_page: int
    md_path: str
    sidecar_path: str
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
    temperature: float = 0.1,
) -> str:
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision load prompt file begin")
    system_text = _load_vision_prompt()
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision load prompt file done",
        {"chars": len(system_text)},
    )
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision read image begin",
        {"path": str(page_image_path)},
    )
    image_bytes = Path(page_image_path).read_bytes()
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision read image done",
        {"bytes": len(image_bytes)},
    )
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision encode image base64 begin")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision encode image base64 done",
        {"b64_chars": len(b64)},
    )
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision build messages begin", {"page": page})
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
    Log(INFO_LOG_LEVEL, "stage2 refine_with_vision build messages done", {"page": page})
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
    )
    Log(
        INFO_LOG_LEVEL,
        "stage2 refine_with_vision chat completion done",
        {"page": page, "char_count": len(content)},
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
) -> Stage2Result:
    data_root = Path(settings.data_root)
    stage2_dir = data_root / "tmp" / source_sha256 / "stage2Vision"
    render_dir = data_root / "tmp" / source_sha256 / "render"
    stage2_dir.mkdir(parents=True, exist_ok=True)

    Log(
        INFO_LOG_LEVEL,
        "stage2 working dirs ready",
        {"stage2_dir": str(stage2_dir), "render_dir": str(render_dir)},
    )

    model: str = settings.vision_model or ""

    pages: list[Stage2PageResult] = []
    skipped_existing = 0
    missing: list[int] = []
    last_error: str | None = None

    Log(
        INFO_LOG_LEVEL,
        "stage2 vision starting",
        {"pages_from_stage1": len(stage1_result.pages), "model": model},
    )

    for s1_page in stage1_result.pages:
        Log(
            INFO_LOG_LEVEL,
            "stage2 page iteration begin",
            {
                "aligned_page": s1_page.aligned_page,
                "original_page": s1_page.original_page,
            },
        )
        stem = Path(s1_page.txt_path).stem
        md_path = stage2_dir / f"{stem}.md"
        sidecar_path = stage2_dir / f"{stem}.json"

        if not force_recompute and md_path.is_file() and sidecar_path.is_file():
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                if sidecar.get("model") == model:
                    md_text = md_path.read_text(encoding="utf-8")
                    Log(
                        INFO_LOG_LEVEL,
                        "stage2 page skip vision using existing md",
                        {
                            "aligned_page": s1_page.aligned_page,
                            "original_page": s1_page.original_page,
                            "md_path": str(md_path),
                            "char_count": len(md_text),
                        },
                    )
                    skipped_existing += 1
                    pages.append(
                        Stage2PageResult(
                            aligned_page=s1_page.aligned_page,
                            original_page=s1_page.original_page,
                            md_path=str(md_path),
                            sidecar_path=str(sidecar_path),
                            char_count=len(md_text),
                        )
                    )
                    Log(
                        INFO_LOG_LEVEL,
                        "stage2 page iteration complete",
                        {
                            "aligned_page": s1_page.aligned_page,
                            "original_page": s1_page.original_page,
                            "cache_hit": True,
                        },
                    )
                    continue
            except Exception:
                Log(
                    WARNING_LOG_LEVEL,
                    "stage2 sidecar read failed, will re-run vision",
                    {"sidecar_path": str(sidecar_path)},
                )

        png_path = render_dir / f"p.{s1_page.aligned_page:04d}.png"
        Log(
            INFO_LOG_LEVEL,
            "stage2 page load OCR txt begin",
            {"path": s1_page.txt_path},
        )
        raw_ocr_text = Path(s1_page.txt_path).read_text(encoding="utf-8")
        Log(
            INFO_LOG_LEVEL,
            "stage2 page load OCR txt done",
            {"char_count": len(raw_ocr_text)},
        )

        try:
            refined = await refine_with_vision(
                client,
                model=model,
                page_image_path=png_path,
                raw_ocr_text=raw_ocr_text,
                request_id=request_id,
                page=s1_page.aligned_page,
            )
        except Exception as exc:
            last_error = str(exc)
            Log(
                WARNING_LOG_LEVEL,
                "stage2 vision page failed",
                {
                    "aligned_page": s1_page.aligned_page,
                    "original_page": s1_page.original_page,
                    "error": str(exc),
                },
            )
            Log(
                INFO_LOG_LEVEL,
                "stage2 page iteration end",
                {
                    "aligned_page": s1_page.aligned_page,
                    "original_page": s1_page.original_page,
                    "outcome": "vision_failed",
                },
            )
            continue

        Log(
            INFO_LOG_LEVEL,
            "stage2 page write markdown begin",
            {"path": str(md_path)},
        )
        md_path.write_text(refined, encoding="utf-8")
        Log(
            INFO_LOG_LEVEL,
            "stage2 page write markdown done",
            {"char_count": len(refined)},
        )
        sidecar_data = {
            "model": model,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        Log(
            INFO_LOG_LEVEL,
            "stage2 page write sidecar begin",
            {"path": str(sidecar_path)},
        )
        sidecar_path.write_text(json.dumps(sidecar_data, ensure_ascii=False), encoding="utf-8")
        Log(INFO_LOG_LEVEL, "stage2 page write sidecar done")
        pages.append(
            Stage2PageResult(
                aligned_page=s1_page.aligned_page,
                original_page=s1_page.original_page,
                md_path=str(md_path),
                sidecar_path=str(sidecar_path),
                char_count=len(refined),
            )
        )
        Log(
            INFO_LOG_LEVEL,
            "stage2 page iteration complete",
            {
                "aligned_page": s1_page.aligned_page,
                "original_page": s1_page.original_page,
            },
        )

    Log(
        INFO_LOG_LEVEL,
        "stage2 vision finished",
        {
            "pages_written": len(pages),
            "skipped_existing": skipped_existing,
            "missing": len(missing),
        },
    )

    return Stage2Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=missing,
        last_error=last_error,
    )

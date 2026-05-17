from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel

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
    system_text = _load_vision_prompt()
    image_bytes = Path(page_image_path).read_bytes()
    b64 = base64.b64encode(image_bytes).decode("ascii")
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
    return await chat_completion_with_retry(
        client,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="stage2_vision",
        page=page,
    )


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

    model: str = settings.vision_model or ""

    pages: list[Stage2PageResult] = []
    skipped_existing = 0
    missing: list[int] = []
    last_error: str | None = None

    for s1_page in stage1_result.pages:
        stem = Path(s1_page.txt_path).stem
        md_path = stage2_dir / f"{stem}.md"
        sidecar_path = stage2_dir / f"{stem}.json"

        if not force_recompute and md_path.is_file() and sidecar_path.is_file():
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                if sidecar.get("model") == model:
                    skipped_existing += 1
                    pages.append(
                        Stage2PageResult(
                            aligned_page=s1_page.aligned_page,
                            original_page=s1_page.original_page,
                            md_path=str(md_path),
                            sidecar_path=str(sidecar_path),
                            char_count=len(md_path.read_text(encoding="utf-8")),
                        )
                    )
                    continue
            except Exception:
                pass

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
            )
        except Exception as exc:
            last_error = str(exc)
            continue

        md_path.write_text(refined, encoding="utf-8")
        sidecar_data = {
            "model": model,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        sidecar_path.write_text(json.dumps(sidecar_data, ensure_ascii=False), encoding="utf-8")
        pages.append(
            Stage2PageResult(
                aligned_page=s1_page.aligned_page,
                original_page=s1_page.original_page,
                md_path=str(md_path),
                sidecar_path=str(sidecar_path),
                char_count=len(refined),
            )
        )

    return Stage2Result(
        pages=pages,
        skipped_existing=skipped_existing,
        missing=missing,
        last_error=last_error,
    )

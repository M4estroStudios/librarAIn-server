from __future__ import annotations

import base64
import json
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from src.core.openai_client import build_openai_client, chat_completion_with_retry
from src.ingestion.pipeline.gpu_vram import require_gpu_vram
from src.ingestion.pipeline.render import render_pdf_page_to_png
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "ingestion" / "pipeline" / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "page_guidance_prompt.md"
SAMPLE_PAGE_COUNT = 5
_BOX_COLOR = (220, 60, 60)
_POINT_COLOR = (40, 120, 220)
_TRAIL_COLOR = (40, 170, 110)
_TRAIL_START = (30, 200, 255)
_TRAIL_END = (255, 122, 26)
_LABEL_BG = (20, 20, 20)
_LABEL_FG = (255, 255, 255)


def _load_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8").strip()


def choose_sample_pages(
    page_count: int,
    annotated_pages: list[int],
    *,
    sample_count: int = SAMPLE_PAGE_COUNT,
    rng: random.Random | None = None,
) -> list[int]:
    if page_count < 1:
        return []
    blocked = {p for p in annotated_pages if isinstance(p, int) and 1 <= p <= page_count}
    pool = [p for p in range(1, page_count + 1) if p not in blocked]
    if not pool:
        return []
    take = min(sample_count, len(pool))
    picker = rng if rng is not None else random.Random()
    return sorted(picker.sample(pool, take))


def normalize_annotations(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    pages: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        if not isinstance(page, int) or page < 1:
            continue
        elements_raw = item.get("elements")
        elements: list[dict[str, Any]] = []
        if isinstance(elements_raw, list):
            for el in elements_raw:
                if not isinstance(el, dict):
                    continue
                name = str(el.get("name") or "").strip()
                kind = str(el.get("type") or "").strip().lower()
                if kind not in ("bbox", "point", "trail"):
                    continue
                coords = el.get("coords")
                if not isinstance(coords, list) or not coords:
                    continue
                elements.append(
                    {
                        "id": str(el.get("id") or "").strip() or None,
                        "name": name or kind,
                        "type": kind,
                        "coords": coords,
                    }
                )
        pages.append({"page": page, "elements": elements})
    return pages


def _map_coord(value: Any, size: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(size - 1, int(round(number / 999.0 * (size - 1)))))


def _draw_label(draw: ImageDraw.ImageDraw, x: int, y: int, text: str) -> None:
    label = (text or "").strip() or "?"
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 2
    top = max(0, y - th - pad * 2 - 2)
    left = max(0, x)
    draw.rectangle([left, top, left + tw + pad * 2, top + th + pad * 2], fill=_LABEL_BG)
    draw.text((left + pad, top + pad), label, fill=_LABEL_FG, font=font)


def flatten_annotations_on_image(
    image: Image.Image,
    elements: list[dict[str, Any]],
) -> Image.Image:
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    for el in elements:
        kind = el.get("type")
        name = str(el.get("name") or kind)
        coords = el.get("coords") or []
        if kind == "bbox" and len(coords) >= 4:
            x1 = _map_coord(coords[0], width)
            y1 = _map_coord(coords[1], height)
            x2 = _map_coord(coords[2], width)
            y2 = _map_coord(coords[3], height)
            left, right = sorted((x1, x2))
            top, bottom = sorted((y1, y2))
            draw.rectangle([left, top, right, bottom], outline=_BOX_COLOR, width=3)
            _draw_label(draw, left, top, name)
        elif kind == "point" and len(coords) >= 2:
            x = _map_coord(coords[0], width)
            y = _map_coord(coords[1], height)
            r = max(4, min(width, height) // 120)
            draw.ellipse([x - r, y - r, x + r, y + r], outline=_POINT_COLOR, width=3)
            _draw_label(draw, x + r + 2, y, name)
        elif kind == "trail" and len(coords) >= 2:
            points: list[tuple[int, int]] = []
            for pair in coords:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                points.append((_map_coord(pair[0], width), _map_coord(pair[1], height)))
            if len(points) >= 2:
                draw.line(points, fill=_TRAIL_COLOR, width=3)
                r = max(5, min(width, height) // 90)
                sx, sy = points[0]
                ex, ey = points[-1]
                draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=_TRAIL_START, outline=(11, 58, 74), width=2)
                draw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=_TRAIL_END, outline=(90, 42, 0), width=2)
                _draw_label(draw, sx, sy, name)
            elif len(points) == 1:
                x, y = points[0]
                draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=_TRAIL_START)
                _draw_label(draw, x + 4, y, name)
    return canvas


def _image_to_data_url(image: Image.Image) -> str:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def build_user_parts(
    *,
    notes: str,
    index_notes: str,
    page_notes: str,
    annotations: list[dict[str, Any]],
    annotated_images: list[tuple[int, Image.Image]],
    original_images: list[tuple[int, Image.Image]],
    sample_images: list[tuple[int, Image.Image]],
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Operator notes (general):\n"
                f"{notes.strip() or '(none)'}\n\n"
                "Operator notes (index formatting):\n"
                f"{index_notes.strip() or '(none)'}\n\n"
                "Operator notes (page formatting):\n"
                f"{page_notes.strip() or '(none)'}\n\n"
                "Annotation metadata (DeepSeek-style coords 0-999):\n"
                f"{json.dumps(annotations, ensure_ascii=False)}"
            ),
        }
    ]
    for page, image in annotated_images:
        parts.append({"type": "text", "text": f"Annotated flattened page {page}:"})
        parts.append({"type": "image_url", "image_url": {"url": _image_to_data_url(image)}})
    for page, image in original_images:
        parts.append({"type": "text", "text": f"Original page {page}:"})
        parts.append({"type": "image_url", "image_url": {"url": _image_to_data_url(image)}})
    for page, image in sample_images:
        parts.append({"type": "text", "text": f"Sample page {page}:"})
        parts.append({"type": "image_url", "image_url": {"url": _image_to_data_url(image)}})
    parts.append(
        {
            "type": "text",
            "text": (
                "Write the system-prompt append now. "
                "It will be injected into the OCR/vision page system prompt for this PDF."
            ),
        }
    )
    return parts


def prepare_guidance_images(
    pdf_path: Path,
    annotations: list[dict[str, Any]],
    sample_pages: list[int],
    *,
    work_dir: Path,
) -> tuple[list[tuple[int, Image.Image]], list[tuple[int, Image.Image]], list[tuple[int, Image.Image]]]:
    by_page = {item["page"]: item.get("elements") or [] for item in annotations}
    annotated_pages = sorted(page for page, els in by_page.items() if els)
    annotated_images: list[tuple[int, Image.Image]] = []
    original_images: list[tuple[int, Image.Image]] = []
    for page in annotated_pages:
        png_path = work_dir / f"annotated_src_{page}.png"
        render_pdf_page_to_png(pdf_path, page - 1, png_path, dpi=120)
        original = Image.open(png_path)
        original.load()
        original_images.append((page, original.copy()))
        flattened = flatten_annotations_on_image(original, by_page.get(page) or [])
        annotated_images.append((page, flattened))
    sample_images: list[tuple[int, Image.Image]] = []
    for page in sample_pages:
        png_path = work_dir / f"sample_{page}.png"
        render_pdf_page_to_png(pdf_path, page - 1, png_path, dpi=120)
        image = Image.open(png_path)
        image.load()
        sample_images.append((page, image.copy()))
    return annotated_images, original_images, sample_images


async def suggest_page_guidance_async(
    pdf_path: Path,
    settings: Settings,
    *,
    notes: str = "",
    index_notes: str = "",
    page_notes: str = "",
    annotations: list[dict[str, Any]] | None = None,
    sample_pages: list[int] | None = None,
    page_count: int | None = None,
) -> dict[str, Any]:
    import tempfile

    from src.api.reicat_vision_suggest import count_pdf_pages

    model = (settings.vision_model or "").strip()
    if not model:
        raise ValueError("VISION_MODEL must be configured")

    annotations = normalize_annotations(annotations or [])
    annotated_page_nums = [item["page"] for item in annotations if item.get("elements")]
    total_pages = page_count if isinstance(page_count, int) and page_count > 0 else count_pdf_pages(pdf_path)
    resolved_samples = list(sample_pages or [])
    if not resolved_samples:
        resolved_samples = choose_sample_pages(total_pages, annotated_page_nums)

    if not annotated_page_nums and not resolved_samples:
        raise ValueError("no pages available for page guidance suggestion")

    require_gpu_vram(settings, "llm")
    with tempfile.TemporaryDirectory(prefix="page_guidance_") as tmp:
        annotated_images, original_images, sample_images = prepare_guidance_images(
            pdf_path,
            annotations,
            resolved_samples,
            work_dir=Path(tmp),
        )
        client = build_openai_client(settings)
        content = await chat_completion_with_retry(
            client,
            model=model,
            messages=[
                {"role": "system", "content": _load_prompt()},
                {
                    "role": "user",
                    "content": build_user_parts(
                        notes=notes,
                        index_notes=index_notes,
                        page_notes=page_notes,
                        annotations=annotations,
                        annotated_images=annotated_images,
                        original_images=original_images,
                        sample_images=sample_images,
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=2048,
            request_id="page-guidance-suggest",
            stage="page_guidance_vision",
            page=0,
            reasoning_effort=settings.reasoning_effort_vision,
            reasoning_enable_thinking=settings.reasoning_enable_thinking_vision,
        )
    guidance = (content or "").strip()
    if guidance.startswith("```"):
        lines = guidance.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        guidance = "\n".join(lines).strip()
    return {
        "guidance": guidance,
        "sample_pages": resolved_samples,
        "annotated_pages": annotated_page_nums,
    }


def suggest_page_guidance(pdf_path: Path, settings: Settings, **kwargs: Any) -> dict[str, Any]:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(suggest_page_guidance_async(pdf_path, settings, **kwargs))
    raise RuntimeError("suggest_page_guidance cannot be called from a running event loop")

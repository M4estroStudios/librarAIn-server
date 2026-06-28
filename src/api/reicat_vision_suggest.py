from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import openai
from PIL import Image, ImageDraw

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client, chat_completion_with_retry
from src.ingestion.pipeline.gpu_vram import require_gpu_vram
from src.ingestion.pipeline.render import render_pdf_page_to_png
from src.models.settings import Settings

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "ingestion" / "pipeline" / "prompts"
_REICAT_PROMPT_FILE = _PROMPTS_DIR / "reicat_vision_prompt.md"

REICAT_LEAD_PAGES = 15
REICAT_TAIL_PAGES = 10
REICAT_COLLAGE_DPI = 120
REICAT_COLLAGE_COLS = 3
REICAT_COLLAGE_CELL_WIDTH = 320
_REICAT_LIST_FIELDS = ("autore", "curatore", "traduttore")
_REICAT_STRING_FIELDS = (
    "titolo",
    "sottotitolo",
    "complementi_del_titolo",
    "numero_edizione",
    "tipo_di_pubblicazione",
    "luogo_di_pubblicazione",
    "editore",
    "titolo_collana",
    "numero_nella_collana",
    "isbn",
)
_REICAT_INT_FIELDS = ("anno_di_pubblicazione", "numero_pagine")


def _load_pdfium() -> Any:
    import pypdfium2 as pdfium  # noqa: PLC0415

    return pdfium


def count_pdf_pages(pdf_path: Path) -> int:
    pdfium = _load_pdfium()
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def reicat_page_sets(
    page_count: int,
    *,
    lead: int = REICAT_LEAD_PAGES,
    tail: int = REICAT_TAIL_PAGES,
) -> tuple[list[int], list[int]]:
    if page_count < 1:
        return [], []
    lead_indices = list(range(min(lead, page_count)))
    if page_count <= lead:
        return lead_indices, []
    tail_start = max(page_count - tail, 0)
    return lead_indices, list(range(tail_start, page_count))


def default_reicat_page_indices(page_count: int) -> list[int]:
    lead_indices, tail_indices = reicat_page_sets(page_count)
    merged = list(dict.fromkeys(lead_indices + tail_indices))
    return merged


def resolve_reicat_page_indices(
    page_count: int,
    pages_one_based: list[int] | None,
) -> list[int]:
    if page_count < 1:
        return []
    if pages_one_based:
        seen: set[int] = set()
        resolved: list[int] = []
        for page in pages_one_based:
            if page < 1 or page > page_count or page in seen:
                continue
            seen.add(page)
            resolved.append(page - 1)
        if resolved:
            return resolved
    return default_reicat_page_indices(page_count)


def split_reicat_collage_groups(indices_zero: list[int]) -> tuple[list[int], list[int]]:
    unique = sorted(set(indices_zero))
    if len(unique) <= 18:
        return unique, []
    mid = (len(unique) + 1) // 2
    return unique[:mid], unique[mid:]


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("model response does not contain a JSON object")
    return json.loads(stripped[start : end + 1])


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"\d+", text.replace(".", ""))
    if not match:
        return None
    parsed = int(match.group(0))
    return parsed if parsed > 0 else None


def normalize_reicat_suggestion(raw: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in _REICAT_STRING_FIELDS:
        normalized[key] = _clean_optional_str(raw.get(key))
    for key in _REICAT_LIST_FIELDS:
        normalized[key] = _clean_str_list(raw.get(key))
    for key in _REICAT_INT_FIELDS:
        normalized[key] = _clean_optional_int(raw.get(key))
    return normalized


def _tile_page_image(page_img: Image.Image, *, cell_width: int, page_label: str) -> Image.Image:
    scale = cell_width / max(page_img.width, 1)
    resized = page_img.resize(
        (cell_width, max(1, int(page_img.height * scale))),
        Image.Resampling.LANCZOS,
    )
    label_h = 24
    tile = Image.new("RGB", (cell_width, resized.height + label_h), (36, 36, 36))
    tile.paste(resized, (0, label_h))
    draw = ImageDraw.Draw(tile)
    draw.text((6, 4), page_label, fill=(232, 232, 232))
    return tile


def build_pages_collage(
    pdf_path: Path,
    page_indices_zero: list[int],
    *,
    work_dir: Path,
    dpi: int = REICAT_COLLAGE_DPI,
    cols: int = REICAT_COLLAGE_COLS,
    cell_width: int = REICAT_COLLAGE_CELL_WIDTH,
) -> Image.Image | None:
    if not page_indices_zero:
        return None
    tiles: list[Image.Image] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    for index in page_indices_zero:
        png_path = work_dir / f"page_{index:04d}.png"
        render_pdf_page_to_png(pdf_path, index, png_path, dpi=dpi)
        with Image.open(png_path) as page_img:
            tile = _tile_page_image(
                page_img.convert("RGB"),
                cell_width=cell_width,
                page_label=f"pag. {index + 1}",
            )
        tiles.append(tile)
    gap = 6
    row_count = (len(tiles) + cols - 1) // cols
    row_heights = [
        max(tile.height for tile in tiles[row * cols : row * cols + cols])
        for row in range(row_count)
    ]
    width = cols * cell_width + max(0, cols - 1) * gap
    height = sum(row_heights) + max(0, row_count - 1) * gap
    collage = Image.new("RGB", (width, height), (24, 24, 24))
    y = 0
    for row in range(row_count):
        x = 0
        row_h = row_heights[row]
        for col in range(cols):
            tile_index = row * cols + col
            if tile_index >= len(tiles):
                break
            tile = tiles[tile_index]
            collage.paste(tile, (x, y + row_h - tile.height))
            x += cell_width + gap
        y += row_h + gap
    return collage


def _image_to_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _load_reicat_prompt() -> str:
    return _REICAT_PROMPT_FILE.read_text(encoding="utf-8").strip()


async def _suggest_with_vision(
    client: openai.OpenAI,
    *,
    model: str,
    settings: Settings,
    lead_image: Image.Image | None,
    tail_image: Image.Image | None,
    lead_pages: list[int],
    tail_pages: list[int],
) -> dict[str, Any]:
    if lead_image is None and tail_image is None:
        raise ValueError("no pages available for REICAT suggestion")

    selected_pages = sorted(set(lead_pages + tail_pages))
    user_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Le immagini seguono sono collage delle pagine selezionate per i metadati REICAT. "
                f"Pagine da analizzare tutte (1-based): {', '.join(str(p + 1) for p in selected_pages) or 'nessuna'}. "
                "Per ogni campo REICAT cerca in TUTTE queste pagine, non solo nella prima."
            ),
        }
    ]
    if lead_image is not None:
        label = "Collage pagine:" if tail_image is None else "Collage pagine (gruppo 1):"
        user_parts.append({"type": "text", "text": label})
        user_parts.append({"type": "image_url", "image_url": {"url": _image_to_data_url(lead_image)}})
    if tail_image is not None:
        user_parts.append({"type": "text", "text": "Collage pagine (gruppo 2):"})
        user_parts.append({"type": "image_url", "image_url": {"url": _image_to_data_url(tail_image)}})

    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": _load_reicat_prompt()},
            {"role": "user", "content": user_parts},
        ],
        temperature=0.1,
        max_tokens=2048,
        request_id="reicat-suggest",
        stage="reicat_vision",
        page=0,
        reasoning_effort=settings.reasoning_effort_vision,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_vision,
    )
    return normalize_reicat_suggestion(_extract_json_object(content))


async def suggest_reicat_metadata_async(
    pdf_path: Path,
    settings: Settings,
    *,
    pages_one_based: list[int] | None = None,
) -> dict[str, Any]:
    model = (settings.vision_model or "").strip()
    if not model:
        raise ValueError("VISION_MODEL must be configured")

    require_gpu_vram(settings, "llm")
    page_count = count_pdf_pages(pdf_path)
    selected_indices = resolve_reicat_page_indices(page_count, pages_one_based)
    if not selected_indices:
        raise ValueError("no pages selected for REICAT suggestion")

    group_a, group_b = split_reicat_collage_groups(selected_indices)

    with tempfile.TemporaryDirectory(prefix="reicat_collage_") as tmp:
        work_dir = Path(tmp)
        lead_image = build_pages_collage(pdf_path, group_a, work_dir=work_dir / "a")
        tail_image = build_pages_collage(pdf_path, group_b, work_dir=work_dir / "b") if group_b else None
        client = build_openai_client(settings)
        reicat = await _suggest_with_vision(
            client,
            model=model,
            settings=settings,
            lead_image=lead_image,
            tail_image=tail_image,
            lead_pages=group_a,
            tail_pages=group_b,
        )

    Log(
        INFO_LOG_LEVEL,
        "reicat vision suggestion completed",
        {
            "page_count": page_count,
            "reicat_pages": len(selected_indices),
            "titolo": bool(reicat.get("titolo")),
            "autore_count": len(reicat.get("autore") or []),
        },
    )
    return {
        "reicat": reicat,
        "page_count": page_count,
        "reicat_pages": [index + 1 for index in selected_indices],
        "lead_pages": [index + 1 for index in group_a],
        "tail_pages": [index + 1 for index in group_b],
    }


def suggest_reicat_metadata(
    pdf_path: Path,
    settings: Settings,
    *,
    pages_one_based: list[int] | None = None,
) -> dict[str, Any]:
    try:
        return asyncio.run(
            suggest_reicat_metadata_async(
                pdf_path,
                settings,
                pages_one_based=pages_one_based,
            )
        )
    except Exception as exc:
        Log(ERROR_LOG_LEVEL, "reicat vision suggestion failed", {"error": str(exc)})
        raise

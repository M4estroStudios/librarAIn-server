from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

from src.api.page_guidance_suggest import flatten_annotations_on_image, normalize_annotations
from src.core.hashing import validate_source_sha256
from src.core.openai_client import build_openai_client, chat_completion_with_retry
from src.ingestion.polyindex.page_index_refresh import refresh_polyindex_for_pages
from src.models.settings import Settings
from src.persistence.book_page_preview import (
    PagePreviewError,
    confirm_page_transcript,
    ensure_page_render_png,
    load_page_transcript,
    resolve_aligned_page_from_original,
)
from src.persistence.polyindex_conflicts import add_conflict_item

_PAGE_MENTION_RE = re.compile(r"@p\.(\d+)", re.IGNORECASE)
_APPLY_SYSTEM = (
    "You revise one book page for Biblioteca apply-changes.\n"
    "Return ONLY JSON: "
    '{"transcript":"<full page markdown or null if unchanged>",'
    '"general_note_append":"<short reusable rule for later pages or empty string>"}.\n'
    "Use general notes, page notes, and images when provided. "
    "If before/after transcripts are both present, prefer preserving operator intent "
    "unless notes explicitly require a different correction."
)


class BiblioApplyError(ValueError):
    pass


def extract_mentioned_pages(text: str) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for match in _PAGE_MENTION_RE.finditer(text or ""):
        page = int(match.group(1))
        if page < 1 or page in seen:
            continue
        seen.add(page)
        ordered.append(page)
    return ordered


def validate_apply_notes(pipeline_notes: str, pages: list[dict[str, Any]]) -> None:
    general = (pipeline_notes or "").strip()
    if general:
        return
    if not pages:
        raise BiblioApplyError("no pages to apply")
    missing = [
        int(page.get("original_page") or 0)
        for page in pages
        if not str(page.get("page_notes") or "").strip()
    ]
    missing = [page for page in missing if page > 0]
    if missing:
        raise BiblioApplyError(
            "note generali assenti: ogni pagina in pillola deve avere note pagina "
            f"(mancano: {', '.join('p.' + str(p) for p in missing)})"
        )


def needs_ai_processing(pipeline_notes: str, pages: list[dict[str, Any]]) -> bool:
    if (pipeline_notes or "").strip():
        return True
    for page in pages:
        if str(page.get("page_notes") or "").strip():
            return True
        if page.get("needs_vision"):
            return True
        annotations = page.get("annotations") or []
        if isinstance(annotations, list) and annotations:
            return True
        text_ann = page.get("text_annotations") or []
        if isinstance(text_ann, list) and text_ann:
            return True
    return False


def order_apply_pages(
    pages: list[dict[str, Any]],
    pipeline_notes: str,
) -> list[dict[str, Any]]:
    by_original = {
        int(page["original_page"]): page
        for page in pages
        if isinstance(page.get("original_page"), int) and int(page["original_page"]) > 0
    }
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _add(page_num: int) -> None:
        if page_num in seen or page_num not in by_original:
            return
        page = by_original[page_num]
        blob = " ".join(
            [
                pipeline_notes or "",
                str(page.get("page_notes") or ""),
            ]
        )
        for dep in extract_mentioned_pages(blob):
            if dep != page_num:
                _add(dep)
        seen.add(page_num)
        ordered.append(page)

    for page_num in extract_mentioned_pages(pipeline_notes or ""):
        _add(page_num)
    for page_num in sorted(by_original):
        _add(page_num)
    return ordered


def _png_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _image_to_data_url(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _parse_ai_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"transcript": None, "general_note_append": text}
    if not isinstance(data, dict):
        return {"transcript": None, "general_note_append": ""}
    transcript = data.get("transcript")
    append = data.get("general_note_append")
    return {
        "transcript": transcript if isinstance(transcript, str) else None,
        "general_note_append": append.strip() if isinstance(append, str) else "",
    }


def _build_user_parts(
    *,
    original_page: int,
    general_notes: str,
    page_notes: str,
    transcript_before: str,
    transcript_after: str | None,
    has_manual: bool,
    needs_vision: bool,
    original_image_url: str | None,
    annotated_image_url: str | None,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Page original={original_page}\n"
                f"Needs vision: {bool(needs_vision)}\n"
                f"Has operator manual edit: {bool(has_manual)}\n\n"
                f"## General notes\n{general_notes or '(empty)'}\n\n"
                f"## Page notes\n{page_notes or '(empty)'}\n"
            ),
        }
    ]
    if needs_vision and original_image_url:
        parts.append({"type": "text", "text": "Original page image:"})
        parts.append({"type": "image_url", "image_url": {"url": original_image_url}})
    if needs_vision and annotated_image_url:
        parts.append({"type": "text", "text": "Annotated page image:"})
        parts.append({"type": "image_url", "image_url": {"url": annotated_image_url}})
    if has_manual and transcript_after is not None:
        parts.append(
            {
                "type": "text",
                "text": (
                    "## Transcript before (disk/baseline)\n"
                    f"{transcript_before or '(empty)'}\n\n"
                    "## Transcript after (operator edit)\n"
                    f"{transcript_after or '(empty)'}\n"
                ),
            }
        )
    else:
        parts.append(
            {
                "type": "text",
                "text": f"## Page transcript\n{transcript_before or '(empty)'}\n",
            }
        )
    return parts


async def _process_one_page_async(
    data_root: Path,
    settings: Settings,
    *,
    source_sha256: str,
    page: dict[str, Any],
    general_notes: str,
    client,
) -> dict[str, Any]:
    from PIL import Image

    original_page = int(page["original_page"])
    aligned_page = int(page["aligned_page"])
    needs_vision = bool(page.get("needs_vision"))
    page_notes = str(page.get("page_notes") or "")
    has_manual = bool(page.get("has_manual_transcript"))
    transcript_after = page.get("transcript_after")
    if isinstance(transcript_after, str):
        manual_text = transcript_after
    else:
        manual_text = None
    try:
        baseline, _, _ = load_page_transcript(data_root, source_sha256, aligned_page)
    except PagePreviewError:
        baseline = str(page.get("transcript_before") or "")
    transcript_before = str(page.get("transcript_before") or baseline)

    original_url = None
    annotated_url = None
    if needs_vision:
        png_path = ensure_page_render_png(data_root, source_sha256, aligned_page)
        original_url = _png_data_url(png_path)
        elements = []
        for item in normalize_annotations(page.get("annotation_pages") or []):
            if int(item.get("page") or 0) == original_page:
                elements = item.get("elements") or []
                break
        if not elements and isinstance(page.get("annotations"), list):
            elements = [
                el
                for el in page["annotations"]
                if isinstance(el, dict)
                and str(el.get("type") or "") in ("bbox", "point", "trail")
            ]
        if elements:
            with Image.open(png_path) as image:
                image.load()
                annotated = flatten_annotations_on_image(image, elements)
            annotated_url = _image_to_data_url(annotated)

    model = (settings.vision_model if needs_vision else settings.editor_model) or ""
    if not model.strip():
        raise BiblioApplyError(
            "VISION_MODEL required for vision pages"
            if needs_vision
            else "EDITOR_MODEL required for text apply"
        )
    content = await chat_completion_with_retry(
        client,
        model=model.strip(),
        messages=[
            {"role": "system", "content": _APPLY_SYSTEM},
            {
                "role": "user",
                "content": _build_user_parts(
                    original_page=original_page,
                    general_notes=general_notes,
                    page_notes=page_notes,
                    transcript_before=transcript_before,
                    transcript_after=manual_text,
                    has_manual=has_manual and manual_text is not None,
                    needs_vision=needs_vision,
                    original_image_url=original_url,
                    annotated_image_url=annotated_url,
                ),
            },
        ],
        temperature=0.1,
        max_tokens=4096,
        request_id=f"biblio-apply-{source_sha256[:12]}",
        stage="biblio_apply",
        page=aligned_page,
        reasoning_effort=settings.reasoning_effort_vision if needs_vision else settings.reasoning_effort_editor,
        reasoning_enable_thinking=(
            settings.reasoning_enable_thinking_vision
            if needs_vision
            else settings.reasoning_enable_thinking_editor
        ),
    )
    parsed = _parse_ai_payload(content or "")
    ai_transcript = parsed.get("transcript")
    append = str(parsed.get("general_note_append") or "").strip()
    conflict = None
    final_text = transcript_before
    if has_manual and manual_text is not None:
        final_text = manual_text
        if isinstance(ai_transcript, str) and ai_transcript.strip() and ai_transcript.strip() != manual_text.strip():
            conflict = add_conflict_item(
                data_root,
                source_sha256=source_sha256,
                aligned_page=aligned_page,
                original_page=original_page,
                manual_text=manual_text,
                ai_text=ai_transcript,
            )
    elif isinstance(ai_transcript, str) and ai_transcript.strip():
        final_text = ai_transcript
    return {
        "original_page": original_page,
        "aligned_page": aligned_page,
        "final_text": final_text,
        "general_note_append": append,
        "conflict": conflict,
        "ai_processed": True,
    }


def _run_coro(coro):
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def run_biblio_apply(
    data_root: Path,
    settings: Settings,
    *,
    source_sha256: str,
    pipeline_notes: str,
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    sha = validate_source_sha256(source_sha256)
    if not isinstance(pages, list) or not pages:
        raise BiblioApplyError("pages required")
    manifest_path = Path(data_root) / "output" / sha / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    normalized: list[dict[str, Any]] = []
    for raw in pages:
        if not isinstance(raw, dict):
            continue
        original = raw.get("original_page")
        if not isinstance(original, int) or original < 1:
            continue
        aligned = raw.get("aligned_page")
        if not isinstance(aligned, int) or aligned < 1:
            try:
                aligned = resolve_aligned_page_from_original(manifest, original)
            except PagePreviewError as exc:
                raise BiblioApplyError(str(exc)) from exc
        item = dict(raw)
        item["original_page"] = original
        item["aligned_page"] = aligned
        normalized.append(item)
    if not normalized:
        raise BiblioApplyError("no valid pages")
    validate_apply_notes(pipeline_notes, normalized)
    general_notes = pipeline_notes or ""
    ordered = order_apply_pages(normalized, general_notes)
    ai_results: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    if needs_ai_processing(general_notes, ordered):
        client = build_openai_client(settings)
        for page in ordered:
            result = _run_coro(
                _process_one_page_async(
                    data_root,
                    settings,
                    source_sha256=sha,
                    page=page,
                    general_notes=general_notes,
                    client=client,
                )
            )
            ai_results.append(result)
            if result.get("general_note_append"):
                block = f"\n\n[AI p.{result['original_page']}] {result['general_note_append']}".rstrip()
                general_notes = (general_notes.rstrip() + block).strip() + "\n"
            if result.get("conflict"):
                conflicts.append(result["conflict"])
            confirm_page_transcript(data_root, sha, int(result["aligned_page"]), str(result["final_text"]))
    else:
        for page in ordered:
            text = page.get("transcript_after")
            if not isinstance(text, str):
                text = str(page.get("transcript_before") or "")
            confirm_page_transcript(data_root, sha, int(page["aligned_page"]), text)
            ai_results.append(
                {
                    "original_page": int(page["original_page"]),
                    "aligned_page": int(page["aligned_page"]),
                    "final_text": text,
                    "general_note_append": "",
                    "conflict": None,
                    "ai_processed": False,
                }
            )

    aligned_pages = sorted({int(item["aligned_page"]) for item in ai_results})
    index_result = refresh_polyindex_for_pages(
        data_root,
        settings,
        sha,
        aligned_pages,
        prompt_notes=general_notes.strip() or None,
    )
    return {
        "ok": True,
        "source_sha256": sha,
        "pipeline_notes": general_notes,
        "pages": ai_results,
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "index_refresh": index_result,
    }


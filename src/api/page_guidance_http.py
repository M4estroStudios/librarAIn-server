from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Callable

from src.api.page_guidance_suggest import suggest_page_guidance
from src.core.log import ERROR_LOG_LEVEL, Log
from src.core.openai_client import use_compute_mode
from src.models.settings import Settings, normalize_compute_mode


def ensure_ingest_ai_page_guidance(
    pdf_path: Path,
    settings: Settings,
    ingest_payload: dict[str, Any],
    text_fields: dict[str, str],
) -> None:
    existing = ingest_payload.get("ai_page_guidance")
    if isinstance(existing, str) and existing.strip():
        ingest_payload["ai_page_guidance"] = existing.strip()
        return

    try:
        annotations = json.loads(text_fields.get("annotations_json") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("annotations_json must be valid JSON") from exc
    if not isinstance(annotations, list):
        raise ValueError("annotations_json must be a JSON array")

    result = suggest_page_guidance(
        pdf_path,
        settings,
        notes=(text_fields.get("notes") or "").strip(),
        index_notes=(text_fields.get("index_notes") or "").strip(),
        page_notes=(text_fields.get("page_notes") or "").strip(),
        annotations=annotations,
    )
    guidance = str(result.get("guidance") or "").strip()
    if not guidance:
        raise ValueError("page guidance suggestion returned empty text")
    ingest_payload["ai_page_guidance"] = guidance


def _parse_page_list(raw: str) -> list[int]:
    text = (raw or "").strip()
    if not text:
        return []
    pages: list[int] = []
    seen: set[int] = set()
    for part in text.replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start = int(left.strip())
            end = int(right.strip())
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                if page >= 1 and page not in seen:
                    seen.add(page)
                    pages.append(page)
            continue
        page = int(token)
        if page >= 1 and page not in seen:
            seen.add(page)
            pages.append(page)
    return pages


def try_handle_page_guidance_post(
    path: str,
    handler: Any,
    *,
    data_root: Path,
    settings: Settings,
    send_json: Callable[..., None],
    parse_multipart: Callable[..., Any],
    request_content_length: Callable[[Any], int],
    max_upload: int,
    safe_filename: Callable[[str], str],
) -> bool:
    if path != "/api/ingest/page-guidance-suggest":
        return False

    content_type = handler.headers.get("Content-Type") or ""
    part_path = data_root / "input" / "raw" / f".upload_{secrets.token_hex(8)}.part"
    try:
        content_length = request_content_length(handler)
        parsed = parse_multipart(
            handler.rfile,
            content_type,
            content_length=content_length,
            max_bytes=max_upload,
            pdf_part_path=part_path,
        )
    except (ValueError, OSError) as exc:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": f"multipart form could not be parsed: {exc}"})
        return True

    uploaded = parsed.pdf
    if uploaded is None:
        part_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "pdf_file upload is required"})
        return True
    if uploaded.size == 0:
        uploaded.path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "empty PDF upload"})
        return True
    with uploaded.path.open("rb") as pdf_handle:
        magic = pdf_handle.read(4)
    if magic != b"%PDF":
        uploaded.path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "uploaded file is not a PDF"})
        return True

    saved_path = uploaded.path.with_name(
        f"{secrets.token_hex(6)}_{safe_filename(uploaded.filename or 'upload.pdf')}"
    )
    uploaded.path.rename(saved_path)
    fields = parsed.text_fields

    try:
        compute_mode = normalize_compute_mode(fields.get("compute_mode"))
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": str(exc), "field": "compute_mode"})
        return True

    if compute_mode == "cloud":
        missing_cloud = settings.missing_cloud_config(job_kind="reicat")
        if missing_cloud:
            saved_path.unlink(missing_ok=True)
            send_json(
                handler,
                400,
                {
                    "ok": False,
                    "error": "cloud compute requires: " + ", ".join(missing_cloud),
                    "field": "compute_mode",
                },
            )
            return True

    try:
        annotations = json.loads(fields.get("annotations_json") or "[]")
    except json.JSONDecodeError:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": "annotations_json must be valid JSON"})
        return True

    try:
        sample_pages = _parse_page_list(fields.get("sample_pages") or "")
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": f"invalid sample_pages: {exc}"})
        return True

    try:
        with use_compute_mode(compute_mode, settings):
            job_settings = settings.for_compute_mode(compute_mode)
            result = suggest_page_guidance(
                saved_path,
                job_settings,
                notes=(fields.get("notes") or "").strip(),
                index_notes=(fields.get("index_notes") or "").strip(),
                page_notes=(fields.get("page_notes") or "").strip(),
                annotations=annotations if isinstance(annotations, list) else [],
                sample_pages=sample_pages or None,
            )
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        Log(ERROR_LOG_LEVEL, "page guidance suggest failed", {"error": str(exc)})
        send_json(handler, 500, {"ok": False, "error": str(exc)})
        return True
    finally:
        saved_path.unlink(missing_ok=True)

    send_json(handler, 200, {"ok": True, **result})
    return True

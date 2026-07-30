from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.core.log import INFO_LOG_LEVEL, Log

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_PROMPT_BYTES = 512 * 1024

_PROMPTS: tuple[dict[str, str], ...] = (
    {"id": "glm_ocr", "label": "GLM OCR", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/glm_ocr_prompt.md"},
    {"id": "vision", "label": "Vision", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/vision_prompt.md"},
    {"id": "editor", "label": "Editor", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/editor_prompt.md"},
    {"id": "toc_refine", "label": "TOC", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/toc_aggregate_refine_prompt.md"},
    {"id": "index_refine", "label": "INDEX", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/index_aggregate_refine_prompt.md"},
    {"id": "reicat", "label": "REICAT", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/reicat_vision_prompt.md"},
    {"id": "page_guidance", "label": "Guidance", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/page_guidance_prompt.md"},
    {"id": "biblio_extract", "label": "Biblio", "group": "ingest", "relpath": "src/ingestion/pipeline/prompts/biblio_extract_prompt.md"},
    {"id": "subject_matcher", "label": "Matcher", "group": "polyindex", "relpath": "src/ingestion/polyindex/prompts/subject_matcher_prompt.md"},
    {"id": "time_index", "label": "Time index", "group": "polyindex", "relpath": "src/ingestion/polyindex/prompts/time_index_extract_prompt.md"},
    {"id": "article", "label": "Articolo", "group": "research", "relpath": "src/search/prompts/article_prompt.md"},
    {"id": "article_finalize", "label": "Finalize", "group": "research", "relpath": "src/search/prompts/article_finalize_prompt.md"},
    {"id": "timeline", "label": "Timeline", "group": "research", "relpath": "src/search/prompts/timeline_prompt.md"},
    {"id": "poh_links", "label": "POH links", "group": "research", "relpath": "src/search/prompts/poh_links_prompt.md"},
    {"id": "etaly_metadata", "label": "e-taly meta", "group": "export", "relpath": "src/search/prompts/etaly_metadata_prompt.md"},
    {"id": "timeline_fill", "label": "Timeline fill", "group": "export", "relpath": "src/search/prompts/timeline_fill_prompt.md"},
)

_PROMPT_BY_ID = {item["id"]: item for item in _PROMPTS}

_GROUP_LABELS = {
    "ingest": "Ingest",
    "polyindex": "Polyindex",
    "research": "Research",
    "export": "Export",
}


def list_prompt_catalog() -> list[dict[str, str]]:
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "group": item["group"],
            "group_label": _GROUP_LABELS.get(item["group"], item["group"]),
            "relpath": item["relpath"],
        }
        for item in _PROMPTS
    ]


def resolve_prompt_path(prompt_id: str, repo_root: Path | None = None) -> Path | None:
    meta = _PROMPT_BY_ID.get(str(prompt_id or "").strip())
    if not meta:
        return None
    root = (repo_root or _REPO_ROOT).resolve()
    path = (root / meta["relpath"]).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.suffix.lower() != ".md":
        return None
    return path


def read_prompt(prompt_id: str, repo_root: Path | None = None) -> dict[str, Any]:
    meta = _PROMPT_BY_ID.get(str(prompt_id or "").strip())
    if not meta:
        raise KeyError("unknown prompt id")
    path = resolve_prompt_path(prompt_id, repo_root)
    if path is None:
        raise KeyError("unknown prompt id")
    if not path.is_file():
        raise FileNotFoundError(f"prompt file missing: {meta['relpath']}")
    content = path.read_text(encoding="utf-8")
    return {
        "id": meta["id"],
        "label": meta["label"],
        "group": meta["group"],
        "group_label": _GROUP_LABELS.get(meta["group"], meta["group"]),
        "relpath": meta["relpath"],
        "content": content,
        "mtime": path.stat().st_mtime,
    }


def write_prompt(prompt_id: str, content: str, repo_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_PROMPT_BYTES:
        raise ValueError("content too large")
    meta = _PROMPT_BY_ID.get(str(prompt_id or "").strip())
    if not meta:
        raise KeyError("unknown prompt id")
    path = resolve_prompt_path(prompt_id, repo_root)
    if path is None:
        raise KeyError("unknown prompt id")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"prompt directory missing: {meta['relpath']}")
    path.write_text(content, encoding="utf-8", newline="\n")
    Log(
        INFO_LOG_LEVEL,
        "admin prompt saved",
        {"id": meta["id"], "relpath": meta["relpath"], "bytes": len(encoded)},
    )
    return read_prompt(prompt_id, repo_root)


def try_handle_prompts_get(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    query: dict[str, list[str]] | None = None,
    repo_root: Path | None = None,
    send_json: SendJson,
) -> bool:
    if path != "/api/admin/prompts":
        return False
    params = query or {}
    prompt_id = (params.get("id") or [""])[0].strip()
    root = repo_root or _REPO_ROOT
    if not prompt_id:
        prompts: list[dict[str, Any]] = []
        for item in list_prompt_catalog():
            try:
                prompts.append(read_prompt(item["id"], root))
            except (FileNotFoundError, OSError) as exc:
                prompts.append({**item, "content": "", "error": str(exc)})
        send_json(
            handler,
            200,
            {
                "ok": True,
                "prompts": prompts,
                "groups": [
                    {"id": key, "label": label}
                    for key, label in _GROUP_LABELS.items()
                ],
            },
        )
        return True
    try:
        payload = read_prompt(prompt_id, root)
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "unknown prompt id"})
        return True
    except FileNotFoundError as exc:
        send_json(handler, 404, {"ok": False, "error": str(exc)})
        return True
    except OSError as exc:
        send_json(handler, 500, {"ok": False, "error": str(exc)})
        return True
    send_json(handler, 200, {"ok": True, **payload})
    return True


def try_handle_prompts_post(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    repo_root: Path | None = None,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    if path != "/api/admin/prompts":
        return False
    try:
        raw = read_body(handler, _MAX_PROMPT_BYTES + 4096)
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
        return True
    if not isinstance(body, dict):
        send_json(handler, 400, {"ok": False, "error": "JSON object required"})
        return True
    prompt_id = str(body.get("id") or "").strip()
    content = body.get("content")
    if not prompt_id:
        send_json(handler, 400, {"ok": False, "error": "id is required"})
        return True
    if not isinstance(content, str):
        send_json(handler, 400, {"ok": False, "error": "content must be a string"})
        return True
    try:
        payload = write_prompt(prompt_id, content, repo_root or _REPO_ROOT)
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "unknown prompt id"})
        return True
    except (TypeError, ValueError) as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    except OSError as exc:
        send_json(handler, 500, {"ok": False, "error": str(exc)})
        return True
    send_json(handler, 200, {"ok": True, **payload})
    return True

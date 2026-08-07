from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.core.hashing import validate_source_sha256
from src.core.log import INFO_LOG_LEVEL, Log

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_PROMPT_BYTES = 512 * 1024
_SNAPSHOT_GROUPS = frozenset({"ingest", "polyindex"})

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


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(dest: Path, content: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        tmp_path.replace(dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


def list_snapshot_prompt_catalog() -> list[dict[str, str]]:
    return [item for item in list_prompt_catalog() if item["group"] in _SNAPSHOT_GROUPS]


def snapshot_book_prompts(
    output_dir: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = (repo_root or _REPO_ROOT).resolve()
    prompts_dir = Path(output_dir) / "prompts"
    items: list[dict[str, Any]] = []
    for meta in list_snapshot_prompt_catalog():
        path = resolve_prompt_path(meta["id"], root)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"prompt file missing: {meta['relpath']}")
        content = path.read_text(encoding="utf-8")
        digest = _content_sha256(content)
        rel_file = f"prompts/{meta['id']}.md"
        _atomic_write_text(prompts_dir / f"{meta['id']}.md", content)
        items.append(
            {
                "id": meta["id"],
                "label": meta["label"],
                "group": meta["group"],
                "relpath": meta["relpath"],
                "file": rel_file,
                "sha256": digest,
            }
        )
    return {"captured_at": _utc_now_iso(), "items": items}


def _load_manifest(manifest_path: Path) -> dict[str, Any] | None:
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def doctor_book_prompts(
    data_root: Path,
    source_sha256: str,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    sha = validate_source_sha256(source_sha256)
    root = (repo_root or _REPO_ROOT).resolve()
    output_dir = Path(data_root) / "output" / sha
    manifest = _load_manifest(output_dir / "manifest.json")
    book_title = ""
    if manifest:
        reicat = manifest.get("reicat")
        if isinstance(reicat, dict):
            book_title = str(reicat.get("titolo") or reicat.get("title") or "")
        if not book_title:
            book_title = str(manifest.get("slug") or "")
    prompts_used = manifest.get("prompts_used") if manifest else None
    if not isinstance(prompts_used, dict) or not isinstance(prompts_used.get("items"), list):
        return {
            "source_sha256": sha,
            "book_title": book_title,
            "status": "missing",
            "ok": False,
            "captured_at": None,
            "items": [],
            "summary": {"total": 0, "ok": 0, "drift": 0, "missing": 1, "corrupt": 0},
        }
    items_out: list[dict[str, Any]] = []
    counts = {"total": 0, "ok": 0, "drift": 0, "missing": 0, "corrupt": 0}
    for raw_item in prompts_used.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        prompt_id = str(raw_item.get("id") or "").strip()
        saved_sha = str(raw_item.get("sha256") or "").strip().lower()
        rel_file = str(raw_item.get("file") or f"prompts/{prompt_id}.md").strip()
        counts["total"] += 1
        snap_path = output_dir / rel_file
        current_path = resolve_prompt_path(prompt_id, root) if prompt_id else None
        item: dict[str, Any] = {
            "id": prompt_id,
            "label": str(raw_item.get("label") or prompt_id),
            "group": str(raw_item.get("group") or ""),
            "relpath": str(raw_item.get("relpath") or ""),
            "file": rel_file,
            "saved_sha256": saved_sha,
            "snapshot_sha256": None,
            "current_sha256": None,
            "status": "ok",
        }
        if not snap_path.is_file():
            item["status"] = "missing_snapshot"
        else:
            try:
                snap_sha = _content_sha256(snap_path.read_text(encoding="utf-8"))
            except OSError:
                item["status"] = "corrupt_snapshot"
                counts["corrupt"] += 1
                items_out.append(item)
                continue
            item["snapshot_sha256"] = snap_sha
            if saved_sha and snap_sha != saved_sha:
                item["status"] = "corrupt_snapshot"
                counts["corrupt"] += 1
                items_out.append(item)
                continue
        if current_path is None or not current_path.is_file():
            if item["status"] == "ok":
                item["status"] = "missing_current"
        else:
            try:
                current_sha = _content_sha256(current_path.read_text(encoding="utf-8"))
            except OSError:
                if item["status"] == "ok":
                    item["status"] = "missing_current"
                current_sha = None
            if current_sha is not None:
                item["current_sha256"] = current_sha
                if item["status"] == "ok" and saved_sha and current_sha != saved_sha:
                    item["status"] = "drift"
        if item["status"] == "ok":
            counts["ok"] += 1
        elif item["status"] == "drift":
            counts["drift"] += 1
        elif item["status"] == "corrupt_snapshot":
            counts["corrupt"] += 1
        else:
            counts["missing"] += 1
        items_out.append(item)
    status = "ok"
    if counts["corrupt"]:
        status = "corrupt"
    elif counts["missing"]:
        status = "missing"
    elif counts["drift"]:
        status = "drift"
    return {
        "source_sha256": sha,
        "book_title": book_title,
        "status": status,
        "ok": status == "ok",
        "captured_at": prompts_used.get("captured_at"),
        "items": items_out,
        "summary": counts,
    }


def doctor_all_books_prompts(
    data_root: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    output_root = Path(data_root) / "output"
    books: list[dict[str, Any]] = []
    if output_root.is_dir():
        for child in sorted(output_root.iterdir()):
            if not child.is_dir() or len(child.name) != 64:
                continue
            try:
                books.append(doctor_book_prompts(data_root, child.name, repo_root=repo_root))
            except ValueError:
                continue
    summary = {
        "book_count": len(books),
        "books_ok": sum(1 for book in books if book["status"] == "ok"),
        "books_drift": sum(1 for book in books if book["status"] == "drift"),
        "books_missing": sum(1 for book in books if book["status"] == "missing"),
        "books_corrupt": sum(1 for book in books if book["status"] == "corrupt"),
    }
    return {"books": books, "summary": summary}


def try_handle_prompts_get(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    query: dict[str, list[str]] | None = None,
    repo_root: Path | None = None,
    data_root: Path | None = None,
    send_json: SendJson,
) -> bool:
    params = query or {}
    root = repo_root or _REPO_ROOT
    if path == "/api/admin/prompts/doctor":
        if data_root is None:
            send_json(handler, 500, {"ok": False, "error": "data_root unavailable"})
            return True
        sha_filter = (params.get("source_sha256") or [""])[0].strip().lower()
        try:
            if sha_filter:
                report = doctor_book_prompts(data_root, sha_filter, repo_root=root)
                send_json(handler, 200, {"ok": True, "books": [report], "summary": {
                    "book_count": 1,
                    "books_ok": 1 if report["status"] == "ok" else 0,
                    "books_drift": 1 if report["status"] == "drift" else 0,
                    "books_missing": 1 if report["status"] == "missing" else 0,
                    "books_corrupt": 1 if report["status"] == "corrupt" else 0,
                }})
            else:
                report = doctor_all_books_prompts(data_root, repo_root=root)
                send_json(handler, 200, {"ok": True, **report})
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
        return True
    if path != "/api/admin/prompts":
        return False
    prompt_id = (params.get("id") or [""])[0].strip()
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

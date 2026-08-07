from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.api.research_batch_registry import ResearchBatchRegistry
from src.persistence.book_pages_audit import audit_all_books
from src.persistence.pipeline_runs import list_pipeline_runs
from src.search.article_catalog import research_status_summary

SendJson = Callable[[BaseHTTPRequestHandler, int, dict[str, Any]], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]

STATUS_VALUES = frozenset(
    {"ok", "partial", "untested", "needs_change", "broken", "deprecated"}
)
PRIORITY_VALUES = frozenset({"critical", "high", "medium", "low"})
DEFAULT_STATUS = "untested"
DEFAULT_PRIORITY = ""
_PROJECT_STATUS_FILENAME = "project_status.json"
_STATS_CACHE_FILENAME = "project_status_stats_cache.json"
_FILE_LOCK = threading.Lock()
_STATS_CACHE_LOCK = threading.Lock()
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_stats_memory: dict[str, Any] = {"data_root": None, "payload": None}


def project_status_path(data_root: Path) -> Path:
    return Path(data_root) / _PROJECT_STATUS_FILENAME


def project_status_stats_cache_path(data_root: Path) -> Path:
    return Path(data_root) / _STATS_CACHE_FILENAME


def clear_project_status_stats_cache(data_root: Path | None = None) -> None:
    with _STATS_CACHE_LOCK:
        _stats_memory["data_root"] = None
        _stats_memory["payload"] = None
        if data_root is not None:
            path = project_status_stats_cache_path(data_root)
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass


def slugify_label(label: str) -> str:
    text = _SLUG_RE.sub("_", (label or "").strip().casefold()).strip("_")
    return text or "node"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _unique_sibling_id(base: str, used: set[str]) -> str:
    candidate = base or "node"
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while f"{candidate}_{n}" in used:
        n += 1
    out = f"{candidate}_{n}"
    used.add(out)
    return out


def normalize_node(raw: Any, *, sibling_ids: set[str]) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    label = str(src.get("label") or "").strip() or "Untitled"
    requested = str(src.get("id") or "").strip() or slugify_label(label)
    node_id = _unique_sibling_id(slugify_label(requested) if requested else slugify_label(label), sibling_ids)
    status = str(src.get("status") or DEFAULT_STATUS).strip()
    if status not in STATUS_VALUES:
        status = DEFAULT_STATUS
    priority = str(src.get("priority") or DEFAULT_PRIORITY).strip()
    if priority not in PRIORITY_VALUES:
        priority = DEFAULT_PRIORITY
    try:
        priority_order = int(src.get("priority_order", 0))
    except (TypeError, ValueError):
        priority_order = 0
    notes = src.get("notes")
    notes_s = "" if notes is None else str(notes)
    description = src.get("description")
    description_s = "" if description is None else str(description)
    updated_at = src.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        updated_at = None
    children_raw = src.get("children")
    child_ids: set[str] = set()
    children: list[dict[str, Any]] = []
    if isinstance(children_raw, list):
        for child in children_raw:
            children.append(normalize_node(child, sibling_ids=child_ids))
    known = {
        "id",
        "label",
        "status",
        "priority",
        "priority_order",
        "notes",
        "description",
        "updated_at",
        "children",
    }
    out = {k: v for k, v in src.items() if k not in known}
    out.update(
        {
            "id": node_id,
            "label": label,
            "status": status,
            "priority": priority,
            "priority_order": priority_order,
            "description": description_s,
            "notes": notes_s,
            "updated_at": updated_at,
            "children": children,
        }
    )
    return out


def normalize_tree(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    version = src.get("version", 1)
    try:
        version_i = int(version)
    except (TypeError, ValueError):
        version_i = 1
    roots_raw = src.get("roots")
    if not isinstance(roots_raw, list):
        roots_raw = []
    root_ids: set[str] = set()
    roots = [normalize_node(item, sibling_ids=root_ids) for item in roots_raw]
    known = {"version", "roots", "updated_at"}
    out = {k: v for k, v in src.items() if k not in known}
    out.update(
        {
            "version": version_i,
            "updated_at": src.get("updated_at") if isinstance(src.get("updated_at"), str) else None,
            "roots": roots,
        }
    )
    return out


def default_tree() -> dict[str, Any]:
    return {"version": 1, "updated_at": None, "roots": []}


def load_project_status(data_root: Path) -> dict[str, Any]:
    path = project_status_path(data_root)
    if not path.is_file():
        return default_tree()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_tree()
    return normalize_tree(raw)


def save_project_status(data_root: Path, tree: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_tree(tree)
    normalized["updated_at"] = _utc_now_iso()
    path = project_status_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    with _FILE_LOCK:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    return normalized


def count_statuses(roots: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in sorted(STATUS_VALUES)}
    stack = list(roots)
    while stack:
        node = stack.pop()
        status = str(node.get("status") or DEFAULT_STATUS)
        if status not in counts:
            status = DEFAULT_STATUS
        counts[status] = counts.get(status, 0) + 1
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    return counts


def _json_len(path: Path, *keys: str) -> int | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, dict):
        return len(cur)
    if isinstance(cur, list):
        return len(cur)
    return None


def collect_polyindex_stats(data_root: Path) -> dict[str, Any]:
    poly = Path(data_root) / "polyindex"
    years = _json_len(poly / "TIME_INDEX.json", "years")
    dates = _json_len(poly / "TIME_INDEX.json", "dates")
    time_total = None if years is None and dates is None else (years or 0) + (dates or 0)
    return {
        "index_subjects": _json_len(poly / "INDEX.json", "subjects"),
        "toc_books": _json_len(poly / "TOC.json", "books"),
        "biblio_nodes": _json_len(poly / "BIBLIO.json", "nodes"),
        "time_index_years": years,
        "time_index_dates": dates,
        "time_index_entries": time_total,
        "files": {
            "INDEX.json": (poly / "INDEX.json").is_file(),
            "TOC.json": (poly / "TOC.json").is_file(),
            "BIBLIO.json": (poly / "BIBLIO.json").is_file(),
            "TIME_INDEX.json": (poly / "TIME_INDEX.json").is_file(),
        },
    }


def collect_runtime_stats(
    *,
    settings: Any,
    registry: JobRegistry,
    research_batch_registry: ResearchBatchRegistry,
) -> dict[str, Any]:
    from src.api.system_preflight import _list_lmstudio_models
    from src.ingestion.pipeline.gpu_vram import collect_gpu_vram_snapshots

    snapshots = collect_gpu_vram_snapshots(gpu_device="all")
    models_payload, lm_root = _list_lmstudio_models(settings)
    loaded = [
        m.get("key") or m.get("display_name")
        for m in models_payload
        if m.get("loaded_instances")
    ]
    return {
        "vram": [
            {
                "device_index": s.device_index,
                "used_gb": round(s.used_gb, 2),
                "free_gb": round(s.free_gb, 2),
                "total_gb": round(s.total_gb, 2),
            }
            for s in snapshots
        ],
        "loaded_models": loaded,
        "lmstudio_root": lm_root,
        "active_jobs": registry.running_job_count() + research_batch_registry.running_count(),
    }


def collect_pipeline_run_stats(sqlite_path: str, *, limit: int = 10) -> list[dict[str, Any]]:
    try:
        rows = list_pipeline_runs(sqlite_path, limit=limit)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        timing = row.get("timing")
        out.append(
            {
                "request_id": row.get("request_id"),
                "source_sha256": row.get("source_sha256"),
                "status": row.get("status"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "book_title": row.get("book_title"),
                "page_count": row.get("page_count"),
                "timing": timing if isinstance(timing, dict) else None,
            }
        )
    return out


def build_project_stats(
    data_root: Path,
    tree: dict[str, Any],
    *,
    settings: Any,
    registry: JobRegistry,
    research_batch_registry: ResearchBatchRegistry,
    sqlite_path: str,
) -> dict[str, Any]:
    books_summary: dict[str, Any]
    try:
        books_summary = audit_all_books(data_root).get("summary") or {}
    except Exception:
        books_summary = {}
    try:
        research = research_status_summary(data_root)
    except Exception:
        research = {}
    try:
        runtime = collect_runtime_stats(
            settings=settings,
            registry=registry,
            research_batch_registry=research_batch_registry,
        )
    except Exception:
        runtime = {
            "vram": [],
            "loaded_models": [],
            "lmstudio_root": None,
            "active_jobs": registry.running_job_count()
            + research_batch_registry.running_count(),
        }
    return {
        "books": books_summary,
        "research": research,
        "polyindex": collect_polyindex_stats(data_root),
        "runtime": runtime,
        "pipeline_runs": collect_pipeline_run_stats(sqlite_path, limit=10),
        "checklist": count_statuses(list(tree.get("roots") or [])),
    }


def _read_stats_disk_cache(data_root: Path) -> dict[str, Any] | None:
    path = project_status_stats_cache_path(data_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("stats"), dict):
        return None
    return {
        "stats": raw["stats"],
        "computed_at": raw.get("computed_at"),
        "cached": True,
    }


def _write_stats_disk_cache(data_root: Path, stats: dict[str, Any], computed_at: str) -> None:
    path = project_status_stats_cache_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"computed_at": computed_at, "stats": stats}
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with _FILE_LOCK:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)


def _memory_stats_payload(data_root: Path) -> dict[str, Any] | None:
    root_key = str(Path(data_root).resolve())
    with _STATS_CACHE_LOCK:
        if _stats_memory.get("data_root") != root_key:
            return None
        payload = _stats_memory.get("payload")
        if not isinstance(payload, dict):
            return None
        return dict(payload)


def _store_stats_payload(data_root: Path, payload: dict[str, Any]) -> None:
    root_key = str(Path(data_root).resolve())
    with _STATS_CACHE_LOCK:
        _stats_memory["data_root"] = root_key
        _stats_memory["payload"] = dict(payload)


def _patch_cached_checklist(data_root: Path, tree: dict[str, Any]) -> None:
    checklist = count_statuses(list(tree.get("roots") or []))
    payload = _memory_stats_payload(data_root)
    if payload is None:
        payload = _read_stats_disk_cache(data_root)
    if payload is None or not isinstance(payload.get("stats"), dict):
        return
    stats = dict(payload["stats"])
    stats["checklist"] = checklist
    computed_at = str(payload.get("computed_at") or _utc_now_iso())
    stored = {"stats": stats, "computed_at": computed_at, "cached": True}
    _store_stats_payload(data_root, stored)
    _write_stats_disk_cache(data_root, stats, computed_at)


def get_project_stats(
    data_root: Path,
    *,
    settings: Any,
    registry: JobRegistry,
    research_batch_registry: ResearchBatchRegistry,
    sqlite_path: str,
    refresh: bool = False,
) -> dict[str, Any]:
    if not refresh:
        mem = _memory_stats_payload(data_root)
        if mem is not None:
            out = dict(mem)
            out["cached"] = True
            return out
        disk = _read_stats_disk_cache(data_root)
        if disk is not None:
            _store_stats_payload(data_root, disk)
            return disk
    tree = load_project_status(data_root)
    stats = build_project_stats(
        data_root,
        tree,
        settings=settings,
        registry=registry,
        research_batch_registry=research_batch_registry,
        sqlite_path=sqlite_path,
    )
    computed_at = _utc_now_iso()
    payload = {"stats": stats, "computed_at": computed_at, "cached": False}
    _store_stats_payload(data_root, payload)
    _write_stats_disk_cache(data_root, stats, computed_at)
    return payload


def try_handle_project_status_get(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Any,
    registry: JobRegistry,
    research_batch_registry: ResearchBatchRegistry,
    sqlite_path: str,
    send_json: SendJson,
    query: dict[str, list[str]] | None = None,
) -> bool:
    if path == "/api/admin/project-status":
        tree = load_project_status(data_root)
        send_json(handler, 200, {"ok": True, "tree": tree})
        return True
    if path != "/api/admin/project-status/stats":
        return False
    q = query or {}
    refresh_raw = (q.get("refresh") or ["0"])[0].strip().lower()
    refresh = refresh_raw in {"1", "true", "yes"}
    payload = get_project_stats(
        data_root,
        settings=settings,
        registry=registry,
        research_batch_registry=research_batch_registry,
        sqlite_path=sqlite_path,
        refresh=refresh,
    )
    send_json(
        handler,
        200,
        {
            "ok": True,
            "stats": payload.get("stats") or {},
            "computed_at": payload.get("computed_at"),
            "cached": bool(payload.get("cached")),
        },
    )
    return True


def try_handle_project_status_put(
    path: str,
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Any,
    registry: JobRegistry,
    research_batch_registry: ResearchBatchRegistry,
    sqlite_path: str,
    send_json: SendJson,
    read_body: ReadBody,
) -> bool:
    if path != "/api/admin/project-status":
        return False
    try:
        raw = read_body(handler, 2 * 1024 * 1024)
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        send_json(handler, 400, {"ok": False, "error": f"invalid json: {exc}"})
        return True
    if not isinstance(payload, dict):
        send_json(handler, 400, {"ok": False, "error": "body must be an object"})
        return True
    tree_body = payload.get("tree") if isinstance(payload.get("tree"), dict) else payload
    tree = save_project_status(data_root, tree_body)
    _patch_cached_checklist(data_root, tree)
    send_json(handler, 200, {"ok": True, "tree": tree})
    return True

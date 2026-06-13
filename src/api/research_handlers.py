from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.search.article_catalog import (
    generate_article_for_poh,
    list_ingested_books,
    list_missing_articles,
    research_status_summary,
    resolve_article_file,
    search_articles,
)


SendJson = Callable[[BaseHTTPRequestHandler, int, Any], None]
SendBytes = Callable[[BaseHTTPRequestHandler, int, bytes, str], None]


class ResearchBatchRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, *, total: int) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "total": total,
                "done": 0,
                "generated": [],
                "errors": [],
                "created_at": now,
                "updated_at": now,
            }
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def append_generated(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["generated"].append(item)
            job["done"] = len(job["generated"]) + len(job["errors"])
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def append_error(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["errors"].append(item)
            job["done"] = len(job["generated"]) + len(job["errors"])
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def finish(self, job_id: str, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


def build_research_routes(
    *,
    data_root: Path,
    web_dir: Path,
    batch_registry: ResearchBatchRegistry,
    send_json: SendJson,
    send_bytes: SendBytes,
    read_json_body: Callable[[BaseHTTPRequestHandler, int], bytes],
) -> tuple[
    Callable[[BaseHTTPRequestHandler, str, dict[str, list[str]]], bool],
    Callable[[BaseHTTPRequestHandler, str], bool],
]:
    def try_get(handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> bool:
        require_auth = getattr(handler, "_require_auth", None)
        if require_auth is None:
            return False
        if path in ("/ricerca", "/ricerca.html"):
            page = web_dir / "ricerca.html"
            if not page.is_file():
                send_json(handler, 500, {"ok": False, "error": "web/ricerca.html missing"})
                return True
            send_bytes(handler, 200, page.read_bytes(), "text/html; charset=utf-8")
            return True

        if path.startswith("/articolo/") and path.endswith(".html"):
            article_name = path.removeprefix("/articolo/")
            resolved = resolve_article_file(data_root, article_name)
            if resolved is None:
                handler.send_error(404, "Article Not Found")
                return True
            send_bytes(handler, 200, resolved.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/api/research/books":
            if not require_auth(query):
                return True
            send_json(handler, 200, {"ok": True, "books": list_ingested_books(data_root)})
            return True

        if path == "/api/research/status":
            if not require_auth(query):
                return True
            send_json(handler, 200, {"ok": True, **research_status_summary(data_root)})
            return True

        if path == "/api/research/missing":
            if not require_auth(query):
                return True
            book_sha = query.get("book_sha", [None])[0]
            missing = list_missing_articles(data_root, book_sha=book_sha)
            send_json(handler, 200, {"ok": True, "missing": missing, "count": len(missing)})
            return True

        if path == "/api/research/search":
            if not require_auth(query):
                return True
            q = (query.get("q", [""])[0] or "").strip()
            if len(q) < 2:
                send_json(handler, 400, {"ok": False, "error": "query must be at least 2 characters"})
                return True
            results = search_articles(data_root, q)
            send_json(handler, 200, {"ok": True, "query": q, "results": results, "count": len(results)})
            return True

        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[3] == "generate":
            if parts[4] == "status":
                if not require_auth(query):
                    return True
                job_id = query.get("job_id", [""])[0]
                snapshot = batch_registry.get(job_id)
                if snapshot is None:
                    send_json(handler, 404, {"ok": False, "error": "job not found"})
                    return True
                send_json(handler, 200, {"ok": True, **snapshot})
                return True

        return False

    def try_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
        if path != "/api/research/generate":
            return False
        require_auth = getattr(handler, "_require_auth", None)
        if require_auth is None or not require_auth():
            return require_auth is not None
        try:
            body = read_json_body(handler, 1024 * 1024)
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return True

        book_sha = payload.get("book_sha")
        poh_ids = payload.get("poh_ids")
        if book_sha is not None and not isinstance(book_sha, str):
            send_json(handler, 400, {"ok": False, "error": "book_sha must be a string"})
            return True
        if poh_ids is not None and (
            not isinstance(poh_ids, list) or not all(isinstance(pid, str) for pid in poh_ids)
        ):
            send_json(handler, 400, {"ok": False, "error": "poh_ids must be a list of strings"})
            return True

        if poh_ids:
            targets = [{"poh_id": pid} for pid in poh_ids]
        else:
            missing = list_missing_articles(
                data_root,
                book_sha=book_sha.strip() if isinstance(book_sha, str) and book_sha.strip() else None,
            )
            targets = [{"poh_id": item["poh_id"], "label": item["label"]} for item in missing]

        if not targets:
            send_json(handler, 200, {"ok": True, "job_id": None, "message": "no missing articles"})
            return True

        job_id = batch_registry.create(total=len(targets))

        def _worker() -> None:
            for item in targets:
                poh_id = str(item["poh_id"])
                try:
                    result = generate_article_for_poh(data_root, poh_id)
                    batch_registry.append_generated(job_id, result)
                    Log(INFO_LOG_LEVEL, "research article generated",
                        {"job_id": job_id, "poh_id": poh_id, "url": result["url"]})
                except Exception as exc:
                    batch_registry.append_error(
                        job_id,
                        {"poh_id": poh_id, "error": str(exc)},
                    )
                    Log(ERROR_LOG_LEVEL, "research article generation failed",
                        {"job_id": job_id, "poh_id": poh_id, "error": str(exc)})
            snapshot = batch_registry.get(job_id)
            errors = len(snapshot["errors"]) if snapshot else 0
            status = "failed" if errors == len(targets) else "succeeded"
            batch_registry.finish(job_id, status)

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"research-batch-{job_id[:8]}",
        ).start()

        Log(INFO_LOG_LEVEL, "research batch job started",
            {"job_id": job_id, "total": len(targets)})
        send_json(handler, 202, {
            "ok": True,
            "job_id": job_id,
            "total": len(targets),
            "status_url": f"/api/research/generate/status?job_id={job_id}",
        })
        return True

    return try_get, try_post

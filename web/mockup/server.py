#!/usr/bin/env python3
"""Lightweight mock HTTP server for ingest UI lab (no GPU / pipeline)."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MOCK_SHA = "a" * 64
DEFAULT_PORT = 8766

_sse_jobs: dict[str, list[dict]] = {}
_transcript_overrides: dict[int, str] = {}
_research_catalog: dict | None = None
_research_status: dict | None = None
_research_missing: dict | None = None
_research_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _load_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _page_svg(aligned: int) -> bytes:
    title = f"Pagina mock {aligned}"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="480" height="640" viewBox="0 0 480 640">
  <rect width="480" height="640" fill="#f5f5f0"/>
  <rect x="24" y="24" width="432" height="592" fill="#fff" stroke="#333" stroke-width="2"/>
  <text x="240" y="120" text-anchor="middle" font-family="Georgia, serif" font-size="28" fill="#222">{title}</text>
  <text x="240" y="170" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#666">librarAIn mockup</text>
  <line x1="48" y1="200" x2="432" y2="200" stroke="#ccc"/>
  <text x="48" y="240" font-family="sans-serif" font-size="13" fill="#444">Anteprima statica per review UI.</text>
  <text x="48" y="270" font-family="sans-serif" font-size="13" fill="#444">Nessuna pipeline reale.</text>
</svg>"""
    return svg.encode("utf-8")


def _transcript_for_page(aligned: int) -> tuple[str, str, str]:
    data = _load_json("transcripts.json")
    pages = data.get("pages") if isinstance(data, dict) else {}
    default = data.get("default") if isinstance(data, dict) else {}
    with _lock:
        if aligned in _transcript_overrides:
            text = _transcript_overrides[aligned]
        elif isinstance(pages, dict) and str(aligned) in pages:
            text = str(pages[str(aligned)])
        else:
            text = str(default.get("text", f"Testo mock pagina {aligned}."))
    stage = str(default.get("stage", "stage3Editor"))
    model = str(default.get("producer_model", "mock-model"))
    return text, stage, model


def _register_sse_job(fixture_name: str) -> str:
    events = _load_json(fixture_name)
    if not isinstance(events, list):
        events = []
    job_id = f"mock-{secrets.token_hex(6)}"
    with _lock:
        _sse_jobs[job_id] = events
    return job_id


def _take_sse_job(job_id: str) -> list[dict] | None:
    with _lock:
        return _sse_jobs.pop(job_id, None)


def _ensure_research_catalog() -> dict:
    global _research_catalog
    if _research_catalog is None:
        _research_catalog = _load_json("research-catalog.json")
        if not isinstance(_research_catalog, dict):
            _research_catalog = {"articles": {}}
    return _research_catalog


def _ensure_research_status() -> dict:
    global _research_status
    if _research_status is None:
        _research_status = _load_json("research-status.json")
        if not isinstance(_research_status, dict):
            _research_status = {
                "total_subjects": 0,
                "articles_count": 0,
                "missing_count": 0,
            }
    return _research_status


def _ensure_research_missing() -> dict:
    global _research_missing
    if _research_missing is None:
        _research_missing = _load_json("research-missing.json")
        if not isinstance(_research_missing, dict):
            _research_missing = {"missing": [], "count": 0}
    return _research_missing


def _sync_research_status() -> dict:
    catalog = _ensure_research_catalog()
    status = _ensure_research_status()
    articles = catalog.get("articles")
    if not isinstance(articles, dict):
        articles = {}
    complete = sum(
        1 for entry in articles.values()
        if isinstance(entry, dict) and not entry.get("no_material")
    )
    status["articles_count"] = complete
    status["missing_count"] = max(0, int(status.get("total_subjects", 0)) - complete)
    return status


def _research_article_html(title: str, body: str, *, no_material: bool) -> str:
    notice = (
        '<p class="notice">Materiale insufficiente: nessuna fonte pertinente disponibile.</p>'
        if no_material else ""
    )
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>{title} — librarAIn</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 46rem; margin: 0 auto; padding: 1.5rem; background: #1e1e1e; color: #d4d4d4; line-height: 1.55; }}
a {{ color: #4ec9b0; }}
.notice {{ color: #f0ad4e; }}
</style>
</head>
<body>
<p><a href="/ricerca.html">← Ricerca</a></p>
<h1>{title}</h1>
{notice}{body}
</body>
</html>"""


def _mock_article_body(poh_id: str, label: str, *, no_material: bool) -> str:
    if no_material:
        return (
            "<p>La biblioteca indicizzata non contiene pagine candidate sufficienti "
            f"per rispondere alla query con fonti verificabili.</p><p><strong>Query:</strong> {label}</p>"
        )
    return (
        f"<p><strong>{label}</strong> — articolo mock generato per review UI.</p>"
        "<p>Contenuto enciclopedico di esempio con citazioni simulate. "
        "Nessuna pipeline LLM reale.</p>"
        "<h2>Cronologia</h2><table border=\"1\" cellpadding=\"6\">"
        "<tr><th>Periodo</th><th>Evento</th></tr>"
        f"<tr><td>1271</td><td>Evento mock per {label}</td></tr></table>"
    )


def _publish_mock_article(poh_id: str, label: str, *, no_material: bool) -> None:
    catalog = _ensure_research_catalog()
    missing = _ensure_research_missing()
    display_title = "Materiale insufficiente" if no_material else label
    snippet = (
        f"Materiale insufficiente La biblioteca indicizzata non contiene pagine candidate "
        f"sufficienti per rispondere alla query con fonti verificabili. Query: {label}"
        if no_material else
        f"{label} {label} — articolo mock generato per review UI."
    )
    articles = catalog.setdefault("articles", {})
    articles[poh_id] = {
        "poh_id": poh_id,
        "title": display_title,
        "snippet": snippet[:180],
        "url": f"/articolo/{poh_id}.html",
        "request_id": f"mock-gen-{poh_id}",
        "skipped_llm": no_material,
        "no_material": no_material,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    if not no_material:
        items = missing.get("missing")
        if isinstance(items, list):
            missing["missing"] = [
                entry for entry in items
                if not (isinstance(entry, dict) and entry.get("poh_id") == poh_id)
            ]
            missing["count"] = len(missing["missing"])


def _research_search(query: str) -> list[dict]:
    catalog = _ensure_research_catalog()
    articles = catalog.get("articles")
    if not isinstance(articles, dict):
        return []
    q = query.strip().casefold()
    results: list[dict] = []
    for poh_id, meta in articles.items():
        if not isinstance(meta, dict) or meta.get("no_material"):
            continue
        hay = f"{meta.get('title', '')} {meta.get('snippet', '')} {poh_id}".casefold()
        if q not in hay:
            tokens = [token for token in q.split() if len(token) >= 2]
            if not tokens or not all(token in hay for token in tokens):
                continue
        results.append({
            "poh_id": poh_id,
            "title": str(meta.get("title") or poh_id),
            "snippet": str(meta.get("snippet") or ""),
            "url": str(meta.get("url") or f"/articolo/{poh_id}.html"),
        })
    return results


def _start_research_job(targets: list[dict]) -> str:
    job_id = f"mock-research-{secrets.token_hex(4)}"
    with _lock:
        _research_jobs[job_id] = {
            "job_id": job_id,
            "targets": list(targets),
            "done": 0,
            "total": len(targets),
            "status": "running" if targets else "succeeded",
            "errors": [],
            "started_at": time.monotonic(),
        }
    return job_id


def _advance_research_job(job_id: str) -> dict | None:
    with _lock:
        job = _research_jobs.get(job_id)
        if job is None:
            return None
        if job["status"] != "running":
            return dict(job)
        elapsed = time.monotonic() - float(job["started_at"])
        expected_done = min(job["total"], int(elapsed / 0.7) + 1)
        while job["done"] < expected_done and job["done"] < job["total"]:
            target = job["targets"][job["done"]]
            poh_id = str(target.get("poh_id") or "")
            label = str(target.get("label") or poh_id)
            no_material = poh_id in {"abruzzo", "accio"}
            _publish_mock_article(poh_id, label, no_material=no_material)
            job["done"] += 1
        if job["done"] >= job["total"]:
            job["status"] = "succeeded"
        return dict(job)


class MockHandler(BaseHTTPRequestHandler):
    server_version = "librarAInMock/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[mock] {self.address_string()} {fmt % args}")

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(code, body, "application/json; charset=utf-8")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _serve_static(self, rel_path: str) -> None:
        path = (WEB_ROOT / rel_path).resolve()
        if not str(path).startswith(str(WEB_ROOT.resolve())):
            self.send_error(403)
            return
        if not path.is_file():
            self.send_error(404)
            return
        suffix = path.suffix.lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        self._send_bytes(200, path.read_bytes(), types.get(suffix, "application/octet-stream"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path == "/admin.html":
            self._serve_static("admin.html")
            return
        if path in ("/ricerca.html", "/ricerca"):
            self._serve_static("ricerca.html")
            return
        if path == "/mockup/lab.html":
            self._serve_static("mockup/lab.html")
            return
        if path == "/mockup/research-lab.html":
            self._serve_static("mockup/research-lab.html")
            return
        if path.startswith("/mockup/"):
            rel = path.lstrip("/")
            candidate = WEB_ROOT / rel
            if candidate.is_file():
                self._serve_static(rel)
                return
        if path == "/health":
            self._send_json(200, {"ok": True, "mock": True})
            return

        if path == "/api/admin/book-pages-audit":
            payload = _load_json("audit.json")
            sha_filter = (query.get("source_sha256") or [""])[0].strip().lower()
            if sha_filter and isinstance(payload, dict):
                books = payload.get("books")
                if isinstance(books, list):
                    payload = dict(payload)
                    payload["books"] = [
                        book for book in books
                        if str(book.get("source_sha256", "")).lower() == sha_filter
                    ]
            self._send_json(200, payload)
            return

        if path == "/api/admin/book-pages/render":
            aligned_raw = (query.get("aligned_page") or [""])[0].strip()
            try:
                aligned = int(aligned_raw)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "aligned_page must be an integer"})
                return
            if aligned < 1:
                self._send_json(400, {"ok": False, "error": "aligned_page must be positive"})
                return
            self._send_bytes(200, _page_svg(aligned), "image/svg+xml")
            return

        if path == "/api/admin/book-pages/transcript":
            aligned_raw = (query.get("aligned_page") or [""])[0].strip()
            try:
                aligned = int(aligned_raw)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "aligned_page must be an integer"})
                return
            text, stage, model = _transcript_for_page(aligned)
            self._send_json(200, {
                "ok": True,
                "source_sha256": MOCK_SHA,
                "aligned_page": aligned,
                "stage": stage,
                "text": text,
                "producer_model": model,
            })
            return

        if path == "/api/research/status":
            status = _sync_research_status()
            self._send_json(200, {"ok": True, **status})
            return

        if path == "/api/research/books":
            payload = _load_json("research-books.json")
            books = payload.get("books") if isinstance(payload, dict) else []
            self._send_json(200, {"ok": True, "books": books if isinstance(books, list) else []})
            return

        if path == "/api/research/missing":
            missing = _ensure_research_missing()
            items = missing.get("missing") if isinstance(missing, dict) else []
            self._send_json(200, {
                "ok": True,
                "missing": items if isinstance(items, list) else [],
                "count": int(missing.get("count", 0)) if isinstance(missing, dict) else 0,
            })
            return

        if path == "/api/research/search":
            q = (query.get("q") or [""])[0].strip()
            if len(q) < 2:
                self._send_json(400, {"ok": False, "error": "query must be at least 2 characters"})
                return
            results = _research_search(q)
            self._send_json(200, {"ok": True, "query": q, "results": results, "count": len(results)})
            return

        if path.startswith("/articolo/") and path.endswith(".html"):
            poh_id = path.removeprefix("/articolo/").removesuffix(".html")
            catalog = _ensure_research_catalog()
            articles = catalog.get("articles")
            meta = articles.get(poh_id) if isinstance(articles, dict) else None
            if not isinstance(meta, dict):
                self.send_error(404)
                return
            label = str(meta.get("title") or poh_id)
            no_material = bool(meta.get("no_material"))
            body = _mock_article_body(poh_id, label, no_material=no_material)
            html = _research_article_html(label, body, no_material=no_material)
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "research" and parts[3] == "generate":
            if parts[4] == "status":
                job_id = (query.get("job_id") or [""])[0]
                job = _advance_research_job(job_id)
                if job is None:
                    self._send_json(404, {"ok": False, "error": "job not found"})
                    return
                self._send_json(200, {
                    "ok": True,
                    "job_id": job["job_id"],
                    "done": job["done"],
                    "total": job["total"],
                    "status": job["status"],
                    "errors": job.get("errors") or [],
                    "generated": [],
                    "request_ids": [],
                })
                return

        if len(parts) == 5 and parts[1] == "api" and parts[2] == "ingest":
            job_id = parts[3]
            action = parts[4]
            if action == "status":
                self._send_json(200, {
                    "ok": True,
                    "job_id": job_id,
                    "status": "done",
                    "global_step": 1,
                    "global_total": 1,
                })
                return
            if action == "events":
                self._handle_sse(job_id)
                return

        if path.startswith("/") and (WEB_ROOT / path.lstrip("/")).is_file():
            self._serve_static(path.lstrip("/"))
            return

        self.send_error(404, "Not Found")

    def _handle_sse(self, job_id: str) -> None:
        events = _take_sse_job(job_id)
        if events is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(event_name: str, payload: dict) -> None:
            line = f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        for item in events:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "progress")
            emit(status, item)
            time.sleep(0.08)
        emit("done", {"status": "done", "result": {"ok": True, "mock": True}})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/admin/book-pages/transcript/confirm":
            payload = self._read_json_body()
            aligned = payload.get("aligned_page")
            text = payload.get("text")
            if not isinstance(aligned, int) or aligned < 1:
                self._send_json(400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            if not isinstance(text, str):
                self._send_json(400, {"ok": False, "error": "text is required"})
                return
            with _lock:
                _transcript_overrides[aligned] = text
            self._send_json(200, {"ok": True, "result": {"aligned_page": aligned}})
            return

        if path == "/api/admin/book-pages/repair":
            payload = self._read_json_body()
            aligned = payload.get("aligned_page")
            if not isinstance(aligned, int) or aligned < 1:
                self._send_json(400, {"ok": False, "error": "aligned_page must be a positive integer"})
                return
            job_id = _register_sse_job("sse-repair-page.json")
            self._send_json(202, {
                "ok": True,
                "job_id": job_id,
                "events_url": f"/api/ingest/{job_id}/events",
            })
            return

        if path == "/api/admin/book-pages/repair-all":
            job_id = _register_sse_job("sse-repair-all.json")
            self._send_json(202, {
                "ok": True,
                "job_id": job_id,
                "events_url": f"/api/ingest/{job_id}/events",
            })
            return

        if path == "/api/ingest/submit":
            scenario = self.headers.get("X-Mock-Scenario", "sse-ingest-partial.json")
            if scenario not in {
                "sse-ingest-partial.json",
                "sse-ingest-done.json",
            }:
                scenario = "sse-ingest-partial.json"
            job_id = _register_sse_job(scenario)
            self._send_json(202, {
                "ok": True,
                "job_id": job_id,
                "status_url": f"/api/ingest/{job_id}/status",
                "events_url": f"/api/ingest/{job_id}/events",
            })
            return

        if path == "/api/research/generate":
            payload = self._read_json_body()
            missing = _ensure_research_missing()
            targets = list(missing.get("missing") or [])
            poh_ids = payload.get("poh_ids")
            if isinstance(poh_ids, list) and poh_ids:
                by_id = {
                    str(entry.get("poh_id")): entry
                    for entry in targets
                    if isinstance(entry, dict)
                }
                targets = [
                    by_id.get(str(poh_id)) or {"poh_id": str(poh_id), "label": str(poh_id)}
                    for poh_id in poh_ids
                ]
            elif isinstance(payload.get("book_sha"), str) and payload.get("book_sha").strip():
                targets = targets[:2]
            job_id = _start_research_job(targets)
            self._send_json(202, {
                "ok": True,
                "job_id": job_id,
                "total": len(targets),
                "status_url": f"/api/research/generate/status?job_id={job_id}",
            })
            return

        self.send_error(404, "Not Found")


def main() -> None:
    import os

    host = os.environ.get("MOCK_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_HTTP_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((host, port), MockHandler)
    print(f"Mock UI server: http://{host}:{port}/mockup/lab.html")
    print(f"Ingest con pannello lab: http://{host}:{port}/index.html?mock=1")
    print(f"Ricerca mock: http://{host}:{port}/ricerca.html?mock=1")
    print(f"Admin research mock: http://{host}:{port}/admin.html?mock=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStop.")


if __name__ == "__main__":
    main()

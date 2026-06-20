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
        if path == "/mockup/lab.html":
            self._serve_static("mockup/lab.html")
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

        parts = path.split("/")
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

        self.send_error(404, "Not Found")


def main() -> None:
    import os

    host = os.environ.get("MOCK_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_HTTP_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((host, port), MockHandler)
    print(f"Mock UI server: http://{host}:{port}/mockup/lab.html")
    print(f"Ingest con pannello lab: http://{host}:{port}/index.html?mock=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStop.")


if __name__ == "__main__":
    main()

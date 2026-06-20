from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.api.ingest_http_server import build_ingest_server
from src.ingestion.progress import make_event

_BOUNDARY = "testboundary42"


def _multipart_body(
    fields: dict[str, str], pdf_bytes: bytes | None
) -> tuple[bytes, str]:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{_BOUNDARY}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    if pdf_bytes is not None:
        parts.append(
            (
                f"--{_BOUNDARY}\r\n"
                'Content-Disposition: form-data; name="pdf_file"; filename="book.pdf"\r\n'
                "Content-Type: application/pdf\r\n\r\n"
            ).encode("utf-8")
            + pdf_bytes
            + b"\r\n"
        )
    parts.append(f"--{_BOUNDARY}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={_BOUNDARY}"


_FORM_FIELDS = {
    "titolo": "Storia di Roma",
    "autore": "Mommsen",
    "toc_range": "5-8",
    "index_range": "200-210",
}


class _ServerHarness:
    def __init__(self, api_token: str = "", max_concurrent_jobs: int = 1) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        settings = SimpleNamespace(
            data_root=self._tmp.name,
            ocr_use_gpu=False,
            openai_provider="remote",
            gpu_vram_check_enabled=False,
        )
        self.httpd, self.registry = build_ingest_server(
            settings,
            host="127.0.0.1",
            port=0,
            api_token=api_token,
            max_concurrent_jobs=max_concurrent_jobs,
        )
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.url(path), data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload: dict = {}
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                pass
            return exc.code, payload

    def submit(
        self,
        fields: dict[str, str] | None = None,
        pdf_bytes: bytes | None = b"%PDF-1.4 fake content",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        body, content_type = _multipart_body(fields or dict(_FORM_FIELDS), pdf_bytes)
        all_headers = {"Content-Type": content_type}
        all_headers.update(headers or {})
        return self.request(
            "/api/ingest/submit", method="POST", body=body, headers=all_headers
        )

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()


_P_PIPELINE = "src.api.ingest_http_server.run_full_pipeline"


class TestIngestSubmit(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()
        self.addCleanup(self.server.close)

    def test_health(self) -> None:
        status, payload = self.server.request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_mockup_fixture_served(self) -> None:
        req = urllib.request.Request(self.server.url("/mockup/fixtures/audit.json"))
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read()
        self.assertIn(b"books", body)

    def test_mockup_lab_script_served(self) -> None:
        req = urllib.request.Request(self.server.url("/mockup/ingest-debug.js"))
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read()
        self.assertIn(b"initPanel", body)

    def test_client_log_script_served(self) -> None:
        req = urllib.request.Request(self.server.url("/log.js"))
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read()
        self.assertIn(b"LibrarAInLog", body)

    def test_submit_returns_202_and_runs_pipeline(self) -> None:
        pipeline_done = threading.Event()

        def fake_pipeline(payload, saved_path, settings, *, reporter, set_global_total):
            self.assertTrue(Path(saved_path).is_file())
            self.assertEqual(payload["reicat"]["titolo"], "Storia di Roma")
            set_global_total(1)
            reporter(make_event("pipeline", "done", result={"ok": True}))
            pipeline_done.set()
            return {"ok": True}

        with patch(_P_PIPELINE, side_effect=fake_pipeline):
            status, payload = self.server.submit()
            self.assertEqual(status, 202)
            self.assertTrue(payload["ok"])
            self.assertIn("job_id", payload)
            self.assertTrue(pipeline_done.wait(timeout=10))

            job_id = payload["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status_code, job = self.server.request(
                    f"/api/ingest/{job_id}/status"
                )
                if job.get("status") == "done":
                    break
                time.sleep(0.05)
            self.assertEqual(status_code, 200)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["result"], {"ok": True})

    def test_submit_without_pdf_rejected(self) -> None:
        status, payload = self.server.submit(pdf_bytes=None)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_submit_non_pdf_rejected_by_magic_bytes(self) -> None:
        status, payload = self.server.submit(pdf_bytes=b"GIF89a not a pdf")
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_submit_invalid_range_rejected(self) -> None:
        fields = dict(_FORM_FIELDS)
        fields["toc_range"] = ""
        status, payload = self.server.submit(fields=fields)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_status_unknown_job_404(self) -> None:
        status, _ = self.server.request("/api/ingest/doesnotexist/status")
        self.assertEqual(status, 404)


class TestIngestAuth(unittest.TestCase):
    TOKEN = "sekret-token"

    def setUp(self) -> None:
        self.server = _ServerHarness(api_token=self.TOKEN)
        self.addCleanup(self.server.close)

    def test_submit_without_token_unauthorized(self) -> None:
        status, payload = self.server.submit()
        self.assertEqual(status, 401)
        self.assertFalse(payload.get("ok", False))

    def test_submit_with_header_token_accepted(self) -> None:
        with patch(_P_PIPELINE, return_value={"ok": True}):
            status, payload = self.server.submit(
                headers={"X-API-Token": self.TOKEN}
            )
        self.assertEqual(status, 202)
        self.assertTrue(payload["ok"])

    def test_submit_with_bearer_token_accepted(self) -> None:
        with patch(_P_PIPELINE, return_value={"ok": True}):
            status, payload = self.server.submit(
                headers={"Authorization": f"Bearer {self.TOKEN}"}
            )
        self.assertEqual(status, 202)

    def test_status_with_query_token_accepted(self) -> None:
        with patch(_P_PIPELINE, return_value={"ok": True}):
            _, payload = self.server.submit(headers={"X-API-Token": self.TOKEN})
        job_id = payload["job_id"]
        status, _ = self.server.request(
            f"/api/ingest/{job_id}/status?token={self.TOKEN}"
        )
        self.assertEqual(status, 200)

    def test_admin_subjects_requires_token(self) -> None:
        status, _ = self.server.request("/api/admin/subjects")
        self.assertEqual(status, 401)
        status, payload = self.server.request(
            "/api/admin/subjects", headers={"X-API-Token": self.TOKEN}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["subjects"], [])

    def test_admin_book_pages_audit_requires_token(self) -> None:
        status, _ = self.server.request("/api/admin/book-pages-audit")
        self.assertEqual(status, 401)
        status, payload = self.server.request(
            "/api/admin/book-pages-audit", headers={"X-API-Token": self.TOKEN}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["book_count"], 0)

    def test_admin_book_page_exclude_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/exclude",
            method="POST",
            body=json.dumps({"source_sha256": "a" * 64, "aligned_page": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_admin_book_page_render_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/render?source_sha256=" + "a" * 64 + "&aligned_page=1"
        )
        self.assertEqual(status, 401)

    def test_admin_book_page_render_returns_png(self) -> None:
        sha = "f" * 64
        data_root = Path(self.server._tmp.name)
        png_path = data_root / "tmp" / sha / "render" / "p.0001.png"
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        processed = data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        processed.joinpath(f"{sha}.pdf").write_bytes(b"%PDF-1.4\n")
        req = urllib.request.Request(
            self.server.url(
                "/api/admin/book-pages/render?"
                + urllib.parse.urlencode({"source_sha256": sha, "aligned_page": "1"})
            ),
            headers={"X-API-Token": self.TOKEN},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/png")
            body = resp.read()
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

    def test_admin_book_page_repair_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/repair",
            method="POST",
            body=json.dumps({"source_sha256": "a" * 64, "aligned_page": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_admin_book_page_transcript_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/transcript?source_sha256=" + "a" * 64 + "&aligned_page=1"
        )
        self.assertEqual(status, 401)

    def test_admin_book_page_transcript_post_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/transcript",
            method="POST",
            body=json.dumps({"source_sha256": "a" * 64, "aligned_page": 1, "text": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_admin_book_page_transcript_confirm_requires_token(self) -> None:
        status, _ = self.server.request(
            "/api/admin/book-pages/transcript/confirm",
            method="POST",
            body=json.dumps({"source_sha256": "a" * 64, "aligned_page": 1, "text": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_health_open_without_token(self) -> None:
        status, _ = self.server.request("/health")
        self.assertEqual(status, 200)


class TestJobQueueing(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness(max_concurrent_jobs=1)
        self.addCleanup(self.server.close)

    def test_second_job_waits_for_free_slot(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()

        def slow_pipeline(payload, saved_path, settings, *, reporter, set_global_total):
            first_started.set()
            release_first.wait(timeout=15)
            reporter(make_event("pipeline", "done", result={}))
            return {}

        with patch(_P_PIPELINE, side_effect=slow_pipeline):
            _, first = self.server.submit()
            self.assertTrue(first_started.wait(timeout=10))

            _, second = self.server.submit()
            second_id = second["job_id"]

            # The second job must report a queue event while the first holds
            # the only slot.
            deadline = time.monotonic() + 10
            queued = False
            while time.monotonic() < deadline:
                _, job = self.server.request(f"/api/ingest/{second_id}/status")
                events = job.get("events", [])
                if any(ev.get("phase") == "queue" for ev in events):
                    queued = True
                    break
                time.sleep(0.05)
            self.assertTrue(queued, "second job never reported queue wait")

            release_first.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, job = self.server.request(f"/api/ingest/{second_id}/status")
                if job.get("status") == "done":
                    break
                time.sleep(0.05)
            self.assertEqual(job["status"], "done")


if __name__ == "__main__":
    unittest.main()

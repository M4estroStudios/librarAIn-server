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
    def __init__(self, max_concurrent_jobs: int = 1) -> None:
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
            self.assertEqual(job["result"], job_id)

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


class TestAdminBookPageRender(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()
        self.addCleanup(self.server.close)

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
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/png")
            body = resp.read()
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")


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


class TestSystemJobsEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()

    def tearDown(self) -> None:
        self.server.close()

    def test_system_jobs_lists_running_job(self) -> None:
        job_id = self.server.registry.create_job()
        self.server.registry.set_global_total(job_id, 2)
        self.server.registry.emit(job_id, make_event("validation", "started"))
        status, payload = self.server.request("/api/system/jobs")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["active_jobs"], 1)
        self.assertEqual(payload["jobs"][0]["job_id"], job_id)
        self.assertIn("validation", [phase["phase"] for phase in payload["jobs"][0]["phases"]])

    def test_system_job_detail(self) -> None:
        job_id = self.server.registry.create_job(job_kind="research")
        self.server.registry.emit(
            job_id,
            {
                "phase": "research_collect",
                "status": "started",
                "page_total": 8,
                "subject_pages": 5,
                "time_pages": 3,
            },
        )
        status, payload = self.server.request(f"/api/system/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job"]["job_id"], job_id)
        collect_phase = next(
            p for p in payload["job"]["phases"] if p["phase"] == "research_collect"
        )
        self.assertEqual(collect_phase["total"], 8)
        self.assertEqual(payload["job"]["system_events_url"], f"/api/system/jobs/{job_id}/events")

    def test_system_jobs_history_endpoint(self) -> None:
        status, payload = self.server.request("/api/system/jobs/history?limit=10")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("jobs", payload)
        self.assertIsInstance(payload["jobs"], list)


class TestAdminSubjectDedupAndDelete(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()
        self.addCleanup(self.server.close)
        self.data_root = Path(self.server._tmp.name)
        self.polyindex_dir = self.data_root / "polyindex"
        self.polyindex_dir.mkdir(parents=True)
        sha = "c" * 64
        (self.polyindex_dir / "INDEX.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "subjects": {
                        "augusto": {
                            "canonical_label": "Augusto",
                            "aliases": [],
                            "books": {
                                sha: {
                                    "title": "Libro",
                                    "slug": "libro",
                                    "aligned_pages": [1],
                                    "original_pages": [1],
                                }
                            },
                        },
                        "ottaviano": {
                            "canonical_label": "Ottaviano",
                            "aliases": ["Augusto"],
                            "books": {
                                sha: {
                                    "title": "Libro",
                                    "slug": "libro",
                                    "aligned_pages": [2],
                                    "original_pages": [2],
                                }
                            },
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_suggestions_empty_then_dismiss_and_delete(self) -> None:
        status, payload = self.server.request("/api/admin/subjects/dedup/suggestions")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["clusters"], [])

        suggestions_path = self.polyindex_dir / "admin_dedup_suggestions.json"
        suggestions_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scanned_at": "2026-01-01T00:00:00+00:00",
                    "clusters": [
                        {
                            "cluster_key": "augusto|ottaviano",
                            "suggested_target_id": "augusto",
                            "score": 0.95,
                            "methods": ["fuzzy"],
                            "llm_reasons": [],
                            "members": [
                                {
                                    "canonical_id": "augusto",
                                    "canonical_label": "Augusto",
                                    "aliases": [],
                                    "book_count": 1,
                                    "has_article": False,
                                },
                                {
                                    "canonical_id": "ottaviano",
                                    "canonical_label": "Ottaviano",
                                    "aliases": ["Augusto"],
                                    "book_count": 1,
                                    "has_article": False,
                                },
                            ],
                        }
                    ],
                    "stats": {},
                }
            ),
            encoding="utf-8",
        )
        status, payload = self.server.request("/api/admin/subjects/dedup/suggestions")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["clusters"]), 1)

        body = json.dumps({"cluster_key": "augusto|ottaviano"}).encode("utf-8")
        status, payload = self.server.request(
            "/api/admin/subjects/dedup/dismiss",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["suggestions"]["clusters"], [])

        delete_body = json.dumps({"canonical_id": "ottaviano"}).encode("utf-8")
        status, payload = self.server.request(
            "/api/admin/subject/delete",
            method="POST",
            body=delete_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(delete_body)),
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["canonical_id"], "ottaviano")
        index = json.loads((self.polyindex_dir / "INDEX.json").read_text(encoding="utf-8"))
        self.assertNotIn("ottaviano", index["subjects"])
        self.assertIn("augusto", index["subjects"])

        missing_body = json.dumps({"canonical_id": "inesistente"}).encode("utf-8")
        status, payload = self.server.request(
            "/api/admin/subject/delete",
            method="POST",
            body=missing_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(missing_body)),
            },
        )
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_dedup_scan_starts_job(self) -> None:
        def fake_scan(*args, **kwargs):
            progress = kwargs.get("progress")
            if progress is not None:
                progress(make_event("subject_dedup", "started", message="scan"))
                progress(make_event("subject_dedup", "done", clusters=0, message="ok"))
            return {
                "schema_version": "1.0",
                "scanned_at": "2026-01-01T00:00:00+00:00",
                "clusters": [],
                "stats": {},
            }

        body = b"{}"
        with patch(
            "src.api.admin_subject_dedup.run_subject_dedup_scan",
            side_effect=fake_scan,
        ):
            status, payload = self.server.request(
                "/api/admin/subjects/dedup/scan",
                method="POST",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            self.assertEqual(status, 202)
            self.assertTrue(payload["ok"])
            job_id = payload["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, job = self.server.request(f"/api/ingest/{job_id}/status")
                if job.get("status") == "done":
                    break
                time.sleep(0.05)
            self.assertEqual(job["status"], "done")


class TestCrossOriginGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()
        self.addCleanup(self.server.close)

    def _post(self, headers: dict[str, str]) -> tuple[int, dict]:
        body = b"{}"
        all_headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        all_headers.update(headers)
        return self.server.request(
            "/api/admin/subjects/dedup/scan",
            method="POST",
            body=body,
            headers=all_headers,
        )

    def test_cross_origin_header_rejected(self) -> None:
        status, payload = self._post({"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", str(payload.get("error")))

    def test_cross_site_fetch_metadata_rejected(self) -> None:
        status, _ = self._post({"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_opaque_origin_rejected(self) -> None:
        status, _ = self._post({"Origin": "null"})
        self.assertEqual(status, 403)

    def test_same_origin_allowed(self) -> None:
        origin = f"http://127.0.0.1:{self.server.port}"
        with patch(
            "src.api.admin_subject_dedup.run_subject_dedup_scan",
            return_value={"schema_version": "1.0", "clusters": [], "stats": {}},
        ):
            status, _ = self._post({"Origin": origin, "Sec-Fetch-Site": "same-origin"})
        self.assertNotEqual(status, 403)

    def test_request_without_origin_allowed(self) -> None:
        with patch(
            "src.api.admin_subject_dedup.run_subject_dedup_scan",
            return_value={"schema_version": "1.0", "clusters": [], "stats": {}},
        ):
            status, _ = self._post({})
        self.assertNotEqual(status, 403)


class TestSourceShaTraversal(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ServerHarness()
        self.addCleanup(self.server.close)

    def test_render_rejects_traversal(self) -> None:
        sha = urllib.parse.quote("../../../../Windows/Temp/evil", safe="")
        status, payload = self.server.request(
            f"/api/admin/book-pages/render?source_sha256={sha}&aligned_page=1"
        )
        self.assertEqual(status, 400)
        self.assertIn("hex digest", str(payload.get("error")))

    def test_transcript_rejects_traversal(self) -> None:
        sha = urllib.parse.quote("../../../../Windows/Temp/evil", safe="")
        status, payload = self.server.request(
            f"/api/admin/book-pages/transcript?source_sha256={sha}&aligned_page=1"
        )
        self.assertEqual(status, 400)
        self.assertIn("hex digest", str(payload.get("error")))

    def test_transcript_save_rejects_traversal_and_writes_nothing(self) -> None:
        payload_body = json.dumps(
            {
                "source_sha256": "../../../../Windows/Temp/evil",
                "aligned_page": 1,
                "text": "pwned",
            }
        ).encode("utf-8")
        status, payload = self.server.request(
            "/api/admin/book-pages/transcript",
            method="POST",
            body=payload_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload_body)),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("hex digest", str(payload.get("error")))
        escaped = Path(self.server._tmp.name).parent / "Windows"
        self.assertFalse(escaped.exists())

    def test_short_hex_sha_rejected(self) -> None:
        status, payload = self.server.request(
            "/api/admin/book-pages/render?source_sha256=abc123&aligned_page=1"
        )
        self.assertEqual(status, 400)
        self.assertIn("64-char", str(payload.get("error")))


if __name__ == "__main__":
    unittest.main()

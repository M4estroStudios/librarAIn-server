from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.api.ingest_http_server import build_ingest_server
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.search.article_catalog import generate_article_for_poh


class _ServerHarness:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        polyindex = self.data_root / "polyindex"
        polyindex.mkdir(parents=True)
        doc = PolyindexIndexDocument(
            subjects={
                "subj_alpha": PolyindexIndexSubjectEntry(
                    canonical_label="Alpha Test",
                    aliases=["Alfa"],
                    books={"abc123": {"title": "Libro A", "slug": "libro-a", "aligned_pages": [1, 2]}},
                ),
                "subj_beta": PolyindexIndexSubjectEntry(
                    canonical_label="Beta Test",
                    books={"abc123": {"title": "Libro A", "slug": "libro-a", "aligned_pages": [3]}},
                ),
            }
        )
        (polyindex / "INDEX.json").write_bytes(doc.to_json_bytes())
        output = self.data_root / "output" / "abc123"
        output.mkdir(parents=True)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": "abc123",
                    "slug": "libro-a",
                    "pages": [{"aligned": 1}],
                    "reicat": {"titolo": "Libro A"},
                }
            ),
            encoding="utf-8",
        )
        from types import SimpleNamespace

        settings = SimpleNamespace(data_root=str(self.data_root))
        self.httpd, _ = build_ingest_server(settings, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get_json(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(self.url(path))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url(path),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def close(self) -> None:
        self.httpd.shutdown()
        self._tmp.cleanup()


class ResearchHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _ServerHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_ricerca_page_served(self) -> None:
        req = urllib.request.Request(self.harness.url("/ricerca.html"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("librarAIn", html)
        self.assertIn("Cerca negli articoli generati", html)
        self.assertIn("/api/research/search", html)

    def test_admin_page_research_generation_section(self) -> None:
        req = urllib.request.Request(self.harness.url("/admin.html"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("Genera articoli mancanti", html)

    def test_status_and_missing(self) -> None:
        status, data = self.harness.get_json("/api/research/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["total_subjects"], 2)
        self.assertEqual(data["missing_count"], 2)
        _, missing = self.harness.get_json("/api/research/missing")
        self.assertEqual(missing["count"], 2)

    def test_generate_search_and_article_page(self) -> None:
        status, accepted = self.harness.post_json("/api/research/generate", {"poh_ids": ["subj_alpha"]})
        self.assertEqual(status, 202)
        job_id = accepted["job_id"]
        self.assertTrue(job_id)
        for _ in range(30):
            _, snap = self.harness.get_json(f"/api/research/generate/status?job_id={job_id}")
            if snap.get("status") in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(snap["status"], "succeeded")
        _, search = self.harness.get_json("/api/research/search?q=alpha")
        self.assertEqual(search["count"], 1)
        url = search["results"][0]["url"]
        req = urllib.request.Request(self.harness.url(url))
        with urllib.request.urlopen(req, timeout=5) as resp:
            article = resp.read().decode("utf-8")
        self.assertIn("Alpha Test", article)

    def test_generate_article_for_poh_writes_file(self) -> None:
        result = generate_article_for_poh(self.harness.data_root, "subj_beta")
        self.assertTrue(result["url"].startswith("/articolo/"))
        path = Path(result["path"])
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()

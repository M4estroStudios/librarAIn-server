from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from src.api.ingest_http_server import build_ingest_server
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.settings import Settings
from src.persistence.research_runs import get_research_run_by_request_id


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
        settings = Settings.model_validate(
            {
                "DATA_ROOT": str(self.data_root),
                "OPENAI_PROVIDER": "local",
                "MATCHER_USE_AI": False,
            }
        )
        self.httpd, _ = build_ingest_server(
            settings,
            host="127.0.0.1",
            port=0,
            max_concurrent_research=2,
        )
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

    def test_submit_and_status(self) -> None:
        status, accepted = self.harness.post_json(
            "/api/research/submit",
            {"query": "Alpha Test", "poh": {"label": "Alpha Test", "id": "subj_alpha"}},
        )
        self.assertEqual(status, 202)
        request_id = accepted["request_id"]
        self.assertTrue(request_id)
        for _ in range(50):
            _, snap = self.harness.get_json(f"/api/research/{request_id}")
            if snap.get("status") in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(snap["status"], "succeeded")
        _, article = self.harness.get_json(f"/api/research/{request_id}/article")
        self.assertIn("markdown", article)
        sqlite_path = str(self.harness.data_root / "db" / "biblioteca.db")
        run_row = get_research_run_by_request_id(sqlite_path, request_id)
        self.assertIsNotNone(run_row)
        assert run_row is not None
        self.assertEqual(run_row["status"], "succeeded")
        self.assertEqual(run_row["poh_id"], "subj_alpha")
        self.assertIsNotNone(run_row["finished_at"])

    def test_generate_search_and_article_page(self) -> None:
        article_path = self.harness.data_root / "research" / "articles" / "subj_alpha.html"
        article_md_path = self.harness.data_root / "research" / "articles" / "subj_alpha.md"

        from src.search.postprocess import PostprocessResult
        from src.search.research_runner import ResearchContextAudit, ResearchRunResult

        def fake_generate_article_for_poh(data_root, poh_id, *, settings, request_id, reporter=None, publish_no_material=True):
            article_path.parent.mkdir(parents=True, exist_ok=True)
            article_path.write_text("<html><body>Alpha Test</body></html>", encoding="utf-8")
            article_md_path.write_text("# Alpha Test", encoding="utf-8")
            catalog_path = self.harness.data_root / "research" / "catalog.json"
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            catalog_path.write_text(
                json.dumps(
                    {
                        "articles": {
                            poh_id: {
                                "poh_id": poh_id,
                                "title": "Alpha Test",
                                "snippet": "Alpha Test",
                                "url": "/articolo/subj_alpha.html",
                                "request_id": request_id,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog_result = {
                "poh_id": poh_id,
                "title": "Alpha Test",
                "url": "/articolo/subj_alpha.html",
                "request_id": request_id,
                "path": str(article_path),
                "markdown_path": str(article_md_path),
                "skipped_llm": False,
            }
            research_result = ResearchRunResult(
                markdown="# Alpha Test",
                markdown_path=str(article_md_path),
                postprocess=PostprocessResult(markdown="# Alpha Test"),
                audit=ResearchContextAudit(
                    context_books_loaded={"abc123": [1, 2]},
                    context_books={"abc123": [1]},
                    subjects_matched=[{"canonical_id": poh_id, "method": "exact"}],
                ),
            )
            return catalog_result, research_result

        with patch(
            "src.api.research_handlers.generate_article_for_poh",
            side_effect=fake_generate_article_for_poh,
        ):
            status, accepted = self.harness.post_json("/api/research/generate", {"poh_ids": ["subj_alpha"]})
            self.assertEqual(status, 202)
            job_id = accepted["job_id"]
            self.assertTrue(job_id)
            for _ in range(50):
                _, snap = self.harness.get_json(f"/api/research/generate/status?job_id={job_id}")
                if snap.get("status") in ("succeeded", "failed"):
                    break
                time.sleep(0.1)
        self.assertEqual(snap["status"], "succeeded")
        self.assertTrue(snap.get("request_ids"))
        _, search = self.harness.get_json("/api/research/search?q=alpha")
        self.assertEqual(search["count"], 1)
        url = search["results"][0]["url"]
        req = urllib.request.Request(self.harness.url(url))
        with urllib.request.urlopen(req, timeout=5) as resp:
            article = resp.read().decode("utf-8")
        self.assertIn("Alpha Test", article)

    def test_generate_search_excludes_no_material(self) -> None:
        catalog_path = self.harness.data_root / "research" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        article_path = self.harness.data_root / "research" / "articles" / "subj_alpha.html"
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article_path.write_text("<html><body>Alpha Test</body></html>", encoding="utf-8")
        catalog_path.write_text(
            json.dumps(
                {
                    "articles": {
                        "subj_alpha": {
                            "poh_id": "subj_alpha",
                            "title": "Alpha Test",
                            "snippet": "Alpha Test",
                            "url": "/articolo/subj_alpha.html",
                            "request_id": "req-no-material",
                            "no_material": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _, search = self.harness.get_json("/api/research/search?q=alpha")
        self.assertEqual(search["count"], 0)
        _, missing = self.harness.get_json("/api/research/missing")
        self.assertEqual(missing["count"], 2)

    def test_generate_article_for_poh_writes_file(self) -> None:
        from src.search.article_catalog import generate_article_for_poh

        result = generate_article_for_poh(
            self.harness.data_root,
            "subj_beta",
            settings=Settings.model_validate(
                {
                    "DATA_ROOT": str(self.harness.data_root),
                    "OPENAI_PROVIDER": "local",
                    "MATCHER_USE_AI": False,
                }
            ),
            request_id="test-req-beta",
        )[0]
        self.assertTrue(result["url"].startswith("/articolo/"))
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        md_path = Path(result["markdown_path"])
        self.assertTrue(md_path.is_file())


if __name__ == "__main__":
    unittest.main()

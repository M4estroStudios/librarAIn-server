from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.api.ingest_http_server import build_ingest_server
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.settings import Settings


class _Harness:
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
                    books={"abc123": {"title": "Libro A", "slug": "a", "aligned_pages": [1]}},
                ),
            }
        )
        (polyindex / "INDEX.json").write_bytes(doc.to_json_bytes())
        articles = self.data_root / "research" / "articles"
        articles.mkdir(parents=True)
        (articles / "subj_alpha.md").write_text("# Alpha\nContenuto alpha.", encoding="utf-8")
        (articles / "subj_alpha.html").write_text("<html><body>Alpha</body></html>", encoding="utf-8")
        catalog = {
            "articles": {
                "subj_alpha": {
                    "poh_id": "subj_alpha",
                    "title": "Alpha Test",
                    "snippet": "alpha",
                    "url": "/articolo/subj_alpha.html",
                }
            }
        }
        (self.data_root / "research" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        settings = Settings.model_validate(
            {
                "DATA_ROOT": str(self.data_root),
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                "GPU_VRAM_CHECK_ENABLED": False,
            }
        )
        self.httpd, _ = build_ingest_server(settings, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(self.url(path), timeout=5) as resp:
            return resp.status, resp.read()

    def close(self) -> None:
        self.httpd.shutdown()
        self._tmp.cleanup()


class DashboardSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Harness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_dashboard_html_served(self) -> None:
        status, body = self.harness.get("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Dashboard", body.decode("utf-8"))

    def test_preflight_endpoint(self) -> None:
        status, body = self.harness.get("/api/system/preflight?operation=research")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("ok", data)
        self.assertEqual(data.get("operation"), "research")

    def test_search_includes_poh_with_article(self) -> None:
        status, body = self.harness.get("/api/research/search?q=alpha")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(data.get("count", 0), 1)
        self.assertTrue(any(r.get("poh_id") == "subj_alpha" for r in data.get("results", [])))

    def test_google_query_smoke_via_api(self) -> None:
        status, body = self.harness.get("/api/research/search?q=alpha")
        data = json.loads(body.decode("utf-8"))
        hit = next((r for r in data.get("results", []) if r.get("poh_id") == "subj_alpha"), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("url"), "/articolo/subj_alpha.html")

    def test_chat_tools_search(self) -> None:
        from src.api.chat_tools import execute_search_tool

        result = execute_search_tool(self.harness.data_root, {"query": "alpha", "n": 5})
        self.assertGreaterEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["poh_id"], "subj_alpha")

    def test_mock_articolo_query(self) -> None:
        status, body = self.harness.get("/articolo/subj_alpha.html?mock=ricerca-google-hit")
        self.assertEqual(status, 200)
        self.assertIn("mock", body.decode("utf-8").lower())

        from src.api.chat_tools import execute_read_source_tool

        result = execute_read_source_tool(self.harness.data_root, {"poh": "subj_alpha"})
        self.assertTrue(result["ok"])
        self.assertIn("Contenuto alpha", result["markdown"])


from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.api.ingest_http_server import build_ingest_server
from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.polyindex_toc import PolyindexTocBookEntry, PolyindexTocChapter, PolyindexTocDocument
from src.models.settings import Settings
from src.persistence.research_runs import get_research_run_by_request_id
from src.search.article_llm import load_article_prompt

SHA_A = "a" * 64
SHA_B = "b" * 64

_E2E_QUERIES: list[dict[str, object]] = [
    {
        "query": "Marco Polo viaggio verso Oriente",
        "poh": {"id": "marco-polo", "label": "Marco Polo"},
        "expects_secondary_poh": False,
    },
    {
        "query": "Kublai Khan imperatore mongolo",
        "poh": {"id": "kublai-khan", "label": "Kublai Khan"},
        "expects_secondary_poh": False,
    },
    {
        "query": "Venezia commerci marittimi",
        "poh": {"id": "venezia", "label": "Venezia"},
        "expects_secondary_poh": False,
    },
    {
        "query": "Marco Polo e Kublai Khan",
        "poh": {"id": "marco-polo", "label": "Marco Polo"},
        "expects_secondary_poh": True,
    },
    {
        "query": "Marco Polo nel 1271",
        "poh": {"id": "marco-polo", "label": "Marco Polo"},
        "expects_secondary_poh": False,
    },
]

_SOURCE_LINK_PATTERN = re.compile(
    r"\[[^\]]*\]\((source:[^)]+)\)",
    re.IGNORECASE,
)


def _write_book(
    data_root: Path,
    source_sha256: str,
    *,
    slug: str,
    title: str,
    pages: dict[int, str],
) -> None:
    output_dir = data_root / "output" / source_sha256
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_pages: list[dict[str, object]] = []
    for aligned, content in sorted(pages.items()):
        filename = f"p.{aligned:04d}.{slug}.md"
        rel_path = f"pages/{filename}"
        (pages_dir / filename).write_text(content, encoding="utf-8")
        manifest_pages.append(
            {
                "aligned": aligned,
                "original": aligned,
                "file": rel_path,
            }
        )
    manifest = {
        "source_sha256": source_sha256,
        "slug": slug,
        "pages": manifest_pages,
        "reicat": {"titolo": title},
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def _build_e2e_fixture(data_root: Path) -> None:
    polyindex = data_root / "polyindex"
    polyindex.mkdir(parents=True)

    _write_book(
        data_root,
        SHA_A,
        slug="viaggi-oriente",
        title="Viaggi in Oriente",
        pages={
            10: "Marco Polo partì da Venezia verso l'Oriente nel 1271.",
            11: "Marco Polo raggiunse la corte di Kublai Khan dopo anni di viaggio.",
            12: "Il viaggio di Marco Polo documentò rotte verso la Cina.",
        },
    )
    _write_book(
        data_root,
        SHA_B,
        slug="impero-mongolo",
        title="Impero mongolo",
        pages={
            5: "Kublai Khan governò l'impero mongolo con capitali diverse.",
            6: "Kublai Khan ricevette emissari da Venezia e dall'Europa.",
            8: "Venezia controllava rotte commerciali marittime nel Mediterraneo.",
        },
    )

    index_doc = PolyindexIndexDocument(
        subjects={
            "marco-polo": PolyindexIndexSubjectEntry(
                canonical_label="Marco Polo",
                aliases=["Polo"],
                books={
                    SHA_A: {
                        "title": "Viaggi in Oriente",
                        "slug": "viaggi-oriente",
                        "aligned_pages": [10, 11, 12],
                    }
                },
            ),
            "kublai-khan": PolyindexIndexSubjectEntry(
                canonical_label="Kublai Khan",
                aliases=["Kublai"],
                books={
                    SHA_A: {
                        "title": "Viaggi in Oriente",
                        "slug": "viaggi-oriente",
                        "aligned_pages": [11],
                    },
                    SHA_B: {
                        "title": "Impero mongolo",
                        "slug": "impero-mongolo",
                        "aligned_pages": [5, 6],
                    },
                },
            ),
            "venezia": PolyindexIndexSubjectEntry(
                canonical_label="Venezia",
                aliases=["Serenissima"],
                books={
                    SHA_A: {
                        "title": "Viaggi in Oriente",
                        "slug": "viaggi-oriente",
                        "aligned_pages": [10],
                    },
                    SHA_B: {
                        "title": "Impero mongolo",
                        "slug": "impero-mongolo",
                        "aligned_pages": [8],
                    },
                },
            ),
        }
    )
    (polyindex / "INDEX.json").write_bytes(index_doc.to_json_bytes())

    toc_doc = PolyindexTocDocument(
        books={
            SHA_A: PolyindexTocBookEntry(
                title="Viaggi in Oriente",
                slug="viaggi-oriente",
                chapters=[
                    PolyindexTocChapter(
                        label="Viaggio di Marco Polo",
                        aligned_page_start=10,
                        aligned_page_end=12,
                        original_page_start=10,
                        original_page_end=12,
                    )
                ],
            ),
            SHA_B: PolyindexTocBookEntry(
                title="Impero mongolo",
                slug="impero-mongolo",
                chapters=[
                    PolyindexTocChapter(
                        label="Kublai Khan",
                        aligned_page_start=5,
                        aligned_page_end=8,
                        original_page_start=5,
                        original_page_end=8,
                    )
                ],
            ),
        }
    )
    (polyindex / "TOC.json").write_bytes(toc_doc.to_json_bytes())

    time_index = {
        "schema_version": "1.0",
        "years": {
            "1271": {
                "books": {
                    SHA_A: {"aligned_pages": [10, 11], "original_pages": [10, 11]},
                }
            }
        },
        "dates": {},
    }
    (polyindex / "TIME_INDEX.json").write_text(
        json.dumps(time_index, ensure_ascii=False),
        encoding="utf-8",
    )


class _E2eResearchLlmClient:
    def __init__(self) -> None:
        self.chat = MagicMock()
        self.chat.completions.create.side_effect = self._create_completion

    def _first_page_ref(self, pages: list[dict[str, object]]) -> tuple[str, int]:
        if not pages:
            return SHA_A, 10
        first = pages[0]
        sha = str(first.get("source_sha256") or SHA_A)
        page = int(first.get("aligned_page") or 10)
        return sha, page

    def _article_response(self, user_message: str) -> str:
        payload = json.loads(user_message)
        query = str(payload.get("query") or "Ricerca")
        pages = payload.get("pages") or []
        sha, page = self._first_page_ref(pages)
        source = f"source:{sha}:aligned:{page}"
        title = query.split(".")[0].strip().title()
        body = f"Il tema {query} è documentato nelle fonti fornite"
        if "kublai" in query.casefold() and "marco" in query.casefold():
            body += "; Marco Polo incontrò Kublai Khan durante il viaggio"
        body += f" [descritto]({source})."
        return f"# {title}\n\n{body}"

    def _poh_response(self, user_message: str) -> str:
        payload = json.loads(user_message)
        article = str(payload.get("article_markdown") or "")
        if "Kublai Khan" in article and "poh:kublai-khan" not in article:
            article = article.replace(
                "Kublai Khan",
                "[Kublai Khan](poh:kublai-khan)",
                1,
            )
        return article

    def _timeline_response(self, user_message: str) -> str:
        payload = json.loads(user_message)
        article = str(payload.get("article_markdown") or "")
        query = str(payload.get("query") or "")
        pages = payload.get("pages") or []
        sha, page = self._first_page_ref(pages)
        source = f"source:{sha}:aligned:{page}"
        period = "1271" if "1271" in query else "1300"
        cronologia = (
            "## Cronologia\n\n"
            "| Periodo | Evento | Fonti |\n"
            "|---------|--------|-------|\n"
            f"| {period} | Evento principale documentato. | [Fonte]({source}) |"
        )
        if "## Cronologia" in article:
            return article
        return f"{article.rstrip()}\n\n{cronologia}\n"

    def _create_completion(self, **kwargs: object) -> MagicMock:
        messages = kwargs["messages"]
        system = str(messages[0]["content"])
        user = str(messages[1]["content"])
        if "passo d" in system:
            content = self._timeline_response(user)
        elif "passo c" in system:
            content = self._poh_response(user)
        elif "stile Wikipedia" in system or load_article_prompt()[:24] in system:
            content = self._article_response(user)
        else:
            content = self._article_response(user)
        response = MagicMock()
        response.choices[0].message.content = content
        return response


def _build_fake_openai_client() -> MagicMock:
    inner = _E2eResearchLlmClient()
    client = MagicMock()
    client.chat = inner.chat
    _client_states[client] = _ClientState(
        token_bucket=AsyncTokenBucket(60),
        retry_attempts=0,
    )
    return client


def _assert_smoke_gate(
    testcase: unittest.TestCase,
    article: dict[str, object],
    run_row: dict[str, object],
    *,
    expects_secondary_poh: bool,
) -> None:
    markdown = str(article["markdown"])
    testcase.assertIn("## Cronologia", markdown)
    testcase.assertNotIn("*[[fonte non verificabile]]*", markdown)

    timeline_rows = article.get("timeline_rows") or []
    testcase.assertGreaterEqual(len(timeline_rows), 1)
    for row in timeline_rows:
        testcase.assertGreaterEqual(len(row.get("source_links") or []), 1)

    citations = article.get("citations") or []
    testcase.assertGreaterEqual(len(citations), 1)

    source_links = _SOURCE_LINK_PATTERN.findall(markdown)
    testcase.assertGreaterEqual(len(source_links), 1)

    if expects_secondary_poh:
        poh_ids = {item["poh_id"] for item in (article.get("pohs_referenced") or [])}
        testcase.assertIn("kublai-khan", poh_ids)
        testcase.assertIn("(poh:kublai-khan)", markdown)

    testcase.assertEqual(run_row["status"], "succeeded")
    testcase.assertIsNotNone(run_row["finished_at"])
    testcase.assertGreater(int(run_row.get("citations_count") or 0), 0)
    context_books = json.loads(str(run_row["context_books_json"]))
    testcase.assertTrue(context_books)


class _ResearchE2eHarness:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        _build_e2e_fixture(self.data_root)
        settings = Settings.model_validate(
            {
                "DATA_ROOT": str(self.data_root),
                "OPENAI_PROVIDER": "local",
                "MATCHER_USE_AI": False,
            }
        )
        self.settings = settings
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

    def post_json(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url(path),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def get_json(self, path: str) -> tuple[int, dict[str, object]]:
        req = urllib.request.Request(self.url(path))
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def wait_for_terminal(self, request_id: str, *, attempts: int = 100) -> dict[str, object]:
        snap: dict[str, object] = {}
        for _ in range(attempts):
            _, snap = self.get_json(f"/api/research/{request_id}")
            if snap.get("status") in ("succeeded", "failed"):
                return snap
            time.sleep(0.1)
        return snap

    def close(self) -> None:
        self.httpd.shutdown()
        self._tmp.cleanup()


class ResearchE2eSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PYTHONUTF8"] = "1"
        self.harness = _ResearchE2eHarness()

    def tearDown(self) -> None:
        self.harness.close()

    @patch("src.search.research_runner.build_openai_client")
    def test_f2_t10_research_smoke_e2e(self, mock_build_client: MagicMock) -> None:
        mock_build_client.return_value = _build_fake_openai_client()
        sqlite_path = self.harness.settings.sqlite_path

        for index, case in enumerate(_E2E_QUERIES, start=1):
            with self.subTest(query=case["query"], index=index):
                payload: dict[str, object] = {"query": case["query"]}
                if case.get("poh"):
                    payload["poh"] = case["poh"]

                status, accepted = self.harness.post_json("/api/research/submit", payload)
                self.assertEqual(status, 202)
                request_id = str(accepted["request_id"])
                self.assertTrue(request_id)

                snap = self.harness.wait_for_terminal(request_id)
                self.assertEqual(snap.get("status"), "succeeded", snap)

                _, article = self.harness.get_json(f"/api/research/{request_id}/article")
                run_row = get_research_run_by_request_id(sqlite_path, request_id)
                self.assertIsNotNone(run_row)
                assert run_row is not None

                _assert_smoke_gate(
                    self,
                    article,
                    run_row,
                    expects_secondary_poh=bool(case.get("expects_secondary_poh")),
                )


if __name__ == "__main__":
    unittest.main()

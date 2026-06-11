from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.time_index import (
    extract_time_references,
    sync_time_index_from_book,
)
from src.ingestion.polyindex.time_index_llm import parse_llm_time_response
from src.models.settings import Settings

SHA_A = "a" * 64
SHA_B = "b" * 64


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATA_ROOT": "data",
        "OPENAI_PROVIDER": "local",
        "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
        "OPENAI_API_KEY": "test-key",
        "EDITOR_MODEL": "test-editor",
        "MAX_PARALLEL_REQUEST": 2,
        "TIME_INDEX_USE_LLM": True,
    }
    base.update(overrides)
    return Settings.model_validate(base)


class TestParseLlmTimeResponse(unittest.TestCase):
    def test_parses_json_payload(self) -> None:
        parsed = parse_llm_time_response(
            '{"years": ["Quattrocento", "1848"], "dates": ["12 marzo 1848"]}'
        )
        self.assertIsNotNone(parsed)
        years, dates = parsed
        self.assertEqual(years, {"Quattrocento", "1848"})
        self.assertEqual(dates, {"12 marzo 1848"})

    def test_parses_fenced_json(self) -> None:
        parsed = parse_llm_time_response(
            '```json\n{"years": ["XIV secolo"], "dates": []}\n```'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], {"XIV secolo"})


class TestExtractTimeReferencesForPage(unittest.IsolatedAsyncioTestCase):
    async def test_merges_llm_and_regex(self) -> None:
        with patch(
            "src.ingestion.polyindex.time_index_llm.extract_time_references_llm",
            new=AsyncMock(
                return_value=({"Quattrocento"}, set())
            ),
        ):
            from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page

            years, dates, used_llm = await extract_time_references_for_page(
                "Nel 1848 e agli inizi del Quattrocento.",
                client=object(),
                settings=_settings(),
                aligned_page=3,
            )
        self.assertTrue(used_llm)
        self.assertEqual(years, {"1848", "Quattrocento"})
        self.assertEqual(dates, set())

    async def test_regex_only_when_llm_disabled(self) -> None:
        with patch(
            "src.ingestion.polyindex.time_index_llm.extract_time_references_llm",
            new=AsyncMock(),
        ) as mock_llm:
            from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page

            years, dates, used_llm = await extract_time_references_for_page(
                "Nel 1848.",
                client=object(),
                settings=_settings(TIME_INDEX_USE_LLM=False),
                aligned_page=1,
            )
        mock_llm.assert_not_called()
        self.assertFalse(used_llm)
        self.assertEqual(years, {"1848"})
        self.assertEqual(dates, set())


    async def test_llm_cache_skips_second_api_call(self) -> None:
        with patch(
            "src.ingestion.polyindex.time_index_llm.extract_time_references_llm",
            new=AsyncMock(return_value=({"Quattrocento"}, set())),
        ) as mock_llm:
            from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page

            settings = _settings()
            text = "Agli inizi del Quattrocento."
            kwargs = {
                "client": object(),
                "settings": settings,
                "aligned_page": 1,
                "source_sha256": SHA_A,
                "book_slug": "libro-a",
            }
            await extract_time_references_for_page(text, **kwargs)
            await extract_time_references_for_page(text, **kwargs)
        self.assertEqual(mock_llm.await_count, 1)


class TestExtractTimeReferences(unittest.TestCase):
    def test_bare_years_in_plausible_range(self) -> None:
        years, dates = extract_time_references(
            "Nel 1848 scoppiarono i moti. La basilica fu consacrata nel 324."
        )
        self.assertEqual(years, {"1848", "324"})
        self.assertEqual(dates, set())

    def test_years_with_era_suffix(self) -> None:
        years, _ = extract_time_references(
            "Cesare morì nel 44 a.C.; Augusto nel 14 d.C."
        )
        self.assertEqual(years, {"44 a.C.", "14 d.C."})

    def test_full_date_with_year(self) -> None:
        years, dates = extract_time_references("Il 12 marzo 1848 la città insorse.")
        self.assertEqual(dates, {"12 marzo 1848"})
        self.assertIn("1848", years)

    def test_date_without_year(self) -> None:
        _, dates = extract_time_references("La festa cade il 1° maggio di ogni anno.")
        self.assertEqual(dates, {"1 maggio"})

    def test_page_reference_numbers_excluded(self) -> None:
        years, _ = extract_time_references("Si veda p. 1234 e pp. 456-789.")
        self.assertEqual(years, set())

    def test_out_of_range_numbers_excluded(self) -> None:
        years, _ = extract_time_references("Erano 50 uomini e 2500 cavalli.")
        self.assertEqual(years, set())


class TestSyncTimeIndexFromBook(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_book(self, sha: str, slug: str, pages: dict[int, str]) -> BookOutput:
        book_dir = self.tmp / "output" / sha
        pages_dir = book_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page_outputs = []
        for aligned, text in pages.items():
            file = pages_dir / f"p.{aligned:04d}.{slug}.md"
            file.write_text(text, encoding="utf-8")
            page_outputs.append(
                BookPageOutput(aligned=aligned, original=aligned, file=file)
            )
        return BookOutput(
            output_dir=book_dir,
            manifest_path=book_dir / "manifest.json",
            slug=slug,
            pages=page_outputs,
        )

    def test_creates_time_index_with_years_and_dates(self) -> None:
        book = self._make_book(
            SHA_A,
            "libro-a",
            {
                1: "Nel 1848 scoppiarono i moti.",
                2: "Il 12 marzo 1848 la città insorse.",
                3: "Testo senza riferimenti temporali.",
            },
        )
        path, stats = sync_time_index_from_book(
            self.polyindex_dir, SHA_A, book, book_title="Libro A"
        )
        self.assertTrue(path.is_file())
        self.assertEqual(stats["n_years"], 1)
        self.assertEqual(stats["n_dates"], 1)

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("1848", data["years"])
        entry = data["years"]["1848"]["books"][SHA_A]
        self.assertEqual(entry["title"], "Libro A")
        self.assertEqual(entry["slug"], "libro-a")
        self.assertEqual(entry["aligned_pages"], [1, 2])
        self.assertEqual(
            data["dates"]["12 marzo 1848"]["books"][SHA_A]["aligned_pages"], [2]
        )

    def test_second_book_merges_and_rerun_is_idempotent(self) -> None:
        book_a = self._make_book(SHA_A, "libro-a", {1: "Nel 1848."})
        book_b = self._make_book(SHA_B, "libro-b", {5: "Era il 1848 anche qui."})

        sync_time_index_from_book(self.polyindex_dir, SHA_A, book_a, book_title="A")
        path, _ = sync_time_index_from_book(
            self.polyindex_dir, SHA_B, book_b, book_title="B"
        )

        data = json.loads(path.read_text(encoding="utf-8"))
        books = data["years"]["1848"]["books"]
        self.assertIn(SHA_A, books)
        self.assertIn(SHA_B, books)

        before = path.read_bytes()
        sync_time_index_from_book(self.polyindex_dir, SHA_B, book_b, book_title="B")
        self.assertEqual(path.read_bytes(), before)

    def test_reingest_replaces_previous_book_entries(self) -> None:
        book_v1 = self._make_book(SHA_A, "libro-a", {1: "Nel 1700."})
        sync_time_index_from_book(self.polyindex_dir, SHA_A, book_v1, book_title="A")

        book_v2 = self._make_book(SHA_A, "libro-a", {1: "Nel 1800."})
        path, _ = sync_time_index_from_book(
            self.polyindex_dir, SHA_A, book_v2, book_title="A"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("1700", data["years"])
        self.assertIn("1800", data["years"])

    def test_years_sorted_with_bc_first(self) -> None:
        book = self._make_book(
            SHA_A,
            "libro-a",
            {1: "Nel 44 a.C. e poi nel 1848 e nel 324."},
        )
        path, _ = sync_time_index_from_book(
            self.polyindex_dir, SHA_A, book, book_title="A"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(data["years"].keys()), ["44 a.C.", "324", "1848"])

    def test_llm_only_year_appears_in_index(self) -> None:
        book = self._make_book(
            SHA_A,
            "libro-a",
            {1: "Agli inizi del Quattrocento la città prosperò."},
        )
        with patch(
            "src.ingestion.polyindex.time_index_llm.extract_time_references_llm",
            new=AsyncMock(return_value=({"Quattrocento"}, set())),
        ):
            path, stats = sync_time_index_from_book(
                self.polyindex_dir,
                SHA_A,
                book,
                book_title="Libro A",
                client=object(),
                settings=_settings(),
            )
        self.assertEqual(stats["n_years"], 1)
        self.assertEqual(stats["n_llm_pages"], 1)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("Quattrocento", data["years"])

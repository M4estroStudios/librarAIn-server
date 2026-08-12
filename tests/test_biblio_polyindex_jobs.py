from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.api.biblio_handlers import BiblioJobError
from src.api.biblio_polyindex_jobs import (
    POLYINDEX_RERUN_STAGES,
    run_polyindex_stage_job,
    run_polyindex_toc_job,
)
from src.models.request import PageRange
from src.models.settings import Settings


SHA = "a" * 64


def _write_book(root: Path, *, with_toc: bool = True, with_index: bool = True) -> Path:
    out = root / "output" / SHA
    pages = out / "pages"
    pages.mkdir(parents=True)
    (pages / "p.0001.book.md").write_text("# page\n", encoding="utf-8")
    (pages / "p.0002.book.md").write_text("Soggetto — 1\n", encoding="utf-8")
    manifest = {
        "slug": "book",
        "original_page_count": 2,
        "pages": [{"aligned": 1, "original": 1}, {"aligned": 2, "original": 2}],
        "reicat": {
            "title": "Demo",
            "autore": ["Autore"],
            "anno_di_pubblicazione": 1900,
        },
        "toc_range": {"start": 1, "end": 1},
        "index_range": {"start": 2, "end": 2},
        "biblio_range": {"start": 2, "end": 2},
    }
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_toc:
        (out / "TOC.md").write_text("# TOC\n", encoding="utf-8")
    if with_index:
        (out / "INDEX.md").write_text("Soggetto — 1\n", encoding="utf-8")
    return out


class BiblioPolyindexJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "polyindex").mkdir(parents=True)
        self.settings = Settings.model_validate(
            {
                "DATA_ROOT": str(self.root),
                "SQLITE_PATH": str(self.root / "db.sqlite"),
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                "OPENAI_API_KEY": "test-key",
                "EDITOR_MODEL": "test-editor",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_stage_rejected(self) -> None:
        with self.assertRaises(BiblioJobError):
            run_polyindex_stage_job(self.root, self.settings, SHA, "stage1_ocr")

    @patch("src.api.biblio_polyindex_jobs.sync_polyindex_toc_from_book")
    @patch("src.api.biblio_polyindex_jobs.refine_toc_md", new_callable=AsyncMock)
    def test_toc_regenerates_md_even_if_missing(
        self, refine_toc: AsyncMock, sync_toc: MagicMock
    ) -> None:
        out = _write_book(self.root, with_toc=False)
        refine_toc.side_effect = lambda path, *args, **kwargs: path
        sync_toc.return_value = self.root / "polyindex" / "TOC.json"
        result = run_polyindex_toc_job(self.root, self.settings, SHA)
        self.assertTrue(result["ok"])
        self.assertTrue((out / "TOC.md").is_file())
        refine_toc.assert_awaited()
        sync_toc.assert_called_once()

    @patch("src.api.biblio_polyindex_jobs.sync_polyindex_toc_from_book")
    @patch("src.api.biblio_polyindex_jobs.refine_toc_md", new_callable=AsyncMock)
    def test_toc_job_ok(self, refine_toc: AsyncMock, sync_toc: MagicMock) -> None:
        out = _write_book(self.root)
        (out / "TOC.md").write_text("# TOC stale\n", encoding="utf-8")
        refine_toc.side_effect = lambda path, *args, **kwargs: path
        sync_toc.return_value = self.root / "polyindex" / "TOC.json"
        result = run_polyindex_stage_job(self.root, self.settings, SHA, "polyindex_toc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "polyindex_toc")
        sync_toc.assert_called_once()
        self.assertIn("# TOC — Demo", (out / "TOC.md").read_text(encoding="utf-8"))

    @patch("src.api.biblio_polyindex_jobs.sync_polyindex_index_from_book")
    @patch("src.api.biblio_polyindex_jobs.build_book_md")
    @patch("src.api.biblio_polyindex_jobs.apply_index_cross_links", new_callable=AsyncMock)
    @patch("src.api.biblio_polyindex_jobs.refine_index_md", new_callable=AsyncMock)
    def test_index_regenerates_md(
        self,
        refine_index: AsyncMock,
        cross_links: AsyncMock,
        build_book: MagicMock,
        sync_index: MagicMock,
    ) -> None:
        out = _write_book(self.root, with_index=False)
        refine_index.side_effect = lambda path, *args, **kwargs: path
        cross_links.return_value = {}
        sync_index.return_value = (self.root / "polyindex" / "INDEX.json", {"n_subjects": 1})
        result = run_polyindex_stage_job(self.root, self.settings, SHA, "polyindex_index")
        self.assertTrue(result["ok"])
        self.assertTrue((out / "INDEX.md").is_file())
        self.assertIn("# INDEX — Demo", (out / "INDEX.md").read_text(encoding="utf-8"))
        refine_index.assert_awaited()
        cross_links.assert_awaited()
        build_book.assert_called_once()
        sync_index.assert_called_once()

    @patch("src.api.biblio_polyindex_jobs.run_biblio_only_job")
    def test_biblio_requires_range(self, run_biblio: MagicMock) -> None:
        _write_book(self.root)
        with self.assertRaises(BiblioJobError):
            run_polyindex_stage_job(self.root, self.settings, SHA, "polyindex_biblio")
        run_biblio.assert_not_called()

    @patch("src.api.biblio_polyindex_jobs.run_biblio_only_job")
    def test_biblio_delegates(self, run_biblio: MagicMock) -> None:
        _write_book(self.root)
        run_biblio.return_value = {"ok": True, "n_entries": 1}
        result = run_polyindex_stage_job(
            self.root,
            self.settings,
            SHA,
            "polyindex_biblio",
            biblio_range=PageRange(start=2, end=2),
        )
        self.assertEqual(result["stage"], "polyindex_biblio")
        run_biblio.assert_called_once()

    def test_biblio_progress_one_step_per_range_page(self) -> None:
        import asyncio

        from src.ingestion.output_writer import BookOutput, BookPageOutput
        from src.ingestion.polyindex.biblio_json import build_book_biblio_from_pages
        from src.models.request import ReicatMetadata, UsefulPagesEnumeration

        out = _write_book(self.root)
        pages = [
            BookPageOutput(aligned=1, original=1, file=out / "pages" / "p.0001.book.md"),
            BookPageOutput(aligned=2, original=2, file=out / "pages" / "p.0002.book.md"),
        ]
        book = BookOutput(
            output_dir=out,
            manifest_path=out / "manifest.json",
            slug="book",
            pages=pages,
        )
        useful = UsefulPagesEnumeration(
            source_sha256=SHA,
            original_page_count=2,
            aligned_page_count=2,
            useful_original_pages=[1, 2],
            original_page_to_aligned_page={1: 1, 2: 2},
            aligned_page_to_original_page={1: 1, 2: 2},
            toc_range_aligned=PageRange(start=1, end=1),
            index_range_aligned=PageRange(start=2, end=2),
            biblio_range_aligned=PageRange(start=1, end=2),
        )
        reicat = ReicatMetadata.model_validate(
            {"title": "Demo", "autore": ["Autore"], "anno_di_pubblicazione": 1900}
        )
        events: list[dict] = []
        with patch(
            "src.ingestion.polyindex.biblio_json.extract_biblio_entries_for_page",
            new=AsyncMock(
                return_value=[
                    {
                        "authors": "Rossi",
                        "title": "Libro",
                        "year": 1900,
                        "extras": {},
                    }
                ]
            ),
        ):
            asyncio.run(
                build_book_biblio_from_pages(
                    book,
                    useful,
                    source_sha256=SHA,
                    client=object(),
                    settings=self.settings,
                    reicat=reicat,
                    progress=events.append,
                )
            )
        started = [e for e in events if e.get("status") == "started"]
        page_steps = [e for e in events if e.get("status") == "page_progress"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["page_total"], 2)
        self.assertEqual(len(page_steps), 2)
        self.assertEqual([e["aligned_page"] for e in page_steps], [1, 2])
        self.assertTrue(all(e.get("counts_as_step") for e in page_steps))

    def test_stage_set_complete(self) -> None:
        self.assertEqual(
            POLYINDEX_RERUN_STAGES,
            {"polyindex_toc", "polyindex_index", "time_index", "polyindex_biblio"},
        )


if __name__ == "__main__":
    unittest.main()

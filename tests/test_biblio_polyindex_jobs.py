from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_toc_requires_md(self) -> None:
        _write_book(self.root, with_toc=False)
        with self.assertRaises(BiblioJobError):
            run_polyindex_toc_job(self.root, self.settings, SHA)

    @patch("src.api.biblio_polyindex_jobs.sync_polyindex_toc_from_book")
    def test_toc_job_ok(self, sync_toc: MagicMock) -> None:
        _write_book(self.root)
        sync_toc.return_value = self.root / "polyindex" / "TOC.json"
        result = run_polyindex_stage_job(self.root, self.settings, SHA, "polyindex_toc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "polyindex_toc")
        sync_toc.assert_called_once()

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

    def test_stage_set_complete(self) -> None:
        self.assertEqual(
            POLYINDEX_RERUN_STAGES,
            {"polyindex_toc", "polyindex_index", "time_index", "polyindex_biblio"},
        )


if __name__ == "__main__":
    unittest.main()

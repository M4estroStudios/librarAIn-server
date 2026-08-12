from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion.index_cross_links_preflight import (
    IndexCrossLinksPreflightError,
    ensure_index_cross_links_preflight,
    run_index_cross_links_preflight,
)


def _settings(data_root: Path) -> MagicMock:
    settings = MagicMock()
    settings.data_root = str(data_root)
    settings.max_parallel_request = 4
    return settings


SHA = "b" * 64


def _write_book(root: Path) -> None:
    out = root / "output" / SHA
    pages = out / "pages"
    pages.mkdir(parents=True)
    (pages / "p.0001.book.md").write_text("Soggetto — 1\n", encoding="utf-8")
    (pages / "p.0002.book.md").write_text("Soggetto, 1\n", encoding="utf-8")
    (out / "INDEX.md").write_text("Soggetto, 1\n", encoding="utf-8")
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


class IndexCrossLinksPreflightTests(unittest.TestCase):
    def test_static_preflight_ok_without_llm_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_book(root)
            settings = _settings(root)
            with patch(
                "src.ingestion.index_cross_links_preflight.build_openai_client",
                return_value=MagicMock(),
            ):
                result = asyncio.run(
                    run_index_cross_links_preflight(
                        root,
                        settings,
                        SHA,
                        run_unit_tests=False,
                        run_sandbox_probe=False,
                    )
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["phases"]["static"]["ok"])

    def test_ensure_raises_on_failed_static(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "output" / SHA
            pages = out / "pages"
            pages.mkdir(parents=True)
            (out / "INDEX.md").write_text("Soggetto, 1\n", encoding="utf-8")
            manifest = {
                "slug": "book",
                "original_page_count": 2,
                "pages": [{"aligned": 1, "original": 1}],
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
            settings = _settings(root)
            with patch(
                "src.ingestion.index_cross_links_preflight.build_openai_client",
                return_value=MagicMock(),
            ):
                with self.assertRaises(IndexCrossLinksPreflightError):
                    ensure_index_cross_links_preflight(
                        root,
                        settings,
                        SHA,
                        run_unit_tests=False,
                        run_sandbox_probe=False,
                    )

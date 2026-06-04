from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.ingestion.polyindex.index_json import sync_polyindex_index_from_book
from src.models.request import PageRange, UsefulPagesEnumeration

SHA_A = "a" * 64
SHA_B = "b" * 64


def _enumeration(sha: str, page_count: int = 20) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=sha,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=page_count, end=page_count),
    )


def _write_index_md(path: Path, lines: list[str]) -> None:
    path.write_text(
        "\n".join(["# INDEX — Libro test", ""] + lines),
        encoding="utf-8",
    )


class FakeEmbeddings:
    def create(self, *, model: str, input: str) -> MagicMock:
        del model
        digest = sum(ord(c) for c in input) % 97
        vector = [float((digest + i) % 13) / 13.0 for i in range(8)]
        item = MagicMock()
        item.embedding = vector
        response = MagicMock()
        response.data = [item]
        return response


def _fake_client() -> MagicMock:
    client = MagicMock()
    client.embeddings = FakeEmbeddings()
    return client


def _settings(data_root: str) -> MagicMock:
    settings = MagicMock()
    settings.matcher_embedding_model = "text-embedding-3-small"
    settings.matcher_similarity_threshold = 0.86
    settings.matcher_use_ai = False
    settings.matcher_llm_model = None
    settings.editor_model = "editor-model"
    return settings


class TestPolyindexIndex(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.polyindex_dir.mkdir(parents=True)
        self.sqlite_path = str(self.tmp / "biblioteca.db")
        self.client = _fake_client()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_two_books_shared_canonicals_and_idempotent_rerun(self) -> None:
        book_a_index = self.tmp / "book_a" / "INDEX.md"
        book_b_index = self.tmp / "book_b" / "INDEX.md"
        book_a_index.parent.mkdir(parents=True)
        book_b_index.parent.mkdir(parents=True)
        _write_index_md(
            book_a_index,
            [
                "Marco Polo — 2, 3",
                "Venezia — 4",
                "Colosseo — 5",
                "Foro Romano — 6",
            ],
        )
        _write_index_md(
            book_b_index,
            [
                "Marco Polo — 2",
                "Venezia — 3",
                "Pantheon — 4",
                "Circo Massimo — 5",
            ],
        )

        settings = _settings(str(self.tmp))
        path_a, stats_a = sync_polyindex_index_from_book(
            self.polyindex_dir,
            SHA_A,
            book_a_index,
            _enumeration(SHA_A),
            self.client,
            self.sqlite_path,
            settings,
            "req-book-a",
        )
        self.assertEqual(stats_a["n_new"], 4)

        data_after_a = json.loads(path_a.read_text(encoding="utf-8"))
        self.assertEqual(len(data_after_a["subjects"]), 4)
        for entry in data_after_a["subjects"].values():
            self.assertIn(SHA_A, entry["books"])

        path_b, stats_b = sync_polyindex_index_from_book(
            self.polyindex_dir,
            SHA_B,
            book_b_index,
            _enumeration(SHA_B),
            self.client,
            self.sqlite_path,
            settings,
            "req-book-b",
        )
        self.assertGreaterEqual(stats_b["n_match"], 2)

        data_after_b = json.loads(path_b.read_text(encoding="utf-8"))
        self.assertEqual(len(data_after_b["subjects"]), 6)

        shared = [
            sid
            for sid, entry in data_after_b["subjects"].items()
            if SHA_A in entry["books"] and SHA_B in entry["books"]
        ]
        self.assertEqual(len(shared), 2)

        first_bytes = path_a.read_bytes()
        path_a_again, stats_rerun = sync_polyindex_index_from_book(
            self.polyindex_dir,
            SHA_A,
            book_a_index,
            _enumeration(SHA_A),
            self.client,
            self.sqlite_path,
            settings,
            "req-book-a-rerun",
        )
        self.assertEqual(path_a_again, path_a)
        self.assertGreaterEqual(stats_rerun["n_match"], 1)
        self.assertEqual(path_a.read_bytes(), first_bytes)

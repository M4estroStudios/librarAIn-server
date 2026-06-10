from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.ingestion.polyindex.index_json import (
    SubjectMergeError,
    list_multibook_subjects,
    merge_polyindex_subjects,
    sort_polyindex_index_file,
    sorted_polyindex_index_bytes,
    sync_polyindex_index_from_book,
)
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

    def test_index_json_subjects_sorted_by_canonical_label(self) -> None:
        book_index = self.tmp / "book" / "INDEX.md"
        book_index.parent.mkdir(parents=True)
        _write_index_md(
            book_index,
            [
                "Venezia — 4",
                "Marco Polo — 2, 3",
                "Colosseo — 5",
            ],
        )
        settings = _settings(str(self.tmp))
        path, _ = sync_polyindex_index_from_book(
            self.polyindex_dir,
            SHA_A,
            book_index,
            _enumeration(SHA_A),
            self.client,
            self.sqlite_path,
            settings,
            "req-sort",
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = [
            entry["canonical_label"]
            for entry in data["subjects"].values()
        ]
        self.assertEqual(labels, sorted(labels, key=str.casefold))

    def test_book_entries_carry_title_and_slug(self) -> None:
        book_index = self.tmp / "book" / "INDEX.md"
        book_index.parent.mkdir(parents=True)
        _write_index_md(book_index, ["Venezia — 4"])
        settings = _settings(str(self.tmp))
        path, _ = sync_polyindex_index_from_book(
            self.polyindex_dir,
            SHA_A,
            book_index,
            _enumeration(SHA_A),
            self.client,
            self.sqlite_path,
            settings,
            "req-meta",
            book_title="Libro di prova",
            book_slug="libro-di-prova",
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = next(iter(data["subjects"].values()))
        book = entry["books"][SHA_A]
        self.assertEqual(book["title"], "Libro di prova")
        self.assertEqual(book["slug"], "libro-di-prova")
        self.assertEqual(book["aligned_pages"], [4])

    def test_list_multibook_subjects_and_merge(self) -> None:
        index_path = self.polyindex_dir / "INDEX.json"
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "subjects": {
                        "augusto": {
                            "canonical_label": "Augusto",
                            "aliases": [],
                            "books": {
                                SHA_A: {
                                    "title": "Libro A",
                                    "slug": "libro-a",
                                    "aligned_pages": [3],
                                    "original_pages": [3],
                                },
                                SHA_B: {
                                    "title": "Libro B",
                                    "slug": "libro-b",
                                    "aligned_pages": [9],
                                    "original_pages": [9],
                                },
                            },
                        },
                        "ottaviano": {
                            "canonical_label": "Ottaviano",
                            "aliases": ["Imperatore Augusto"],
                            "books": {
                                SHA_B: {
                                    "title": "Libro B",
                                    "slug": "libro-b",
                                    "aligned_pages": [11],
                                    "original_pages": [11],
                                }
                            },
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        multibook = list_multibook_subjects(self.polyindex_dir, min_books=2)
        self.assertEqual(len(multibook), 1)
        self.assertEqual(multibook[0]["canonical_id"], "augusto")
        self.assertEqual(multibook[0]["book_count"], 2)
        self.assertEqual(multibook[0]["books"][0]["title"], "Libro A")

        result = merge_polyindex_subjects(
            self.polyindex_dir, "augusto", ["ottaviano"]
        )
        self.assertEqual(result["target_id"], "augusto")
        self.assertIn("Ottaviano", result["aliases"])
        self.assertIn("Imperatore Augusto", result["aliases"])

        data = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertNotIn("ottaviano", data["subjects"])
        merged = data["subjects"]["augusto"]
        self.assertEqual(merged["books"][SHA_B]["aligned_pages"], [9, 11])
        self.assertEqual(merged["books"][SHA_B]["title"], "Libro B")

        with self.assertRaises(SubjectMergeError):
            merge_polyindex_subjects(self.polyindex_dir, "augusto", ["inesistente"])
        with self.assertRaises(SubjectMergeError):
            merge_polyindex_subjects(self.polyindex_dir, "augusto", ["augusto"])

    def test_sort_polyindex_index_file_reorders_subjects(self) -> None:
        index_path = self.polyindex_dir / "INDEX.json"
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "subjects": {
                        "venezia": {
                            "canonical_label": "Venezia",
                            "aliases": ["Laguna"],
                            "books": {},
                        },
                        "marco-polo": {
                            "canonical_label": "Marco Polo",
                            "aliases": [],
                            "books": {},
                        },
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.assertTrue(sort_polyindex_index_file(index_path))
        data = json.loads(index_path.read_text(encoding="utf-8"))
        labels = [entry["canonical_label"] for entry in data["subjects"].values()]
        self.assertEqual(labels, ["Marco Polo", "Venezia"])
        self.assertEqual(data["subjects"]["venezia"]["aliases"], ["Laguna"])
        self.assertFalse(sort_polyindex_index_file(index_path))
        self.assertEqual(
            index_path.read_bytes(),
            sorted_polyindex_index_bytes(data),
        )

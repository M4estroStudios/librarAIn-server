from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.models.polyindex_index import (
    PolyindexIndexBookEntry,
    PolyindexIndexDocument,
    PolyindexIndexSubjectEntry,
)
from src.ingestion.polyindex.subject_matcher import _subjects_map
from src.models.polyindex_toc import (
    PolyindexTocBookEntry,
    PolyindexTocChapter,
    PolyindexTocDocument,
)

SHA = "a" * 64
SHA_B = "b" * 64


class TestPolyindexTocModels(unittest.TestCase):
    def test_toc_document_roundtrip(self) -> None:
        document = PolyindexTocDocument(
            books={
                SHA: PolyindexTocBookEntry(
                    title="Libro",
                    slug="libro",
                    chapters=[
                        PolyindexTocChapter(
                            label="Capitolo I",
                            aligned_page_start=1,
                            aligned_page_end=10,
                            original_page_start=1,
                            original_page_end=10,
                        )
                    ],
                )
            }
        )
        restored = PolyindexTocDocument.model_validate(
            json.loads(document.to_json_bytes().decode("utf-8"))
        )
        self.assertEqual(restored.books[SHA].title, "Libro")
        self.assertEqual(len(restored.books[SHA].chapters), 1)

    def test_toc_document_rejects_invalid_schema_version(self) -> None:
        with self.assertRaises(ValidationError):
            PolyindexTocDocument.model_validate(
                {"schema_version": "9.9", "books": {}}
            )

    def test_toc_document_load_file_invalid_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "TOC.json"
            path.write_text("{not json", encoding="utf-8")
            document = PolyindexTocDocument.load_file(path)
            self.assertEqual(document.books, {})

    def test_toc_document_load_file_missing_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "TOC.json"
            document = PolyindexTocDocument.load_file(path)
            self.assertEqual(document.books, {})
            self.assertEqual(document.schema_version, "1.0")

    def test_toc_document_load_json_invalid_shape_returns_empty(self) -> None:
        document = PolyindexTocDocument.load_json(
            {
                "schema_version": "1.0",
                "books": {
                    SHA: {
                        "title": "Libro",
                        "slug": "libro",
                        "chapters": [
                            {
                                "aligned_page_start": 1,
                                "aligned_page_end": 10,
                                "original_page_start": 1,
                                "original_page_end": 10,
                            }
                        ],
                    }
                },
            }
        )
        self.assertEqual(document.books, {})

    def test_toc_document_load_json_ignores_extra_fields(self) -> None:
        document = PolyindexTocDocument.load_json(
            {
                "schema_version": "1.0",
                "legacy_field": "ignored",
                "books": {
                    SHA: {
                        "title": "Libro",
                        "slug": "libro",
                        "extra_meta": {"source": "legacy"},
                        "chapters": [],
                    }
                },
            }
        )
        self.assertIn(SHA, document.books)
        self.assertEqual(document.books[SHA].title, "Libro")
        self.assertFalse(hasattr(document.books[SHA], "extra_meta"))

    def test_toc_document_write_atomic_roundtrip(self) -> None:
        document = PolyindexTocDocument(
            books={
                SHA: PolyindexTocBookEntry(
                    title="Libro",
                    slug="libro",
                    chapters=[
                        PolyindexTocChapter(
                            label="Capitolo I",
                            aligned_page_start=1,
                            aligned_page_end=10,
                            original_page_start=1,
                            original_page_end=10,
                        )
                    ],
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "TOC.json"
            document.write_atomic(path)
            restored = PolyindexTocDocument.load_file(path)
            self.assertEqual(restored.books[SHA].title, "Libro")
            self.assertEqual(len(restored.books[SHA].chapters), 1)


class TestPolyindexIndexModels(unittest.TestCase):
    def test_index_document_sorted_and_merge_pages(self) -> None:
        document = PolyindexIndexDocument(
            subjects={
                "venezia": PolyindexIndexSubjectEntry(
                    canonical_label="Venezia",
                    aliases=["Laguna"],
                    books={
                        SHA: PolyindexIndexBookEntry(
                            aligned_pages=[4],
                            original_pages=[4],
                        )
                    },
                ),
                "marco-polo": PolyindexIndexSubjectEntry(
                    canonical_label="Marco Polo",
                ),
            }
        )
        sorted_doc = document.sorted()
        labels = [entry.canonical_label for entry in sorted_doc.subjects.values()]
        self.assertEqual(labels, ["Marco Polo", "Venezia"])
        self.assertEqual(sorted_doc.subjects["venezia"].aliases, ["Laguna"])

        entry = document.subjects["venezia"]
        entry.merge_book_pages(SHA, [5], [5], book_title="Libro", book_slug="libro")
        book = entry.books[SHA]
        self.assertEqual(book.aligned_pages, [4, 5])
        self.assertEqual(book.title, "Libro")

    def test_index_document_rejects_invalid_subject_shape(self) -> None:
        with self.assertRaises(ValidationError):
            PolyindexIndexDocument.model_validate(
                {
                    "schema_version": "1.0",
                    "subjects": {"bad": {"aliases": "not-a-list"}},
                }
            )

    def test_index_document_load_json_invalid_returns_empty(self) -> None:
        document = PolyindexIndexDocument.load_json(["not", "a", "dict"])
        self.assertEqual(document.subjects, {})

    def test_index_document_load_file_missing_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "INDEX.json"
            document = PolyindexIndexDocument.load_file(path)
            self.assertEqual(document.subjects, {})

    def test_index_document_load_json_invalid_pages_type_returns_empty(self) -> None:
        document = PolyindexIndexDocument.load_json(
            {
                "schema_version": "1.0",
                "subjects": {
                    "venezia": {
                        "canonical_label": "Venezia",
                        "books": {
                            SHA: {
                                "aligned_pages": "4",
                                "original_pages": [4],
                            }
                        },
                    }
                },
            }
        )
        self.assertEqual(document.subjects, {})

    def test_index_document_load_json_ignores_extra_fields(self) -> None:
        document = PolyindexIndexDocument.load_json(
            {
                "schema_version": "1.0",
                "migration_note": "legacy",
                "subjects": {
                    "venezia": {
                        "canonical_label": "Venezia",
                        "aliases": [],
                        "deprecated_id": "old-id",
                        "books": {
                            SHA: {
                                "title": "Libro",
                                "slug": "libro",
                                "aligned_pages": [4],
                                "original_pages": [4],
                                "source_path": "/tmp/book.pdf",
                            }
                        },
                    }
                },
            }
        )
        self.assertIn("venezia", document.subjects)
        entry = document.subjects["venezia"]
        self.assertEqual(entry.canonical_label, "Venezia")
        self.assertEqual(entry.books[SHA].aligned_pages, [4])
        self.assertFalse(hasattr(entry, "deprecated_id"))

    def test_index_subject_ensure_alias(self) -> None:
        entry = PolyindexIndexSubjectEntry(
            canonical_label="Venezia",
            aliases=["Laguna"],
        )
        entry.ensure_alias("Venezia")
        entry.ensure_alias("  Venezia  ")
        entry.ensure_alias("Laguna")
        entry.ensure_alias("San Marco")
        self.assertEqual(entry.aliases, ["Laguna", "San Marco"])

    def test_index_document_as_matcher_state(self) -> None:
        document = PolyindexIndexDocument(
            subjects={
                "venezia": PolyindexIndexSubjectEntry(
                    canonical_label="Venezia",
                    aliases=["Laguna"],
                    books={
                        SHA: PolyindexIndexBookEntry(
                            title="Libro",
                            slug="libro",
                            aligned_pages=[4],
                            original_pages=[4],
                        )
                    },
                )
            }
        )
        state = document.as_matcher_state()
        self.assertEqual(state["schema_version"], "1.0")
        subjects = _subjects_map(state)
        self.assertIn("venezia", subjects)
        entry = subjects["venezia"]
        self.assertEqual(entry["canonical_label"], "Venezia")
        self.assertEqual(entry["aliases"], ["Laguna"])
        book = entry["books"][SHA]
        self.assertEqual(book["aligned_pages"], [4])
        self.assertEqual(book["title"], "Libro")

    def test_index_document_sorted_orders_books(self) -> None:
        document = PolyindexIndexDocument(
            subjects={
                "venezia": PolyindexIndexSubjectEntry(
                    canonical_label="Venezia",
                    books={
                        SHA_B: PolyindexIndexBookEntry(aligned_pages=[2]),
                        SHA: PolyindexIndexBookEntry(aligned_pages=[1]),
                    },
                )
            }
        )
        sorted_doc = document.sorted()
        book_keys = list(sorted_doc.subjects["venezia"].books.keys())
        self.assertEqual(book_keys, sorted([SHA, SHA_B]))

    def test_index_document_write_atomic_roundtrip(self) -> None:
        document = PolyindexIndexDocument(
            subjects={
                "marco-polo": PolyindexIndexSubjectEntry(
                    canonical_label="Marco Polo",
                    books={
                        SHA: PolyindexIndexBookEntry(
                            aligned_pages=[1],
                            original_pages=[1],
                        )
                    },
                ),
                "venezia": PolyindexIndexSubjectEntry(
                    canonical_label="Venezia",
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "INDEX.json"
            document.write_atomic(path)
            restored = PolyindexIndexDocument.load_file(path)
            labels = [
                entry.canonical_label for entry in restored.subjects.values()
            ]
            self.assertEqual(labels, ["Marco Polo", "Venezia"])
            self.assertEqual(restored.subjects["marco-polo"].books[SHA].aligned_pages, [1])


if __name__ == "__main__":
    unittest.main()

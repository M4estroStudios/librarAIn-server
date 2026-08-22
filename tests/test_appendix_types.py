"""Tests for appendix tipologies catalog and typed section extraction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from src.api.appendix_types import (
    appendix_sections_from_form,
    extract_typed_appendix_pdfs,
    list_appendix_types,
    normalize_appendix_sections,
    slugify_appendix_type,
    upsert_appendix_type,
)
from tests.test_pdf_alignment import _minimal_pdf_bytes


class AppendixTypesTests(unittest.TestCase):
    def test_slugify_and_upsert_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db = Path(tmp_dir) / "biblioteca.db"
            item = upsert_appendix_type(str(db), "  Cronologia  ")
            self.assertEqual(item["name"], "Cronologia")
            self.assertEqual(item["slug"], "cronologia")
            again = upsert_appendix_type(str(db), "cronologia")
            self.assertEqual(again["name"], "Cronologia")
            items = list_appendix_types(str(db))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["slug"], "cronologia")

    def test_normalize_sections_and_extract_folders(self) -> None:
        sections = normalize_appendix_sections(
            {
                "sections": [
                    {"type": "Cronologia", "pages": "2-3"},
                    {"type": "Glossario", "pages": [3, 5]},
                ],
                "splits": {"3": 0.4},
            }
        )
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["slug"], "cronologia")
        self.assertEqual(sections[0]["pages"], [2, 3])
        self.assertEqual(sections[1]["pages"], [3, 5])
        self.assertEqual(slugify_appendix_type("Nota al testo"), "nota-al-testo")

        from src.api.appendix_types import normalize_appendix_splits

        splits = normalize_appendix_splits(
            {"sections": [], "splits": {"3": 0.4, "9": 0.2}},
            sections,
        )
        self.assertEqual(splits, {"3": 0.4})

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "book.pdf"
            source.write_bytes(_minimal_pdf_bytes(6))
            written = extract_typed_appendix_pdfs(
                source,
                sections,
                data_root=root,
                book_stem="mybook",
            )
            self.assertEqual(len(written), 2)
            crono = root / "input" / "raw_appendix" / "mybook" / "appendici" / "cronologia" / "appendix.pdf"
            gloss = root / "input" / "raw_appendix" / "mybook" / "appendici" / "glossario" / "appendix.pdf"
            self.assertTrue(crono.is_file())
            self.assertTrue(gloss.is_file())
            self.assertEqual(len(PdfReader(str(crono)).pages), 2)
            self.assertEqual(len(PdfReader(str(gloss)).pages), 2)

    def test_legacy_appendix_pages_fallback(self) -> None:
        sections = appendix_sections_from_form({"appendix_pages": "1,3-4", "appendix_sections_json": ""})
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["type"], "Appendice")
        self.assertEqual(sections[0]["pages"], [1, 3, 4])

        typed = appendix_sections_from_form(
            {
                "appendix_pages": "1-10",
                "appendix_sections_json": json.dumps(
                    [{"type": "Cronologia", "pages": "2-3"}],
                    ensure_ascii=False,
                ),
            }
        )
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0]["type"], "Cronologia")
        self.assertEqual(typed[0]["pages"], [2, 3])


if __name__ == "__main__":
    unittest.main()

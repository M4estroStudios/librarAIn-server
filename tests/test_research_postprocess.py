from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.search.article_llm import build_no_material_article
from src.search.postprocess import markdown_to_article_html, postprocess_markdown


class ResearchPostprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        output = self.data_root / "output" / "abc123"
        output.mkdir(parents=True)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": "abc123",
                    "slug": "libro-a",
                    "pages": [{"aligned": 112, "file": "pages/p.0112.md"}],
                }
            ),
            encoding="utf-8",
        )
        self.index = PolyindexIndexDocument(
            subjects={
                "marco-polo": PolyindexIndexSubjectEntry(
                    canonical_label="Marco Polo",
                    aliases=[],
                    books={},
                )
            }
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_invalid_source_link_replaced(self) -> None:
        markdown = (
            "Test [fonte](source:abc123:aligned:999) e "
            "[ok](source:abc123:aligned:112)."
        )
        result = postprocess_markdown(
            markdown,
            data_root=self.data_root,
            index_document=self.index,
            request_id="req1",
        )
        self.assertIn("*[[fonte non verificabile]]*", result.markdown)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.citations[0].aligned_page, 112)
        self.assertEqual(result.invalid_source_links, 1)

    def test_cronologia_rows_reordered(self) -> None:
        markdown = """# Articolo

## Cronologia

| Periodo | Evento | Fonti |
|---------|--------|-------|
| 1400 | Tardo | [f](source:abc123:aligned:112) |
| 1200 | Primo | [f](source:abc123:aligned:112) |
"""
        result = postprocess_markdown(
            markdown,
            data_root=self.data_root,
            index_document=self.index,
            request_id="req2",
        )
        self.assertGreaterEqual(len(result.timeline_rows), 2)
        self.assertEqual(result.timeline_rows[0].period, "1200")
        self.assertEqual(result.timeline_rows[1].period, "1400")

    def test_valid_poh_link_kept(self) -> None:
        markdown = "Vedi [Marco Polo](poh:marco-polo)."
        result = postprocess_markdown(
            markdown,
            data_root=self.data_root,
            index_document=self.index,
            request_id="req3",
        )
        self.assertIn("(poh:marco-polo)", result.markdown)
        self.assertEqual(len(result.pohs_referenced), 1)

    def test_annotazioni_section_removed(self) -> None:
        markdown = """# Articolo

Testo.

## Annotazioni

- TODO: risolvere poh:unknown-foo

## Cronologia

| Periodo | Evento | Fonti |
|---------|--------|-------|
| 1200 | Primo | [f](source:abc123:aligned:112) |
"""
        result = postprocess_markdown(
            markdown,
            data_root=self.data_root,
            index_document=self.index,
            request_id="req4",
        )
        self.assertNotIn("## Annotazioni", result.markdown)
        self.assertNotIn("TODO", result.markdown)
        self.assertIn("## Cronologia", result.markdown)

    def test_html_renders_lists_and_italic_without_double_escape(self) -> None:
        markdown = (
            "# Titolo\n\n"
            "Paragrafo con *[lectisternium](poh:lectisternium)* e [Filippo l'Arabo](poh:filippo).\n\n"
            "## Annotazioni\n\n"
            "- TODO: foo\n"
        )
        html = markdown_to_article_html("Titolo", markdown)
        self.assertNotIn("<h2>Titolo</h2>", html)
        self.assertIn("<em><a href=\"poh:lectisternium\">lectisternium</a></em>", html)
        self.assertIn("Filippo l&#x27;Arabo", html)
        self.assertNotIn("&amp;#x27;", html)
        self.assertNotIn("TODO", html)

    def test_no_material_html_notice(self) -> None:
        html = markdown_to_article_html(
            "Materiale insufficiente",
            build_no_material_article("Acerra"),
            no_material=True,
        )
        self.assertIn('class="notice"', html)


if __name__ == "__main__":
    unittest.main()

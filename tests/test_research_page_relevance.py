from __future__ import annotations

import unittest

from src.models.polyindex_index import (
    PolyindexIndexBookEntry,
    PolyindexIndexDocument,
    PolyindexIndexSubjectEntry,
)
from src.search.article_llm import (
    build_no_material_article,
    is_insufficient_sources_article,
    is_no_material_article,
    normalize_no_material_article,
)
from src.search.page_relevance import (
    collect_subject_terms,
    filter_relevant_pages,
    page_mentions_subject,
)
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh


SHA = "a" * 64


def _page(text: str, *, aligned: int = 1) -> LoadedPage:
    return LoadedPage(
        source_sha256=SHA,
        aligned_page=aligned,
        book_title="Storia di Roma Antica",
        book_slug="storia-di-roma-antica",
        markdown=text,
        truncated=False,
    )


def _document() -> PolyindexIndexDocument:
    return PolyindexIndexDocument(
        subjects={
            "acerra": PolyindexIndexSubjectEntry(
                canonical_label="Acerra",
                aliases=[],
                books={
                    SHA: PolyindexIndexBookEntry(
                        title="Libro",
                        slug="libro",
                        aligned_pages=[119],
                    )
                },
            ),
            "pirro": PolyindexIndexSubjectEntry(
                canonical_label="Pirro",
                aliases=[],
                books={
                    SHA: PolyindexIndexBookEntry(
                        title="Libro",
                        slug="libro",
                        aligned_pages=[151],
                    )
                },
            ),
        }
    )


class PageRelevanceTests(unittest.TestCase):
    def test_collect_subject_terms_from_poh(self) -> None:
        terms = collect_subject_terms(
            "Acerra",
            ResearchPoh(id="acerra", label="Acerra"),
            _document(),
        )
        self.assertIn("acerra", terms)

    def test_page_mentions_subject(self) -> None:
        self.assertTrue(
            page_mentions_subject(
                "La città di Acerra fu menzionata nel testo.",
                ["acerra"],
            )
        )
        self.assertFalse(
            page_mentions_subject(
                "La dedica del tempio di Giunone Moneta sul Campidoglio.",
                ["acerra"],
            )
        )

    def test_filter_relevant_pages_discards_unrelated(self) -> None:
        pages = [
            _page("La dedica del tempio sul Campidoglio nel 345 a.C.", aligned=119),
            _page("Acerra compare tra le città della Campania.", aligned=120),
        ]
        filtered = filter_relevant_pages(
            pages,
            query="Acerra",
            poh=ResearchPoh(id="acerra", label="Acerra"),
            document=_document(),
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].aligned_page, 120)


class NoMaterialArticleTests(unittest.TestCase):
    def test_detects_standard_no_material_title(self) -> None:
        article = build_no_material_article("Acerra")
        self.assertTrue(is_no_material_article(article))

    def test_detects_llm_negative_article(self) -> None:
        negative = (
            "# Acerra\n\n"
            "Le fonti fornite non contengono alcuna informazione relativa ad Acerra."
        )
        self.assertTrue(is_insufficient_sources_article(negative))

    def test_normalize_negative_to_standard(self) -> None:
        negative = (
            "# Acerra\n\n"
            "Le fonti fornite non contengono alcuna informazione relativa ad Acerra."
        )
        normalized = normalize_no_material_article("Acerra", negative)
        self.assertTrue(normalized.startswith("# Materiale insufficiente"))


if __name__ == "__main__":
    unittest.main()

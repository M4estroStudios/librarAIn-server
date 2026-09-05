from __future__ import annotations

import unittest

from src.ingestion.annotation_rules import (
    choose_representative_annotated_pages,
    compile_page_annotation_block,
    compose_index_prompt_notes,
    compose_page_prompt_notes,
    guidance_looks_truncated,
    guidance_missing_sections,
    normalize_label,
    notes_cache_hash,
)

PILOT_RAW_NAMES = [
    "Box_Curiosità",
    "Box_Curiosità_wIMG_connessa",
    "Sezione_(##)",
    "Header_dispari",
    "Header_Pari",
    "Immagine_part-page_full_width",
    "Capitolo_(#)",
    "doppia_Box_Curiosità",
    "Box_Curiosità_Sandwitch",
    "Sezione (##)",
    "Box Curiosità",
    "immagine_part-page_part_width",
    "Cronologia_Monocolonna",
    "Testata Pari",
    "Testata Dispari",
    "Indice_Analitico_a_2_Colonne",
    "Decorazione_di_Fine_Capitolo",
    "Immagine part-page full width",
    "Multipage_Box_Curiosità_(part_1_top)",
    "Multipage_Box_Curiosità_(parte_2_bottom)",
    "Introduzione Lista",
    "Cronologia su 2 colonne",
    "Senso_di_Marcia_multicolonna",
    "Capitolo (#)",
    "Header Pari",
    "Header dispari",
    "doppia Box Curiosità",
    "Box Curiosità Sandwitch",
    "Tripla Box curiosità",
    "Box Curiosità con Tabella",
    "Decorazione di Fine Capitolo",
    "Immagine Full Page",
    "Box Curiosità w/IMG connessa",
    "Grafico Genealigico",
    "Immagine Multi-Page (parte 1, sx)",
    "Immagine Multi-Page (part 2, dx)",
    "Multipage Box Curiosità (part 1, top)",
    "Multipage Box Curiosità (parte 2 bottom)",
    "Immagine_Multi-Page_(parte_1_sx)",
    "Immagine_Multi-Page_(part_2_dx)",
    "Titolo Appendice",
    "Senso di Marcia multicolonna",
    "Titolo_Appendice",
    "Cronologia Monocolonna",
    "Titolo Indice",
    "Indice Analitico a 2 Colonne",
    "2+ righe di pagine cit",
    "2_righe_di_pagine_cit",
    "Titolo TOC",
    "TOC 1 colonna",
    "TOC_1_colonna",
]


class NormalizeLabelTests(unittest.TestCase):
    def test_spaces_and_underscores_match(self) -> None:
        self.assertEqual(normalize_label("Box Curiosità"), "box_curiosita")
        self.assertEqual(normalize_label("Box_Curiosità"), "box_curiosita")

    def test_header_aliases(self) -> None:
        self.assertEqual(normalize_label("Testata Pari"), "header_pari")
        self.assertEqual(normalize_label("Header_dispari"), "header_dispari")

    def test_pilot_names_fit_canonical_budget(self) -> None:
        canonical = {normalize_label(name) for name in PILOT_RAW_NAMES}
        self.assertLessEqual(len(canonical), 30)
        self.assertIn("box_curiosita", canonical)
        self.assertIn("capitolo", canonical)
        self.assertIn("sezione", canonical)
        self.assertIn("indice_analitico_2_colonne", canonical)
        self.assertIn("2_righe_di_pagine_cit", canonical)
        self.assertIn("grafico_genealogico", canonical)


class ComposeNotesTests(unittest.TestCase):
    def test_page_notes_keep_raw_and_guidance(self) -> None:
        text = compose_page_prompt_notes(
            page_notes="Regola box in graffe.",
            guidance="Use asides.",
        )
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("Regola box in graffe.", text)
        self.assertIn("Use asides.", text)
        self.assertIn("## Note operatore", text)

    def test_budget_truncates_guidance_not_raw(self) -> None:
        raw = "A" * 200
        guidance = "B" * 500
        text = compose_page_prompt_notes(page_notes=raw, guidance=guidance, budget=280)
        assert text is not None
        self.assertIn(raw, text)
        self.assertLess(len(text), 400)
        self.assertTrue(text.startswith("## Note operatore"))

    def test_index_notes_are_raw_only(self) -> None:
        self.assertEqual(compose_index_prompt_notes(index_notes="  luoghi in italics  "), "luoghi in italics")
        self.assertIsNone(compose_index_prompt_notes(index_notes="  "))


class PageBlockTests(unittest.TestCase):
    def test_block_includes_canonical_and_description(self) -> None:
        block = compile_page_annotation_block(
            [
                {
                    "name": "Box Curiosità",
                    "description": "titolo ALL CAPS",
                    "type": "bbox",
                    "coords": [1, 2, 3, 4],
                }
            ]
        )
        self.assertIn("[box_curiosita]", block)
        self.assertIn("titolo ALL CAPS", block)
        self.assertIn("{", block)

    def test_representative_pages_cover_labels(self) -> None:
        annotations = [
            {"page": 1, "elements": [{"name": "Box Curiosità", "type": "bbox", "coords": [1, 2, 3, 4]}]},
            {"page": 2, "elements": [{"name": "Sezione (##)", "type": "bbox", "coords": [1, 2, 3, 4]}]},
            {"page": 3, "elements": [{"name": "Box Curiosità", "type": "bbox", "coords": [1, 2, 3, 4]}]},
        ]
        chosen = choose_representative_annotated_pages(annotations, limit=2)
        self.assertEqual(chosen, [1, 2])


class HashAndGuidanceTests(unittest.TestCase):
    def test_empty_hash(self) -> None:
        self.assertEqual(notes_cache_hash("", "", ""), "")
        self.assertNotEqual(notes_cache_hash("notes"), "")

    def test_truncated_and_missing_sections(self) -> None:
        self.assertTrue(guidance_looks_truncated("- @Immagine"))
        self.assertFalse(guidance_looks_truncated("Use asides for boxes.\nDone."))
        missing = guidance_missing_sections(
            "headers and boxes only",
            notes="TOC rules",
            index_notes="indice due colonne",
            page_notes="box",
        )
        self.assertIn("toc", missing)
        self.assertIn("index", missing)

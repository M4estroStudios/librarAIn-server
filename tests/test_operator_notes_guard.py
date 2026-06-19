from __future__ import annotations

import unittest

from src.ingestion.markdown_artifacts import (
    finalize_editor_page_output,
    finalize_vision_page_output,
    is_invalid_aggregate_refine_output,
    is_operator_notes_leak,
    strip_operator_notes_leak,
)

_NOTES = (
    "A pagine 395-402 del pdf trovi una Cronologia con Anno - Eventi.\n\n"
    "**Numerazione:** pagina del LIBRO in alto — offset costante PDF−3."
)
_STAGE2 = (
    "<!-- librarain:model=test -->\n"
    "LE NUOVE CURIE DI TULLO OSTILIO\n\n"
    "Le trenta curie sono il risultato della somma delle sette curie antiche."
)
_LEAKED = (
    "<!-- librarain:model=test -->\n"
    "**OPERATOR NOTES**\n\n"
    f"{_NOTES}\n"
)


class TestOperatorNotesGuard(unittest.TestCase):
    def test_strip_operator_notes_heading_and_body(self) -> None:
        cleaned = strip_operator_notes_leak(_LEAKED, _NOTES)
        self.assertNotIn("OPERATOR NOTES", cleaned)
        self.assertNotIn("Cronologia con Anno", cleaned)

    def test_finalize_editor_falls_back_to_stage2_on_full_leak(self) -> None:
        output, used_fallback = finalize_editor_page_output(
            _LEAKED,
            _STAGE2,
            prompt_notes=_NOTES,
        )
        self.assertTrue(used_fallback)
        self.assertIn("LE NUOVE CURIE", output)
        self.assertNotIn("OPERATOR NOTES", output)

    def test_finalize_editor_keeps_valid_cleanup(self) -> None:
        refined = "LE NUOVE CURIE DI TULLO OSTILIO\n\nLe trenta curie."
        output, used_fallback = finalize_editor_page_output(
            refined,
            _STAGE2,
            prompt_notes=_NOTES,
        )
        self.assertFalse(used_fallback)
        self.assertIn("LE NUOVE CURIE", output)

    def test_finalize_vision_strips_leaked_notes(self) -> None:
        output = finalize_vision_page_output(_LEAKED, _NOTES)
        self.assertNotIn("OPERATOR NOTES", output)

    def test_invalid_aggregate_refine_detects_refusal(self) -> None:
        self.assertTrue(
            is_invalid_aggregate_refine_output(
                "Please provide the raw Markdown text from the OCR scan.",
                "Capitolo I 12",
                prompt_notes=_NOTES,
            )
        )

    def test_is_operator_notes_leak_with_signature_line(self) -> None:
        self.assertTrue(is_operator_notes_leak(_LEAKED, _NOTES))
        self.assertFalse(is_operator_notes_leak(_STAGE2, _NOTES))


if __name__ == "__main__":
    unittest.main()

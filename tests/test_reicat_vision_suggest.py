import unittest

from src.api.reicat_vision_suggest import (
    _extract_json_object,
    default_reicat_page_indices,
    normalize_reicat_suggestion,
    reicat_page_sets,
    resolve_reicat_page_indices,
    split_reicat_collage_groups,
)


class ReicatPageSetsTests(unittest.TestCase):
    def test_short_book_only_lead(self) -> None:
        lead, tail = reicat_page_sets(8)
        self.assertEqual(lead, list(range(8)))
        self.assertEqual(tail, [])

    def test_long_book_lead_and_tail(self) -> None:
        lead, tail = reicat_page_sets(40, lead=15, tail=10)
        self.assertEqual(lead, list(range(15)))
        self.assertEqual(tail, list(range(30, 40)))

    def test_empty_book(self) -> None:
        lead, tail = reicat_page_sets(0)
        self.assertEqual(lead, [])
        self.assertEqual(tail, [])

    def test_default_reicat_page_indices(self) -> None:
        indices = default_reicat_page_indices(40)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[14], 14)
        self.assertEqual(indices[-1], 39)

    def test_resolve_custom_pages(self) -> None:
        indices = resolve_reicat_page_indices(20, [1, 3, 5])
        self.assertEqual(indices, [0, 2, 4])

    def test_split_collage_groups(self) -> None:
        first, second = split_reicat_collage_groups(list(range(25)))
        self.assertEqual(len(first), 13)
        self.assertEqual(len(second), 12)


class ReicatSuggestionNormalizeTests(unittest.TestCase):
    def test_normalize_lists_and_strings(self) -> None:
        normalized = normalize_reicat_suggestion(
            {
                "titolo": "  Titolo ",
                "autore": "Autore Uno, Autore Due",
                "curatore": ["Curatore"],
                "anno_di_pubblicazione": "1998",
                "numero_pagine": "pp. 320",
                "isbn": "",
            }
        )
        self.assertEqual(normalized["titolo"], "Titolo")
        self.assertEqual(normalized["autore"], ["Autore Uno", "Autore Due"])
        self.assertEqual(normalized["curatore"], ["Curatore"])
        self.assertEqual(normalized["anno_di_pubblicazione"], 1998)
        self.assertEqual(normalized["numero_pagine"], 320)
        self.assertIsNone(normalized["isbn"])

    def test_extract_json_from_fenced_response(self) -> None:
        payload = _extract_json_object(
            '```json\n{"titolo": "Libro", "autore": ["A"]}\n```'
        )
        self.assertEqual(payload["titolo"], "Libro")
        self.assertEqual(payload["autore"], ["A"])


if __name__ == "__main__":
    unittest.main()

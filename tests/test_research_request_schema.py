from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from src.search.request_schema import (
    DEFAULT_MAX_BOOKS,
    DEFAULT_MAX_PAGES_PER_BOOK,
    QUERY_MAX_LENGTH,
    ResearchInputErrorCode,
    ResearchRequest,
)
from src.search.request_validation import validate_research_request


def _valid_payload(**overrides: object) -> dict:
    payload = {"query": "Chi era Marco Polo?"}
    payload.update(overrides)
    return payload


class ResearchRequestSchemaTests(unittest.TestCase):
    def test_minimal_valid_payload(self) -> None:
        request = validate_research_request(_valid_payload())
        self.assertEqual(request.query, "Chi era Marco Polo?")
        self.assertIsNone(request.poh)
        self.assertEqual(request.options.max_books, DEFAULT_MAX_BOOKS)
        self.assertEqual(
            request.options.max_pages_per_book,
            DEFAULT_MAX_PAGES_PER_BOOK,
        )
        self.assertTrue(request.options.dedup)

    def test_full_payload_with_poh_and_options(self) -> None:
        request = validate_research_request(
            {
                "query": "  Viaggi verso la Cina  ",
                "poh": {
                    "id": "subj_marco_polo",
                    "label": "Marco Polo",
                    "time_range": "1271-1295",
                },
                "options": {
                    "max_books": 3,
                    "max_pages_per_book": 4,
                    "dedup": False,
                },
            }
        )
        self.assertEqual(request.query, "Viaggi verso la Cina")
        assert request.poh is not None
        self.assertEqual(request.poh.id, "subj_marco_polo")
        self.assertEqual(request.poh.label, "Marco Polo")
        self.assertEqual(request.poh.time_range, "1271-1295")
        self.assertEqual(request.options.max_books, 3)
        self.assertEqual(request.options.max_pages_per_book, 4)
        self.assertFalse(request.options.dedup)

    def test_query_too_short_after_trim(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request(_valid_payload(query="  ab "))
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.QUERY_INVALID.value)
        self.assertEqual(error["field"], "query")

    def test_query_too_long(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request(_valid_payload(query="x" * (QUERY_MAX_LENGTH + 1)))
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.QUERY_INVALID.value)
        self.assertEqual(error["field"], "query")

    def test_missing_query(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request({})
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.QUERY_INVALID.value)
        self.assertEqual(error["field"], "query")

    def test_poh_without_label(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request(
                _valid_payload(poh={"id": "subj_marco_polo", "label": "   "})
            )
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.POH_INVALID.value)
        self.assertEqual(error["field"], "poh.label")

    def test_options_max_books_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request(
                _valid_payload(options={"max_books": 0, "max_pages_per_book": 8})
            )
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.OPTIONS_INVALID.value)
        self.assertEqual(error["field"], "options.max_books")

    def test_options_max_pages_per_book_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_research_request(
                _valid_payload(options={"max_books": 5, "max_pages_per_book": 0})
            )
        error = json.loads(str(ctx.exception))
        self.assertEqual(error["code"], ResearchInputErrorCode.OPTIONS_INVALID.value)
        self.assertEqual(error["field"], "options.max_pages_per_book")

    def test_model_validate_direct_success(self) -> None:
        request = ResearchRequest.model_validate(_valid_payload())
        self.assertEqual(request.query, "Chi era Marco Polo?")

    def test_model_validate_rejects_invalid_type(self) -> None:
        with self.assertRaises(ValidationError):
            ResearchRequest.model_validate(_valid_payload(options={"max_books": "five"}))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.api.biblio_apply import (
    BiblioApplyError,
    extract_mentioned_pages,
    needs_ai_processing,
    order_apply_pages,
    validate_apply_notes,
)
from src.persistence.polyindex_conflicts import (
    add_conflict_item,
    count_conflicts_for_book,
    delete_conflict_item,
    list_conflict_items,
)

SHA = "c" * 64


class TestBiblioApplyRules(unittest.TestCase):
    def test_extract_mentioned_pages_order(self) -> None:
        self.assertEqual(
            extract_mentioned_pages("vedi @p.12 poi @p.3 e ancora @p.12"),
            [12, 3],
        )

    def test_validate_requires_notes(self) -> None:
        with self.assertRaises(BiblioApplyError):
            validate_apply_notes(
                "",
                [{"original_page": 1, "page_notes": ""}, {"original_page": 2, "page_notes": "ok"}],
            )
        validate_apply_notes("note generali", [{"original_page": 1, "page_notes": ""}])
        validate_apply_notes(
            "",
            [{"original_page": 1, "page_notes": "a"}, {"original_page": 2, "page_notes": "b"}],
        )

    def test_order_prefers_general_mentions_then_deps(self) -> None:
        pages = [
            {"original_page": 5, "page_notes": ""},
            {"original_page": 2, "page_notes": "usa @p.5"},
            {"original_page": 9, "page_notes": ""},
        ]
        ordered = order_apply_pages(pages, "inizia da @p.9")
        self.assertEqual([page["original_page"] for page in ordered], [9, 5, 2])

    def test_needs_ai_false_for_manual_only_flags(self) -> None:
        self.assertFalse(
            needs_ai_processing(
                "",
                [{"original_page": 1, "page_notes": "", "needs_vision": False}],
            )
        )
        self.assertTrue(
            needs_ai_processing(
                "",
                [{"original_page": 1, "page_notes": "fix OCR", "needs_vision": False}],
            )
        )


class TestConflictsStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_list_delete(self) -> None:
        item = add_conflict_item(
            self.root,
            source_sha256=SHA,
            aligned_page=4,
            original_page=4,
            manual_text="manual",
            ai_text="ai",
        )
        self.assertIsNotNone(item)
        self.assertEqual(count_conflicts_for_book(self.root, SHA), 1)
        listed = list_conflict_items(self.root, source_sha256=SHA)
        self.assertEqual(listed[0]["manual_text"], "manual")
        result = delete_conflict_item(self.root, listed[0]["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(count_conflicts_for_book(self.root, SHA), 0)


if __name__ == "__main__":
    unittest.main()

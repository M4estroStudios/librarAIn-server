from __future__ import annotations

import unittest

from src.models.polyindex_toc import PolyindexTocBookEntry, PolyindexTocChapter, PolyindexTocDocument
from src.search.chapter_expansion import expand_chapters


def _chapter(
    label: str,
    start: int,
    end: int,
) -> PolyindexTocChapter:
    return PolyindexTocChapter(
        label=label,
        aligned_page_start=start,
        aligned_page_end=end,
        original_page_start=start,
        original_page_end=end,
    )


def _book(chapters: list[PolyindexTocChapter]) -> PolyindexTocBookEntry:
    return PolyindexTocBookEntry(title="Book", slug="book", chapters=chapters)


def _toc(books: dict[str, PolyindexTocBookEntry]) -> PolyindexTocDocument:
    return PolyindexTocDocument(books=books)


class TestChapterExpansion(unittest.TestCase):
    def test_small_chapter_expands_to_full_range(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap I", 14, 16)])})
        result = expand_chapters({"sha1": [15]}, toc, request_id="req-1")
        self.assertEqual(result.pages, {"sha1": [14, 15, 16]})
        self.assertEqual(result.expanded_chapters, 1)
        self.assertEqual(result.unknown_pages, 0)

    def test_large_chapter_keeps_candidate_only(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap II", 50, 60)])})
        result = expand_chapters({"sha1": [55]}, toc, request_id="req-2")
        self.assertEqual(result.pages, {"sha1": [55]})
        self.assertEqual(result.expanded_chapters, 0)

    def test_chapter_with_exactly_six_pages_does_not_expand(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap III", 10, 15)])})
        result = expand_chapters({"sha1": [12]}, toc, request_id="req-3")
        self.assertEqual(result.pages, {"sha1": [12]})
        self.assertEqual(result.expanded_chapters, 0)

    def test_multiple_candidates_same_chapter_deduplicated(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap I", 20, 22)])})
        result = expand_chapters({"sha1": [20, 22]}, toc, request_id="req-4")
        self.assertEqual(result.pages, {"sha1": [20, 21, 22]})
        self.assertEqual(result.expanded_chapters, 1)

    def test_page_not_in_chapter_kept_as_is(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap I", 10, 20)])})
        result = expand_chapters({"sha1": [99]}, toc, request_id="req-5")
        self.assertEqual(result.pages, {"sha1": [99]})
        self.assertEqual(result.unknown_pages, 1)

    def test_book_missing_from_toc_keeps_candidates(self) -> None:
        result = expand_chapters({"sha-missing": [3, 7]}, _toc({}), request_id="req-6")
        self.assertEqual(result.pages, {"sha-missing": [3, 7]})
        self.assertEqual(result.expanded_chapters, 0)
        self.assertEqual(result.unknown_pages, 2)

    def test_empty_chapters_keeps_candidates(self) -> None:
        toc = _toc({"sha1": _book([])})
        result = expand_chapters({"sha1": [5]}, toc, request_id="req-7")
        self.assertEqual(result.pages, {"sha1": [5]})
        self.assertEqual(result.unknown_pages, 1)

    def test_max_pages_per_book_truncates_expanded_pages_first(self) -> None:
        toc = _toc({"sha1": _book([_chapter("Cap I", 10, 14)])})
        result = expand_chapters(
            {"sha1": [12]},
            toc,
            max_pages_per_book=2,
            request_id="req-8",
        )
        self.assertEqual(result.pages, {"sha1": [10, 12]})

    def test_max_books_keeps_books_with_more_candidates(self) -> None:
        toc = _toc(
            {
                "sha-few": _book([_chapter("A", 1, 3)]),
                "sha-many": _book([_chapter("B", 10, 12)]),
            }
        )
        result = expand_chapters(
            {"sha-few": [2], "sha-many": [10, 11, 12]},
            toc,
            max_books=1,
            request_id="req-9",
        )
        self.assertEqual(list(result.pages.keys()), ["sha-many"])
        self.assertEqual(result.books_dropped, 1)

    def test_empty_input_returns_empty(self) -> None:
        result = expand_chapters({}, _toc({}), request_id="req-10")
        self.assertEqual(result.pages, {})
        self.assertEqual(result.books_dropped, 0)


if __name__ == "__main__":
    unittest.main()

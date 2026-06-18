from __future__ import annotations

from dataclasses import dataclass

from src.core.log import INFO_LOG_LEVEL, Log
from src.models.polyindex_toc import PolyindexTocBookEntry, PolyindexTocChapter, PolyindexTocDocument
from src.search.request_schema import DEFAULT_MAX_BOOKS, DEFAULT_MAX_PAGES_PER_BOOK

MIN_CHAPTER_PAGES_FOR_FULL_EXPANSION = 6


@dataclass(frozen=True)
class ChapterExpansionResult:
    pages: dict[str, list[int]]
    expanded_chapters: int
    unknown_pages: int
    books_dropped: int


def _chapter_page_count(chapter: PolyindexTocChapter) -> int:
    return chapter.aligned_page_end - chapter.aligned_page_start + 1


def _find_chapter(chapters: list[PolyindexTocChapter], page: int) -> PolyindexTocChapter | None:
    for chapter in chapters:
        if chapter.aligned_page_start <= page <= chapter.aligned_page_end:
            return chapter
    return None


def _chapter_pages(chapter: PolyindexTocChapter) -> set[int]:
    return set(range(chapter.aligned_page_start, chapter.aligned_page_end + 1))


def _expand_book_pages(
    book_entry: PolyindexTocBookEntry | None,
    candidates: list[int],
) -> tuple[set[int], int, int]:
    original = sorted(set(candidates))
    if book_entry is None or not book_entry.chapters:
        return set(original), 0, len(original)

    expanded: set[int] = set()
    expanded_chapters = 0
    unknown_pages = 0
    seen_chapters: set[tuple[int, int]] = set()

    for page in original:
        chapter = _find_chapter(book_entry.chapters, page)
        if chapter is None:
            expanded.add(page)
            unknown_pages += 1
            continue

        chapter_key = (chapter.aligned_page_start, chapter.aligned_page_end)
        if _chapter_page_count(chapter) < MIN_CHAPTER_PAGES_FOR_FULL_EXPANSION:
            expanded.update(_chapter_pages(chapter))
            if chapter_key not in seen_chapters:
                seen_chapters.add(chapter_key)
                expanded_chapters += 1
        else:
            expanded.add(page)

    return expanded, expanded_chapters, unknown_pages


def _trim_book_pages(original: list[int], expanded: set[int], max_pages_per_book: int) -> list[int]:
    original_sorted = sorted(set(original))
    additional = sorted(expanded.difference(original_sorted))
    merged = original_sorted + additional
    return sorted(merged[:max_pages_per_book])


def expand_chapters(
    candidate_pages: dict[str, list[int]],
    toc: PolyindexTocDocument,
    *,
    max_books: int = DEFAULT_MAX_BOOKS,
    max_pages_per_book: int = DEFAULT_MAX_PAGES_PER_BOOK,
    request_id: str = "",
) -> ChapterExpansionResult:
    if not candidate_pages:
        return ChapterExpansionResult(
            pages={},
            expanded_chapters=0,
            unknown_pages=0,
            books_dropped=0,
        )

    ranked_books = sorted(
        candidate_pages.items(),
        key=lambda item: (-len(set(item[1])), item[0]),
    )
    selected_books = ranked_books[:max_books]
    books_dropped = max(0, len(ranked_books) - len(selected_books))

    result_pages: dict[str, list[int]] = {}
    total_expanded_chapters = 0
    total_unknown_pages = 0

    for source_sha256, candidates in selected_books:
        book_entry = toc.books.get(source_sha256)
        expanded, expanded_chapters, unknown_pages = _expand_book_pages(book_entry, candidates)
        trimmed = _trim_book_pages(candidates, expanded, max_pages_per_book)
        if trimmed:
            result_pages[source_sha256] = trimmed
        total_expanded_chapters += expanded_chapters
        total_unknown_pages += unknown_pages

    Log(
        INFO_LOG_LEVEL,
        "research chapter expansion completed",
        {
            "request_id": request_id,
            "input_books": len(candidate_pages),
            "output_books": len(result_pages),
            "expanded_chapters": total_expanded_chapters,
            "unknown_pages": total_unknown_pages,
            "books_dropped": books_dropped,
        },
    )

    return ChapterExpansionResult(
        pages=result_pages,
        expanded_chapters=total_expanded_chapters,
        unknown_pages=total_unknown_pages,
        books_dropped=books_dropped,
    )

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.core.parallel import parallel_map
from src.ingestion.markdown_artifacts import clean_markdown_channel_artifacts
from src.search.request_schema import DEFAULT_MAX_BOOKS, DEFAULT_MAX_PAGES_PER_BOOK

_TRUNCATION_SUFFIX = "\n\n[… contenuto troncato …]\n"


@dataclass(frozen=True)
class LoadedPage:
    source_sha256: str
    aligned_page: int
    book_title: str
    book_slug: str
    markdown: str
    truncated: bool


@dataclass(frozen=True)
class PagesLoadResult:
    pages: list[LoadedPage]
    loaded_pages: dict[str, list[int]]
    missing_pages: int
    truncated_pages: int
    total_chars: int
    books_dropped: int


@dataclass(frozen=True)
class _BookManifest:
    source_sha256: str
    slug: str
    title: str
    aligned_to_file: dict[int, Path]


def _apply_page_budget(
    candidate_pages: dict[str, list[int]],
    *,
    max_books: int,
    max_pages_per_book: int,
) -> tuple[dict[str, list[int]], int]:
    ranked_books = sorted(
        candidate_pages.items(),
        key=lambda item: (-len(set(item[1])), item[0]),
    )
    selected_books = ranked_books[:max_books]
    books_dropped = max(0, len(ranked_books) - len(selected_books))
    result: dict[str, list[int]] = {}
    for source_sha256, pages in selected_books:
        trimmed = sorted(set(pages))[:max_pages_per_book]
        if trimmed:
            result[source_sha256] = trimmed
    return result, books_dropped


def _truncate_markdown(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = max_chars - len(_TRUNCATION_SUFFIX)
    if cut < 1:
        return _TRUNCATION_SUFFIX[:max_chars], True
    return text[:cut].rstrip() + _TRUNCATION_SUFFIX, True


def _normalize_markdown(text: str) -> str:
    cleaned = clean_markdown_channel_artifacts(text)
    return cleaned.strip()


def _load_manifest(output_dir: Path) -> _BookManifest | None:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None

    source_sha256 = str(raw.get("source_sha256") or output_dir.name)
    slug = str(raw.get("slug") or "")
    reicat = raw.get("reicat") if isinstance(raw.get("reicat"), dict) else {}
    title = str(reicat.get("titolo") or reicat.get("title") or slug or source_sha256[:16])

    aligned_to_file: dict[int, Path] = {}
    pages = raw.get("pages")
    if isinstance(pages, list):
        for entry in pages:
            if not isinstance(entry, dict):
                continue
            aligned = entry.get("aligned")
            rel_path = entry.get("file")
            if not isinstance(aligned, int) or not isinstance(rel_path, str) or not rel_path:
                continue
            aligned_to_file[aligned] = output_dir / rel_path

    return _BookManifest(
        source_sha256=source_sha256,
        slug=slug,
        title=title,
        aligned_to_file=aligned_to_file,
    )


def _load_page_markdown(
    page_path: Path,
    *,
    max_chars_per_page: int | None,
) -> tuple[str, bool]:
    try:
        raw_text = page_path.read_text(encoding="utf-8")
    except OSError:
        return "", False
    normalized = _normalize_markdown(raw_text)
    if max_chars_per_page is None or max_chars_per_page <= 0:
        return normalized, False
    return _truncate_markdown(normalized, max_chars_per_page)


@dataclass(frozen=True)
class _BookLoadRequest:
    source_sha256: str
    aligned_pages: list[int]
    data_root: Path
    max_chars_per_page: int | None
    request_id: str


@dataclass(frozen=True)
class _BookLoadResult:
    source_sha256: str
    pages: list[LoadedPage]
    loaded_page_numbers: list[int]
    missing_pages: int
    truncated_pages: int
    total_chars: int
    manifest_missing: bool


def _load_aligned_page(
    item: tuple[_BookManifest, int, int],
) -> tuple[LoadedPage | None, int, int, bool]:
    manifest, aligned_page, max_chars_per_page = item
    page_path = manifest.aligned_to_file.get(aligned_page)
    if page_path is None or not page_path.is_file():
        return None, 1, 0, False
    markdown, truncated = _load_page_markdown(
        page_path,
        max_chars_per_page=max_chars_per_page,
    )
    truncated_count = 1 if truncated else 0
    return (
        LoadedPage(
            source_sha256=manifest.source_sha256,
            aligned_page=aligned_page,
            book_title=manifest.title,
            book_slug=manifest.slug,
            markdown=markdown,
            truncated=truncated,
        ),
        0,
        len(markdown),
        truncated_count > 0,
    )


def _load_book_pages(request: _BookLoadRequest) -> _BookLoadResult:
    output_dir = request.data_root / "output" / request.source_sha256
    manifest = _load_manifest(output_dir)
    if manifest is None:
        return _BookLoadResult(
            source_sha256=request.source_sha256,
            pages=[],
            loaded_page_numbers=[],
            missing_pages=len(request.aligned_pages),
            truncated_pages=0,
            total_chars=0,
            manifest_missing=True,
        )

    page_items = [
        (manifest, aligned_page, request.max_chars_per_page)
        for aligned_page in request.aligned_pages
    ]
    page_results = parallel_map(_load_aligned_page, page_items)

    pages: list[LoadedPage] = []
    loaded_page_numbers: list[int] = []
    missing_pages = 0
    truncated_pages = 0
    total_chars = 0
    for page, missing, chars, truncated in page_results:
        if missing:
            missing_pages += missing
            continue
        if page is None:
            continue
        if truncated:
            truncated_pages += 1
        pages.append(page)
        loaded_page_numbers.append(page.aligned_page)
        total_chars += chars

    return _BookLoadResult(
        source_sha256=manifest.source_sha256,
        pages=pages,
        loaded_page_numbers=loaded_page_numbers,
        missing_pages=missing_pages,
        truncated_pages=truncated_pages,
        total_chars=total_chars,
        manifest_missing=False,
    )


def load_pages(
    candidate_pages: dict[str, list[int]],
    data_root: Path,
    *,
    max_books: int = DEFAULT_MAX_BOOKS,
    max_pages_per_book: int = DEFAULT_MAX_PAGES_PER_BOOK,
    max_chars_per_page: int | None = None,
    request_id: str = "",
) -> PagesLoadResult:
    if not candidate_pages:
        return PagesLoadResult(
            pages=[],
            loaded_pages={},
            missing_pages=0,
            truncated_pages=0,
            total_chars=0,
            books_dropped=0,
        )

    selected_pages, books_dropped = _apply_page_budget(
        candidate_pages,
        max_books=max_books,
        max_pages_per_book=max_pages_per_book,
    )
    if not selected_pages:
        Log(
            INFO_LOG_LEVEL,
            "research pages loader completed",
            {
                "request_id": request_id,
                "input_books": len(candidate_pages),
                "loaded_books": 0,
                "loaded_page_count": 0,
                "missing_pages": 0,
                "truncated_pages": 0,
                "total_chars": 0,
                "books_dropped": books_dropped,
            },
        )
        return PagesLoadResult(
            pages=[],
            loaded_pages={},
            missing_pages=0,
            truncated_pages=0,
            total_chars=0,
            books_dropped=books_dropped,
        )

    loaded: list[LoadedPage] = []
    loaded_pages: dict[str, list[int]] = {}
    missing_pages = 0
    truncated_pages = 0
    total_chars = 0

    book_requests = [
        _BookLoadRequest(
            source_sha256=source_sha256,
            aligned_pages=selected_pages[source_sha256],
            data_root=data_root,
            max_chars_per_page=max_chars_per_page,
            request_id=request_id,
        )
        for source_sha256 in sorted(selected_pages)
    ]
    book_results = parallel_map(_load_book_pages, book_requests)

    for book_result in book_results:
        if book_result.manifest_missing:
            Log(
                WARNING_LOG_LEVEL,
                "research pages loader manifest missing",
                {
                    "request_id": request_id,
                    "source_sha256": book_result.source_sha256,
                    "aligned_pages": selected_pages.get(book_result.source_sha256, []),
                },
            )
        elif book_result.missing_pages:
            Log(
                WARNING_LOG_LEVEL,
                "research pages loader page missing",
                {
                    "request_id": request_id,
                    "source_sha256": book_result.source_sha256,
                    "missing_pages": book_result.missing_pages,
                },
            )
        if book_result.truncated_pages:
            Log(
                WARNING_LOG_LEVEL,
                "research pages loader page truncated",
                {
                    "request_id": request_id,
                    "source_sha256": book_result.source_sha256,
                    "truncated_pages": book_result.truncated_pages,
                    "max_chars_per_page": max_chars_per_page,
                },
            )
        missing_pages += book_result.missing_pages
        truncated_pages += book_result.truncated_pages
        total_chars += book_result.total_chars
        loaded.extend(book_result.pages)
        if book_result.loaded_page_numbers:
            loaded_pages[book_result.source_sha256] = book_result.loaded_page_numbers

    Log(
        INFO_LOG_LEVEL,
        "research pages loader completed",
        {
            "request_id": request_id,
            "input_books": len(candidate_pages),
            "loaded_books": len(loaded_pages),
            "loaded_page_count": len(loaded),
            "missing_pages": missing_pages,
            "truncated_pages": truncated_pages,
            "total_chars": total_chars,
            "books_dropped": books_dropped,
        },
    )

    return PagesLoadResult(
        pages=loaded,
        loaded_pages=loaded_pages,
        missing_pages=missing_pages,
        truncated_pages=truncated_pages,
        total_chars=total_chars,
        books_dropped=books_dropped,
    )

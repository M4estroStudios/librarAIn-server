from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.ingestion.markdown_artifacts import clean_markdown_channel_artifacts
from src.search.request_schema import DEFAULT_MAX_BOOKS, DEFAULT_MAX_PAGES_PER_BOOK

DEFAULT_MAX_CHARS_PER_PAGE = 12000
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
    max_chars_per_page: int,
) -> tuple[str, bool]:
    try:
        raw_text = page_path.read_text(encoding="utf-8")
    except OSError:
        return "", False
    normalized = _normalize_markdown(raw_text)
    return _truncate_markdown(normalized, max_chars_per_page)


def load_pages(
    candidate_pages: dict[str, list[int]],
    data_root: Path,
    *,
    max_books: int = DEFAULT_MAX_BOOKS,
    max_pages_per_book: int = DEFAULT_MAX_PAGES_PER_BOOK,
    max_chars_per_page: int = DEFAULT_MAX_CHARS_PER_PAGE,
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

    for source_sha256 in sorted(selected_pages):
        aligned_pages = selected_pages[source_sha256]
        output_dir = data_root / "output" / source_sha256
        manifest = _load_manifest(output_dir)
        if manifest is None:
            missing_pages += len(aligned_pages)
            Log(
                WARNING_LOG_LEVEL,
                "research pages loader manifest missing",
                {
                    "request_id": request_id,
                    "source_sha256": source_sha256,
                    "aligned_pages": aligned_pages,
                },
            )
            continue

        book_loaded: list[int] = []
        for aligned_page in aligned_pages:
            page_path = manifest.aligned_to_file.get(aligned_page)
            if page_path is None or not page_path.is_file():
                missing_pages += 1
                Log(
                    WARNING_LOG_LEVEL,
                    "research pages loader page missing",
                    {
                        "request_id": request_id,
                        "source_sha256": source_sha256,
                        "aligned_page": aligned_page,
                    },
                )
                continue

            markdown, truncated = _load_page_markdown(
                page_path,
                max_chars_per_page=max_chars_per_page,
            )
            if truncated:
                truncated_pages += 1
                Log(
                    WARNING_LOG_LEVEL,
                    "research pages loader page truncated",
                    {
                        "request_id": request_id,
                        "source_sha256": source_sha256,
                        "aligned_page": aligned_page,
                        "max_chars_per_page": max_chars_per_page,
                    },
                )

            loaded.append(
                LoadedPage(
                    source_sha256=manifest.source_sha256,
                    aligned_page=aligned_page,
                    book_title=manifest.title,
                    book_slug=manifest.slug,
                    markdown=markdown,
                    truncated=truncated,
                )
            )
            book_loaded.append(aligned_page)
            total_chars += len(markdown)

        if book_loaded:
            loaded_pages[manifest.source_sha256] = book_loaded

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

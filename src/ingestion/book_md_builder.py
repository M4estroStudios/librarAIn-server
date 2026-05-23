from __future__ import annotations

import json
from pathlib import Path

from src.ingestion.output_writer import BookOutput, BookPageOutput, _atomic_write_bytes
from src.models.request import UsefulPagesEnumeration


def _load_reicat(book_output: BookOutput) -> dict[str, object]:
    try:
        data = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
        reicat = data.get("reicat")
        if isinstance(reicat, dict):
            return reicat
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        pass
    return {}


def _resolve_title(reicat: dict[str, object], book_output: BookOutput) -> str:
    title = reicat.get("titolo") or reicat.get("title")
    if title:
        return str(title).strip()
    return book_output.slug


def _resolve_author_line(reicat: dict[str, object]) -> str:
    authors = reicat.get("autore") or reicat.get("authors")
    author = ""
    if isinstance(authors, list) and authors:
        author = str(authors[0]).strip()
    year = reicat.get("anno_di_pubblicazione") or reicat.get("publication_year")
    if year is not None:
        return f"_{author} — {year}_"
    return f"_{author}_"


def _concat_page_bodies(pages: list[BookPageOutput]) -> str:
    chunks: list[str] = []
    for index, page in enumerate(pages):
        if not page.file.is_file():
            raise FileNotFoundError(f"page md not found: {page.file}")
        if index > 0:
            chunks.append(
                f"\n\n---\n\n<!-- p.{page.aligned} (orig. p.{page.original}) -->\n\n"
            )
        chunks.append(page.file.read_text(encoding="utf-8"))
    return "".join(chunks)


def build_book_md(
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> Path:
    del useful_pages_enumeration
    reicat = _load_reicat(book_output)
    title = _resolve_title(reicat, book_output)
    author_line = _resolve_author_line(reicat)
    selected_pages = sorted(book_output.pages, key=lambda page: page.aligned)
    body = _concat_page_bodies(selected_pages)
    content = f"# {title}\n\n{author_line}\n\n{body}"
    dest = book_output.output_dir / f"{book_output.slug}.md"
    _atomic_write_bytes(dest, content.encode("utf-8"))
    return dest

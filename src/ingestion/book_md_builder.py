from __future__ import annotations

import filecmp
import json
import os
import shutil
from pathlib import Path

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.models.request import UsefulPagesEnumeration

_STREAM_CHUNK_SIZE = 65536
_PAGE_SEPARATOR_TEMPLATE = "\n\n---\n\n<!-- p.{aligned} (orig. p.{original}) -->\n\n"


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


def _page_separator(page: BookPageOutput) -> bytes:
    return _PAGE_SEPARATOR_TEMPLATE.format(
        aligned=page.aligned,
        original=page.original,
    ).encode("utf-8")


def _stream_book_md(dest: Path, *, header: bytes, pages: list[BookPageOutput]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        with tmp_path.open("wb") as out:
            out.write(header)
            for index, page in enumerate(pages):
                if index > 0:
                    out.write(_page_separator(page))
                if not page.file.is_file():
                    raise FileNotFoundError(f"page md not found: {page.file}")
                with page.file.open("rb") as src:
                    shutil.copyfileobj(src, out, length=_STREAM_CHUNK_SIZE)
        if dest.is_file() and filecmp.cmp(dest, tmp_path, shallow=False):
            tmp_path.unlink(missing_ok=True)
            return
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


def build_book_md(
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> Path:
    del useful_pages_enumeration
    reicat = _load_reicat(book_output)
    title = _resolve_title(reicat, book_output)
    author_line = _resolve_author_line(reicat)
    selected_pages = sorted(book_output.pages, key=lambda page: page.aligned)
    header = f"# {title}\n\n{author_line}\n\n".encode("utf-8")
    dest = book_output.output_dir / f"{book_output.slug}.md"
    _stream_book_md(dest, header=header, pages=selected_pages)
    return dest

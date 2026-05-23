from __future__ import annotations

import json
from pathlib import Path

from src.ingestion.output_writer import BookOutput, _atomic_write_bytes
from src.models.request import UsefulPagesEnumeration


def _resolve_title(book_output: BookOutput) -> str:
    try:
        data = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
        reicat = data.get("reicat")
        if isinstance(reicat, dict):
            title = reicat.get("titolo") or reicat.get("title")
            if title:
                return str(title).strip()
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        pass
    return book_output.slug


def build_toc_md(
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> Path:
    toc_pages = useful_pages_enumeration.toc_range_aligned.as_set()
    selected_pages = sorted(
        (page for page in book_output.pages if page.aligned in toc_pages),
        key=lambda page: page.aligned,
    )

    bodies: list[str] = []
    for page in selected_pages:
        if not page.file.is_file():
            raise FileNotFoundError(f"page md not found: {page.file}")
        bodies.append(page.file.read_text(encoding="utf-8"))

    title = _resolve_title(book_output)
    body = "\n\n---\n\n".join(bodies)
    content = f"# TOC — {title}\n\n{body}"
    dest = book_output.output_dir / "TOC.md"
    _atomic_write_bytes(dest, content.encode("utf-8"))
    return dest

from __future__ import annotations

import json
from pathlib import Path

from src.ingestion.output_writer import BookOutput, _atomic_write_bytes
from src.ingestion.markdown_artifacts import clean_markdown_channel_artifacts
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


def build_index_md(
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> Path:
    index_pages = useful_pages_enumeration.index_range_aligned.as_set()
    selected_pages = sorted(
        (page for page in book_output.pages if page.aligned in index_pages),
        key=lambda page: page.aligned,
    )

    bodies: list[str] = []
    for page in selected_pages:
        if not page.file.is_file():
            raise FileNotFoundError(f"page md not found: {page.file}")
        bodies.append(page.file.read_text(encoding="utf-8"))

    title = _resolve_title(book_output)
    body = "\n\n---\n\n".join(bodies)
    content = clean_markdown_channel_artifacts(f"# INDEX — {title}\n\n{body}")
    if content and not content.endswith("\n"):
        content += "\n"
    dest = book_output.output_dir / "INDEX.md"
    _atomic_write_bytes(dest, content.encode("utf-8"))
    return dest

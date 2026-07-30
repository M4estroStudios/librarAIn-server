from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.biblio_hash import compute_biblio_id
from src.ingestion.biblio_llm import extract_biblio_entries_for_page
from src.ingestion.output_writer import BookOutput, _atomic_write_bytes
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.models.polyindex_biblio import (
    BiblioCitation,
    BiblioNode,
    BiblioPageRef,
    BiblioReviewItem,
    PolyindexBiblioDocument,
)
from src.models.request import PageRange, ReicatMetadata, UsefulPagesEnumeration
from src.models.settings import Settings


def corpus_node_from_reicat(
    reicat: ReicatMetadata,
    *,
    source_sha256: str,
    slug: str | None = None,
) -> tuple[str, BiblioNode]:
    authors = ", ".join(reicat.authors)
    title = reicat.title
    year = reicat.publication_year
    node_id, _, _, _ = compute_biblio_id(authors, title, year)
    extras: dict[str, Any] = {}
    if reicat.publisher:
        extras["publisher"] = reicat.publisher
    if reicat.publication_place:
        extras["publication_place"] = reicat.publication_place
    if reicat.isbn:
        extras["isbn"] = reicat.isbn
    if reicat.edition_number:
        extras["edition_number"] = reicat.edition_number
    if reicat.subtitle:
        extras["subtitle"] = reicat.subtitle
    node = BiblioNode(
        authors=authors,
        title=title,
        year=year,
        extras=extras,
        in_corpus=True,
        source_sha256=source_sha256,
        slug=slug,
        incomplete=year is None,
    )
    return node_id, node


def book_biblio_path(book_output: BookOutput) -> Path:
    return book_output.output_dir / "BIBLIO.json"


def write_book_biblio(path: Path, payload: dict[str, Any]) -> Path:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if not content.endswith("\n"):
        content += "\n"
    _atomic_write_bytes(path, content.encode("utf-8"))
    return path


def load_book_biblio(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


async def build_book_biblio_from_pages(
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
    *,
    source_sha256: str,
    client: openai.OpenAI,
    settings: Settings,
    reicat: ReicatMetadata,
    request_id: str = "",
    prompt_notes: str | None = None,
    biblio_range_original: PageRange | None = None,
) -> dict[str, Any]:
    corpus_id, corpus_node = corpus_node_from_reicat(
        reicat,
        source_sha256=source_sha256,
        slug=book_output.slug,
    )
    range_aligned = useful_pages_enumeration.biblio_range_aligned
    entries: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []

    if range_aligned is not None:
        selected = sorted(
            (page for page in book_output.pages if page.aligned in range_aligned.as_set()),
            key=lambda page: page.aligned,
        )
        for page in selected:
            if not page.file.is_file():
                raise FileNotFoundError(f"page md not found: {page.file}")
            text = page.file.read_text(encoding="utf-8")
            page_entries = await extract_biblio_entries_for_page(
                text,
                client=client,
                settings=settings,
                request_id=request_id,
                aligned_page=page.aligned,
                prompt_notes=prompt_notes,
                source_sha256=source_sha256,
                book_slug=book_output.slug,
            )
            for item in page_entries:
                item = dict(item)
                item["aligned_page"] = page.aligned
                item["original_page"] = page.original
                if item.get("all_unknown"):
                    review_queue.append(
                        {
                            "source_sha256": source_sha256,
                            "aligned_page": page.aligned,
                            "original_page": page.original,
                            "line": item.get("line"),
                            "raw": item.get("raw"),
                            "authors": item.get("authors") or "unknown",
                            "title": item.get("title") or "unknown",
                            "year": item.get("year"),
                            "extras": item.get("extras") or {},
                        }
                    )
                    continue
                entries.append(item)

    payload = {
        "schema_version": "1.0",
        "source_sha256": source_sha256,
        "slug": book_output.slug,
        "node_id": corpus_id,
        "corpus_node": corpus_node.model_dump(mode="json"),
        "biblio_range_original": (
            biblio_range_original.model_dump() if biblio_range_original is not None else None
        ),
        "biblio_range_aligned": (
            range_aligned.model_dump() if range_aligned is not None else None
        ),
        "entries": entries,
        "review_queue": review_queue,
        "empty": range_aligned is not None and not entries and not review_queue,
    }
    dest = book_biblio_path(book_output)
    write_book_biblio(dest, payload)
    Log(
        INFO_LOG_LEVEL,
        "book BIBLIO.json written",
        {
            "path": str(dest),
            "entries": len(entries),
            "review_queue": len(review_queue),
        },
    )
    return payload


def sync_polyindex_biblio_from_book_payload(
    polyindex_dir: Path,
    source_sha256: str,
    payload: dict[str, Any],
) -> tuple[Path, dict[str, int]]:
    path = polyindex_dir / "BIBLIO.json"
    with polyindex_dir_lock(polyindex_dir, ".biblio.lock"):
        document = PolyindexBiblioDocument.load_file(path)
        document.purge_source_citations(source_sha256)
        for node_id, node in list(document.nodes.items()):
            if node.source_sha256 == source_sha256 and node.in_corpus:
                node.in_corpus = False
                node.source_sha256 = None
        document.prune_orphan_nodes()

        corpus_raw = payload.get("corpus_node") or {}
        corpus_id = str(payload.get("node_id") or "")
        if corpus_id and isinstance(corpus_raw, dict):
            document.upsert_node(corpus_id, BiblioNode.model_validate(corpus_raw))

        for item in payload.get("entries") or []:
            if not isinstance(item, dict):
                continue
            to_id = str(item.get("id") or "")
            if not to_id or not corpus_id:
                continue
            node = BiblioNode(
                authors=str(item.get("authors") or "unknown"),
                title=str(item.get("title") or "unknown"),
                year=item.get("year") if isinstance(item.get("year"), int) else None,
                extras=item.get("extras") if isinstance(item.get("extras"), dict) else {},
                incomplete=bool(item.get("incomplete")),
            )
            document.upsert_node(to_id, node)
            aligned = item.get("aligned_page")
            original = item.get("original_page")
            aligned_pages = [aligned] if isinstance(aligned, int) else []
            original_pages = [original] if isinstance(original, int) else []
            refs = []
            if isinstance(aligned, int):
                refs.append(
                    BiblioPageRef(
                        aligned_page=aligned,
                        original_page=original if isinstance(original, int) else None,
                        line=item.get("line") if isinstance(item.get("line"), int) else None,
                        raw=item.get("raw") if isinstance(item.get("raw"), str) else None,
                    )
                )
            document.add_citation(
                BiblioCitation(
                    from_id=corpus_id,
                    to_id=to_id,
                    source_sha256=source_sha256,
                    aligned_pages=aligned_pages,
                    original_pages=original_pages,
                    refs=refs,
                )
            )

        for item in payload.get("review_queue") or []:
            if not isinstance(item, dict):
                continue
            document.review_queue.append(BiblioReviewItem.model_validate(item))

        document.prune_orphan_nodes()
        document.write_atomic(path, sort_document=True)

    stats = {
        "n_entries": len(payload.get("entries") or []),
        "n_review": len(payload.get("review_queue") or []),
        "empty": 1 if payload.get("empty") else 0,
    }
    return path, stats


async def sync_polyindex_biblio_from_book(
    polyindex_dir: Path,
    source_sha256: str,
    book_output: BookOutput,
    useful_pages_enumeration: UsefulPagesEnumeration,
    *,
    client: openai.OpenAI,
    settings: Settings,
    reicat: ReicatMetadata,
    request_id: str = "",
    prompt_notes: str | None = None,
    biblio_range_original: PageRange | None = None,
) -> tuple[Path, dict[str, int], dict[str, Any]]:
    payload = await build_book_biblio_from_pages(
        book_output,
        useful_pages_enumeration,
        source_sha256=source_sha256,
        client=client,
        settings=settings,
        reicat=reicat,
        request_id=request_id,
        prompt_notes=prompt_notes,
        biblio_range_original=biblio_range_original,
    )
    path, stats = sync_polyindex_biblio_from_book_payload(
        polyindex_dir, source_sha256, payload
    )
    return path, stats, payload


def ensure_corpus_node_only(
    polyindex_dir: Path,
    *,
    source_sha256: str,
    reicat: ReicatMetadata,
    slug: str | None = None,
) -> Path:
    path = polyindex_dir / "BIBLIO.json"
    node_id, node = corpus_node_from_reicat(
        reicat, source_sha256=source_sha256, slug=slug
    )
    with polyindex_dir_lock(polyindex_dir, ".biblio.lock"):
        document = PolyindexBiblioDocument.load_file(path)
        document.upsert_node(node_id, node)
        document.write_atomic(path, sort_document=True)
    return path


def purge_biblio_aligned_page(
    polyindex_dir: Path,
    source_sha256: str,
    aligned_page: int,
) -> None:
    path = polyindex_dir / "BIBLIO.json"
    if not path.is_file():
        return
    with polyindex_dir_lock(polyindex_dir, ".biblio.lock"):
        document = PolyindexBiblioDocument.load_file(path)
        document.purge_aligned_page(source_sha256, aligned_page)
        document.write_atomic(path, sort_document=True)

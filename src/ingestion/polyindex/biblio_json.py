from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.biblio_hash import compute_biblio_id
from src.ingestion.biblio_llm import extract_biblio_entries_for_page, normalize_biblio_entry
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


def normalize_book_biblio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized_entries: list[dict[str, Any]] = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        normalized = normalize_biblio_entry(item)
        if normalized is None:
            continue
        for key in ("aligned_page", "original_page", "raw"):
            if key in item:
                normalized[key] = item[key]
        normalized_entries.append(normalized)
    payload = dict(payload)
    payload["entries"] = normalized_entries
    normalized_review: list[dict[str, Any]] = []
    for item in payload.get("review_queue") or []:
        if not isinstance(item, dict):
            continue
        normalized = normalize_biblio_entry(item)
        if normalized is None:
            continue
        review_item = dict(item)
        review_item["authors"] = normalized["authors"]
        review_item["title"] = normalized["title"]
        review_item["year"] = normalized["year"]
        review_item["extras"] = normalized["extras"]
        normalized_review.append(review_item)
    payload["review_queue"] = normalized_review
    return payload


def reconcile_polyindex_biblio_nodes(document: PolyindexBiblioDocument) -> bool:
    id_map: dict[str, str] = {}
    replacements: dict[str, tuple[str, BiblioNode]] = {}
    for old_id, node in list(document.nodes.items()):
        normalized = normalize_biblio_entry(
            {
                "authors": node.authors,
                "title": node.title,
                "year": node.year,
                "extras": dict(node.extras),
            }
        )
        if normalized is None:
            continue
        new_id = normalized["id"]
        new_node = BiblioNode(
            authors=normalized["authors"],
            title=normalized["title"],
            year=normalized["year"],
            extras=normalized["extras"],
            in_corpus=node.in_corpus,
            source_sha256=node.source_sha256,
            slug=node.slug,
            incomplete=bool(normalized["incomplete"]),
        )
        unchanged = (
            new_id == old_id
            and new_node.title == node.title
            and new_node.authors == node.authors
            and new_node.extras == node.extras
            and new_node.incomplete == node.incomplete
        )
        if unchanged:
            continue
        if new_id != old_id:
            id_map[old_id] = new_id
        replacements[old_id] = (new_id, new_node)
    if not replacements:
        return False
    for old_id in replacements:
        document.nodes.pop(old_id, None)
    for _old_id, (new_id, new_node) in replacements.items():
        existing = document.nodes.get(new_id)
        if existing is None:
            document.nodes[new_id] = new_node
            continue
        existing.authors = new_node.authors
        existing.title = new_node.title
        existing.year = new_node.year
        existing.extras = new_node.extras
        existing.incomplete = new_node.incomplete
        if new_node.in_corpus:
            existing.in_corpus = True
        if new_node.source_sha256:
            existing.source_sha256 = new_node.source_sha256
        if new_node.slug:
            existing.slug = new_node.slug
    for citation in document.citations:
        if citation.from_id in id_map:
            citation.from_id = id_map[citation.from_id]
        if citation.to_id in id_map:
            citation.to_id = id_map[citation.to_id]
    seen: set[tuple[str, str, str]] = set()
    deduped: list[BiblioCitation] = []
    for citation in document.citations:
        key = (citation.from_id, citation.to_id, citation.source_sha256)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(citation)
    document.citations = deduped
    document.prune_orphan_nodes()
    return True


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
    payload = normalize_book_biblio_payload(payload)
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


def ensure_polyindex_corpus_from_outputs(data_root: Path) -> None:
    output_root = data_root / "output"
    if not output_root.is_dir():
        return
    polyindex_dir = data_root / "polyindex"
    path = polyindex_dir / "BIBLIO.json"
    pending: list[tuple[str, BiblioNode]] = []
    for book_dir in sorted(output_root.iterdir()):
        if not book_dir.is_dir():
            continue
        source_sha256 = book_dir.name
        biblio_path = book_dir / "BIBLIO.json"
        if biblio_path.is_file():
            payload = load_book_biblio(biblio_path)
            corpus_raw = payload.get("corpus_node")
            corpus_id = str(payload.get("node_id") or "")
            if corpus_id and isinstance(corpus_raw, dict):
                pending.append((corpus_id, BiblioNode.model_validate(corpus_raw)))
                continue
        manifest_path = book_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(manifest, dict):
            continue
        reicat_raw = manifest.get("reicat")
        if not isinstance(reicat_raw, dict):
            continue
        try:
            reicat = ReicatMetadata.model_validate(reicat_raw)
        except Exception:
            continue
        slug = str(manifest.get("slug") or "") or None
        node_id, node = corpus_node_from_reicat(
            reicat,
            source_sha256=source_sha256,
            slug=slug,
        )
        pending.append((node_id, node))
    if not pending:
        return
    with polyindex_dir_lock(polyindex_dir, ".biblio.lock"):
        document = PolyindexBiblioDocument.load_file(path)
        changed = False
        for node_id, node in pending:
            existing = document.nodes.get(node_id)
            if existing is None or node.in_corpus:
                document.upsert_node(node_id, node)
                changed = True
        if changed:
            document.write_atomic(path, sort_document=True)


def ensure_polyindex_biblio_from_outputs(data_root: Path) -> None:
    output_root = data_root / "output"
    if not output_root.is_dir():
        return
    polyindex_dir = data_root / "polyindex"
    path = polyindex_dir / "BIBLIO.json"
    document = PolyindexBiblioDocument.load_file(path)
    for book_dir in sorted(output_root.iterdir()):
        if not book_dir.is_dir():
            continue
        source_sha256 = book_dir.name
        biblio_path = book_dir / "BIBLIO.json"
        if not biblio_path.is_file():
            continue
        payload = load_book_biblio(biblio_path)
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            continue
        has_citations = any(
            citation.source_sha256 == source_sha256 for citation in document.citations
        )
        if has_citations:
            continue
        sync_polyindex_biblio_from_book_payload(polyindex_dir, source_sha256, payload)
        document = PolyindexBiblioDocument.load_file(path)


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

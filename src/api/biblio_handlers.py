from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai

from src.core.hashing import validate_source_sha256
from src.core.log import INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client
from src.ingestion.biblio_hash import compute_biblio_id
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.pdf_alignment import build_page_removal_mapping
from src.ingestion.polyindex.biblio_json import sync_polyindex_biblio_from_book
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
from src.persistence.book_page_exclude import load_book_exclusions


class BiblioJobError(ValueError):
    pass


def _polyindex_path(data_root: Path) -> Path:
    return data_root / "polyindex" / "BIBLIO.json"


def load_biblio_document(data_root: Path) -> PolyindexBiblioDocument:
    return PolyindexBiblioDocument.load_file(_polyindex_path(data_root))


def search_biblio(
    data_root: Path,
    *,
    authors: str = "",
    title: str = "",
    year: str = "",
    entry_id: str = "",
    mode: str = "cita",
) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    authors_q = authors.strip().casefold()
    title_q = title.strip().casefold()
    year_q = year.strip()
    id_q = entry_id.strip().lower()

    matched_ids: list[str] = []
    for node_id, node in document.nodes.items():
        if id_q and id_q not in node_id.lower():
            continue
        if authors_q and authors_q not in node.authors.casefold():
            continue
        if title_q and title_q not in node.title.casefold():
            continue
        if year_q:
            if node.year is None or year_q not in str(node.year):
                continue
        matched_ids.append(node_id)

    results: list[dict[str, Any]] = []
    for node_id in matched_ids:
        node = document.nodes[node_id]
        if mode == "citato_da":
            related = [
                {
                    "id": citation.from_id,
                    "node": document.nodes.get(citation.from_id).model_dump(mode="json")
                    if citation.from_id in document.nodes
                    else None,
                    "aligned_pages": citation.aligned_pages,
                    "source_sha256": citation.source_sha256,
                }
                for citation in document.citations
                if citation.to_id == node_id
            ]
        else:
            related = [
                {
                    "id": citation.to_id,
                    "node": document.nodes.get(citation.to_id).model_dump(mode="json")
                    if citation.to_id in document.nodes
                    else None,
                    "aligned_pages": citation.aligned_pages,
                    "source_sha256": citation.source_sha256,
                }
                for citation in document.citations
                if citation.from_id == node_id
            ]
        results.append(
            {
                "id": node_id,
                "node": node.model_dump(mode="json"),
                "related": related,
                "mode": mode if mode in {"cita", "citato_da"} else "cita",
            }
        )
    return {"ok": True, "count": len(results), "results": results}


def list_biblio_review_queue(data_root: Path) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    items = [item.model_dump(mode="json") for item in document.sorted().review_queue]
    return {"ok": True, "count": len(items), "items": items}


def biblio_graph(data_root: Path) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    payload = document.graph_payload()
    payload["ok"] = True
    return payload


def _save_document(data_root: Path, document: PolyindexBiblioDocument) -> None:
    path = _polyindex_path(data_root)
    with polyindex_dir_lock(path.parent, ".biblio.lock"):
        document.write_atomic(path, sort_document=True)


def discard_review_item(
    data_root: Path,
    *,
    source_sha256: str,
    aligned_page: int,
    line: int | None,
    raw: str | None,
) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    before = len(document.review_queue)
    document.review_queue = [
        item
        for item in document.review_queue
        if not (
            item.source_sha256 == source_sha256
            and item.aligned_page == aligned_page
            and item.line == line
            and (raw is None or item.raw == raw)
        )
    ]
    _save_document(data_root, document)
    return {"ok": True, "removed": before - len(document.review_queue)}


def resolve_review_item(
    data_root: Path,
    *,
    source_sha256: str,
    aligned_page: int,
    line: int | None,
    raw: str | None,
    authors: str,
    title: str,
    year: int | None,
    extras: dict[str, Any] | None = None,
    link_to_id: str | None = None,
) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    review_item = None
    kept: list[BiblioReviewItem] = []
    for item in document.review_queue:
        if (
            item.source_sha256 == source_sha256
            and item.aligned_page == aligned_page
            and item.line == line
            and (raw is None or item.raw == raw)
            and review_item is None
        ):
            review_item = item
            continue
        kept.append(item)
    if review_item is None:
        raise BiblioJobError("review item not found")
    document.review_queue = kept

    from_id = None
    for node_id, node in document.nodes.items():
        if node.in_corpus and node.source_sha256 == source_sha256:
            from_id = node_id
            break
    if not from_id:
        raise BiblioJobError("corpus node not found for source_sha256")

    if link_to_id:
        to_id = link_to_id.strip().lower()
        if to_id not in document.nodes:
            raise BiblioJobError("link_to_id not found")
    else:
        to_id, _, _, _ = compute_biblio_id(authors, title, year)
        document.upsert_node(
            to_id,
            BiblioNode(
                authors=authors.strip() or "unknown",
                title=title.strip() or "unknown",
                year=year,
                extras=extras or {},
                incomplete=False,
            ),
        )

    document.add_citation(
        BiblioCitation(
            from_id=from_id,
            to_id=to_id,
            source_sha256=source_sha256,
            aligned_pages=[aligned_page],
            original_pages=[review_item.original_page]
            if isinstance(review_item.original_page, int)
            else [],
            refs=[
                BiblioPageRef(
                    aligned_page=aligned_page,
                    original_page=review_item.original_page,
                    line=line,
                    raw=raw or review_item.raw,
                )
            ],
        )
    )
    _save_document(data_root, document)
    return {"ok": True, "from_id": from_id, "to_id": to_id}


def update_biblio_node(
    data_root: Path,
    *,
    node_id: str,
    authors: str,
    title: str,
    year: int | None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document = load_biblio_document(data_root)
    old_id = node_id.strip().lower()
    if old_id not in document.nodes:
        raise BiblioJobError("node not found")
    old_node = document.nodes[old_id]
    new_id, _, _, _ = compute_biblio_id(authors, title, year)
    new_node = BiblioNode(
        authors=authors.strip() or "unknown",
        title=title.strip() or "unknown",
        year=year,
        extras=_merge_keep(old_node.extras, extras),
        in_corpus=old_node.in_corpus,
        source_sha256=old_node.source_sha256,
        slug=old_node.slug,
        incomplete=False,
    )
    if new_id != old_id:
        del document.nodes[old_id]
        if new_id in document.nodes:
            document.nodes[new_id].merge_from(
                authors=new_node.authors,
                title=new_node.title,
                year=new_node.year,
                extras=new_node.extras,
                in_corpus=new_node.in_corpus,
                source_sha256=new_node.source_sha256,
                slug=new_node.slug,
                incomplete=False,
            )
        else:
            document.nodes[new_id] = new_node
        for citation in document.citations:
            if citation.from_id == old_id:
                citation.from_id = new_id
            if citation.to_id == old_id:
                citation.to_id = new_id
    else:
        document.nodes[old_id] = new_node
    _save_document(data_root, document)
    return {"ok": True, "id": new_id, "node": document.nodes[new_id].model_dump(mode="json")}


def _merge_keep(base: dict[str, Any], incoming: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if not incoming:
        return merged
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        merged[key] = value
    return merged


def _load_reicat_from_manifest(manifest: dict[str, Any]) -> ReicatMetadata:
    reicat = manifest.get("reicat")
    if not isinstance(reicat, dict):
        raise BiblioJobError("manifest missing reicat")
    return ReicatMetadata.model_validate(reicat)


def _book_output_from_disk(data_root: Path, source_sha256: str) -> BookOutput:
    sha = validate_source_sha256(source_sha256)
    output_dir = data_root / "output" / sha
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BiblioJobError("manifest not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slug = str(manifest.get("slug") or "book")
    pages_dir = output_dir / "pages"
    pages: list[BookPageOutput] = []
    for entry in manifest.get("pages") or []:
        if not isinstance(entry, dict):
            continue
        aligned = entry.get("aligned")
        original = entry.get("original")
        if not isinstance(aligned, int):
            continue
        file_path = pages_dir / f"p.{aligned:04d}.{slug}.md"
        if not file_path.is_file():
            alt = entry.get("file")
            if isinstance(alt, str):
                candidate = output_dir / alt
                if candidate.is_file():
                    file_path = candidate
                else:
                    continue
            else:
                continue
        pages.append(
            BookPageOutput(
                aligned=aligned,
                original=original if isinstance(original, int) else aligned,
                file=file_path,
            )
        )
    return BookOutput(
        output_dir=output_dir,
        manifest_path=manifest_path,
        slug=slug,
        pages=pages,
    )


def run_biblio_only_job(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    biblio_range: PageRange,
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    sha = validate_source_sha256(source_sha256)
    book_output = _book_output_from_disk(data_root, sha)
    manifest = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
    reicat = _load_reicat_from_manifest(manifest)
    excluded_aligned, pages_to_remove = load_book_exclusions(
        data_root, sha, manifest=manifest
    )
    original_page_count = int(manifest.get("original_page_count") or 0)
    if original_page_count < 1:
        raise BiblioJobError("invalid original_page_count in manifest")
    if set(biblio_range.as_set()).intersection(pages_to_remove):
        raise BiblioJobError("biblio_range intersects pages_to_remove")
    aligned_total, o2a, a2o = build_page_removal_mapping(original_page_count, pages_to_remove)
    try:
        start_aligned = o2a[biblio_range.start]
        end_aligned = o2a[biblio_range.end]
    except KeyError as exc:
        raise BiblioJobError(f"biblio_range page not in useful pages: {exc.args[0]}") from exc
    useful = UsefulPagesEnumeration(
        source_sha256=sha,
        original_page_count=original_page_count,
        aligned_page_count=aligned_total,
        useful_original_pages=sorted(o2a.keys()),
        original_page_to_aligned_page=o2a,
        aligned_page_to_original_page=a2o,
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=1, end=1),
        biblio_range_aligned=PageRange(start=start_aligned, end=end_aligned),
    )
    openai_client = client or build_openai_client(settings)
    polyindex_dir = data_root / "polyindex"
    path, stats, payload = _run_async(
        sync_polyindex_biblio_from_book(
            polyindex_dir,
            sha,
            book_output,
            useful,
            client=openai_client,
            settings=settings,
            reicat=reicat,
            request_id=request_id or sha,
            prompt_notes=prompt_notes,
            biblio_range_original=biblio_range,
        )
    )
    manifest["biblio_range"] = biblio_range.model_dump()
    book_output.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if payload.get("empty"):
        raise BiblioJobError(
            "biblio range produced no entries; choose retry or manual review"
        )
    Log(
        INFO_LOG_LEVEL,
        "biblio-only job completed",
        {"source_sha256": sha[:16], "path": str(path), **stats},
    )
    return {
        "ok": True,
        "source_sha256": sha,
        "biblio_json_path": str(path),
        "excluded_aligned_ignored": excluded_aligned,
        **stats,
        "review_queue": payload.get("review_queue") or [],
    }


def _run_async(awaitable):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(awaitable)).result()

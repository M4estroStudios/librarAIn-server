from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1.0"
PolyindexBiblioSchemaVersion = Literal["1.0"]


def _dedupe_sort_pages(pages: list[int]) -> list[int]:
    return sorted(set(int(page) for page in pages if isinstance(page, int) and page > 0))


def _merge_extras(base: dict[str, Any], incoming: dict[str, Any] | None) -> dict[str, Any]:
    if not incoming:
        return dict(base)
    merged = dict(base)
    for key, value in incoming.items():
        if value is None or value == "" or value == []:
            continue
        existing = merged.get(key)
        if existing is None or existing == "" or existing == []:
            merged[key] = value
            continue
        if existing == value:
            continue
        if isinstance(existing, list):
            if value not in existing:
                existing.append(value)
            continue
        if isinstance(value, list):
            merged[key] = [existing, *[item for item in value if item != existing]]
            continue
        merged[key] = [existing, value]
    return merged


class BiblioPageRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aligned_page: int
    original_page: int | None = None
    line: int | None = None
    raw: str | None = None


class BiblioCitation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_id: str
    to_id: str
    source_sha256: str
    aligned_pages: list[int] = Field(default_factory=list)
    original_pages: list[int] = Field(default_factory=list)
    refs: list[BiblioPageRef] = Field(default_factory=list)

    def remove_aligned_page(self, aligned_page: int) -> bool:
        before = len(self.aligned_pages)
        self.aligned_pages = [page for page in self.aligned_pages if page != aligned_page]
        self.original_pages = [page for page in self.original_pages if page != aligned_page]
        self.refs = [ref for ref in self.refs if ref.aligned_page != aligned_page]
        return len(self.aligned_pages) != before or before > 0 and aligned_page not in self.aligned_pages


class BiblioReviewItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_sha256: str
    aligned_page: int
    original_page: int | None = None
    line: int | None = None
    raw: str | None = None
    authors: str = "unknown"
    title: str = "unknown"
    year: int | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class BiblioNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    authors: str
    title: str
    year: int | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    in_corpus: bool = False
    source_sha256: str | None = None
    slug: str | None = None
    incomplete: bool = False

    def merge_from(
        self,
        *,
        authors: str | None = None,
        title: str | None = None,
        year: int | None = None,
        extras: dict[str, Any] | None = None,
        in_corpus: bool | None = None,
        source_sha256: str | None = None,
        slug: str | None = None,
        incomplete: bool | None = None,
    ) -> None:
        if authors and (not self.authors or self.authors == "unknown"):
            self.authors = authors
        if title and (not self.title or self.title == "unknown"):
            self.title = title
        if year is not None and self.year is None:
            self.year = year
        self.extras = _merge_extras(self.extras, extras)
        if in_corpus:
            self.in_corpus = True
        if source_sha256:
            self.source_sha256 = source_sha256
        if slug:
            self.slug = slug
        if incomplete is not None:
            self.incomplete = bool(incomplete) and not (
                self.authors != "unknown" and self.title != "unknown" and self.year is not None
            )


class PolyindexBiblioDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: PolyindexBiblioSchemaVersion = SCHEMA_VERSION
    nodes: dict[str, BiblioNode] = Field(default_factory=dict)
    citations: list[BiblioCitation] = Field(default_factory=list)
    review_queue: list[BiblioReviewItem] = Field(default_factory=list)

    @classmethod
    def empty(cls) -> PolyindexBiblioDocument:
        return cls(schema_version=SCHEMA_VERSION)

    @classmethod
    def load_json(cls, raw: object) -> PolyindexBiblioDocument:
        if isinstance(raw, dict):
            try:
                return cls.model_validate(raw)
            except ValidationError:
                pass
        return cls.empty()

    @classmethod
    def load_file(cls, path: Path) -> PolyindexBiblioDocument:
        if not path.is_file():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls.empty()
        return cls.load_json(raw)

    def upsert_node(self, node_id: str, node: BiblioNode) -> None:
        existing = self.nodes.get(node_id)
        if existing is None:
            self.nodes[node_id] = node
            return
        existing.merge_from(
            authors=node.authors,
            title=node.title,
            year=node.year,
            extras=node.extras,
            in_corpus=node.in_corpus,
            source_sha256=node.source_sha256,
            slug=node.slug,
            incomplete=node.incomplete,
        )

    def add_citation(self, citation: BiblioCitation) -> None:
        for existing in self.citations:
            if (
                existing.from_id == citation.from_id
                and existing.to_id == citation.to_id
                and existing.source_sha256 == citation.source_sha256
            ):
                existing.aligned_pages = _dedupe_sort_pages(
                    existing.aligned_pages + citation.aligned_pages
                )
                existing.original_pages = _dedupe_sort_pages(
                    existing.original_pages + citation.original_pages
                )
                existing.refs.extend(citation.refs)
                return
        citation.aligned_pages = _dedupe_sort_pages(citation.aligned_pages)
        citation.original_pages = _dedupe_sort_pages(citation.original_pages)
        self.citations.append(citation)

    def purge_source_citations(self, source_sha256: str) -> None:
        self.citations = [
            citation for citation in self.citations if citation.source_sha256 != source_sha256
        ]
        self.review_queue = [
            item for item in self.review_queue if item.source_sha256 != source_sha256
        ]

    def purge_aligned_page(self, source_sha256: str, aligned_page: int) -> None:
        kept: list[BiblioCitation] = []
        for citation in self.citations:
            if citation.source_sha256 != source_sha256:
                kept.append(citation)
                continue
            citation.remove_aligned_page(aligned_page)
            if citation.aligned_pages or citation.refs:
                kept.append(citation)
        self.citations = kept
        self.review_queue = [
            item
            for item in self.review_queue
            if not (item.source_sha256 == source_sha256 and item.aligned_page == aligned_page)
        ]
        self.prune_orphan_nodes()

    def prune_orphan_nodes(self) -> None:
        referenced = {citation.from_id for citation in self.citations}
        referenced.update(citation.to_id for citation in self.citations)
        for node_id, node in list(self.nodes.items()):
            if node.in_corpus:
                continue
            if node_id not in referenced:
                del self.nodes[node_id]

    def sorted(self) -> PolyindexBiblioDocument:
        copy = self.model_copy(deep=True)
        copy.nodes = dict(sorted(copy.nodes.items()))
        copy.citations = sorted(
            copy.citations,
            key=lambda item: (item.from_id, item.to_id, item.source_sha256),
        )
        for citation in copy.citations:
            citation.aligned_pages = _dedupe_sort_pages(citation.aligned_pages)
            citation.original_pages = _dedupe_sort_pages(citation.original_pages)
        copy.review_queue = sorted(
            copy.review_queue,
            key=lambda item: (item.source_sha256, item.aligned_page, item.line or 0),
        )
        return copy

    def to_json_bytes(self, *, sort_document: bool = True) -> bytes:
        document = self.sorted() if sort_document else self
        return json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def write_atomic(self, path: Path, *, sort_document: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.to_json_bytes(sort_document=sort_document)
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            tmp_path.write_bytes(content)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.is_file():
                tmp_path.unlink(missing_ok=True)

    def graph_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "nodes": [
                {
                    "id": node_id,
                    "authors": node.authors,
                    "title": node.title,
                    "year": node.year,
                    "extras": node.extras,
                    "in_corpus": node.in_corpus,
                    "source_sha256": node.source_sha256,
                    "slug": node.slug,
                    "incomplete": node.incomplete,
                }
                for node_id, node in self.sorted().nodes.items()
            ],
            "edges": [
                {
                    "from": citation.from_id,
                    "to": citation.to_id,
                    "source_sha256": citation.source_sha256,
                    "aligned_pages": citation.aligned_pages,
                }
                for citation in self.sorted().citations
            ],
        }

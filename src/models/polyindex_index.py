from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1.0"
PolyindexIndexSchemaVersion = Literal["1.0"]


def _normalize_label_for_sort(raw: str) -> str:
    text = " ".join(raw.strip().split()).lower()
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return without_marks.rstrip(".,;:!?")


def _dedupe_sort_pages(pages: list[int]) -> list[int]:
    return sorted(set(pages))


class PolyindexIndexBookEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    slug: str | None = None
    aligned_pages: list[int] = Field(default_factory=list)
    original_pages: list[int] = Field(default_factory=list)

    def merge_pages(
        self,
        aligned_pages: list[int],
        original_pages: list[int],
        *,
        title: str | None = None,
        slug: str | None = None,
    ) -> None:
        if title is None:
            title = self.title
        if slug is None:
            slug = self.slug
        merged_aligned = list(self.aligned_pages)
        merged_original = list(self.original_pages)
        merged_aligned.extend(aligned_pages)
        merged_original.extend(original_pages)
        if title:
            self.title = title
        if slug:
            self.slug = slug
        self.aligned_pages = _dedupe_sort_pages(merged_aligned)
        self.original_pages = _dedupe_sort_pages(merged_original)


class PolyindexIndexSubjectEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_label: str
    aliases: list[str] = Field(default_factory=list)
    books: dict[str, PolyindexIndexBookEntry] = Field(default_factory=dict)

    def ensure_alias(self, alias_label: str) -> None:
        if alias_label.strip() == self.canonical_label.strip():
            return
        if alias_label not in self.aliases:
            self.aliases.append(alias_label)

    def merge_book_pages(
        self,
        source_sha256: str,
        aligned_pages: list[int],
        original_pages: list[int],
        *,
        book_title: str | None = None,
        book_slug: str | None = None,
    ) -> None:
        existing = self.books.get(source_sha256)
        if existing is None:
            existing = PolyindexIndexBookEntry()
            self.books[source_sha256] = existing
        existing.merge_pages(
            aligned_pages,
            original_pages,
            title=book_title,
            slug=book_slug,
        )


class PolyindexIndexDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: PolyindexIndexSchemaVersion = SCHEMA_VERSION
    subjects: dict[str, PolyindexIndexSubjectEntry] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> PolyindexIndexDocument:
        return cls(schema_version=SCHEMA_VERSION, subjects={})

    @classmethod
    def load_json(cls, raw: object) -> PolyindexIndexDocument:
        if isinstance(raw, dict):
            try:
                return cls.model_validate(raw)
            except ValidationError:
                pass
        return cls.empty()

    @classmethod
    def load_file(cls, path: Path) -> PolyindexIndexDocument:
        if not path.is_file():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls.empty()
        return cls.load_json(raw)

    def sorted(self) -> PolyindexIndexDocument:
        if not self.subjects:
            return self.model_copy(deep=True)

        def sort_key(item: tuple[str, PolyindexIndexSubjectEntry]) -> tuple[str, str]:
            canonical_id, entry = item
            return _normalize_label_for_sort(entry.canonical_label), canonical_id

        sorted_subjects: dict[str, PolyindexIndexSubjectEntry] = {}
        for canonical_id, entry in sorted(self.subjects.items(), key=sort_key):
            sorted_entry = entry.model_copy(deep=True)
            sorted_entry.aliases = sorted(
                sorted_entry.aliases,
                key=lambda alias: _normalize_label_for_sort(alias),
            )
            sorted_entry.books = dict(sorted(sorted_entry.books.items()))
            sorted_subjects[canonical_id] = sorted_entry
        return self.model_copy(update={"subjects": sorted_subjects})

    def as_matcher_state(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

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

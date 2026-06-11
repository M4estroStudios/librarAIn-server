from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1.0"
PolyindexTocSchemaVersion = Literal["1.0"]


class PolyindexTocChapter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    aligned_page_start: int
    aligned_page_end: int
    original_page_start: int
    original_page_end: int


class PolyindexTocBookEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    slug: str
    chapters: list[PolyindexTocChapter] = Field(default_factory=list)


class PolyindexTocDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: PolyindexTocSchemaVersion = SCHEMA_VERSION
    books: dict[str, PolyindexTocBookEntry] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> PolyindexTocDocument:
        return cls(schema_version=SCHEMA_VERSION, books={})

    @classmethod
    def load_json(cls, raw: object) -> PolyindexTocDocument:
        if isinstance(raw, dict):
            try:
                return cls.model_validate(raw)
            except ValidationError:
                pass
        return cls.empty()

    @classmethod
    def load_file(cls, path: Path) -> PolyindexTocDocument:
        if not path.is_file():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls.empty()
        return cls.load_json(raw)

    def upsert_book(self, source_sha256: str, book_entry: PolyindexTocBookEntry) -> None:
        self.schema_version = SCHEMA_VERSION
        self.books[source_sha256] = book_entry

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def write_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.to_json_bytes()
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            tmp_path.write_bytes(content)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.is_file():
                tmp_path.unlink(missing_ok=True)

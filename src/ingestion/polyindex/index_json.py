from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import openai

from src.core.log import Log, WARNING_LOG_LEVEL
from src.ingestion.polyindex.index_md_parser import RawSubject, parse_index_md
from src.ingestion.polyindex.subject_matcher import MatchDecision, match_subject
from src.models.request import UsefulPagesEnumeration
from src.models.settings import Settings

if sys.platform != "win32":
    import fcntl

SCHEMA_VERSION = "1.0"


def _empty_index_document() -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "subjects": {}}


def _atomic_write_json(dest: Path, payload: dict[str, object]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)


@contextmanager
def _index_file_lock(polyindex_dir: Path) -> Iterator[None]:
    polyindex_dir.mkdir(parents=True, exist_ok=True)
    lock_path = polyindex_dir / ".index.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if sys.platform != "win32":
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            pass  # TODO: Windows file locking when polyindex runs on win32
        try:
            yield
        finally:
            if sys.platform != "win32":
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _dedupe_sort_pages(pages: list[int]) -> list[int]:
    return sorted(set(pages))


def _merge_book_pages(
    entry: dict[str, Any],
    source_sha256: str,
    aligned_pages: list[int],
    original_pages: list[int],
) -> None:
    books = entry.get("books")
    if not isinstance(books, dict):
        books = {}
        entry["books"] = books
    existing = books.get(source_sha256)
    if isinstance(existing, dict):
        prior_aligned = existing.get("aligned_pages")
        prior_original = existing.get("original_pages")
        aligned_merged = list(prior_aligned) if isinstance(prior_aligned, list) else []
        original_merged = list(prior_original) if isinstance(prior_original, list) else []
    else:
        aligned_merged = []
        original_merged = []
    aligned_merged.extend(aligned_pages)
    original_merged.extend(original_pages)
    books[source_sha256] = {
        "aligned_pages": _dedupe_sort_pages(aligned_merged),
        "original_pages": _dedupe_sort_pages(original_merged),
    }


def _ensure_alias(entry: dict[str, Any], alias_label: str) -> None:
    aliases = entry.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
        entry["aliases"] = aliases
    canonical = entry.get("canonical_label")
    if isinstance(canonical, str) and alias_label.strip() == canonical.strip():
        return
    if alias_label not in aliases:
        aliases.append(alias_label)


def _apply_decision(
    document: dict[str, object],
    raw_subject: RawSubject,
    decision: MatchDecision,
    source_sha256: str,
) -> None:
    subjects = document.get("subjects")
    if not isinstance(subjects, dict):
        subjects = {}
        document["subjects"] = subjects

    if decision.action == "new":
        entry: dict[str, Any] = {
            "canonical_label": raw_subject.alias_of or raw_subject.raw_label,
            "aliases": [],
            "books": {},
        }
        subjects[decision.canonical_id] = entry
        if raw_subject.alias_of:
            _ensure_alias(entry, raw_subject.raw_label)
        _merge_book_pages(
            entry,
            source_sha256,
            raw_subject.aligned_pages,
            raw_subject.original_pages,
        )
        return

    entry_obj = subjects.get(decision.canonical_id)
    if not isinstance(entry_obj, dict):
        entry_obj = {
            "canonical_label": raw_subject.raw_label,
            "aliases": [],
            "books": {},
        }
        subjects[decision.canonical_id] = entry_obj

    if decision.action == "alias":
        _ensure_alias(entry_obj, raw_subject.raw_label)

    _merge_book_pages(
        entry_obj,
        source_sha256,
        raw_subject.aligned_pages,
        raw_subject.original_pages,
    )


def update_polyindex_index(
    polyindex_dir: Path,
    source_sha256: str,
    raw_subjects: list[RawSubject],
    client: openai.OpenAI,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    prompt_notes: str | None = None,
) -> tuple[Path, dict[str, int]]:
    """
    Merge book subject rows into polyindex/INDEX.json.

    Existing book entries for subjects no longer present in the current INDEX.md
    are intentionally preserved (historical cross-run retention).
    """
    index_path = polyindex_dir / "INDEX.json"
    stats = {"n_new": 0, "n_match": 0, "n_alias": 0}

    with _index_file_lock(polyindex_dir):
        if index_path.is_file():
            document = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                document = _empty_index_document()
        else:
            document = _empty_index_document()

        document["schema_version"] = SCHEMA_VERSION

        for raw_subject in raw_subjects:
            decision = match_subject(
                raw_subject,
                document,
                client,
                sqlite_path,
                settings,
                request_id,
                prompt_notes=prompt_notes,
            )
            _apply_decision(document, raw_subject, decision, source_sha256)
            if decision.action == "new":
                stats["n_new"] += 1
            elif decision.action == "match":
                stats["n_match"] += 1
            else:
                stats["n_alias"] += 1

        _atomic_write_json(index_path, document)

    return index_path, stats


def sync_polyindex_index_from_book(
    polyindex_dir: Path,
    source_sha256: str,
    index_md_path: Path,
    useful_pages_enumeration: UsefulPagesEnumeration,
    client: openai.OpenAI,
    sqlite_path: str,
    settings: Settings,
    request_id: str,
    *,
    prompt_notes: str | None = None,
) -> tuple[Path, dict[str, int]]:
    raw_subjects = parse_index_md(index_md_path, useful_pages_enumeration)
    if not raw_subjects:
        Log(
            WARNING_LOG_LEVEL,
            "polyindex index sync skipped: no subjects parsed",
            {"index_md_path": str(index_md_path), "request_id": request_id},
        )
        index_path = polyindex_dir / "INDEX.json"
        return index_path, {"n_new": 0, "n_match": 0, "n_alias": 0}

    return update_polyindex_index(
        polyindex_dir,
        source_sha256,
        raw_subjects,
        client,
        sqlite_path,
        settings,
        request_id,
        prompt_notes=prompt_notes,
    )

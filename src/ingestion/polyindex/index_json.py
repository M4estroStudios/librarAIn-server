from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    normalize_label,
    parse_index_md_with_skipped,
    write_skipped_lines_report,
)
from src.ingestion.polyindex.subject_matcher import (
    MatchDecision,
    allocate_canonical_id,
    find_exact_canonical,
    match_subject,
)
from src.models.polyindex_index import (
    SCHEMA_VERSION,
    PolyindexIndexBookEntry,
    PolyindexIndexDocument,
    PolyindexIndexSubjectEntry,
)
from src.search.poh_time_range import lookup_poh_time_range
from src.models.request import UsefulPagesEnumeration
from src.models.settings import Settings

def sorted_polyindex_index_bytes(raw_document: dict[str, object]) -> bytes:
    document = PolyindexIndexDocument.load_json(raw_document)
    return document.to_json_bytes(sort_document=True)


def sort_polyindex_index_file(index_path: Path) -> bool:
    if not index_path.is_file():
        return False
    with polyindex_dir_lock(index_path.parent, ".index.lock"):
        raw = index_path.read_bytes()
        document = PolyindexIndexDocument.load_file(index_path)
        content = document.to_json_bytes(sort_document=True)
        if content == raw:
            return False
        document.write_atomic(index_path, sort_document=True)
    return True


def _subjects_for_matcher(document: PolyindexIndexDocument) -> dict[str, dict[str, Any]]:
    return {
        canonical_id: entry.model_dump(mode="json")
        for canonical_id, entry in document.subjects.items()
    }


def _revalidate_decision(
    document: PolyindexIndexDocument,
    raw_subject: RawSubject,
    decision: MatchDecision,
) -> MatchDecision:
    subjects = _subjects_for_matcher(document)
    target_label = raw_subject.alias_of or raw_subject.raw_label
    normalized = normalize_label(target_label)

    if decision.action in ("match", "alias"):
        if decision.canonical_id in subjects:
            return decision
        existing = find_exact_canonical(subjects, normalized)
        if existing is not None:
            return MatchDecision(action=decision.action, canonical_id=existing)
        return MatchDecision(
            action="new",
            canonical_id=allocate_canonical_id(subjects, normalized),
        )

    existing_entry = document.subjects.get(decision.canonical_id)
    if existing_entry is not None:
        canonical_norm = normalize_label(existing_entry.canonical_label)
        if normalized == canonical_norm:
            return MatchDecision(action="match", canonical_id=decision.canonical_id)
        return MatchDecision(action="alias", canonical_id=decision.canonical_id)
    existing = find_exact_canonical(subjects, normalized)
    if existing is not None:
        return MatchDecision(action="match", canonical_id=existing)
    return decision


def _apply_decision(
    document: PolyindexIndexDocument,
    raw_subject: RawSubject,
    decision: MatchDecision,
    source_sha256: str,
    *,
    book_title: str | None = None,
    book_slug: str | None = None,
) -> None:
    if decision.action == "new":
        entry = PolyindexIndexSubjectEntry(
            canonical_label=raw_subject.alias_of or raw_subject.raw_label,
        )
        document.subjects[decision.canonical_id] = entry
        if raw_subject.alias_of:
            entry.ensure_alias(raw_subject.raw_label)
        entry.merge_book_pages(
            source_sha256,
            raw_subject.aligned_pages,
            raw_subject.original_pages,
            book_title=book_title,
            book_slug=book_slug,
        )
        return

    entry = document.subjects.get(decision.canonical_id)
    if entry is None:
        entry = PolyindexIndexSubjectEntry(canonical_label=raw_subject.raw_label)
        document.subjects[decision.canonical_id] = entry

    if decision.action == "alias":
        entry.ensure_alias(raw_subject.raw_label)

    entry.merge_book_pages(
        source_sha256,
        raw_subject.aligned_pages,
        raw_subject.original_pages,
        book_title=book_title,
        book_slug=book_slug,
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
    book_title: str | None = None,
    book_slug: str | None = None,
) -> tuple[Path, dict[str, int]]:
    index_path = polyindex_dir / "INDEX.json"
    stats = {"n_new": 0, "n_match": 0, "n_alias": 0}

    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        snapshot = PolyindexIndexDocument.load_file(index_path)

    decisions: list[tuple[RawSubject, MatchDecision]] = []
    for raw_subject in raw_subjects:
        decision = match_subject(
            raw_subject,
            snapshot.as_matcher_state(),
            client,
            sqlite_path,
            settings,
            request_id,
            prompt_notes=prompt_notes,
        )
        _apply_decision(
            snapshot,
            raw_subject,
            decision,
            source_sha256,
            book_title=book_title,
            book_slug=book_slug,
        )
        decisions.append((raw_subject, decision))

    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        document = PolyindexIndexDocument.load_file(index_path)
        document.schema_version = SCHEMA_VERSION

        for raw_subject, decision in decisions:
            final = _revalidate_decision(document, raw_subject, decision)
            _apply_decision(
                document,
                raw_subject,
                final,
                source_sha256,
                book_title=book_title,
                book_slug=book_slug,
            )
            if final.action == "new":
                stats["n_new"] += 1
            elif final.action == "match":
                stats["n_match"] += 1
            else:
                stats["n_alias"] += 1

        document.write_atomic(index_path, sort_document=True)

    return index_path, stats


def _resolve_subject_time_range(
    polyindex_dir: Path,
    canonical_id: str,
    entry: PolyindexIndexSubjectEntry,
) -> str | None:
    stored = (entry.time_range or "").strip()
    if stored:
        return stored
    return lookup_poh_time_range(polyindex_dir, canonical_id, entry.canonical_label)


def list_multibook_subjects(
    polyindex_dir: Path,
    *,
    min_books: int = 2,
) -> list[dict[str, Any]]:
    index_path = polyindex_dir / "INDEX.json"
    document = PolyindexIndexDocument.load_file(index_path)
    if not document.subjects:
        return []

    result: list[dict[str, Any]] = []
    for canonical_id, entry in document.subjects.items():
        if len(entry.books) < min_books:
            continue
        book_summaries = []
        for sha, book in sorted(entry.books.items()):
            book_summaries.append(
                {
                    "source_sha256": sha,
                    "title": book.title,
                    "slug": book.slug,
                    "page_count": len(book.aligned_pages),
                }
            )
        result.append(
            {
                "canonical_id": canonical_id,
                "canonical_label": entry.canonical_label,
                "aliases": list(entry.aliases),
                "book_count": len(entry.books),
                "books": book_summaries,
                "time_range": _resolve_subject_time_range(
                    polyindex_dir, canonical_id, entry
                ),
            }
        )
    result.sort(key=lambda item: (-item["book_count"], str(item["canonical_label"]).casefold()))
    return result


def get_polyindex_subject(polyindex_dir: Path, canonical_id: str) -> dict[str, Any] | None:
    index_path = polyindex_dir / "INDEX.json"
    document = PolyindexIndexDocument.load_file(index_path)
    entry = document.subjects.get(canonical_id)
    if entry is None:
        return None
    books: dict[str, Any] = {}
    for sha, book in sorted(entry.books.items()):
        books[sha] = {
            "source_sha256": sha,
            "title": book.title,
            "slug": book.slug,
            "aligned_pages": list(book.aligned_pages),
            "original_pages": list(book.original_pages),
            "page_count": len(book.aligned_pages),
        }
    return {
        "canonical_id": canonical_id,
        "canonical_label": entry.canonical_label,
        "aliases": list(entry.aliases),
        "book_count": len(entry.books),
        "books": books,
        "time_range": _resolve_subject_time_range(
            polyindex_dir, canonical_id, entry
        ),
    }


class SubjectMergeError(ValueError):
    pass


class SubjectUpdateError(ValueError):
    pass


def _load_subject_entry(
    document: PolyindexIndexDocument,
    canonical_id: str,
) -> PolyindexIndexSubjectEntry:
    entry = document.subjects.get(canonical_id)
    if entry is None:
        raise SubjectUpdateError(f"subject not found: {canonical_id}")
    return entry


def update_polyindex_subject_metadata(
    polyindex_dir: Path,
    canonical_id: str,
    *,
    aliases: list[str] | None = None,
    time_range: str | None = None,
    clear_time_range: bool = False,
) -> dict[str, Any]:
    index_path = polyindex_dir / "INDEX.json"
    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        if not index_path.is_file():
            raise SubjectUpdateError("INDEX.json not found")
        document = PolyindexIndexDocument.load_file(index_path)
        entry = _load_subject_entry(document, canonical_id)
        if aliases is not None:
            entry.set_aliases(aliases)
        if clear_time_range:
            entry.time_range = None
        elif time_range is not None:
            cleaned = time_range.strip()
            entry.time_range = cleaned or None
        document.write_atomic(index_path, sort_document=True)
    return get_polyindex_subject(polyindex_dir, canonical_id) or {}


def update_polyindex_subject_pages(
    polyindex_dir: Path,
    canonical_id: str,
    source_sha256: str,
    *,
    add_pages: list[int] | None = None,
    remove_pages: list[int] | None = None,
    book_title: str | None = None,
    book_slug: str | None = None,
) -> dict[str, Any]:
    sha = source_sha256.strip().lower()
    if not sha:
        raise SubjectUpdateError("source_sha256 is required")
    to_add = sorted({int(page) for page in (add_pages or []) if int(page) > 0})
    to_remove = sorted({int(page) for page in (remove_pages or []) if int(page) > 0})
    if not to_add and not to_remove:
        raise SubjectUpdateError("add_pages or remove_pages is required")

    index_path = polyindex_dir / "INDEX.json"
    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        if not index_path.is_file():
            raise SubjectUpdateError("INDEX.json not found")
        document = PolyindexIndexDocument.load_file(index_path)
        entry = _load_subject_entry(document, canonical_id)
        book = entry.books.get(sha)
        if book is None and to_remove:
            raise SubjectUpdateError(f"book not linked to subject: {sha}")
        if book is None:
            book = PolyindexIndexBookEntry()
            entry.books[sha] = book
        for aligned_page in to_remove:
            book.remove_aligned_page(aligned_page)
        if to_add:
            book.merge_pages(
                to_add,
                to_add,
                title=book_title,
                slug=book_slug,
            )
        if not book.aligned_pages:
            entry.books.pop(sha, None)
        document.write_atomic(index_path, sort_document=True)
    result = get_polyindex_subject(polyindex_dir, canonical_id)
    if result is None:
        raise SubjectUpdateError(f"subject not found after update: {canonical_id}")
    return result


def remove_polyindex_subject_book(
    polyindex_dir: Path,
    canonical_id: str,
    source_sha256: str,
) -> dict[str, Any]:
    sha = source_sha256.strip().lower()
    if not sha:
        raise SubjectUpdateError("source_sha256 is required")
    index_path = polyindex_dir / "INDEX.json"
    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        if not index_path.is_file():
            raise SubjectUpdateError("INDEX.json not found")
        document = PolyindexIndexDocument.load_file(index_path)
        entry = _load_subject_entry(document, canonical_id)
        if not entry.remove_book(sha):
            raise SubjectUpdateError(f"book not linked to subject: {sha}")
        document.write_atomic(index_path, sort_document=True)
    result = get_polyindex_subject(polyindex_dir, canonical_id)
    if result is None:
        raise SubjectUpdateError(f"subject not found after update: {canonical_id}")
    return result


def merge_polyindex_subjects(
    polyindex_dir: Path,
    target_id: str,
    source_ids: list[str],
) -> dict[str, Any]:
    cleaned_sources = [sid for sid in dict.fromkeys(source_ids) if sid and sid != target_id]
    if not cleaned_sources:
        raise SubjectMergeError("no valid source subjects to merge")

    index_path = polyindex_dir / "INDEX.json"
    with polyindex_dir_lock(polyindex_dir, ".index.lock"):
        if not index_path.is_file():
            raise SubjectMergeError("INDEX.json not found")
        document = PolyindexIndexDocument.load_file(index_path)
        if not document.subjects:
            raise SubjectMergeError("INDEX.json has no subjects")

        target = document.subjects.get(target_id)
        if target is None:
            raise SubjectMergeError(f"target subject not found: {target_id}")

        missing = [sid for sid in cleaned_sources if sid not in document.subjects]
        if missing:
            raise SubjectMergeError(f"source subjects not found: {', '.join(missing)}")

        for source_id in cleaned_sources:
            source = document.subjects[source_id]
            if source.canonical_label.strip():
                target.ensure_alias(source.canonical_label)
            for alias in source.aliases:
                if alias.strip():
                    target.ensure_alias(alias)
            for sha, book in source.books.items():
                target.merge_book_pages(
                    sha,
                    list(book.aligned_pages),
                    list(book.original_pages),
                    book_title=book.title,
                    book_slug=book.slug,
                )
            del document.subjects[source_id]

        document.write_atomic(index_path, sort_document=True)

    Log(
        INFO_LOG_LEVEL,
        "polyindex subjects merged",
        {
            "target_id": target_id,
            "source_ids": cleaned_sources,
            "merged_count": len(cleaned_sources),
        },
    )
    return {
        "target_id": target_id,
        "canonical_label": target.canonical_label,
        "aliases": list(target.aliases),
        "book_count": len(target.books),
        "merged_source_ids": cleaned_sources,
    }


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
    book_title: str | None = None,
    book_slug: str | None = None,
) -> tuple[Path, dict[str, int]]:
    raw_subjects, skipped = parse_index_md_with_skipped(
        index_md_path, useful_pages_enumeration
    )
    if skipped:
        Log(
            WARNING_LOG_LEVEL,
            "polyindex index sync skipped lines",
            {
                "index_md_path": str(index_md_path),
                "request_id": request_id,
                "skipped_count": len(skipped),
                "parsed_count": len(raw_subjects),
            },
        )
        write_skipped_lines_report(index_md_path, skipped)
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
        book_title=book_title,
        book_slug=book_slug,
    )

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import openai

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
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
    *,
    book_title: str | None = None,
    book_slug: str | None = None,
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
        if book_title is None and isinstance(existing.get("title"), str):
            book_title = existing["title"]
        if book_slug is None and isinstance(existing.get("slug"), str):
            book_slug = existing["slug"]
    else:
        aligned_merged = []
        original_merged = []
    aligned_merged.extend(aligned_pages)
    original_merged.extend(original_pages)
    book_entry: dict[str, Any] = {}
    if book_title:
        book_entry["title"] = book_title
    if book_slug:
        book_entry["slug"] = book_slug
    book_entry["aligned_pages"] = _dedupe_sort_pages(aligned_merged)
    book_entry["original_pages"] = _dedupe_sort_pages(original_merged)
    books[source_sha256] = book_entry


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


def _sort_index_document(document: dict[str, object]) -> dict[str, object]:
    subjects = document.get("subjects")
    if not isinstance(subjects, dict) or not subjects:
        return document

    def _subject_sort_key(item: tuple[str, object]) -> tuple[str, str]:
        canonical_id, entry = item
        label = ""
        if isinstance(entry, dict):
            canonical = entry.get("canonical_label")
            if isinstance(canonical, str):
                label = normalize_label(canonical)
        return label, canonical_id

    sorted_subjects: dict[str, object] = {}
    for canonical_id, entry in sorted(subjects.items(), key=_subject_sort_key):
        if not isinstance(entry, dict):
            sorted_subjects[canonical_id] = entry
            continue
        sorted_entry = dict(entry)
        aliases = sorted_entry.get("aliases")
        if isinstance(aliases, list):
            sorted_entry["aliases"] = sorted(
                aliases,
                key=lambda alias: normalize_label(str(alias)),
            )
        books = sorted_entry.get("books")
        if isinstance(books, dict):
            sorted_entry["books"] = dict(sorted(books.items()))
        sorted_subjects[canonical_id] = sorted_entry

    document["subjects"] = sorted_subjects
    return document


def sorted_polyindex_index_bytes(raw_document: dict[str, object]) -> bytes:
    sorted_doc = _sort_index_document(raw_document)
    return json.dumps(sorted_doc, ensure_ascii=False, indent=2).encode("utf-8")


def sort_polyindex_index_file(index_path: Path) -> bool:
    if not index_path.is_file():
        return False
    with _index_file_lock(index_path.parent):
        raw = index_path.read_bytes()
        document = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return False
        sorted_doc = _sort_index_document(document)
        content = json.dumps(sorted_doc, ensure_ascii=False, indent=2).encode("utf-8")
        if content == raw:
            return False
        _atomic_write_json(index_path, sorted_doc)
    return True


def _apply_decision(
    document: dict[str, object],
    raw_subject: RawSubject,
    decision: MatchDecision,
    source_sha256: str,
    *,
    book_title: str | None = None,
    book_slug: str | None = None,
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
            book_title=book_title,
            book_slug=book_slug,
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
        book_title=book_title,
        book_slug=book_slug,
    )


def _load_index_document(index_path: Path) -> dict[str, object]:
    if index_path.is_file():
        document = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            return document
    return _empty_index_document()


def _revalidate_decision(
    document: dict[str, object],
    raw_subject: RawSubject,
    decision: MatchDecision,
) -> MatchDecision:
    """Cheap re-validation of a decision against the live document.

    Decisions are computed outside the file lock against a snapshot; a
    concurrent ingest (or an admin merge) may have changed the document in
    the meantime. No network calls are allowed here.
    """
    subjects = document.get("subjects")
    if not isinstance(subjects, dict):
        subjects = {}
        document["subjects"] = subjects

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

    # action == "new": the chosen id may have been created concurrently.
    existing_entry = subjects.get(decision.canonical_id)
    if isinstance(existing_entry, dict):
        canonical_norm = normalize_label(
            str(existing_entry.get("canonical_label", target_label))
        )
        if normalized == canonical_norm:
            return MatchDecision(action="match", canonical_id=decision.canonical_id)
        return MatchDecision(action="alias", canonical_id=decision.canonical_id)
    existing = find_exact_canonical(subjects, normalized)
    if existing is not None:
        return MatchDecision(action="match", canonical_id=existing)
    return decision


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
    """
    Merge book subject rows into polyindex/INDEX.json.

    Existing book entries for subjects no longer present in the current INDEX.md
    are intentionally preserved (historical cross-run retention).

    The expensive matching (embeddings/LLM) runs against a snapshot WITHOUT
    holding the file lock; the lock is held only for the final cheap
    read-revalidate-merge-write step, so parallel ingests do not serialize on
    network calls.
    """
    index_path = polyindex_dir / "INDEX.json"
    stats = {"n_new": 0, "n_match": 0, "n_alias": 0}

    # Phase 1: snapshot (atomic writes via os.replace make this read safe).
    with _index_file_lock(polyindex_dir):
        snapshot = _load_index_document(index_path)

    # Phase 2: matching outside the lock. Decisions are applied to the local
    # snapshot as they are made so that later subjects can match earlier ones
    # from the same book.
    decisions: list[tuple[RawSubject, MatchDecision]] = []
    for raw_subject in raw_subjects:
        decision = match_subject(
            raw_subject,
            snapshot,
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

    # Phase 3: re-read, revalidate, merge and write under the lock.
    with _index_file_lock(polyindex_dir):
        document = _load_index_document(index_path)
        document["schema_version"] = SCHEMA_VERSION

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

        _atomic_write_json(index_path, _sort_index_document(document))

    return index_path, stats


def list_multibook_subjects(
    polyindex_dir: Path,
    *,
    min_books: int = 2,
) -> list[dict[str, Any]]:
    """Return subjects whose pages come from at least `min_books` books."""
    index_path = polyindex_dir / "INDEX.json"
    if not index_path.is_file():
        return []
    document = json.loads(index_path.read_text(encoding="utf-8"))
    subjects = document.get("subjects") if isinstance(document, dict) else None
    if not isinstance(subjects, dict):
        return []

    result: list[dict[str, Any]] = []
    for canonical_id, entry in subjects.items():
        if not isinstance(entry, dict):
            continue
        books = entry.get("books")
        if not isinstance(books, dict) or len(books) < min_books:
            continue
        book_summaries = []
        for sha, book in sorted(books.items()):
            if not isinstance(book, dict):
                continue
            aligned = book.get("aligned_pages")
            book_summaries.append(
                {
                    "source_sha256": sha,
                    "title": book.get("title"),
                    "slug": book.get("slug"),
                    "page_count": len(aligned) if isinstance(aligned, list) else 0,
                }
            )
        result.append(
            {
                "canonical_id": canonical_id,
                "canonical_label": entry.get("canonical_label"),
                "aliases": entry.get("aliases") or [],
                "book_count": len(books),
                "books": book_summaries,
            }
        )
    result.sort(key=lambda item: (-item["book_count"], str(item["canonical_label"]).casefold()))
    return result


class SubjectMergeError(ValueError):
    pass


def merge_polyindex_subjects(
    polyindex_dir: Path,
    target_id: str,
    source_ids: list[str],
) -> dict[str, Any]:
    """Merge the given source subjects into the target subject.

    Source canonical labels and aliases become aliases of the target; book
    pages are merged. Source entries are removed.
    """
    cleaned_sources = [sid for sid in dict.fromkeys(source_ids) if sid and sid != target_id]
    if not cleaned_sources:
        raise SubjectMergeError("no valid source subjects to merge")

    index_path = polyindex_dir / "INDEX.json"
    with _index_file_lock(polyindex_dir):
        if not index_path.is_file():
            raise SubjectMergeError("INDEX.json not found")
        document = json.loads(index_path.read_text(encoding="utf-8"))
        subjects = document.get("subjects") if isinstance(document, dict) else None
        if not isinstance(subjects, dict):
            raise SubjectMergeError("INDEX.json has no subjects")

        target = subjects.get(target_id)
        if not isinstance(target, dict):
            raise SubjectMergeError(f"target subject not found: {target_id}")

        missing = [sid for sid in cleaned_sources if not isinstance(subjects.get(sid), dict)]
        if missing:
            raise SubjectMergeError(f"source subjects not found: {', '.join(missing)}")

        for source_id in cleaned_sources:
            source = subjects[source_id]
            source_label = source.get("canonical_label")
            if isinstance(source_label, str) and source_label.strip():
                _ensure_alias(target, source_label)
            source_aliases = source.get("aliases")
            if isinstance(source_aliases, list):
                for alias in source_aliases:
                    if isinstance(alias, str) and alias.strip():
                        _ensure_alias(target, alias)
            source_books = source.get("books")
            if isinstance(source_books, dict):
                for sha, book in source_books.items():
                    if not isinstance(book, dict):
                        continue
                    aligned = book.get("aligned_pages")
                    original = book.get("original_pages")
                    _merge_book_pages(
                        target,
                        sha,
                        list(aligned) if isinstance(aligned, list) else [],
                        list(original) if isinstance(original, list) else [],
                        book_title=book.get("title") if isinstance(book.get("title"), str) else None,
                        book_slug=book.get("slug") if isinstance(book.get("slug"), str) else None,
                    )
            del subjects[source_id]

        _atomic_write_json(index_path, _sort_index_document(document))

    Log(
        INFO_LOG_LEVEL,
        "polyindex subjects merged",
        {
            "target_id": target_id,
            "source_ids": cleaned_sources,
            "merged_count": len(cleaned_sources),
        },
    )
    books = target.get("books")
    return {
        "target_id": target_id,
        "canonical_label": target.get("canonical_label"),
        "aliases": target.get("aliases") or [],
        "book_count": len(books) if isinstance(books, dict) else 0,
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

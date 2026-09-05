from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.index_md_links import (
    build_first_index_source_page_by_label,
    clean_match_label,
    page_href,
    rewrite_index_md_with_page_links,
)
from src.ingestion.index_page_subject_links import (
    _llm_link_subject_on_page,
    _record_index_connection,
    _sanitize_llm_subject_links,
    link_subject_mentions_in_page,
)
from src.ingestion.output_writer import (
    BookOutput,
    BookPageOutput,
    _atomic_write_bytes,
    load_book_index_document,
    merge_page_index_connections,
    split_page_frontmatter,
    strip_page_frontmatter,
    unwrap_links_to_page_hrefs,
)
from src.ingestion.polyindex.index_md_parser import RawSubject, normalize_label, parse_index_md
from src.ingestion.progress import (
    PHASE_POLYINDEX_INDEX,
    STATUS_PAGE_PROGRESS,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
)
from src.models.polyindex_index import BookIndexDocument, BookIndexSubjectEntry
from src.models.request import UsefulPagesEnumeration
from src.models.settings import Settings

_EXISTING_PAGE_LINK_PATTERN = re.compile(r"\[(\d+)\]\([^)]+\)")


def book_index_json_path(output_dir: Path, slug: str) -> Path:
    return output_dir / f"INDEX_{slug}.json"


def canonical_book_index_json_path(output_dir: Path) -> Path:
    """Stable per-book artifact name, alongside the legacy slugged filename."""
    return output_dir / "INDEX_BOOK.json"


def allocate_subject_keys(subjects: list[RawSubject]) -> list[tuple[RawSubject, str]]:
    used: set[str] = set()
    allocated: list[tuple[RawSubject, str]] = []
    for subject in subjects:
        base = normalize_label(clean_match_label(subject.raw_label)) or "subject"
        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        allocated.append((subject, key))
    return allocated


def _subjects_by_content_page(
    subject_keys: list[tuple[RawSubject, str]],
    first_index_page: dict[str, int],
    slug: str,
    index_pages: set[int],
) -> dict[int, list[tuple[str, str]]]:
    by_page: dict[int, list[tuple[str, str]]] = {}
    for subject, _key in subject_keys:
        label = clean_match_label(subject.raw_label)
        norm = normalize_label(label)
        source_x = first_index_page.get(norm)
        if source_x is None:
            continue
        href = page_href(source_x, slug, from_pages_dir=True)
        for aligned in subject.aligned_pages:
            if aligned in index_pages:
                continue
            by_page.setdefault(aligned, []).append((label, href))
    for aligned, items in by_page.items():
        dedup: dict[str, tuple[str, str]] = {}
        for label, href in items:
            dedup[normalize_label(label)] = (label, href)
        by_page[aligned] = list(dedup.values())
    return by_page


def build_book_index_document(
    subject_keys: list[tuple[RawSubject, str]],
    first_index_page: dict[str, int] | None = None,
) -> BookIndexDocument:
    source_map = first_index_page or {}
    subjects: dict[str, BookIndexSubjectEntry] = {}
    page_subjects: dict[str, list[str]] = defaultdict(list)
    for subject, key in subject_keys:
        label = clean_match_label(subject.raw_label)
        aliases = [subject.alias_of] if subject.alias_of else []
        subjects[key] = BookIndexSubjectEntry(
            canonical_label=label,
            aliases=aliases,
            aligned_pages=list(subject.aligned_pages),
            original_pages=list(subject.original_pages),
            source_index_page=source_map.get(normalize_label(label)),
            global_ref=None,
        )
        for aligned in subject.aligned_pages:
            page_key = str(aligned)
            if key not in page_subjects[page_key]:
                page_subjects[page_key].append(key)
    sorted_pages = {
        page: keys
        for page, keys in sorted(page_subjects.items(), key=lambda item: int(item[0]))
    }
    return BookIndexDocument(
        subjects=dict(sorted(subjects.items())),
        page_subjects=sorted_pages,
    )


def _merge_book_index_document(
    existing: BookIndexDocument,
    update: BookIndexDocument,
) -> BookIndexDocument:
    subjects = dict(existing.subjects)
    subjects.update(update.subjects)
    page_subjects = dict(existing.page_subjects)
    for page, keys in update.page_subjects.items():
        merged = list(page_subjects.get(page, []))
        for key in keys:
            if key not in merged:
                merged.append(key)
        page_subjects[page] = merged
    return BookIndexDocument(
        subjects=subjects,
        page_subjects=page_subjects,
    )


def _rejoin_page_text(original_text: str, new_body: str) -> str:
    fm, _old_body = split_page_frontmatter(original_text)
    body = new_body if new_body.endswith("\n") else new_body + "\n"
    if fm is None:
        return body
    return f"---{fm if fm.startswith(chr(10)) else chr(10) + fm}---\n{body.lstrip(chr(10))}"


async def _link_one_content_page(
    *,
    aligned: int,
    subject_items: list[tuple[str, str]],
    page: BookPageOutput | None,
    client: Any | None,
    settings: Settings,
    request_id: str,
    reset_content_pages: bool,
    index_target_hrefs: set[str],
) -> dict[str, int]:
    result = {"regex_links": 0, "llm_links": 0, "unresolved": 0, "pages_updated": 0}
    if page is None or not page.file.is_file():
        result["unresolved"] = len(subject_items)
        return result
    original_page_text = page.file.read_text(encoding="utf-8")
    page_body = strip_page_frontmatter(original_page_text)
    if reset_content_pages:
        page_body = unwrap_links_to_page_hrefs(page_body, index_target_hrefs)
    href_by_label = {label: href for label, href in subject_items}
    updated, report = link_subject_mentions_in_page(page_body, subject_items)
    result["regex_links"] = len(report.success_regex)
    for label in report.failed_regex.copy():
        if label in report.regex_resolved_labels:
            continue
        llm_text = await _llm_link_subject_on_page(
            client,
            settings,
            page_text=updated,
            label=label,
            href=href_by_label[label],
            request_id=request_id,
            aligned_page=aligned,
        )
        sanitized = (
            _sanitize_llm_subject_links(
                updated,
                llm_text,
                label=label,
                href=href_by_label[label],
            )
            if llm_text is not None
            else None
        )
        if sanitized is None:
            report.failed_ai.append(label)
            continue
        updated, new_visible = sanitized
        for visible in new_visible:
            _record_index_connection(report, label, visible, via_ai=True)
        result["llm_links"] += len(new_visible)
    result["unresolved"] = len(report.failed_ai)
    merged_text = merge_page_index_connections(
        _rejoin_page_text(original_page_text, updated),
        aligned_page=aligned,
        original_page=page.original,
        index_connections=report,
    )
    if merged_text != original_page_text:
        _atomic_write_bytes(page.file, merged_text.encode("utf-8"))
        result["pages_updated"] = 1
    return result


async def apply_index_cross_links(
    index_md_path: Path,
    book_output: BookOutput,
    useful_pages: UsefulPagesEnumeration,
    *,
    client: Any | None,
    settings: Settings,
    request_id: str = "",
    progress: ProgressReporter | None = None,
    max_subjects: int | None = None,
    parallel_pages: bool = True,
) -> dict[str, int | str]:
    import asyncio

    from src.core.parallel import gather_cancellable
    from src.ingestion.polyindex.index_md_parser import parse_index_md

    def emit(status: str, *, counts_as_step: bool = False, **fields: Any) -> None:
        if progress is None:
            return
        progress(
            make_event(
                PHASE_POLYINDEX_INDEX,
                status,
                counts_as_step=counts_as_step,
                **fields,
            )
        )

    stats: dict[str, int | str] = {
        "subjects": 0,
        "index_lines_linked": 0,
        "index_pages_updated": 0,
        "pages_updated": 0,
        "regex_links": 0,
        "llm_links": 0,
        "unresolved": 0,
        "index_json_path": "",
        "parallel_pages": int(parallel_pages),
    }
    index_json_path = book_index_json_path(book_output.output_dir, book_output.slug)
    all_subjects = [s for s in parse_index_md(index_md_path, useful_pages) if s.aligned_pages]
    content_subjects = all_subjects
    if max_subjects is not None and max_subjects > 0:
        content_subjects = all_subjects[:max_subjects]
    stats["subjects"] = len(content_subjects)
    if not all_subjects:
        Log(
            WARNING_LOG_LEVEL,
            "index cross links skipped: no subjects",
            {"request_id": request_id, "index_md_path": str(index_md_path)},
        )
        emit(STATUS_STARTED, page_total=1, message="Nessun soggetto INDEX da collegare")
        emit(
            STATUS_PAGE_PROGRESS,
            counts_as_step=True,
            page_index=1,
            page_total=1,
            message="Regex INDEX: nessun soggetto",
        )
        empty_document = BookIndexDocument()
        empty_document.write_atomic(index_json_path)
        empty_document.write_atomic(canonical_book_index_json_path(book_output.output_dir))
        stats["index_json_path"] = str(index_json_path)
        return stats

    index_subject_keys = allocate_subject_keys(all_subjects)
    content_subject_keys = allocate_subject_keys(content_subjects)
    first_index_page = build_first_index_source_page_by_label(book_output, useful_pages)
    index_page_set = useful_pages.index_range_aligned.as_set()
    o2a = useful_pages.original_page_to_aligned_page
    a2o = useful_pages.aligned_page_to_original_page
    pages_by_aligned = {page.aligned: page for page in book_output.pages}
    index_pages = [
        aligned
        for aligned in sorted(index_page_set)
        if (page := pages_by_aligned.get(aligned)) is not None and page.file.is_file()
    ]
    by_page = _subjects_by_content_page(
        content_subject_keys, first_index_page, book_output.slug, index_page_set
    )
    content_pages = sorted(by_page.keys())
    page_total = 1 + len(index_pages) + len(content_pages)
    step = 0
    emit(
        STATUS_STARTED,
        page_total=page_total,
        message=f"Regex INDEX ({len(all_subjects)}) + collegamento {len(content_pages)} pagine contenuto",
    )

    original_text = index_md_path.read_text(encoding="utf-8")
    rewritten = rewrite_index_md_with_page_links(
        original_text,
        index_subject_keys,
        slug=book_output.slug,
        original_to_aligned=o2a,
        aligned_to_original=a2o,
        from_pages_dir=False,
    )
    if rewritten != original_text:
        _atomic_write_bytes(index_md_path, rewritten.encode("utf-8"))
        stats["index_lines_linked"] = sum(
            1 for line in rewritten.splitlines() if _EXISTING_PAGE_LINK_PATTERN.search(line)
        )
    step += 1
    emit(
        STATUS_PAGE_PROGRESS,
        counts_as_step=True,
        page_index=step,
        page_total=page_total,
        message=f"Regex INDEX: {len(all_subjects)} soggetti",
    )

    index_pages_updated = 0
    for aligned in index_pages:
        page = pages_by_aligned[aligned]
        original_page_text = page.file.read_text(encoding="utf-8")
        page_body = strip_page_frontmatter(original_page_text)
        updated = rewrite_index_md_with_page_links(
            page_body,
            index_subject_keys,
            slug=book_output.slug,
            original_to_aligned=o2a,
            aligned_to_original=a2o,
            from_pages_dir=True,
        )
        if updated != page_body:
            _atomic_write_bytes(
                page.file,
                _rejoin_page_text(original_page_text, updated).encode("utf-8"),
            )
            index_pages_updated += 1
        step += 1
        emit(
            STATUS_PAGE_PROGRESS,
            counts_as_step=True,
            page_index=step,
            page_total=page_total,
            aligned_page=aligned,
            message=f"Collegamento pagina indice {aligned}",
        )
    stats["index_pages_updated"] = index_pages_updated

    reset_content_pages = True
    index_target_hrefs = {
        page_href(aligned, book_output.slug, from_pages_dir=True)
        for aligned in index_page_set
    }
    step_lock = asyncio.Lock()

    async def _run_content(aligned: int) -> dict[str, int]:
        return await _link_one_content_page(
            aligned=aligned,
            subject_items=by_page[aligned],
            page=pages_by_aligned.get(aligned),
            client=client,
            settings=settings,
            request_id=request_id,
            reset_content_pages=reset_content_pages,
            index_target_hrefs=index_target_hrefs,
        )

    if parallel_pages and content_pages:
        sem = asyncio.Semaphore(max(1, int(settings.max_parallel_request)))

        async def _guarded(aligned: int) -> tuple[int, dict[str, int]]:
            nonlocal step
            async with sem:
                outcome = await _run_content(aligned)
            async with step_lock:
                step += 1
                page_index = step
                for key in ("regex_links", "llm_links", "unresolved", "pages_updated"):
                    stats[key] = int(stats[key]) + int(outcome[key])
            emit(
                STATUS_PAGE_PROGRESS,
                counts_as_step=True,
                page_index=page_index,
                page_total=page_total,
                aligned_page=aligned,
                message=f"Collegamento pagina {aligned}",
            )
            return aligned, outcome

        await gather_cancellable(*(_guarded(aligned) for aligned in content_pages))
    else:
        for aligned in content_pages:
            outcome = await _run_content(aligned)
            for key in ("regex_links", "llm_links", "unresolved", "pages_updated"):
                stats[key] = int(stats[key]) + int(outcome[key])
            step += 1
            emit(
                STATUS_PAGE_PROGRESS,
                counts_as_step=True,
                page_index=step,
                page_total=page_total,
                aligned_page=aligned,
                message=f"Collegamento pagina {aligned}",
            )

    if max_subjects is None:
        document = build_book_index_document(index_subject_keys, first_index_page)
    else:
        document = build_book_index_document(content_subject_keys, first_index_page)
        if index_json_path.is_file():
            document = _merge_book_index_document(
                load_book_index_document(index_json_path),
                document,
            )
    document.write_atomic(index_json_path)
    document.write_atomic(canonical_book_index_json_path(book_output.output_dir))
    stats["index_json_path"] = str(index_json_path)

    Log(
        INFO_LOG_LEVEL,
        "index cross links completed",
        {"request_id": request_id, **stats},
    )
    return stats


def audit_index_cross_links_readiness(
    index_md_path: Path,
    book_output: BookOutput,
    useful_pages: UsefulPagesEnumeration,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    stats: dict[str, int] = {
        "subjects_total": 0,
        "subjects_with_aligned_pages": 0,
        "index_pages_expected": 0,
        "index_pages_missing": 0,
        "content_pages_expected": 0,
        "content_pages_missing": 0,
    }
    if not index_md_path.is_file():
        issues.append("INDEX.md missing")
        return {"ok": False, "issues": issues, "warnings": warnings, "stats": stats}
    all_subjects = parse_index_md(index_md_path, useful_pages)
    with_aligned = [subject for subject in all_subjects if subject.aligned_pages]
    stats["subjects_total"] = len(all_subjects)
    stats["subjects_with_aligned_pages"] = len(with_aligned)
    if not with_aligned:
        issues.append("no INDEX subjects with aligned pages")
    index_page_set = useful_pages.index_range_aligned.as_set()
    stats["index_pages_expected"] = len(index_page_set)
    pages_by_aligned = {page.aligned: page for page in book_output.pages}
    missing_index_pages = [
        aligned
        for aligned in sorted(index_page_set)
        if not (pages_by_aligned.get(aligned) is not None and pages_by_aligned[aligned].file.is_file())
    ]
    stats["index_pages_missing"] = len(missing_index_pages)
    if missing_index_pages:
        issues.append(
            f"missing {len(missing_index_pages)} index page files "
            f"(e.g. {missing_index_pages[:5]})"
        )
    if with_aligned:
        subject_keys = allocate_subject_keys(with_aligned)
        first_index_page = build_first_index_source_page_by_label(book_output, useful_pages)
        by_page = _subjects_by_content_page(
            subject_keys,
            first_index_page,
            book_output.slug,
            index_page_set,
        )
        stats["content_pages_expected"] = len(by_page)
        missing_content_pages = [
            aligned
            for aligned in sorted(by_page.keys())
            if not (pages_by_aligned.get(aligned) is not None and pages_by_aligned[aligned].file.is_file())
        ]
        stats["content_pages_missing"] = len(missing_content_pages)
        if missing_content_pages:
            sample = missing_content_pages[:5]
            warnings.append(
                f"{len(missing_content_pages)} content pages missing files (e.g. {sample})"
            )
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "stats": stats,
    }

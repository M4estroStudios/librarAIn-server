from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.output_writer import (
    BookOutput,
    _atomic_write_bytes,
    _page_filename,
    strip_page_frontmatter,
)
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    _parse_original_pages,
    _try_parse_vedi_line,
    _try_split_label_and_pages,
    normalize_label,
    strip_index_cross_link_markup,
)
from src.models.polyindex_index import BookIndexDocument, BookIndexSubjectEntry
from src.models.request import UsefulPagesEnumeration
from src.models.settings import Settings

POLYINDEX_REF_PLACEHOLDER = "polyindex:pending"

_LIST_PREFIX_PATTERN = re.compile(r"^([ \t]*[-*][ \t]+)")
_EXISTING_PAGE_LINK_PATTERN = re.compile(r"\[(\d+)\]\([^)]+\)")
_MD_LINK_OR_CODE_PATTERN = re.compile(
    r"\[[^\]]*\]\([^)]+\)|`[^`]+`|<a\b[^>]*>.*?</a>",
    re.IGNORECASE | re.DOTALL,
)
_LLM_SUBJECT_LINK_PROMPT = """You edit book page markdown.
Wrap every clear occurrence of the given subject label with a markdown link to the provided href.
Rules:
- Keep all other text identical.
- The visible link text must stay exactly as written on the page.
- Use the provided href unchanged as the link destination.
- If a label is already inside a markdown link, leave that occurrence unchanged.
- Do not add HTML anchors or fragment identifiers.
- If the label is not present, return exactly: __NO_MATCH__
- Output only the full page markdown (or __NO_MATCH__), no commentary.
"""


def book_index_json_path(output_dir: Path, slug: str) -> Path:
    return output_dir / f"INDEX_{slug}.json"


def _clean_match_label(raw_label: str) -> str:
    return _LIST_PREFIX_PATTERN.sub("", raw_label).strip()


def _md_href(target: str) -> str:
    if re.search(r"[\s()]", target):
        return f"<{target}>"
    return target


def allocate_subject_keys(subjects: list[RawSubject]) -> list[tuple[RawSubject, str]]:
    used: set[str] = set()
    allocated: list[tuple[RawSubject, str]] = []
    for subject in subjects:
        base = normalize_label(_clean_match_label(subject.raw_label)) or "subject"
        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        allocated.append((subject, key))
    return allocated


def _page_href_from_index(aligned_page: int, slug: str) -> str:
    return f"pages/{_page_filename(aligned_page, slug)}"


def _linkify_pages_part(
    pages_part: str,
    original_to_aligned: dict[int, int],
    slug: str,
) -> str:
    plain = _EXISTING_PAGE_LINK_PATTERN.sub(r"\1", pages_part)
    pieces: list[str] = []
    cursor = 0
    for match in re.finditer(
        r"(\d+\s*[-\u2013\u2014]\s*\d+|\d+)|([^0-9]+)",
        plain,
    ):
        token = match.group(0)
        if match.group(1):
            pages = _parse_original_pages(token)
            linked: list[str] = []
            for original in pages:
                aligned = original_to_aligned.get(original)
                if aligned is None:
                    linked.append(str(original))
                    continue
                linked.append(f"[{original}]({_page_href_from_index(aligned, slug)})")
            pieces.append(", ".join(linked) if linked else token)
        else:
            pieces.append(token)
        cursor = match.end()
    if cursor == 0:
        return pages_part
    return "".join(pieces)


def _rewrite_index_line(
    line: str,
    *,
    slug: str,
    original_to_aligned: dict[int, int],
    key_queues: dict[str, deque[str]],
    label_queues: dict[str, deque[str]],
) -> str:
    stripped = strip_index_cross_link_markup(line).strip()
    if not stripped:
        return line
    if _try_parse_vedi_line(stripped) is not None:
        return stripped
    split = _try_split_label_and_pages(stripped)
    if split is None:
        return stripped
    raw_label, pages_part = split
    list_prefix = ""
    prefix_match = _LIST_PREFIX_PATTERN.match(stripped)
    label_text = raw_label
    if prefix_match:
        list_prefix = prefix_match.group(1)
        label_text = _clean_match_label(raw_label)
    norm = normalize_label(label_text)
    queue = key_queues.get(norm)
    label_queue = label_queues.get(norm)
    if not queue or not label_queue:
        return stripped
    queue.popleft()
    index_label = label_queue.popleft()
    linked_pages = _linkify_pages_part(pages_part, original_to_aligned, slug)
    linked_label = f"[{index_label}]({_md_href(POLYINDEX_REF_PLACEHOLDER)})"
    separator = ", "
    if f" {pages_part}" in stripped or stripped.endswith(pages_part):
        for candidate in (" — ", " – ", "—", "–", "; ", ": ", ", "):
            probe = f"{raw_label}{candidate}"
            if stripped.startswith(probe) or stripped.startswith(f"{list_prefix}{label_text}{candidate}"):
                separator = candidate
                break
    return f"{list_prefix}{linked_label}{separator}{linked_pages}"


def rewrite_index_md_with_page_links(
    index_text: str,
    subject_keys: list[tuple[RawSubject, str]],
    *,
    slug: str,
    original_to_aligned: dict[int, int],
) -> str:
    key_queues: dict[str, deque[str]] = defaultdict(deque)
    label_queues: dict[str, deque[str]] = defaultdict(deque)
    for subject, key in subject_keys:
        label = _clean_match_label(subject.raw_label)
        norm = normalize_label(label)
        key_queues[norm].append(key)
        label_queues[norm].append(label)

    lines_out: list[str] = []
    for line in index_text.splitlines():
        lines_out.append(
            _rewrite_index_line(
                line,
                slug=slug,
                original_to_aligned=original_to_aligned,
                key_queues=key_queues,
                label_queues=label_queues,
            )
        )
    body = "\n".join(lines_out)
    if index_text.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return body


def _protected_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MD_LINK_OR_CODE_PATTERN.finditer(text)]


def _span_overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for span_start, span_end in spans:
        if start < span_end and end > span_start:
            return True
    return False


def _label_regex(label: str) -> re.Pattern[str] | None:
    cleaned = _clean_match_label(label)
    if not cleaned:
        return None
    parts = [re.escape(part) for part in cleaned.split() if part]
    if not parts:
        return None
    body = r"\s+".join(parts)
    return re.compile(rf"(?<!\w)({body})(?!\w)", re.IGNORECASE)


def link_subject_mentions_in_page(
    page_text: str,
    subjects: list[tuple[str, str]],
) -> tuple[str, list[str]]:
    text = page_text
    unresolved: list[str] = []
    ordered = sorted(subjects, key=lambda item: len(item[0]), reverse=True)
    for label, _key in ordered:
        pattern = _label_regex(label)
        if pattern is None:
            unresolved.append(label)
            continue
        href = _md_href(label)
        protected = _protected_spans(text)
        matches = [
            m
            for m in pattern.finditer(text)
            if not _span_overlaps(m.start(), m.end(), protected)
        ]
        if not matches:
            unresolved.append(label)
            continue
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            pieces.append(text[cursor : match.start()])
            pieces.append(f"[{match.group(1)}]({href})")
            cursor = match.end()
        pieces.append(text[cursor:])
        text = "".join(pieces)
    return text, unresolved


async def _llm_link_subject_on_page(
    client: Any,
    settings: Settings,
    *,
    page_text: str,
    label: str,
    request_id: str,
    aligned_page: int,
) -> str | None:
    model = settings.editor_model
    if not model or client is None:
        return None
    href = _md_href(label)
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(_LLM_SUBJECT_LINK_PROMPT, None),
        },
        {
            "role": "user",
            "content": (
                f"subject_label: {label}\n"
                f"href: {href}\n\n"
                f"page_markdown:\n{page_text}"
            ),
        },
    ]
    try:
        result = await chat_completion_with_retry(
            client,
            model=model,
            messages=messages,
            temperature=0.0,
            max_tokens=min(8000, max(1000, len(page_text) + 200)),
            request_id=request_id,
            stage="index_cross_links",
            page=aligned_page,
        )
    except Exception as exc:
        Log(
            WARNING_LOG_LEVEL,
            "index cross link llm failed",
            {
                "request_id": request_id,
                "aligned_page": aligned_page,
                "label": label,
                "error": str(exc),
            },
        )
        return None
    stripped = result.strip()
    if not stripped or stripped == "__NO_MATCH__":
        return None
    return result if result.endswith("\n") else result + "\n"


def _subjects_by_aligned_page(
    subject_keys: list[tuple[RawSubject, str]],
) -> dict[int, list[tuple[str, str]]]:
    by_page: dict[int, list[tuple[str, str]]] = {}
    for subject, key in subject_keys:
        label = _clean_match_label(subject.raw_label)
        for aligned in subject.aligned_pages:
            by_page.setdefault(aligned, []).append((label, key))
    for aligned, items in by_page.items():
        dedup: dict[str, tuple[str, str]] = {}
        for label, key in items:
            dedup[key] = (label, key)
        by_page[aligned] = list(dedup.values())
    return by_page


def build_book_index_document(
    subject_keys: list[tuple[RawSubject, str]],
) -> BookIndexDocument:
    subjects: dict[str, BookIndexSubjectEntry] = {}
    page_subjects: dict[str, list[str]] = defaultdict(list)
    for subject, key in subject_keys:
        label = _clean_match_label(subject.raw_label)
        aliases = [subject.alias_of] if subject.alias_of else []
        subjects[key] = BookIndexSubjectEntry(
            canonical_label=label,
            aliases=aliases,
            aligned_pages=list(subject.aligned_pages),
            original_pages=list(subject.original_pages),
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


async def apply_index_cross_links(
    index_md_path: Path,
    book_output: BookOutput,
    useful_pages: UsefulPagesEnumeration,
    *,
    client: Any | None,
    settings: Settings,
    request_id: str = "",
) -> dict[str, int | str]:
    from src.ingestion.polyindex.index_md_parser import parse_index_md

    stats: dict[str, int | str] = {
        "subjects": 0,
        "index_lines_linked": 0,
        "pages_updated": 0,
        "regex_links": 0,
        "llm_links": 0,
        "unresolved": 0,
        "index_json_path": "",
    }
    index_json_path = book_index_json_path(book_output.output_dir, book_output.slug)
    subjects = parse_index_md(index_md_path, useful_pages)
    subjects = [s for s in subjects if s.aligned_pages]
    stats["subjects"] = len(subjects)
    if not subjects:
        Log(
            WARNING_LOG_LEVEL,
            "index cross links skipped: no subjects",
            {"request_id": request_id, "index_md_path": str(index_md_path)},
        )
        BookIndexDocument().write_atomic(index_json_path)
        stats["index_json_path"] = str(index_json_path)
        return stats

    subject_keys = allocate_subject_keys(subjects)
    original_text = index_md_path.read_text(encoding="utf-8")
    rewritten = rewrite_index_md_with_page_links(
        original_text,
        subject_keys,
        slug=book_output.slug,
        original_to_aligned=useful_pages.original_page_to_aligned_page,
    )
    pages_by_aligned = {page.aligned: page for page in book_output.pages}
    by_page = _subjects_by_aligned_page(subject_keys)
    page_updates: dict[int, str] = {}

    for aligned, subject_items in sorted(by_page.items()):
        page = pages_by_aligned.get(aligned)
        if page is None or not page.file.is_file():
            stats["unresolved"] = int(stats["unresolved"]) + len(subject_items)
            continue
        original_page_text = page.file.read_text(encoding="utf-8")
        page_body = strip_page_frontmatter(original_page_text)
        updated, unresolved = link_subject_mentions_in_page(
            page_body, subject_items
        )
        stats["regex_links"] = int(stats["regex_links"]) + len(subject_items) - len(unresolved)

        still_unresolved: list[str] = []
        for label in unresolved:
            llm_text = await _llm_link_subject_on_page(
                client,
                settings,
                page_text=updated,
                label=label,
                request_id=request_id,
                aligned_page=aligned,
            )
            if llm_text is None or llm_text == updated:
                still_unresolved.append(label)
                continue
            updated = llm_text
            stats["llm_links"] = int(stats["llm_links"]) + 1

        stats["unresolved"] = int(stats["unresolved"]) + len(still_unresolved)
        if updated != page_body:
            page_updates[aligned] = updated

    document = build_book_index_document(subject_keys)

    if rewritten != original_text:
        _atomic_write_bytes(index_md_path, rewritten.encode("utf-8"))
        stats["index_lines_linked"] = sum(
            1 for line in rewritten.splitlines() if _EXISTING_PAGE_LINK_PATTERN.search(line)
        )
    for aligned, content in page_updates.items():
        page = pages_by_aligned[aligned]
        _atomic_write_bytes(page.file, content.encode("utf-8"))
    stats["pages_updated"] = len(page_updates)
    document.write_atomic(index_json_path)
    stats["index_json_path"] = str(index_json_path)

    Log(
        INFO_LOG_LEVEL,
        "index cross links completed",
        {"request_id": request_id, **stats},
    )
    return stats

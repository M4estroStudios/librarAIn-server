from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.core.text import slugify
from src.ingestion.output_writer import BookOutput, _atomic_write_bytes, _page_filename
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    _parse_original_pages,
    _try_parse_vedi_line,
    _try_split_label_and_pages,
    normalize_label,
    strip_index_cross_link_markup,
)
from src.models.request import UsefulPagesEnumeration
from src.models.settings import Settings

_LIST_PREFIX_PATTERN = re.compile(r"^([ \t]*[-*][ \t]+)")
_EXISTING_PAGE_LINK_PATTERN = re.compile(r"\[(\d+)\]\([^)]+\)")
_MD_LINK_OR_CODE_PATTERN = re.compile(
    r"\[[^\]]*\]\([^)]+\)|`[^`]+`|<a\b[^>]*>.*?</a>",
    re.IGNORECASE | re.DOTALL,
)

_LLM_SUBJECT_LINK_PROMPT = """You edit book page markdown.
Wrap the first clear occurrence of the given subject label with a markdown link to the provided href.
Rules:
- Change only that one occurrence; keep all other text identical.
- If the label is already inside a markdown link, return the text unchanged.
- If the label is not present, return exactly: __NO_MATCH__
- Output only the full page markdown (or __NO_MATCH__), no commentary.
"""


def _clean_match_label(raw_label: str) -> str:
    return _LIST_PREFIX_PATTERN.sub("", raw_label).strip()


def _subject_anchor_id(raw_label: str, used: set[str]) -> str:
    base = "idx-" + (slugify(_clean_match_label(raw_label)) or "subject")
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _page_href_from_index(aligned_page: int, slug: str) -> str:
    return f"pages/{_page_filename(aligned_page, slug)}"


def _index_href_from_page(anchor_id: str) -> str:
    return f"../INDEX.md#{anchor_id}"


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
    anchor_by_norm: dict[str, str],
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
    anchor = anchor_by_norm.get(normalize_label(label_text))
    linked_pages = _linkify_pages_part(pages_part, original_to_aligned, slug)
    anchor_html = f'<a id="{anchor}"></a>' if anchor else ""
    separator = ", "
    if f" {pages_part}" in stripped or stripped.endswith(pages_part):
        for candidate in (" — ", " – ", "—", "–", "; ", ": ", ", "):
            probe = f"{raw_label}{candidate}"
            if stripped.startswith(probe) or stripped.startswith(f"{list_prefix}{label_text}{candidate}"):
                separator = candidate
                break
    return f"{list_prefix}{anchor_html}{label_text}{separator}{linked_pages}"


def rewrite_index_md_with_page_links(
    index_text: str,
    subjects: list[RawSubject],
    *,
    slug: str,
    original_to_aligned: dict[int, int],
) -> tuple[str, dict[str, str]]:
    used_anchors: set[str] = set()
    anchor_by_norm: dict[str, str] = {}
    for subject in subjects:
        if not subject.aligned_pages:
            continue
        label = _clean_match_label(subject.raw_label)
        norm = normalize_label(label)
        if norm not in anchor_by_norm:
            anchor_by_norm[norm] = _subject_anchor_id(label, used_anchors)

    lines_out: list[str] = []
    for line in index_text.splitlines():
        lines_out.append(
            _rewrite_index_line(
                line,
                slug=slug,
                original_to_aligned=original_to_aligned,
                anchor_by_norm=anchor_by_norm,
            )
        )
    body = "\n".join(lines_out)
    if index_text.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return body, anchor_by_norm


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
    """Link subject labels to INDEX anchors. Returns (new_text, unresolved_labels)."""
    text = page_text
    unresolved: list[str] = []
    ordered = sorted(subjects, key=lambda item: len(item[0]), reverse=True)
    for label, anchor_id in ordered:
        pattern = _label_regex(label)
        if pattern is None:
            unresolved.append(label)
            continue
        href = _index_href_from_page(anchor_id)
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
    href: str,
    request_id: str,
    aligned_page: int,
) -> str | None:
    model = settings.editor_model
    if not model or client is None:
        return None
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
    subjects: list[RawSubject],
    anchor_by_norm: dict[str, str],
) -> dict[int, list[tuple[str, str]]]:
    by_page: dict[int, list[tuple[str, str]]] = {}
    for subject in subjects:
        label = _clean_match_label(subject.raw_label)
        anchor = anchor_by_norm.get(normalize_label(label))
        if not anchor or not subject.aligned_pages:
            continue
        for aligned in subject.aligned_pages:
            by_page.setdefault(aligned, []).append((label, anchor))
    for aligned, items in by_page.items():
        dedup: dict[str, tuple[str, str]] = {}
        for label, anchor in items:
            dedup[normalize_label(label)] = (label, anchor)
        by_page[aligned] = list(dedup.values())
    return by_page


async def apply_index_cross_links(
    index_md_path: Path,
    book_output: BookOutput,
    useful_pages: UsefulPagesEnumeration,
    *,
    client: Any | None,
    settings: Settings,
    request_id: str = "",
) -> dict[str, int]:
    from src.ingestion.polyindex.index_md_parser import parse_index_md

    stats = {
        "subjects": 0,
        "index_lines_linked": 0,
        "pages_updated": 0,
        "regex_links": 0,
        "llm_links": 0,
        "unresolved": 0,
    }
    subjects = parse_index_md(index_md_path, useful_pages)
    subjects = [s for s in subjects if s.aligned_pages]
    stats["subjects"] = len(subjects)
    if not subjects:
        Log(
            WARNING_LOG_LEVEL,
            "index cross links skipped: no subjects",
            {"request_id": request_id, "index_md_path": str(index_md_path)},
        )
        return stats

    original_text = index_md_path.read_text(encoding="utf-8")
    rewritten, anchor_by_norm = rewrite_index_md_with_page_links(
        original_text,
        subjects,
        slug=book_output.slug,
        original_to_aligned=useful_pages.original_page_to_aligned_page,
    )
    if rewritten != original_text:
        _atomic_write_bytes(index_md_path, rewritten.encode("utf-8"))
        stats["index_lines_linked"] = sum(
            1 for line in rewritten.splitlines() if _EXISTING_PAGE_LINK_PATTERN.search(line)
        )

    pages_by_aligned = {page.aligned: page for page in book_output.pages}
    by_page = _subjects_by_aligned_page(subjects, anchor_by_norm)

    for aligned, subject_items in sorted(by_page.items()):
        page = pages_by_aligned.get(aligned)
        if page is None or not page.file.is_file():
            stats["unresolved"] += len(subject_items)
            continue
        original_page_text = page.file.read_text(encoding="utf-8")
        updated, unresolved = link_subject_mentions_in_page(
            original_page_text, subject_items
        )
        stats["regex_links"] += len(subject_items) - len(unresolved)

        still_unresolved: list[str] = []
        for label in unresolved:
            anchor = anchor_by_norm.get(normalize_label(label))
            if not anchor:
                still_unresolved.append(label)
                continue
            href = _index_href_from_page(anchor)
            llm_text = await _llm_link_subject_on_page(
                client,
                settings,
                page_text=updated,
                label=label,
                href=href,
                request_id=request_id,
                aligned_page=aligned,
            )
            if llm_text is None or llm_text == updated:
                still_unresolved.append(label)
                continue
            updated = llm_text
            stats["llm_links"] += 1

        stats["unresolved"] += len(still_unresolved)
        if updated != original_page_text:
            _atomic_write_bytes(page.file, updated.encode("utf-8"))
            stats["pages_updated"] += 1

    Log(
        INFO_LOG_LEVEL,
        "index cross links completed",
        {"request_id": request_id, **stats},
    )
    return stats

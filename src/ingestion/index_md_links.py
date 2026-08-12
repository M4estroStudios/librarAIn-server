from __future__ import annotations

import re
from collections import defaultdict, deque

from src.ingestion.output_writer import BookOutput, _page_filename, strip_page_frontmatter
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    _is_skippable_index_line,
    _parse_original_pages,
    _try_parse_vedi_line,
    _try_split_label_and_pages,
    join_split_index_lines,
    normalize_label,
    strip_index_cross_link_markup,
)
from src.models.request import UsefulPagesEnumeration

POLYINDEX_REF_PLACEHOLDER = "polyindex:pending"

_LIST_PREFIX_PATTERN = re.compile(r"^([ \t]*[-*][ \t]+)")
_EXISTING_PAGE_LINK_PATTERN = re.compile(r"\[(\d+)\]\([^)]+\)")
_INDEX_ARTICLE_PREFIX = re.compile(
    r"^(?:a|al|alla|allo|ai|agli|alle|ad|all['\u2019])\s+",
    re.IGNORECASE,
)
_PAGE_PATH_IN_HREF = re.compile(r"(?:^|/)p\.\d{4}\.")
_MALFORMED_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def clean_match_label(raw_label: str) -> str:
    return _LIST_PREFIX_PATTERN.sub("", raw_label).strip()


def label_match_variants(label: str) -> list[str]:
    cleaned = clean_match_label(label)
    if not cleaned:
        return []
    variants = [cleaned]
    stripped = _INDEX_ARTICLE_PREFIX.sub("", cleaned).strip()
    if stripped and stripped != cleaned:
        variants.append(stripped)
    return variants


def is_page_markdown_href(href: str) -> bool:
    clean = href.strip().strip("<>")
    return bool(_PAGE_PATH_IN_HREF.search(clean))


def subject_href_lookup(subjects: list[tuple[str, str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for label, href in subjects:
        for variant in label_match_variants(label):
            lookup.setdefault(normalize_label(variant), href)
    return lookup


def repair_placeholder_subject_links(
    page_text: str,
    subjects: list[tuple[str, str]],
) -> str:
    lookup = subject_href_lookup(subjects)

    def _repl(match: re.Match[str]) -> str:
        visible = match.group(1)
        href = match.group(2).strip().strip("<>")
        if is_page_markdown_href(href):
            return match.group(0)
        target = lookup.get(normalize_label(clean_match_label(visible)))
        if target is None:
            return match.group(0)
        return f"[{visible}]({md_href(target)})"

    return _MALFORMED_MD_LINK.sub(_repl, page_text)


def md_href(target: str) -> str:
    if re.search(r"[\s()]", target):
        return f"<{target}>"
    return target


def page_href(aligned_page: int, slug: str, *, from_pages_dir: bool) -> str:
    name = _page_filename(aligned_page, slug)
    return name if from_pages_dir else f"pages/{name}"


def linkify_pages_part(
    pages_part: str,
    original_to_aligned: dict[int, int],
    slug: str,
    *,
    from_pages_dir: bool,
    aligned_to_original: dict[int, int] | None = None,
) -> str:
    valid_aligned = set(original_to_aligned.values())
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
            for aligned in pages:
                if aligned not in valid_aligned:
                    linked.append(str(aligned))
                    continue
                linked.append(
                    f"[{aligned}]({page_href(aligned, slug, from_pages_dir=from_pages_dir)})"
                )
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
    aligned_to_original: dict[int, int],
    key_queues: dict[str, deque[str]],
    label_queues: dict[str, deque[str]],
    from_pages_dir: bool,
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
        label_text = clean_match_label(raw_label)
    norm = normalize_label(label_text)
    queue = key_queues.get(norm)
    label_queue = label_queues.get(norm)
    if not queue or not label_queue:
        return line
    queue.popleft()
    index_label = label_queue.popleft()
    aligned_to_original = aligned_to_original or {v: k for k, v in original_to_aligned.items()}
    linked_pages = linkify_pages_part(
        pages_part,
        original_to_aligned,
        slug,
        from_pages_dir=from_pages_dir,
        aligned_to_original=aligned_to_original,
    )
    linked_label = f"[{index_label}]({md_href(POLYINDEX_REF_PLACEHOLDER)})"
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
    aligned_to_original: dict[int, int] | None = None,
    from_pages_dir: bool = False,
) -> str:
    a2o = aligned_to_original or {v: k for k, v in original_to_aligned.items()}
    key_queues: dict[str, deque[str]] = defaultdict(deque)
    label_queues: dict[str, deque[str]] = defaultdict(deque)
    for subject, key in subject_keys:
        label = clean_match_label(subject.raw_label)
        norm = normalize_label(label)
        key_queues[norm].append(key)
        label_queues[norm].append(label)

    normalized = join_split_index_lines(index_text)
    lines_out: list[str] = []
    for line in normalized.splitlines():
        lines_out.append(
            _rewrite_index_line(
                line,
                slug=slug,
                original_to_aligned=original_to_aligned,
                aligned_to_original=a2o,
                key_queues=key_queues,
                label_queues=label_queues,
                from_pages_dir=from_pages_dir,
            )
        )
    body = "\n".join(lines_out)
    if normalized.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return body


def build_first_index_source_page_by_label(
    book_output: BookOutput,
    useful_pages: UsefulPagesEnumeration,
) -> dict[str, int]:
    index_set = useful_pages.index_range_aligned.as_set()
    first: dict[str, int] = {}
    for page in sorted(book_output.pages, key=lambda item: item.aligned):
        if page.aligned not in index_set or not page.file.is_file():
            continue
        body = join_split_index_lines(
            strip_page_frontmatter(page.file.read_text(encoding="utf-8"))
        )
        for line in body.splitlines():
            stripped = strip_index_cross_link_markup(line).strip()
            if _is_skippable_index_line(stripped):
                continue
            vedi = _try_parse_vedi_line(stripped)
            if vedi is not None:
                norm = normalize_label(vedi[0])
                if norm:
                    first.setdefault(norm, page.aligned)
                continue
            split = _try_split_label_and_pages(stripped)
            if split is None:
                continue
            raw_label, pages_part = split
            if not _parse_original_pages(pages_part):
                continue
            norm = normalize_label(clean_match_label(raw_label))
            if norm:
                first.setdefault(norm, page.aligned)
    return first

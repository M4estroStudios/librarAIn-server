from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from src.core.log import Log, WARNING_LOG_LEVEL
from src.models.request import UsefulPagesEnumeration

EM_DASH = "\u2014"
EN_DASH = "\u2013"
VEDI_PATTERN = re.compile(r"^(.+?)\s+vedi\s+(.+)$", re.IGNORECASE)
PAGE_TOKEN_SPLIT = re.compile(r"[,.\s]+")
RANGE_TOKEN_PATTERN = re.compile(r"^(\d+)\s*[-\u2013\u2014]\s*(\d+)$")
SINGLE_PAGE_PATTERN = re.compile(r"^(\d+)$")
# Fallback for lines without an explicit separator: a label (containing at
# least one letter) followed by a trailing run of page numbers/ranges.
TRAILING_PAGES_PATTERN = re.compile(
    r"^(?P<label>.*[^\W\d_])[\s,;:]+(?P<pages>\d[\d\s,.\u2013\u2014-]*)$"
)


@dataclass(frozen=True)
class RawSubject:
    raw_label: str
    original_pages: list[int]
    aligned_pages: list[int]
    alias_of: str | None = None


@dataclass(frozen=True)
class SkippedIndexLine:
    line: str
    reason: str


def _is_skippable_index_line(stripped: str) -> bool:
    if not stripped:
        return True
    if stripped == "---":
        return True
    if stripped.startswith("# INDEX"):
        return True
    if stripped.startswith("#"):
        return True
    return False


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


_INDEX_ANCHOR_PATTERN = re.compile(r'<a id="[^"]*"></a>')
_INDEX_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_NON_URL_SAFE = re.compile(r"[^a-z0-9]+")


def strip_index_cross_link_markup(text: str) -> str:
    text = _INDEX_ANCHOR_PATTERN.sub("", text)
    return _INDEX_MD_LINK_PATTERN.sub(r"\1", text)


def _light_normalize_label(raw: str) -> str:
    return _collapse_whitespace(raw.strip())


def normalize_label(raw: str) -> str:
    text = _collapse_whitespace(raw.strip()).lower()
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = without_marks.rstrip(".,;:!?")
    return _NON_URL_SAFE.sub("-", cleaned).strip("-")


def _expand_page_range(start: int, end: int) -> list[int]:
    if start <= end:
        return list(range(start, end + 1))
    return list(range(end, start + 1))


def _parse_page_token(token: str) -> list[int]:
    stripped = token.strip()
    if not stripped:
        return []

    range_match = RANGE_TOKEN_PATTERN.match(stripped)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return _expand_page_range(start, end)

    single_match = SINGLE_PAGE_PATTERN.match(stripped)
    if single_match:
        return [int(single_match.group(1))]

    return []


def _parse_original_pages(pages_part: str) -> list[int]:
    pages: list[int] = []
    for token in PAGE_TOKEN_SPLIT.split(pages_part.strip()):
        pages.extend(_parse_page_token(token))
    return pages


def _dedupe_sort_pages(pages: list[int]) -> list[int]:
    return sorted(set(pages))


def _try_parse_vedi_line(stripped: str) -> tuple[str, str] | None:
    match = VEDI_PATTERN.match(stripped)
    if match is None:
        return None
    source = _light_normalize_label(match.group(1))
    target = _light_normalize_label(match.group(2))
    if not source or not target:
        return None
    return source, target


def join_split_index_lines(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    merged: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = strip_index_cross_link_markup(line).strip()
        if i + 1 < len(lines):
            next_stripped = strip_index_cross_link_markup(lines[i + 1]).strip()
            if (
                stripped.endswith(",")
                and not re.search(r"\d", stripped)
                and _parse_original_pages(next_stripped)
            ):
                prefix = line[: len(line) - len(line.lstrip())]
                label = stripped.rstrip(",").strip()
                merged.append(f"{prefix}{label}, {next_stripped}")
                i += 2
                continue
        merged.append(line)
        i += 1
    body = "\n".join(merged)
    if text.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return body


def _try_split_label_and_pages(stripped: str) -> tuple[str, str] | None:
    for separator in (f" {EM_DASH} ", EM_DASH, f" {EN_DASH} ", EN_DASH):
        if separator not in stripped:
            continue
        label_part, _, pages_part = stripped.partition(separator)
        pages_part = pages_part.strip()
        if pages_part and re.search(r"\d", pages_part):
            label = _light_normalize_label(label_part)
            if label:
                return label, pages_part

    if "," in stripped:
        label_part, _, pages_part = stripped.partition(",")
        pages_part = pages_part.strip()
        if pages_part and re.search(r"\d", pages_part):
            label = _light_normalize_label(label_part)
            if label:
                return label, pages_part

    for separator in (";", ":"):
        if separator not in stripped:
            continue
        label_part, _, pages_part = stripped.partition(separator)
        pages_part = pages_part.strip()
        if not pages_part or not re.search(r"\d", pages_part):
            continue
        label = _light_normalize_label(label_part)
        if label and re.search(r"[^\W\d_]", label):
            return label, pages_part

    trailing = TRAILING_PAGES_PATTERN.match(stripped)
    if trailing is not None:
        label = _light_normalize_label(trailing.group("label"))
        pages_part = trailing.group("pages").strip()
        if label and pages_part and _parse_original_pages(pages_part):
            return label, pages_part

    return None


def _map_index_pages(
    index_pages: list[int],
    aligned_to_original: dict[int, int],
    line: str,
) -> tuple[list[int], list[int]] | None:
    # INDEX cites aligned pages (post-removal book pagination).
    aligned_sorted = _dedupe_sort_pages(index_pages)
    original_out: list[int] = []
    aligned_out: list[int] = []
    for aligned_page in aligned_sorted:
        original_page = aligned_to_original.get(aligned_page)
        if original_page is None:
            Log(
                WARNING_LOG_LEVEL,
                "index subject page not in mapping",
                {"line": line, "aligned_page": aligned_page},
            )
            continue
        original_out.append(original_page)
        aligned_out.append(aligned_page)

    if not original_out:
        return None
    return original_out, aligned_out


def _is_all_caps_heading(stripped: str) -> bool:
    letters = [char for char in stripped if char.isalpha()]
    if not letters:
        return False
    if re.search(r"\d", stripped):
        return False
    return all(char.isupper() for char in letters)


def index_entry_sort_key(stripped: str) -> tuple[str, str] | None:
    if _is_skippable_index_line(stripped):
        return None

    vedi_parts = _try_parse_vedi_line(stripped)
    if vedi_parts is not None:
        source_label, _ = vedi_parts
        return normalize_label(source_label), source_label.lower()

    label_and_pages = _try_split_label_and_pages(stripped)
    if label_and_pages is not None:
        raw_label, _ = label_and_pages
        return normalize_label(raw_label), raw_label.lower()

    if _is_all_caps_heading(stripped):
        normalized = normalize_label(stripped)
        return normalized, stripped.lower()

    return None


def sort_index_md_body(body: str) -> str:
    prefix_lines: list[str] = []
    entry_lines: list[tuple[tuple[str, str], str]] = []

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if prefix_lines and prefix_lines[-1] != "":
                prefix_lines.append("")
            continue
        if stripped == "---":
            continue

        sort_key = index_entry_sort_key(stripped)
        if sort_key is not None:
            entry_lines.append((sort_key, stripped))
            continue

        prefix_lines.append(stripped)

    entry_lines.sort(key=lambda item: item[0])
    sorted_entries = [line for _, line in entry_lines]

    parts: list[str] = []
    prefix_text = "\n".join(prefix_lines).strip()
    if prefix_text:
        parts.append(prefix_text)
    if sorted_entries:
        parts.append("\n".join(sorted_entries))
    return "\n".join(parts)


def parse_index_md_with_skipped(
    index_md_path: Path,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> tuple[list[RawSubject], list[SkippedIndexLine]]:
    text = index_md_path.read_text(encoding="utf-8")
    mapping = useful_pages_enumeration.aligned_page_to_original_page
    subjects: list[RawSubject] = []
    skipped: list[SkippedIndexLine] = []

    for line in text.splitlines():
        stripped = strip_index_cross_link_markup(line).strip()
        if _is_skippable_index_line(stripped):
            continue

        vedi_parts = _try_parse_vedi_line(stripped)
        if vedi_parts is not None:
            source_label, target_label = vedi_parts
            subjects.append(
                RawSubject(
                    raw_label=source_label,
                    original_pages=[],
                    aligned_pages=[],
                    alias_of=target_label,
                )
            )
            continue

        label_and_pages = _try_split_label_and_pages(stripped)
        if label_and_pages is None:
            if not _is_all_caps_heading(stripped):
                skipped.append(
                    SkippedIndexLine(line=stripped, reason="no_label_pages_separator")
                )
            continue

        raw_label, pages_part = label_and_pages
        index_pages = _parse_original_pages(pages_part)
        if not index_pages:
            skipped.append(
                SkippedIndexLine(line=stripped, reason="no_parsable_pages")
            )
            continue

        mapped = _map_index_pages(index_pages, mapping, stripped)
        if mapped is None:
            skipped.append(
                SkippedIndexLine(line=stripped, reason="all_pages_out_of_mapping")
            )
            continue

        mapped_original, mapped_aligned = mapped
        subjects.append(
            RawSubject(
                raw_label=raw_label,
                original_pages=mapped_original,
                aligned_pages=mapped_aligned,
            )
        )

    return subjects, skipped


def parse_index_md(
    index_md_path: Path,
    useful_pages_enumeration: UsefulPagesEnumeration,
) -> list[RawSubject]:
    subjects, _ = parse_index_md_with_skipped(index_md_path, useful_pages_enumeration)
    return subjects


def write_skipped_lines_report(
    index_md_path: Path,
    skipped: list[SkippedIndexLine],
) -> Path:
    """Persist unparsed INDEX.md lines next to the source file for human review."""
    report_path = index_md_path.with_name("INDEX.skipped.md")
    lines = [
        "# INDEX — righe scartate dal parser",
        "",
        f"Sorgente: {index_md_path.name}",
        f"Totale scartate: {len(skipped)}",
        "",
    ]
    for item in skipped:
        lines.append(f"- [{item.reason}] {item.line}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path

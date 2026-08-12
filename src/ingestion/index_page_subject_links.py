from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from src.core.log import Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.index_md_links import (
    clean_match_label,
    label_match_variants,
    md_href,
    repair_placeholder_subject_links,
)
from src.ingestion.output_writer import (
    IndexConnectionReport,
    _SUBJECT_PAGE_LINK,
    linked_visible_texts_for_page_href,
    normalize_page_md_href,
)
from src.models.settings import Settings

_MD_LINK_OR_CODE_PATTERN = re.compile(
    r"\[[^\]]*\]\([^)]+\)|`[^`]+`|<a\b[^>]*>.*?</a>",
    re.IGNORECASE | re.DOTALL,
)
_LLM_SUBJECT_LINK_PROMPT = """You edit book page markdown for index cross-linking.
The index_subject is the full index entry label. The page often uses shorter or different wording for the same topic.
Find the best matching mention already on the page (person, monument, place, building, etc.) and wrap it with a markdown link to the provided href.
Rules:
- Keep all other text identical.
- The visible link text must be exactly as written on the page, not the full index_subject unless that exact text is on the page.
- Link only the mention(s) that refer to this index subject. Do not link other people, monuments, or places on the page.
- Accept close spelling/transliteration variants (example: index "Firdusi" vs page "Firdausi").
- Prefer linking the shortest clear name/title span (e.g. "Firdausi"), not a whole surrounding phrase, unless the subject is only clear as a longer phrase.
- Use the provided href unchanged as the link destination.
- If a span is already inside a markdown link, leave it unchanged.
- Do not add HTML anchors or fragment identifiers.
- If no reasonable mention exists on the page, return exactly: __NO_MATCH__
- Output only the full page markdown (or __NO_MATCH__), no commentary.
"""
_INDEX_LOCATION_TAIL = re.compile(
    r"^(?:(?:a|al|alla|allo|ai|agli|alle|ad|all['\u2019])\s+)?(.+?)\s+a\s+(?!\d)\S",
    re.IGNORECASE,
)
def _protected_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MD_LINK_OR_CODE_PATTERN.finditer(text)]


def _span_overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for span_start, span_end in spans:
        if start < span_end and end > span_start:
            return True
    return False


def _label_regex(label: str) -> re.Pattern[str] | None:
    patterns = _label_regex_patterns(label)
    return patterns[0] if patterns else None


def _index_core_label_variants(label: str) -> list[str]:
    cleaned = clean_match_label(label)
    match = _INDEX_LOCATION_TAIL.match(cleaned)
    if not match:
        return []
    core = match.group(1).strip()
    return [core] if core else []


def _label_regex_patterns(label: str) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    seen: set[str] = set()
    variants = label_match_variants(label)
    for core in _index_core_label_variants(label):
        if core not in variants:
            variants.append(core)
    for variant in variants:
        parts = [re.escape(part) for part in variant.split() if part]
        if not parts:
            continue
        body = r"\s+".join(parts)
        if body in seen:
            continue
        seen.add(body)
        patterns.append(re.compile(rf"(?<!\w)({body})(?!\w)", re.IGNORECASE))
    return patterns


def _labels_equivalent(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def _record_index_connection(
    report: IndexConnectionReport,
    index_label: str,
    visible_text: str,
    *,
    via_ai: bool = False,
) -> None:
    visible = " ".join(visible_text.split())
    if index_label not in report.regex_resolved_labels:
        report.regex_resolved_labels.append(index_label)
    if _labels_equivalent(index_label, visible):
        if via_ai:
            report.success_ai.append(visible)
        else:
            report.success_regex.append(visible)
        return
    if via_ai:
        if index_label not in report.failed_regex:
            report.failed_regex.append(index_label)
        report.success_ai.append(visible)
    else:
        report.failed_regex.append(index_label)
        report.success_regex.append(visible)


def _subject_already_linked(body: str, label: str) -> bool:
    from src.ingestion.output_writer import _subject_linked_in_body

    for variant in label_match_variants(label):
        if _subject_linked_in_body(body, variant):
            return True
    return False


def link_subject_mentions_in_page(
    page_text: str,
    subjects: list[tuple[str, str]],
) -> tuple[str, IndexConnectionReport]:
    """subjects: list of (label, href)."""
    text = repair_placeholder_subject_links(page_text, subjects)
    report = IndexConnectionReport.empty()
    ordered = sorted(subjects, key=lambda item: len(item[0]), reverse=True)
    for label, href in ordered:
        regexes = _label_regex_patterns(label)
        if not regexes:
            report.failed_regex.append(label)
            continue
        target = md_href(href)
        protected = _protected_spans(text)
        matches: list[re.Match[str]] = []
        for pattern in regexes:
            matches = [
                m
                for m in pattern.finditer(text)
                if not _span_overlaps(m.start(), m.end(), protected)
            ]
            if matches:
                break
        if not matches:
            already_exact = _already_linked_visible_text(text, label, fuzzy=False)
            if already_exact is not None:
                _record_index_connection(report, label, already_exact, via_ai=False)
                continue
            already_fuzzy = _already_linked_visible_text(text, label, fuzzy=True)
            if already_fuzzy is not None:
                _record_index_connection(report, label, already_fuzzy, via_ai=True)
                continue
            report.failed_regex.append(label)
            continue
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            pieces.append(text[cursor : match.start()])
            pieces.append(f"[{match.group(1)}]({target})")
            cursor = match.end()
        pieces.append(text[cursor:])
        text = "".join(pieces)
        _record_index_connection(report, label, matches[0].group(1), via_ai=False)
    return text, report


def _already_linked_visible_text(
    body: str,
    label: str,
    *,
    fuzzy: bool = False,
) -> str | None:
    wanted = {
        " ".join(variant.split()).casefold()
        for variant in label_match_variants(label) + _index_core_label_variants(label)
        if variant
    }
    for match in _SUBJECT_PAGE_LINK.finditer(body):
        visible = match.group(1)
        visible_norm = " ".join(visible.split()).casefold()
        if visible_norm in wanted:
            return visible
        if fuzzy and _visible_matches_subject(visible, label):
            return visible
    return None


def _llm_subject_hints(label: str) -> str:
    hints: list[str] = []
    seen: set[str] = set()
    for variant in label_match_variants(label):
        if variant not in seen:
            seen.add(variant)
            hints.append(variant)
    for core in _index_core_label_variants(label):
        if core not in seen:
            seen.add(core)
            hints.append(core)
    return ", ".join(hints)


def _new_link_visible_texts(before: str, after: str, href: str) -> list[str]:
    before_counts: dict[str, int] = {}
    for text in linked_visible_texts_for_page_href(before, href):
        before_counts[text] = before_counts.get(text, 0) + 1
    new: list[str] = []
    for text in linked_visible_texts_for_page_href(after, href):
        used = before_counts.get(text, 0)
        if used > 0:
            before_counts[text] = used - 1
            continue
        new.append(text)
    return new


def _normalize_visible_for_match(text: str) -> str:
    cleaned = re.sub(r"[*_`]+", "", text)
    return " ".join(cleaned.split()).casefold()


def _visible_matches_subject(visible: str, label: str) -> bool:
    visible_norm = _normalize_visible_for_match(visible)
    if not visible_norm:
        return False
    candidates = [
        *label_match_variants(label),
        *_index_core_label_variants(label),
    ]
    visible_tokens = [tok for tok in visible_norm.split() if len(tok) > 2]
    for candidate in candidates:
        cand = _normalize_visible_for_match(candidate)
        if not cand:
            continue
        if cand in visible_norm or visible_norm in cand:
            return True
        for cand_tok in (tok for tok in cand.split() if len(tok) > 2):
            for vis_tok in visible_tokens:
                if SequenceMatcher(None, cand_tok, vis_tok).ratio() >= 0.84:
                    return True
    return False


def _sanitize_llm_subject_links(
    before: str,
    after: str,
    *,
    label: str,
    href: str,
) -> tuple[str, list[str]] | None:
    target = normalize_page_md_href(href)
    before_counts: dict[str, int] = {}
    for text in linked_visible_texts_for_page_href(before, href):
        before_counts[text] = before_counts.get(text, 0) + 1

    kept_visible: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for match in _SUBJECT_PAGE_LINK.finditer(after):
        pieces.append(after[cursor : match.start()])
        visible = match.group(1)
        link_href = normalize_page_md_href(match.group(2))
        if link_href == target:
            used = before_counts.get(visible, 0)
            if used > 0:
                before_counts[visible] = used - 1
                pieces.append(match.group(0))
            elif _visible_matches_subject(visible, label):
                kept_visible.append(visible)
                pieces.append(f"[{visible}]({md_href(href)})")
            else:
                pieces.append(visible)
        else:
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(after[cursor:])
    if not kept_visible:
        return None
    text = "".join(pieces)
    return (text if text.endswith("\n") else text + "\n"), kept_visible


def _llm_added_subject_link(before: str, after: str, href: str) -> bool:
    return bool(_new_link_visible_texts(before, after, href))


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
    target = md_href(href)
    messages = [
        {"role": "system", "content": build_system_prompt(_LLM_SUBJECT_LINK_PROMPT, None)},
        {
            "role": "user",
            "content": (
                f"index_subject: {label}\n"
                f"page_search_hints: {_llm_subject_hints(label)}\n"
                f"href: {target}\n\n"
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



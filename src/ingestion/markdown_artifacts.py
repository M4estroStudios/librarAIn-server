from __future__ import annotations

import re
from pathlib import Path

_ARTIFACT_LINE_PATTERN = re.compile(
    r"^(\s*<!--.*-->|\s*<\|[^>]*\|>|\s*<channel[^>]*>.*)$",
    re.IGNORECASE,
)
_CHANNEL_THOUGHT_LINE = re.compile(r"^\s*<\|channel>thought\s*$", re.IGNORECASE)
_CHANNEL_OUTPUT_PREFIX = re.compile(r"^\s*<channel\|>\s*", re.IGNORECASE)
_LIBRARAIN_MODEL_PREFIX = "<!-- librarain:model="
_OPERATOR_NOTES_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+operator\s+notes|\*{1,2}\s*operator\s+notes\s*\*{0,2})\s*$",
    re.IGNORECASE,
)
_OPERATOR_NOTES_XML = re.compile(
    r"<operator_notes>.*?</operator_notes>\s*",
    re.IGNORECASE | re.DOTALL,
)
_REFINE_REFUSAL = re.compile(
    r"please provide the raw markdown",
    re.IGNORECASE,
)
_FAKE_CRONO_TABLE = re.compile(
    r"\*{3}\s*\n+\|\s*ANNO\s*\|\s*EVENTI\s*\|.*?\*{3}",
    re.IGNORECASE | re.DOTALL,
)


def strip_lmstudio_channel_artifacts(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if _CHANNEL_THOUGHT_LINE.match(line):
            continue
        without_prefix = _CHANNEL_OUTPUT_PREFIX.sub("", line)
        if without_prefix != line:
            if without_prefix.strip():
                lines.append(without_prefix)
            continue
        if _ARTIFACT_LINE_PATTERN.match(line):
            continue
        lowered = line.lower()
        if "<|channel|>" in lowered or lowered.strip() in {"<channel>", "<|channel|>"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def clean_markdown_channel_artifacts(text: str) -> str:
    if text.startswith(_LIBRARAIN_MODEL_PREFIX):
        first_line, _, body = text.partition("\n")
        cleaned_body = strip_lmstudio_channel_artifacts(body)
        if not cleaned_body:
            return first_line + "\n"
        return f"{first_line}\n{cleaned_body}"
    return strip_lmstudio_channel_artifacts(text)


def _split_model_prefix(text: str) -> tuple[str, str]:
    if text.startswith(_LIBRARAIN_MODEL_PREFIX):
        first_line, _, body = text.partition("\n")
        return first_line + "\n", body
    return "", text


def _note_signatures(prompt_notes: str) -> list[str]:
    signatures: list[str] = []
    seen: set[str] = set()
    for chunk in prompt_notes.split("\n\n"):
        normalized = " ".join(chunk.split())
        if len(normalized) >= 25 and normalized not in seen:
            signatures.append(normalized)
            seen.add(normalized)
    for line in prompt_notes.splitlines():
        normalized = line.strip()
        if len(normalized) >= 25 and normalized not in seen:
            signatures.append(normalized)
            seen.add(normalized)
    return signatures


def _remove_note_signatures(text: str, prompt_notes: str | None) -> str:
    if not prompt_notes:
        return text
    cleaned = text
    notes = prompt_notes.strip()
    if notes and notes in cleaned:
        cleaned = cleaned.replace(notes, "")
    for signature in _note_signatures(notes):
        cleaned = cleaned.replace(signature, "")
    return cleaned


def _remove_operator_notes_heading_block(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if _OPERATOR_NOTES_HEADING.match(line.strip()):
            skipping = True
            continue
        if skipping:
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                skipping = False
                kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept)


def strip_operator_notes_leak(text: str, prompt_notes: str | None = None) -> str:
    prefix, body = _split_model_prefix(text)
    cleaned = _OPERATOR_NOTES_XML.sub("", body)
    cleaned = _FAKE_CRONO_TABLE.sub("", cleaned)
    cleaned = _remove_operator_notes_heading_block(cleaned)
    cleaned = _remove_note_signatures(cleaned, prompt_notes)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
    had_trailing_newline = text.endswith("\n")
    if prefix:
        if cleaned:
            result = f"{prefix}{cleaned}"
        else:
            result = prefix.rstrip("\n")
    else:
        result = cleaned
    if had_trailing_newline and result and not result.endswith("\n"):
        result += "\n"
    return result


def is_refine_refusal_output(text: str) -> bool:
    return bool(_REFINE_REFUSAL.search(text))


def is_operator_notes_leak(text: str, prompt_notes: str | None = None) -> bool:
    if _OPERATOR_NOTES_HEADING.search(text):
        return True
    if _OPERATOR_NOTES_XML.search(text):
        return True
    if is_refine_refusal_output(text):
        return True
    if prompt_notes:
        notes = prompt_notes.strip()
        if notes and notes in text:
            return True
        for signature in _note_signatures(notes):
            if signature in text:
                return True
    return False


def meaningful_text_length(text: str) -> int:
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            continue
        parts.append(stripped)
    return len("\n".join(parts))


def finalize_vision_page_output(content: str, prompt_notes: str | None = None) -> str:
    cleaned = clean_markdown_channel_artifacts(content)
    return strip_operator_notes_leak(cleaned, prompt_notes)


def finalize_editor_page_output(
    refined: str,
    source_md: str,
    *,
    prompt_notes: str | None = None,
) -> tuple[str, bool]:
    refined_clean = clean_markdown_channel_artifacts(refined)
    source_clean = clean_markdown_channel_artifacts(source_md)
    stripped_refined = strip_operator_notes_leak(refined_clean, prompt_notes)
    source_stripped = strip_operator_notes_leak(source_clean, prompt_notes)

    if not is_operator_notes_leak(refined_clean, prompt_notes):
        if stripped_refined.strip():
            return stripped_refined, False
        if source_stripped.strip():
            return source_stripped, True
        return stripped_refined, False

    refined_len = meaningful_text_length(stripped_refined)
    source_len = meaningful_text_length(source_stripped)
    threshold = max(80, int(0.35 * source_len)) if source_len else 80
    if refined_len < threshold:
        return source_stripped, True
    if stripped_refined.strip():
        return stripped_refined, False
    return source_stripped, True


def is_invalid_aggregate_refine_output(
    output: str,
    input_text: str,
    *,
    prompt_notes: str | None = None,
) -> bool:
    if not output.strip():
        return True
    if is_refine_refusal_output(output):
        return True
    if is_operator_notes_leak(output, prompt_notes):
        return True
    input_len = meaningful_text_length(input_text)
    output_len = meaningful_text_length(strip_operator_notes_leak(output, prompt_notes))
    if input_len >= 40 and output_len < max(20, int(0.2 * input_len)):
        return True
    return False


def rewrite_markdown_file(path: Path) -> bool:
    raw = path.read_text(encoding="utf-8")
    cleaned = clean_markdown_channel_artifacts(raw)
    if not cleaned.endswith("\n"):
        cleaned += "\n"
    if cleaned == raw:
        return False
    if cleaned == raw + "\n" and raw.endswith("\n"):
        return False
    path.write_text(cleaned, encoding="utf-8")
    return True

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

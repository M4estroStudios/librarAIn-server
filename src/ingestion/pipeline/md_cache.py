"""Shared model-tagged Markdown cache used by stage2, stage3 and toc/index refine.

Cached files start with a marker line recording the model (and optional notes
hash) that produced them. A cache hit requires the model to match; if a notes
hash is supplied, that must match too.
"""

from __future__ import annotations

import re
from pathlib import Path

MARKER_PREFIX = "<!-- librarain:model="
_MARKER_RE = re.compile(
    r"^<!-- librarain:model=(?P<model>.+?)(?:\s+notes=(?P<notes>[0-9a-fA-F]+))?\s*-->\s*$"
)


def stage_md_marker_line(model: str, notes_hash: str = "") -> str:
    hashed = (notes_hash or "").strip()
    if hashed:
        return f"{MARKER_PREFIX}{model} notes={hashed} -->\n"
    return f"{MARKER_PREFIX}{model} -->\n"


def parse_stage_md_marker(first_line: str) -> tuple[str | None, str | None]:
    match = _MARKER_RE.match(first_line.strip())
    if not match:
        return None, None
    return match.group("model"), match.group("notes")


def stage_md_cached_model(first_line: str) -> str | None:
    model, _notes = parse_stage_md_marker(first_line)
    return model


def split_stage_md_marker(raw: str) -> tuple[str | None, str]:
    if not raw.strip():
        return None, raw
    if "\n" in raw:
        first, body = raw.split("\n", 1)
    else:
        first, body = raw, ""
    model, _notes = parse_stage_md_marker(first.strip())
    if model is not None:
        return model, body
    return None, raw


def strip_stage_md_marker(raw: str) -> str:
    model, body = split_stage_md_marker(raw)
    if model is not None:
        return body
    return raw


def read_stage_md(md_path: Path, model: str, notes_hash: str = "") -> str | None:
    """Return the cached body if the file matches `model` and `notes_hash`.

    Legacy files without a marker are a hit only when `notes_hash` is empty.
    Markers without `notes=` are a hit only when `notes_hash` is empty.
    """
    if not md_path.is_file():
        return None
    raw = md_path.read_text(encoding="utf-8")
    if not raw.strip():
        return None
    if "\n" in raw:
        first, body = raw.split("\n", 1)
    else:
        first, body = raw, ""
    cached_model, cached_notes = parse_stage_md_marker(first)
    wanted_hash = (notes_hash or "").strip()
    if cached_model is None:
        if wanted_hash:
            return None
        return raw
    if cached_model != model:
        return None
    marker_hash = (cached_notes or "").strip()
    if marker_hash != wanted_hash:
        return None
    return body


def write_stage_md(md_path: Path, model: str, body: str, notes_hash: str = "") -> None:
    md_path.write_text(stage_md_marker_line(model, notes_hash) + body, encoding="utf-8")

"""Shared model-tagged Markdown cache used by stage2, stage3 and toc/index refine.

Cached files start with a marker line recording the model that produced them;
a cache hit requires the model to match, so switching models invalidates the
cache automatically.
"""

from __future__ import annotations

from pathlib import Path

MARKER_PREFIX = "<!-- librarain:model="


def stage_md_marker_line(model: str) -> str:
    return f"{MARKER_PREFIX}{model} -->\n"


def stage_md_cached_model(first_line: str) -> str | None:
    line = first_line.strip()
    if not line.startswith(MARKER_PREFIX) or not line.endswith(" -->"):
        return None
    return line[len(MARKER_PREFIX) : -4]


def read_stage_md(md_path: Path, model: str) -> str | None:
    """Return the cached body if the file was produced by `model`.

    Files without a marker line are treated as valid legacy cache (returned
    as-is); files produced by a different model invalidate the cache.
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
    cached = stage_md_cached_model(first)
    if cached is None:
        return raw
    if cached != model:
        return None
    return body


def write_stage_md(md_path: Path, model: str, body: str) -> None:
    md_path.write_text(stage_md_marker_line(model) + body, encoding="utf-8")

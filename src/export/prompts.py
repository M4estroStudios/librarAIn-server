from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "search" / "prompts"
_ETALY_METADATA_PROMPT_PATH = _PROMPTS_DIR / "etaly_metadata_prompt.md"
_TIMELINE_FILL_PROMPT_PATH = _PROMPTS_DIR / "timeline_fill_prompt.md"


def load_etaly_metadata_prompt() -> str:
    return _ETALY_METADATA_PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_timeline_fill_prompt() -> str:
    return _TIMELINE_FILL_PROMPT_PATH.read_text(encoding="utf-8").strip()

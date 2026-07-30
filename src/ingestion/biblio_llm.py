from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import openai

from src.core.log import Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.ingestion.biblio_hash import (
    compute_biblio_id,
    is_all_unknown,
    normalize_year_for_hash,
)
from src.models.settings import Settings

_MAX_COMPLETION_TOKENS = 4096
_CACHE_SCHEMA_VERSION = "1.0"
_PROMPT_PATH = (
    Path(__file__).resolve().parent / "pipeline" / "prompts" / "biblio_extract_prompt.md"
)


def _optional_model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _biblio_llm_model(settings: Settings) -> str:
    for attr in ("ocrvision_model", "glm_ocr_model", "vision_model"):
        model = _optional_model_name(getattr(settings, attr, None))
        if model:
            return model
    return "gpt-4.1-mini"


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_notes_sha256(prompt_notes: str | None) -> str:
    notes = (prompt_notes or "").strip()
    return hashlib.sha256(notes.encode("utf-8")).hexdigest()


def _cache_path(
    settings: Settings, source_sha256: str, aligned_page: int, book_slug: str
) -> Path:
    slug = book_slug.strip() or "book"
    return (
        Path(settings.data_root)
        / "tmp"
        / source_sha256
        / "stageBiblio"
        / f"p.{aligned_page:04d}.{slug}.json"
    )


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _json_candidates(content: str) -> list[str]:
    stripped = content.strip()
    if not stripped:
        return []
    candidates = [stripped]
    if stripped.startswith("```"):
        unfenced = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        unfenced = re.sub(r"\s*```$", "", unfenced)
        candidates.append(unfenced.strip())
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    if fenced:
        candidates.append(fenced.group(1).strip())
    for match in re.finditer(r'\{\s*"entries"\s*:', stripped, re.IGNORECASE):
        start = match.start()
        depth = 0
        for index in range(start, len(stripped)):
            char = stripped[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start : index + 1])
                    break
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _coerce_year(value: object) -> int | None:
    year_norm = normalize_year_for_hash(value)
    if year_norm == "unknown":
        return None
    try:
        return int(year_norm)
    except ValueError:
        return None


def _normalize_entry(raw_entry: dict[str, Any]) -> dict[str, Any] | None:
    authors = raw_entry.get("authors")
    if authors is None:
        authors = "unknown"
    elif isinstance(authors, list):
        authors = ",".join(str(part).strip() for part in authors if str(part).strip()) or "unknown"
    else:
        authors = str(authors).strip() or "unknown"
    title = raw_entry.get("title")
    title = str(title).strip() if title is not None else "unknown"
    if not title:
        title = "unknown"
    year = _coerce_year(raw_entry.get("year"))
    line_raw = raw_entry.get("line")
    line = None
    if isinstance(line_raw, int) and line_raw > 0:
        line = line_raw
    elif isinstance(line_raw, str) and line_raw.strip().isdigit():
        line = int(line_raw.strip())
    raw_text = raw_entry.get("raw")
    raw = str(raw_text).strip() if raw_text is not None else None
    extras_raw = raw_entry.get("extras")
    extras = dict(extras_raw) if isinstance(extras_raw, dict) else {}
    entry_id, authors_norm, title_norm, year_norm = compute_biblio_id(authors, title, year)
    return {
        "id": entry_id,
        "authors": authors,
        "title": title,
        "year": year,
        "line": line,
        "raw": raw,
        "extras": extras,
        "all_unknown": is_all_unknown(authors_norm, title_norm, year_norm),
        "incomplete": authors_norm == "unknown"
        or title_norm == "unknown"
        or year_norm == "unknown",
    }


def parse_biblio_llm_response(content: str) -> list[dict[str, Any]] | None:
    for candidate in _json_candidates(content):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        entries_raw = payload.get("entries")
        if not isinstance(entries_raw, list):
            continue
        entries: list[dict[str, Any]] = []
        for item in entries_raw:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_entry(item)
            if normalized is not None:
                entries.append(normalized)
        return entries
    return None


def _read_cache(
    cache_path: Path,
    *,
    model: str,
    source_text_sha256: str,
    prompt_notes_sha256: str,
) -> list[dict[str, Any]] | None:
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return None
    if payload.get("model") != model:
        return None
    if payload.get("source_text_sha256") != source_text_sha256:
        return None
    if payload.get("prompt_notes_sha256") != prompt_notes_sha256:
        return None
    entries = payload.get("entries")
    return entries if isinstance(entries, list) else None


def _write_cache(
    cache_path: Path,
    *,
    model: str,
    source_text_sha256: str,
    prompt_notes_sha256: str,
    entries: list[dict[str, Any]],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "model": model,
        "source_text_sha256": source_text_sha256,
        "prompt_notes_sha256": prompt_notes_sha256,
        "entries": entries,
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def extract_biblio_entries_for_page(
    text: str,
    *,
    client: openai.OpenAI,
    settings: Settings,
    request_id: str = "",
    aligned_page: int = 0,
    prompt_notes: str | None = None,
    source_sha256: str = "",
    book_slug: str = "",
) -> list[dict[str, Any]]:
    model = _biblio_llm_model(settings)
    text_hash = _text_sha256(text)
    notes_hash = _prompt_notes_sha256(prompt_notes)
    cache_path = _cache_path(settings, source_sha256, aligned_page, book_slug)
    cached = _read_cache(
        cache_path,
        model=model,
        source_text_sha256=text_hash,
        prompt_notes_sha256=notes_hash,
    )
    if cached is not None:
        return [entry for entry in (_normalize_entry(item) for item in cached if isinstance(item, dict)) if entry]

    system_prompt = build_system_prompt(_load_prompt(), prompt_notes)
    user_content = json.dumps(
        {"aligned_page": aligned_page, "page_text": text},
        ensure_ascii=False,
    )
    content = await chat_completion_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="biblio",
        page=aligned_page,
    )
    parsed = parse_biblio_llm_response(content)
    if parsed is None:
        Log(
            WARNING_LOG_LEVEL,
            "biblio LLM response not parseable as JSON",
            {
                "request_id": request_id,
                "aligned_page": aligned_page,
                "content_preview": content[:200],
            },
        )
        raise ValueError(f"biblio LLM response not parseable for page {aligned_page}")
    if source_sha256:
        _write_cache(
            cache_path,
            model=model,
            source_text_sha256=text_hash,
            prompt_notes_sha256=notes_hash,
            entries=parsed,
        )
    return parsed

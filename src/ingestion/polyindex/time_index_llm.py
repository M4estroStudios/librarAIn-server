from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import openai

from src.core.log import Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_system_prompt, chat_completion_with_retry
from src.models.settings import Settings

_MAX_COMPLETION_TOKENS = 2048
_CACHE_SCHEMA_VERSION = "1.0"
_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "time_index_extract_prompt.md"
)
_ITALIAN_MONTHS = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
)


def _optional_model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _time_index_llm_model(settings: Settings) -> str:
    for attr in ("time_index_llm_model", "matcher_llm_model", "editor_model"):
        model = _optional_model_name(getattr(settings, attr, None))
        if model:
            return model
    return "gpt-4.1-mini"


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_notes_sha256(prompt_notes: str | None) -> str:
    notes = (prompt_notes or "").strip()
    return hashlib.sha256(notes.encode("utf-8")).hexdigest()


def _time_index_cache_path(
    settings: Settings, source_sha256: str, aligned_page: int, book_slug: str
) -> Path:
    slug = book_slug.strip() or "book"
    return (
        Path(settings.data_root)
        / "tmp"
        / source_sha256
        / "stageTimeIndex"
        / f"p.{aligned_page:04d}.{slug}.json"
    )


def _labels_from_cache_lists(years_raw: object, dates_raw: object) -> tuple[set[str], set[str]] | None:
    if not isinstance(years_raw, list) or not isinstance(dates_raw, list):
        return None
    years = {
        normalized
        for item in years_raw
        if isinstance(item, str) and (normalized := _normalize_llm_label(item))
    }
    dates = {
        normalized
        for item in dates_raw
        if isinstance(item, str) and (normalized := _normalize_llm_label(item))
    }
    return years, dates


def _read_time_index_cache(
    cache_path: Path,
    *,
    model: str,
    source_text_sha256: str,
    prompt_notes_sha256: str,
) -> tuple[set[str], set[str]] | None:
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
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
    return _labels_from_cache_lists(payload.get("years"), payload.get("dates"))


def _write_time_index_cache(
    cache_path: Path,
    *,
    model: str,
    source_text_sha256: str,
    prompt_notes_sha256: str,
    years: set[str],
    dates: set[str],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "model": model,
        "source_text_sha256": source_text_sha256,
        "prompt_notes_sha256": prompt_notes_sha256,
        "years": sorted(years),
        "dates": sorted(dates),
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_extract_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _normalize_llm_label(label: str) -> str:
    cleaned = re.sub(r"\s+", " ", label.strip())
    if not cleaned:
        return ""
    for month in _ITALIAN_MONTHS:
        pattern = re.compile(re.escape(month), re.IGNORECASE)
        if pattern.search(cleaned):
            cleaned = pattern.sub(month, cleaned)
            break
    return cleaned


def _json_candidates_from_llm_content(content: str) -> list[str]:
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
    for match in re.finditer(r'\{\s*"years"\s*:', stripped, re.IGNORECASE):
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


def parse_llm_time_response(content: str) -> tuple[set[str], set[str]] | None:
    for candidate in _json_candidates_from_llm_content(content):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        years_raw = payload.get("years")
        dates_raw = payload.get("dates")
        if not isinstance(years_raw, list) or not isinstance(dates_raw, list):
            continue
        years = {
            normalized
            for item in years_raw
            if isinstance(item, str) and (normalized := _normalize_llm_label(item))
        }
        dates = {
            normalized
            for item in dates_raw
            if isinstance(item, str) and (normalized := _normalize_llm_label(item))
        }
        return years, dates
    return None


async def extract_time_references_llm(
    text: str,
    client: openai.OpenAI,
    settings: Settings,
    *,
    request_id: str = "",
    aligned_page: int = 0,
    prompt_notes: str | None = None,
) -> tuple[set[str], set[str]]:
    system_prompt = build_system_prompt(_load_extract_system_prompt(), prompt_notes)
    user_content = json.dumps({"page_text": text}, ensure_ascii=False)
    content = await chat_completion_with_retry(
        client,
        model=_time_index_llm_model(settings),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=_MAX_COMPLETION_TOKENS,
        request_id=request_id,
        stage="time_index",
        page=aligned_page,
    )
    parsed = parse_llm_time_response(content)
    if parsed is not None:
        return parsed
    Log(
        WARNING_LOG_LEVEL,
        "time index LLM response not parseable as JSON; falling back to regex only",
        {
            "request_id": request_id,
            "aligned_page": aligned_page,
            "content_preview": content[:200],
        },
    )
    return set(), set()


async def extract_time_references_for_page(
    text: str,
    *,
    client: openai.OpenAI | None = None,
    settings: Settings | None = None,
    request_id: str = "",
    aligned_page: int = 0,
    prompt_notes: str | None = None,
    source_sha256: str = "",
    book_slug: str = "",
) -> tuple[set[str], set[str], bool]:
    from src.ingestion.polyindex.time_index import extract_time_references

    regex_years, regex_dates = extract_time_references(text)
    if client is None or settings is None or not settings.time_index_use_llm:
        return regex_years, regex_dates, False

    model = _time_index_llm_model(settings)
    text_hash = _text_sha256(text)
    notes_hash = _prompt_notes_sha256(prompt_notes)
    cache_path = _time_index_cache_path(settings, source_sha256, aligned_page, book_slug)
    cached = _read_time_index_cache(
        cache_path,
        model=model,
        source_text_sha256=text_hash,
        prompt_notes_sha256=notes_hash,
    )
    if cached is not None:
        llm_years, llm_dates = cached
        return regex_years | llm_years, regex_dates | llm_dates, True

    try:
        llm_years, llm_dates = await extract_time_references_llm(
            text,
            client,
            settings,
            request_id=request_id,
            aligned_page=aligned_page,
            prompt_notes=prompt_notes,
        )
    except Exception as exc:
        Log(
            WARNING_LOG_LEVEL,
            "time index LLM extraction failed; falling back to regex only",
            {
                "request_id": request_id,
                "aligned_page": aligned_page,
                "error": repr(exc),
            },
        )
        return regex_years, regex_dates, False

    if source_sha256:
        _write_time_index_cache(
            cache_path,
            model=model,
            source_text_sha256=text_hash,
            prompt_notes_sha256=notes_hash,
            years=llm_years,
            dates=llm_dates,
        )
    return regex_years | llm_years, regex_dates | llm_dates, True

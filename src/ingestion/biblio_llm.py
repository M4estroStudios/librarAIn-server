from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import openai

from src.core.log import Log, INFO_LOG_LEVEL, WARNING_LOG_LEVEL
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
    for attr in ("editor_model", "matcher_llm_model", "time_index_llm_model", "research_model", "ocrvision_model", "glm_ocr_model", "vision_model"):
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


_PLACE_TAIL_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s'.-]{0,47}$")
_TITLE_TAIL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r",\s*a cura di\s+(.+)$", re.IGNORECASE), "curators"),
    (re.compile(r",\s*(?:a\s+)?ed\.?\s+(?:di\s+)?(.+)$", re.IGNORECASE), "editor"),
    (re.compile(r",\s*(\d+\s*voll\.?)$", re.IGNORECASE), "volumes"),
    (re.compile(r",\s*vol\.?\s*(\d+(?:-\d+)?)$", re.IGNORECASE), "volume"),
    (re.compile(r",\s*pp\.?\s*(.+)$", re.IGNORECASE), "pages"),
    (re.compile(r",\s*trad\.?\s+(?:di\s+)?(.+)$", re.IGNORECASE), "translator"),
    (re.compile(r",\s*introd\.?\s+(?:di\s+)?(.+)$", re.IGNORECASE), "introduction_by"),
]


def _strip_author_prefix(title: str, authors: str) -> str:
    authors_clean = authors.strip()
    if not authors_clean or authors_clean == "unknown":
        return title.strip()
    title_clean = title.strip()
    for sep in (", ", " — ", " - "):
        prefix = authors_clean + sep
        if title_clean.casefold().startswith(prefix.casefold()):
            return title_clean[len(prefix) :].strip()
    return title_clean


def _refine_biblio_fields(
    authors: str,
    title: str,
    extras: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    merged_extras = dict(extras)
    refined_title = _strip_author_prefix(title, authors)
    changed = True
    while changed:
        changed = False
        for pattern, key in _TITLE_TAIL_PATTERNS:
            match = pattern.search(refined_title)
            if not match:
                continue
            value = match.group(1).strip()
            if value:
                merged_extras.setdefault(key, value)
            refined_title = refined_title[: match.start()].strip().rstrip(",")
            changed = True
            break
    refined_title = refined_title.strip() or title.strip() or "unknown"
    return authors, refined_title, merged_extras


def _extract_publication_place(title_part: str, extras: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    work = title_part.strip()
    merged_extras = dict(extras)
    if "," not in work:
        return work, merged_extras
    maybe_title, maybe_place = work.rsplit(",", 1)
    maybe_place = maybe_place.strip()
    if (
        maybe_place
        and _PLACE_TAIL_RE.match(maybe_place)
        and not re.search(r"\d", maybe_place)
    ):
        merged_extras.setdefault("publication_place", maybe_place)
        work = maybe_title.strip() or work
    return work, merged_extras


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
    authors, title, extras = _refine_biblio_fields(authors, title, extras)
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


def normalize_biblio_entry(raw_entry: dict[str, Any]) -> dict[str, Any] | None:
    return _normalize_entry(raw_entry)


_YEAR_TAIL_RE = re.compile(
    r"^(?P<body>.+?)\s+(?P<year>(?:1[0-9]{3}|20[0-9]{2})(?:-[0-9]{2,4})?)\.\s*$"
)


def _clean_biblio_page_lines(text: str) -> list[tuple[int, str]]:
    lines_out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = re.sub(r"<!--.*?-->", "", raw).strip()
        if not line:
            continue
        if line.casefold().startswith("bibliografia"):
            continue
        lines_out.append((len(lines_out) + 1, line))
    return lines_out


def parse_biblio_lines_fallback(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    last_authors = "unknown"
    for line_num, line in _clean_biblio_page_lines(text):
        match = _YEAR_TAIL_RE.match(line)
        if not match:
            continue
        body = match.group("body").strip().rstrip(",")
        year = _coerce_year(match.group("year"))
        upper = body.upper()
        if upper.startswith("ID.") or upper.startswith("ID,"):
            authors = last_authors
            title_part = body[2:].lstrip("., ")
        else:
            parts = body.split(",", 1)
            if len(parts) < 2:
                continue
            authors = parts[0].strip()
            title_part = parts[1].strip()
            last_authors = authors
        extras: dict[str, Any] = {}
        title, extras = _extract_publication_place(title_part, extras)
        normalized = _normalize_entry(
            {
                "authors": authors,
                "title": title,
                "year": year,
                "line": line_num,
                "raw": line,
                "extras": extras,
            }
        )
        if normalized is not None:
            entries.append(normalized)
    return entries


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
        reasoning_enable_thinking=False,
    )
    parsed = parse_biblio_llm_response(content)
    if parsed is None:
        content = await chat_completion_with_retry(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        'Risposta non valida: serve solo JSON con chiave "entries", '
                        "senza markdown né spiegazioni."
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=_MAX_COMPLETION_TOKENS,
            request_id=request_id,
            stage="biblio",
            page=aligned_page,
            reasoning_enable_thinking=False,
        )
        parsed = parse_biblio_llm_response(content)
    if parsed is None:
        parsed = parse_biblio_lines_fallback(text)
        if parsed:
            Log(
                INFO_LOG_LEVEL,
                "biblio line parser fallback used",
                {"request_id": request_id, "aligned_page": aligned_page, "entries": len(parsed)},
            )
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

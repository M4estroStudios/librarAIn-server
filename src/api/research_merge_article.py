from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.core.openai_client_sync import chat_completion_with_retry_sync
from src.core.openai_client import build_openai_client
from src.core.log import INFO_LOG_LEVEL, Log
from src.models.settings import Settings
from src.search.pages_loader import load_pages
from src.search.article_catalog import publish_poh_article, list_index_subjects
from src.search.article_llm import research_model, strip_article_markdown_fences

_MERGE_SYSTEM = (
    "Sei un redattore enciclopedico. Integra materiale nuovo in un articolo esistente "
    "senza ripetere frasi verbatim dall'originale. Mantieni tono neutro in italiano. "
    "Output solo Markdown UTF-8 valido con link source: dove appropriato. "
    "Non duplica paragrafi identici dall'articolo originale."
)


def _merge_user_payload(
    *,
    target_poh_id: str,
    label: str,
    existing_markdown: str,
    new_pages: list[dict[str, Any]],
    reicat: dict[str, Any],
    operator_notes: str,
) -> str:
    payload = {
        "target_poh_id": target_poh_id,
        "label": label,
        "existing_article_markdown": existing_markdown,
        "new_book_material": {
            "reicat": reicat,
            "pages": new_pages,
            "operator_notes": operator_notes,
        },
        "instruction": (
            "Aggiorna l'articolo incorporando il nuovo materiale del libro. "
            "Evita ripetizioni verbatim; integra nuove prospettive e fonti."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def merge_article_markdown(
    *,
    settings: Settings,
    target_poh_id: str,
    existing_markdown: str,
    new_pages: list[dict[str, Any]],
    reicat: dict[str, Any],
    operator_notes: str,
    request_id: str,
) -> str:
    subjects = list_index_subjects(Path(settings.data_root))
    entry = subjects.get(target_poh_id)
    label = entry.canonical_label if entry else target_poh_id
    client = build_openai_client(settings)
    model = research_model(settings)
    user_message = _merge_user_payload(
        target_poh_id=target_poh_id,
        label=label,
        existing_markdown=existing_markdown,
        new_pages=new_pages,
        reicat=reicat,
        operator_notes=operator_notes,
    )
    Log(
        INFO_LOG_LEVEL,
        "merge article LLM begin",
        {"request_id": request_id, "poh_id": target_poh_id, "model": model},
    )
    content = chat_completion_with_retry_sync(
        client,
        model=model,
        messages=[
            {"role": "system", "content": _MERGE_SYSTEM},
            {"role": "user", "content": user_message},
        ],
        temperature=settings.research_temperature,
        max_tokens=8192,
        request_id=request_id,
        stage="research_merge_article",
        reasoning_effort=settings.reasoning_effort_research,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_research,
    )
    return strip_article_markdown_fences(content)


def consecutive_duplicate_ratio(original: str, merged: str, min_lines: int = 3) -> float:
    orig_lines = [line.strip() for line in original.splitlines() if line.strip()]
    merged_lines = [line.strip() for line in merged.splitlines() if line.strip()]
    if len(orig_lines) < min_lines:
        return 0.0
    duplicates = 0
    for i in range(len(orig_lines) - min_lines + 1):
        block = orig_lines[i : i + min_lines]
        block_text = "\n".join(block)
        if block_text in merged:
            duplicates += min_lines
    return duplicates / max(1, len(merged_lines))


def _load_book_material(
    data_root: Path,
    *,
    poh_id: str,
    book_sha: str,
    request_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subjects = list_index_subjects(data_root)
    entry = subjects.get(poh_id)
    if entry is None or book_sha not in entry.books:
        return [], {}
    aligned = list(entry.books[book_sha].aligned_pages)
    if not aligned:
        return [], {}
    pages_result = load_pages(
        {book_sha: aligned},
        data_root,
        request_id=request_id,
    )
    new_pages = [
        {
            "source_sha256": p.source_sha256,
            "aligned_page": p.aligned_page,
            "book_title": p.book_title,
            "text": p.markdown,
        }
        for p in pages_result.pages
    ]
    manifest_path = data_root / "output" / book_sha / "manifest.json"
    reicat: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest.get("reicat"), dict):
                reicat = manifest["reicat"]
            elif isinstance(manifest, dict):
                reicat = {
                    "titolo": manifest.get("slug") or book_sha,
                }
        except (json.JSONDecodeError, OSError):
            pass
    return new_pages, reicat


def handle_merge_article_request(
    data_root: Path,
    settings: Settings,
    payload: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    target_poh_id = str(payload.get("target_poh_id") or "").strip()
    if not target_poh_id:
        raise ValueError("target_poh_id is required")
    existing = str(payload.get("existing_markdown") or "").strip()
    if not existing:
        md_path = data_root / "research" / "articles" / f"{target_poh_id}.md"
        if md_path.is_file():
            existing = md_path.read_text(encoding="utf-8")
    if not existing:
        raise ValueError("existing article markdown is required")
    new_pages = payload.get("new_pages")
    if not isinstance(new_pages, list):
        new_pages = []
    reicat = payload.get("reicat")
    if not isinstance(reicat, dict):
        reicat = {}
    book_sha = str(payload.get("book_sha") or "").strip().lower()
    if book_sha and not new_pages:
        loaded_pages, loaded_reicat = _load_book_material(
            data_root,
            poh_id=target_poh_id,
            book_sha=book_sha,
            request_id=request_id,
        )
        new_pages = loaded_pages
        if loaded_reicat:
            reicat = {**loaded_reicat, **reicat}
    operator_notes = str(payload.get("operator_notes") or payload.get("context_note") or "")
    merged = merge_article_markdown(
        settings=settings,
        target_poh_id=target_poh_id,
        existing_markdown=existing,
        new_pages=new_pages,
        reicat=reicat,
        operator_notes=operator_notes,
        request_id=request_id,
    )
    subjects = list_index_subjects(data_root)
    entry = subjects.get(target_poh_id)
    title = entry.canonical_label if entry else target_poh_id
    title_match = re.match(r"^#\s+(.+)", merged.strip())
    if title_match:
        title = title_match.group(1).strip()
    published = publish_poh_article(
        data_root,
        poh_id=target_poh_id,
        title=title,
        markdown=merged,
        request_id=request_id,
    )
    return {
        "ok": True,
        "poh_id": target_poh_id,
        "url": published.get("url"),
        "duplicate_line_ratio": consecutive_duplicate_ratio(existing, merged),
        "markdown_chars": len(merged),
    }

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.search.article_catalog import list_index_subjects, search_poh_catalog
from src.search.poh_time_range import lookup_poh_time_range


def _books_for_poh(data_root: Path, poh_id: str) -> list[dict[str, Any]]:
    subjects = list_index_subjects(data_root)
    entry = subjects.get(poh_id)
    if entry is None:
        return []
    books: list[dict[str, Any]] = []
    for sha, book in entry.books.items():
        books.append(
            {
                "source_sha256": sha,
                "title": book.title or sha,
                "slug": book.slug or "",
                "aligned_pages": list(book.aligned_pages),
            }
        )
    books.sort(key=lambda b: str(b["title"]).casefold())
    return books


def execute_search_tool(data_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    n = int(arguments.get("n") or 10)
    n = max(1, min(n, 50))
    results = search_poh_catalog(data_root, query, limit=n)
    polyindex = data_root / "polyindex"
    enriched = []
    for item in results:
        poh_id = str(item["poh_id"])
        label = str(item.get("label") or poh_id)
        time_range = lookup_poh_time_range(polyindex, poh_id, label)
        enriched.append(
            {
                "poh_id": poh_id,
                "label": label,
                "books": _books_for_poh(data_root, poh_id),
                "time_range": time_range,
                "has_article": bool(item.get("has_article")),
                "url": item.get("url"),
            }
        )
    return {"results": enriched, "count": len(enriched)}


def execute_read_source_tool(data_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    poh = str(arguments.get("poh") or "").strip()
    if not poh:
        return {"ok": False, "reason": "missing_poh", "error": "poh is required"}
    subjects = list_index_subjects(data_root)
    entry = subjects.get(poh)
    label = entry.canonical_label if entry else poh
    md_path = data_root / "research" / "articles" / f"{poh}.md"
    if not md_path.is_file():
        safe = poh.replace("/", "_")
        md_path = data_root / "research" / "articles" / f"{safe}.md"
    if not md_path.is_file():
        return {
            "ok": False,
            "reason": "no_article",
            "poh_id": poh,
            "label": label,
            "hint": "Usa offerArticleGeneration per proporre la generazione dell'articolo.",
        }
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": "read_error", "error": str(exc)}
    return {
        "ok": True,
        "poh_id": poh,
        "label": label,
        "markdown": markdown,
        "url": f"/articolo/{poh}.html",
    }


def execute_offer_article_generation(data_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    poh = str(arguments.get("poh") or "").strip()
    if not poh:
        return {"ok": False, "error": "poh is required"}
    subjects = list_index_subjects(data_root)
    entry = subjects.get(poh)
    if entry is None:
        return {"ok": False, "error": f"unknown poh: {poh}"}
    time_range = lookup_poh_time_range(data_root / "polyindex", poh, entry.canonical_label)
    return {
        "ok": True,
        "poh_id": poh,
        "label": entry.canonical_label,
        "aliases": list(entry.aliases),
        "books": _books_for_poh(data_root, poh),
        "time_range": time_range,
        "action": "offerArticleGeneration",
    }


CHAT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Cerca soggetti POH nell'indice biblioteca (tutti, anche senza articolo).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Testo di ricerca"},
                    "n": {"type": "integer", "description": "Numero massimo risultati", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "readSource",
            "description": "Legge l'articolo markdown pubblicato per un POH.",
            "parameters": {
                "type": "object",
                "properties": {
                    "poh": {"type": "string", "description": "poh_id"},
                },
                "required": ["poh"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "offerArticleGeneration",
            "description": "Propone all'UI la generazione di un articolo per un POH senza articolo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "poh": {"type": "string", "description": "poh_id"},
                },
                "required": ["poh"],
            },
        },
    },
]


def execute_chat_tool(data_root: Path, name: str, arguments_json: str) -> str:
    try:
        arguments = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "search":
        result = execute_search_tool(data_root, arguments)
    elif name == "readSource":
        result = execute_read_source_tool(data_root, arguments)
    elif name == "offerArticleGeneration":
        result = execute_offer_article_generation(data_root, arguments)
    else:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    return json.dumps(result, ensure_ascii=False)

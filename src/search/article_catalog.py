from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingestion.polyindex.index_json import PolyindexIndexDocument
from src.models.polyindex_index import PolyindexIndexSubjectEntry
from src.models.settings import Settings
from src.search.postprocess import markdown_to_article_html
from src.search.research_runner import (
    build_poh_research_request,
    run_research,
)
from src.search.article_llm import is_no_material_article


def _normalize_search(text: str) -> str:
    lowered = " ".join(text.strip().split()).lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _catalog_path(data_root: Path) -> Path:
    return data_root / "research" / "catalog.json"


def _articles_dir(data_root: Path) -> Path:
    return data_root / "research" / "articles"


def _article_file(data_root: Path, poh_id: str) -> Path:
    safe_id = re.sub(r"[^\w.\-]", "_", poh_id)
    return _articles_dir(data_root) / f"{safe_id}.html"


def _article_markdown_file(data_root: Path, poh_id: str) -> Path:
    safe_id = re.sub(r"[^\w.\-]", "_", poh_id)
    return _articles_dir(data_root) / f"{safe_id}.md"


def _article_url(poh_id: str) -> str:
    safe_id = re.sub(r"[^\w.\-]", "_", poh_id)
    return f"/articolo/{safe_id}.html"


def _load_catalog(data_root: Path) -> dict[str, Any]:
    path = _catalog_path(data_root)
    if not path.is_file():
        return {"articles": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"articles": {}}
    if not isinstance(raw, dict):
        return {"articles": {}}
    articles = raw.get("articles")
    if not isinstance(articles, dict):
        raw["articles"] = {}
    return raw


def _save_catalog(data_root: Path, catalog: dict[str, Any]) -> None:
    path = _catalog_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _snippet_from_text(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def list_ingested_books(data_root: Path) -> list[dict[str, Any]]:
    output_dir = data_root / "output"
    if not output_dir.is_dir():
        return []
    books: list[dict[str, Any]] = []
    for book_dir in sorted(output_dir.iterdir()):
        if not book_dir.is_dir():
            continue
        manifest_path = book_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        reicat = manifest.get("reicat") if isinstance(manifest.get("reicat"), dict) else {}
        title = reicat.get("titolo") or manifest.get("slug") or book_dir.name
        books.append(
            {
                "source_sha256": str(manifest.get("source_sha256") or book_dir.name),
                "title": str(title),
                "slug": str(manifest.get("slug") or ""),
                "page_count": len(manifest.get("pages") or []),
            }
        )
    books.sort(key=lambda item: str(item["title"]).casefold())
    return books


def list_index_subjects(data_root: Path) -> dict[str, PolyindexIndexSubjectEntry]:
    index_path = data_root / "polyindex" / "INDEX.json"
    document = PolyindexIndexDocument.load_file(index_path)
    return document.subjects


def _is_no_material_entry(meta: object) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("no_material"))


def _article_is_complete(data_root: Path, poh_id: str, meta: object) -> bool:
    if not _article_file(data_root, poh_id).is_file():
        return False
    return not _is_no_material_entry(meta)


def list_missing_articles(
    data_root: Path,
    *,
    book_sha: str | None = None,
) -> list[dict[str, Any]]:
    subjects = list_index_subjects(data_root)
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    if not isinstance(articles, dict):
        articles = {}
    missing: list[dict[str, Any]] = []
    book_sha_norm = book_sha.strip() if book_sha else None
    for poh_id, entry in subjects.items():
        meta = articles.get(poh_id)
        if _article_is_complete(data_root, poh_id, meta):
            continue
        if book_sha_norm and book_sha_norm not in entry.books:
            continue
        missing.append(
            {
                "poh_id": poh_id,
                "label": entry.canonical_label,
                "aliases": list(entry.aliases),
                "book_count": len(entry.books),
            }
        )
    missing.sort(key=lambda item: str(item["label"]).casefold())
    return missing


def research_status_summary(data_root: Path) -> dict[str, int]:
    subjects = list_index_subjects(data_root)
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    if not isinstance(articles, dict):
        articles = {}
    article_count = sum(
        1
        for poh_id, meta in articles.items()
        if _article_is_complete(data_root, str(poh_id), meta)
    )
    return {
        "total_subjects": len(subjects),
        "articles_count": article_count,
        "missing_count": max(0, len(subjects) - article_count),
    }


def search_articles(data_root: Path, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    q = _normalize_search(query)
    if not q:
        return []
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    if not isinstance(articles, dict):
        return []
    subjects = list_index_subjects(data_root)
    hits: list[tuple[int, dict[str, Any]]] = []
    for poh_id, meta in articles.items():
        if not _article_file(data_root, str(poh_id)).is_file():
            continue
        if not isinstance(meta, dict):
            continue
        if _is_no_material_entry(meta):
            continue
        subject = subjects.get(poh_id)
        title = str(meta.get("title") or (subject.canonical_label if subject else poh_id))
        snippet = str(meta.get("snippet") or "")
        alias_blob = " ".join(subject.aliases) if subject else ""
        haystack = _normalize_search(f"{title} {snippet} {alias_blob} {poh_id}")
        if q not in haystack:
            tokens = [token for token in q.split() if len(token) >= 2]
            if not tokens or not all(token in haystack for token in tokens):
                continue
        score = 0
        if _normalize_search(title).startswith(q):
            score += 100
        if q in _normalize_search(title):
            score += 50
        if q in haystack:
            score += 10
        hits.append(
            (
                score,
                {
                    "poh_id": poh_id,
                    "title": title,
                    "snippet": snippet,
                    "url": _article_url(str(poh_id)),
                },
            )
        )
    hits.sort(key=lambda item: (-item[0], str(item[1]["title"]).casefold()))
    return [item[1] for item in hits[:limit]]


def publish_poh_article(
    data_root: Path,
    *,
    poh_id: str,
    title: str,
    markdown: str,
    request_id: str,
    skipped_llm: bool = False,
) -> dict[str, Any]:
    no_material = is_no_material_article(markdown)
    plain_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", markdown)
    plain_text = re.sub(r"[#*`]", " ", plain_text)
    snippet = _snippet_from_text(plain_text)
    md_path = _article_markdown_file(data_root, poh_id)
    html_path = _article_file(data_root, poh_id)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_md = md_path.with_suffix(".tmp")
    tmp_md.write_text(markdown, encoding="utf-8")
    tmp_md.replace(md_path)
    display_title = "Materiale insufficiente" if no_material else title
    html = markdown_to_article_html(display_title, markdown, no_material=no_material)
    tmp_html = html_path.with_suffix(".tmp.html")
    tmp_html.write_text(html, encoding="utf-8")
    tmp_html.replace(html_path)
    catalog = _load_catalog(data_root)
    articles = catalog.setdefault("articles", {})
    articles[poh_id] = {
        "poh_id": poh_id,
        "title": display_title if no_material else title,
        "snippet": snippet,
        "url": _article_url(poh_id),
        "request_id": request_id,
        "skipped_llm": skipped_llm or no_material,
        "no_material": no_material,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_catalog(data_root, catalog)
    return {
        "poh_id": poh_id,
        "title": display_title if no_material else title,
        "url": _article_url(poh_id),
        "request_id": request_id,
        "skipped_llm": skipped_llm or no_material,
        "no_material": no_material,
        "markdown_path": str(md_path),
        "path": str(html_path),
    }


def generate_article_for_poh(
    data_root: Path,
    poh_id: str,
    *,
    settings: Settings,
    request_id: str,
    reporter=None,
    publish_no_material: bool = True,
) -> dict[str, Any]:
    subjects = list_index_subjects(data_root)
    entry = subjects.get(poh_id)
    if entry is None:
        raise ValueError(f"unknown poh_id: {poh_id}")
    title = entry.canonical_label
    request = build_poh_research_request(poh_id, title)
    result = run_research(
        request,
        data_root=data_root,
        settings=settings,
        request_id=request_id,
        reporter=reporter,
    )
    if result.skipped_llm and not publish_no_material:
        raise RuntimeError(
            "no source pages found for this POH; research model was not called"
        )
    return publish_poh_article(
        data_root,
        poh_id=poh_id,
        title=title,
        markdown=result.markdown,
        request_id=request_id,
        skipped_llm=result.skipped_llm,
    )


def resolve_article_file(data_root: Path, article_name: str) -> Path | None:
    if not article_name.endswith(".html"):
        return None
    stem = article_name[:-5]
    if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
        return None
    candidate = _articles_dir(data_root) / f"{stem}.html"
    if candidate.is_file():
        return candidate
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    if isinstance(articles, dict):
        for poh_id in articles:
            if re.sub(r"[^\w.\-]", "_", str(poh_id)) == stem:
                path = _article_file(data_root, str(poh_id))
                if path.is_file():
                    return path
    return None

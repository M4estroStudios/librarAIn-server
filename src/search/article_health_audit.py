from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.search.article_catalog import (
    _article_file,
    _article_is_complete,
    _article_markdown_file,
    _article_url,
    _articles_dir,
    _is_no_material_entry,
    _load_catalog,
    list_index_subjects,
)
from src.search.article_llm import is_no_material_article


def _safe_article_stem(poh_id: str) -> str:
    return re.sub(r"[^\w.\-]", "_", poh_id)


def _read_text_file(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, "file_missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"read_error: {exc}"
    if not text.strip():
        return "", "empty"
    return text, None


def _html_damage_reason(html: str) -> str | None:
    if len(html) < 200:
        return "HTML troppo corto"
    lowered = html.lower()
    if "<html" not in lowered:
        return "HTML senza tag html"
    if "<body" not in lowered:
        return "HTML senza body"
    return None


def _append_issue(
    issues: list[dict[str, Any]],
    counts: dict[str, int],
    *,
    issue: str,
    poh_id: str,
    label: str,
    detail: str,
    url: str | None = None,
) -> None:
    counts[issue] = counts.get(issue, 0) + 1
    payload: dict[str, Any] = {
        "issue": issue,
        "poh_id": poh_id,
        "label": label,
        "detail": detail,
    }
    if url:
        payload["url"] = url
    issues.append(payload)


def _article_has_generated_files(data_root: Path, poh_id: str) -> bool:
    return _article_file(data_root, poh_id).is_file() and _article_markdown_file(data_root, poh_id).is_file()


def _collect_generated_articles(
    data_root: Path,
    *,
    articles_meta: dict[str, Any],
    subjects: dict[str, Any],
    issue_poh_ids: set[str],
) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    for poh_id, meta in articles_meta.items():
        if not isinstance(meta, dict):
            continue
        if not _article_has_generated_files(data_root, str(poh_id)):
            continue
        entry = subjects.get(poh_id)
        label = entry.canonical_label if entry is not None else str(meta.get("title") or poh_id)
        generated.append(
            {
                "poh_id": str(poh_id),
                "label": label,
                "url": _article_url(str(poh_id)),
                "generated_at": meta.get("generated_at"),
                "no_material": bool(meta.get("no_material")),
                "ok": str(poh_id) not in issue_poh_ids,
            }
        )
    generated.sort(key=lambda item: str(item["label"]).casefold())
    return generated


def audit_articles_health(data_root: Path) -> dict[str, Any]:
    subjects = list_index_subjects(data_root)
    catalog = _load_catalog(data_root)
    articles_meta = catalog.get("articles", {})
    if not isinstance(articles_meta, dict):
        articles_meta = {}

    issues: list[dict[str, Any]] = []
    by_issue: dict[str, int] = {}
    known_stems: set[str] = set()

    for poh_id, entry in subjects.items():
        stem = _safe_article_stem(poh_id)
        known_stems.add(stem)
        label = entry.canonical_label
        meta = articles_meta.get(poh_id)
        html_path = _article_file(data_root, poh_id)
        md_path = _article_markdown_file(data_root, poh_id)
        url = _article_url(poh_id) if html_path.is_file() else None
        catalog_no_material = _is_no_material_entry(meta)

        if meta is None and not html_path.is_file():
            continue

        if meta is not None and not html_path.is_file():
            _append_issue(
                issues,
                by_issue,
                issue="damaged",
                poh_id=poh_id,
                label=label,
                detail="Voce nel catalogo ma file HTML assente",
            )
            continue

        if meta is None and html_path.is_file():
            _append_issue(
                issues,
                by_issue,
                issue="orphan_catalog",
                poh_id=poh_id,
                label=label,
                detail="File HTML presente ma POH non nel catalogo",
                url=url,
            )

        html_text: str | None = None
        if html_path.is_file():
            html_text, html_err = _read_text_file(html_path)
            if html_err == "empty":
                _append_issue(
                    issues,
                    by_issue,
                    issue="empty_file",
                    poh_id=poh_id,
                    label=label,
                    detail="File HTML vuoto",
                    url=url,
                )
            elif html_err and html_err != "file_missing":
                _append_issue(
                    issues,
                    by_issue,
                    issue="damaged",
                    poh_id=poh_id,
                    label=label,
                    detail=f"HTML illeggibile ({html_err})",
                    url=url,
                )
            elif html_text:
                html_damage = _html_damage_reason(html_text)
                if html_damage:
                    _append_issue(
                        issues,
                        by_issue,
                        issue="damaged",
                        poh_id=poh_id,
                        label=label,
                        detail=html_damage,
                        url=url,
                    )

        md_text: str | None = None
        if md_path.is_file():
            md_text, md_err = _read_text_file(md_path)
            if md_err == "empty":
                _append_issue(
                    issues,
                    by_issue,
                    issue="empty_file",
                    poh_id=poh_id,
                    label=label,
                    detail="File markdown vuoto",
                    url=url,
                )
            elif md_err and md_err != "file_missing":
                _append_issue(
                    issues,
                    by_issue,
                    issue="damaged",
                    poh_id=poh_id,
                    label=label,
                    detail=f"Markdown illeggibile ({md_err})",
                    url=url,
                )
            elif md_text is not None:
                md_text = md_text or None

        if catalog_no_material:
            _append_issue(
                issues,
                by_issue,
                issue="no_material",
                poh_id=poh_id,
                label=label,
                detail="Stub «materiale insufficiente» (non è un articolo completo)",
                url=url,
            )
            continue

        if not html_path.is_file():
            continue

        if not md_path.is_file():
            _append_issue(
                issues,
                by_issue,
                issue="markdown_missing",
                poh_id=poh_id,
                label=label,
                detail="Markdown sorgente assente",
                url=url,
            )
        elif md_text and is_no_material_article(md_text):
            _append_issue(
                issues,
                by_issue,
                issue="content_mismatch",
                poh_id=poh_id,
                label=label,
                detail="Markdown indica materiale insufficiente ma catalogo segnala articolo completo",
                url=url,
            )
        elif md_text and len(md_text.strip()) < 240 and not is_no_material_article(md_text):
            _append_issue(
                issues,
                by_issue,
                issue="incomplete",
                poh_id=poh_id,
                label=label,
                detail=f"Contenuto molto breve ({len(md_text.strip())} caratteri)",
                url=url,
            )

    for poh_id, meta in articles_meta.items():
        if poh_id in subjects:
            continue
        label = str(meta.get("title") or poh_id) if isinstance(meta, dict) else str(poh_id)
        _append_issue(
            issues,
            by_issue,
            issue="unknown_subject",
            poh_id=str(poh_id),
            label=label,
            detail="Voce catalogo per POH non presente in INDEX.json",
            url=_article_url(str(poh_id)) if _article_file(data_root, str(poh_id)).is_file() else None,
        )

    articles_dir = _articles_dir(data_root)
    if articles_dir.is_dir():
        catalog_stems = {_safe_article_stem(str(poh_id)) for poh_id in articles_meta}
        for html_path in articles_dir.glob("*.html"):
            stem = html_path.stem
            if stem in known_stems or stem in catalog_stems:
                continue
            _append_issue(
                issues,
                by_issue,
                issue="orphan_file",
                poh_id=stem,
                label=stem,
                detail="File HTML non collegato a un POH dell'indice",
                url=f"/articolo/{stem}.html",
            )

    issues.sort(key=lambda item: (str(item["issue"]), str(item["label"]).casefold()))
    complete_count = sum(
        1
        for poh_id in subjects
        if _article_is_complete(data_root, poh_id, articles_meta.get(poh_id))
    )
    affected_poh_ids = {str(item["poh_id"]) for item in issues}
    generated = _collect_generated_articles(
        data_root,
        articles_meta=articles_meta,
        subjects=subjects,
        issue_poh_ids=affected_poh_ids,
    )
    return {
        "total_subjects": len(subjects),
        "complete_count": complete_count,
        "missing_count": max(0, len(subjects) - complete_count),
        "generated_count": len(generated),
        "issues_count": len(issues),
        "affected_poh_count": len(affected_poh_ids),
        "by_issue": by_issue,
        "generated": generated,
        "issues": issues,
    }

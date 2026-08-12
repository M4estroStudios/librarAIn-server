from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.hashing import validate_source_sha256
from src.models.request import ReicatMetadata

_BOOK_ARTIFACT_FILES = {
    "manifest": "manifest.json",
    "toc": "TOC.md",
    "index": "INDEX.md",
    "biblio": "BIBLIO.json",
}


def list_biblio_candidates(data_root: Path) -> dict[str, Any]:
    from src.persistence.book_pages_audit import audit_all_books
    from src.persistence.polyindex_conflicts import count_conflicts_for_book
    from src.persistence.polyindex_deprecated import count_deprecated_for_book

    report = audit_all_books(data_root)
    books_out: list[dict[str, Any]] = []
    for book in report.get("books") or []:
        if not isinstance(book, dict):
            continue
        sha = str(book.get("source_sha256") or "")
        if not sha:
            continue
        stages = book.get("stages") if isinstance(book.get("stages"), dict) else {}
        output_stage = stages.get("output") if isinstance(stages.get("output"), dict) else {}
        output_present = int(output_stage.get("present_count") or 0)
        if output_present < 1 and not book.get("complete"):
            continue
        manifest_path = data_root / "output" / sha / "manifest.json"
        biblio_path = data_root / "output" / sha / "BIBLIO.json"
        biblio_range = None
        authors = None
        year = None
        original_page_count = None
        aligned_page_count = None
        pages_to_remove: list[int] = []
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = {}
            if isinstance(manifest, dict):
                opc = manifest.get("original_page_count")
                if isinstance(opc, int) and opc > 0:
                    original_page_count = opc
                apc = manifest.get("aligned_page_count")
                if isinstance(apc, int) and apc > 0:
                    aligned_page_count = apc
                raw_removed = manifest.get("pages_to_remove")
                if isinstance(raw_removed, list):
                    pages_to_remove = sorted(
                        {
                            int(page)
                            for page in raw_removed
                            if isinstance(page, int) and page > 0
                        }
                    )
                raw_range = manifest.get("biblio_range")
                if isinstance(raw_range, dict):
                    start = raw_range.get("start")
                    end = raw_range.get("end")
                    if isinstance(start, int) and isinstance(end, int):
                        biblio_range = {"start": start, "end": end}
                reicat = manifest.get("reicat")
                if isinstance(reicat, dict):
                    autores = reicat.get("autore") or reicat.get("authors")
                    if isinstance(autores, list):
                        authors = ", ".join(str(a) for a in autores if str(a).strip())
                    year = reicat.get("anno_di_pubblicazione") or reicat.get("publication_year")
        entry_count = 0
        if biblio_path.is_file():
            try:
                local = json.loads(biblio_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                local = {}
            if isinstance(local, dict) and isinstance(local.get("entries"), list):
                entry_count = len(local["entries"])
                if biblio_range is None and isinstance(local.get("biblio_range_original"), dict):
                    raw_range = local["biblio_range_original"]
                    start = raw_range.get("start")
                    end = raw_range.get("end")
                    if isinstance(start, int) and isinstance(end, int):
                        biblio_range = {"start": start, "end": end}
        books_out.append(
            {
                "source_sha256": sha,
                "title": book.get("title") or sha[:16],
                "slug": book.get("slug"),
                "authors": authors,
                "year": year,
                "expected_page_count": book.get("expected_page_count"),
                "original_page_count": original_page_count,
                "aligned_page_count": aligned_page_count,
                "pages_to_remove": pages_to_remove,
                "complete": bool(book.get("complete")),
                "output_pages": output_present,
                "eligible": output_present > 0,
                "has_biblio": biblio_path.is_file(),
                "biblio_entry_count": entry_count,
                "biblio_range": biblio_range,
                "deprecated_count": count_deprecated_for_book(data_root, sha),
                "conflicts_count": count_conflicts_for_book(data_root, sha),
            }
        )
    books_out.sort(key=lambda item: str(item.get("title") or "").casefold())
    return {"ok": True, "count": len(books_out), "books": books_out}


def _resolve_book_time_index_path(output_dir: Path) -> Path | None:
    from src.ingestion.polyindex.time_index import book_time_index_json_path

    slug = "book"
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        if isinstance(manifest, dict) and manifest.get("slug"):
            slug = str(manifest["slug"])
    path = book_time_index_json_path(output_dir, slug)
    if path.is_file():
        return path
    matches = sorted(output_dir.glob("TIME_INDEX_*.json"))
    return matches[0] if matches else None


def read_book_artifact(data_root: Path, source_sha256: str, kind: str) -> dict[str, Any]:
    try:
        sha = validate_source_sha256(source_sha256)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    key = (kind or "").strip().lower()
    output_dir = data_root / "output" / sha
    if key == "time_index":
        path = _resolve_book_time_index_path(output_dir)
        if path is None:
            return {"ok": False, "error": "file not found", "name": "TIME_INDEX_*.json"}
        filename = path.name
    else:
        filename = _BOOK_ARTIFACT_FILES.get(key)
        if not filename:
            return {"ok": False, "error": "unknown artifact kind"}
        path = output_dir / filename
        if not path.is_file():
            return {"ok": False, "error": "file not found", "name": filename}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc), "name": filename}
    data: Any = None
    if key in ("manifest", "biblio", "time_index"):
        try:
            data = json.loads(text)
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        except json.JSONDecodeError:
            data = None
    return {"ok": True, "kind": key, "name": filename, "text": text, "data": data}


def update_manifest_reicat(
    data_root: Path, source_sha256: str, reicat_payload: dict[str, Any]
) -> dict[str, Any]:
    from datetime import datetime, timezone

    try:
        sha = validate_source_sha256(source_sha256)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    manifest_path = data_root / "output" / sha / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "manifest not found"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"unable to read manifest: {exc}"}
    if not isinstance(manifest, dict):
        return {"ok": False, "error": "invalid manifest"}
    try:
        reicat = ReicatMetadata.model_validate(reicat_payload)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    manifest["reicat"] = reicat.model_dump(by_alias=True)
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "error": f"unable to write manifest: {exc}"}
    return {
        "ok": True,
        "source_sha256": sha,
        "reicat": manifest["reicat"],
        "manifest": manifest,
    }

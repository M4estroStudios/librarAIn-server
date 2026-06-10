"""Backfill title/slug into the per-book entries of data/polyindex/INDEX.json.

Metadata is resolved from TOC.json (which already stores title/slug per book
hash) with data/output/<hash>/manifest.json as fallback.

Usage: python -m scripts.backfill_index_book_meta [--data-root data]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ingestion.polyindex.index_json import sort_polyindex_index_file


def _load_book_meta(data_root: Path) -> dict[str, dict[str, str]]:
    meta: dict[str, dict[str, str]] = {}

    toc_path = data_root / "polyindex" / "TOC.json"
    if toc_path.is_file():
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
        books = toc.get("books")
        if isinstance(books, dict):
            for sha, entry in books.items():
                if not isinstance(entry, dict):
                    continue
                item: dict[str, str] = {}
                if isinstance(entry.get("title"), str):
                    item["title"] = entry["title"]
                if isinstance(entry.get("slug"), str):
                    item["slug"] = entry["slug"]
                if item:
                    meta[sha] = item

    output_root = data_root / "output"
    if output_root.is_dir():
        for manifest_path in sorted(output_root.glob("*/manifest.json")):
            sha = manifest_path.parent.name
            if sha in meta and "title" in meta[sha] and "slug" in meta[sha]:
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = meta.setdefault(sha, {})
            reicat = manifest.get("reicat")
            if "title" not in item and isinstance(reicat, dict):
                title = reicat.get("titolo") or reicat.get("title")
                if title:
                    item["title"] = str(title).strip()
            if "slug" not in item and isinstance(manifest.get("slug"), str):
                item["slug"] = manifest["slug"]

    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data", type=Path)
    args = parser.parse_args()

    data_root: Path = args.data_root
    index_path = data_root / "polyindex" / "INDEX.json"
    if not index_path.is_file():
        raise SystemExit(f"INDEX.json not found: {index_path}")

    meta = _load_book_meta(data_root)
    if not meta:
        raise SystemExit("no book metadata found in TOC.json or manifests")

    document = json.loads(index_path.read_text(encoding="utf-8"))
    subjects = document.get("subjects")
    if not isinstance(subjects, dict):
        raise SystemExit("INDEX.json has no subjects")

    updated_entries = 0
    unknown_hashes: set[str] = set()
    for entry in subjects.values():
        if not isinstance(entry, dict):
            continue
        books = entry.get("books")
        if not isinstance(books, dict):
            continue
        for sha, book in books.items():
            if not isinstance(book, dict):
                continue
            item = meta.get(sha)
            if item is None:
                unknown_hashes.add(sha)
                continue
            changed = False
            rebuilt: dict[str, object] = {}
            if "title" in item:
                rebuilt["title"] = item["title"]
                changed = changed or book.get("title") != item["title"]
            if "slug" in item:
                rebuilt["slug"] = item["slug"]
                changed = changed or book.get("slug") != item["slug"]
            rebuilt["aligned_pages"] = book.get("aligned_pages", [])
            rebuilt["original_pages"] = book.get("original_pages", [])
            if changed:
                books[sha] = rebuilt
                updated_entries += 1

    index_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sort_polyindex_index_file(index_path)

    print(f"updated book entries: {updated_entries}")
    if unknown_hashes:
        print(f"hashes without metadata (left untouched): {len(unknown_hashes)}")
        for sha in sorted(unknown_hashes):
            print(f"  - {sha}")


if __name__ == "__main__":
    main()

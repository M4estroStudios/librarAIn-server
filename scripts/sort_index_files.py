#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.polyindex.index_json import (
    sort_polyindex_index_file,
    sorted_polyindex_index_bytes,
)
from src.ingestion.toc_index_refine import sort_index_md_file, sorted_index_md_text


def _resolve_data_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from src.core.config import load_settings

        return Path(load_settings().data_root).resolve()
    except Exception:
        return (ROOT / "data").resolve()


def _iter_index_md_paths(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    paths: list[Path] = []
    for book_dir in sorted(output_dir.iterdir()):
        if not book_dir.is_dir():
            continue
        index_path = book_dir / "INDEX.md"
        if index_path.is_file():
            paths.append(index_path)
    return paths


def _sort_index_md(index_path: Path, *, dry_run: bool) -> str:
    raw = index_path.read_text(encoding="utf-8")
    output = sorted_index_md_text(raw)
    if output == raw:
        return f"UNCHANGED {index_path.parent.name}/INDEX.md"
    if dry_run:
        return f"DRY-RUN {index_path.parent.name}/INDEX.md: would sort"
    sort_index_md_file(index_path)
    return f"OK {index_path.parent.name}/INDEX.md: sorted"


def _sort_polyindex_json(polyindex_dir: Path, *, dry_run: bool) -> str:
    index_path = polyindex_dir / "INDEX.json"
    if not index_path.is_file():
        return "SKIP polyindex/INDEX.json: file not found"
    raw = index_path.read_bytes()
    document = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return "SKIP polyindex/INDEX.json: invalid root"
    content = sorted_polyindex_index_bytes(document)
    if content == raw:
        return "UNCHANGED polyindex/INDEX.json"
    if dry_run:
        return "DRY-RUN polyindex/INDEX.json: would sort"
    sort_polyindex_index_file(index_path)
    return "OK polyindex/INDEX.json: sorted"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sort INDEX.md per book and polyindex/INDEX.json alphabetically",
    )
    parser.add_argument(
        "--data-root",
        help="DATA_ROOT override (default: from .env or ./data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing files",
    )
    parser.add_argument(
        "--books-only",
        action="store_true",
        help="Sort only data/output/*/INDEX.md",
    )
    parser.add_argument(
        "--polyindex-only",
        action="store_true",
        help="Sort only polyindex/INDEX.json",
    )
    args = parser.parse_args(argv)
    if args.books_only and args.polyindex_only:
        print("Use at most one of --books-only and --polyindex-only")
        return 1

    data_root = _resolve_data_root(args.data_root)
    output_dir = data_root / "output"
    polyindex_dir = data_root / "polyindex"
    sort_books = not args.polyindex_only
    sort_polyindex = not args.books_only

    print(f"data_root={data_root}")
    print(f"dry_run={args.dry_run} books={sort_books} polyindex={sort_polyindex}\n")

    changed = 0
    unchanged = 0
    skipped = 0

    if sort_books:
        index_paths = _iter_index_md_paths(output_dir)
        if not index_paths:
            print(f"No INDEX.md files under {output_dir}")
        for index_path in index_paths:
            line = _sort_index_md(index_path, dry_run=args.dry_run)
            print(line)
            if line.startswith("OK ") or line.startswith("DRY-RUN "):
                changed += 1
            elif line.startswith("UNCHANGED "):
                unchanged += 1
            else:
                skipped += 1

    if sort_polyindex:
        line = _sort_polyindex_json(polyindex_dir, dry_run=args.dry_run)
        print(line)
        if line.startswith("OK ") or line.startswith("DRY-RUN "):
            changed += 1
        elif line.startswith("UNCHANGED "):
            unchanged += 1
        else:
            skipped += 1

    print(f"\nSummary: changed={changed} unchanged={unchanged} skipped={skipped}")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

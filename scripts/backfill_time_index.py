"""Backfill data/polyindex/TIME_INDEX.json from already-processed books.

Reads every data/output/<hash>/manifest.json, rebuilds a BookOutput view and
re-runs the year/date extraction page by page.

Usage: python -m scripts.backfill_time_index [--data-root data]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.time_index import sync_time_index_from_book


def _book_output_from_manifest(manifest_path: Path) -> tuple[BookOutput, str | None]:
    output_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slug = str(manifest.get("slug") or output_dir.name)
    pages: list[BookPageOutput] = []
    for entry in manifest.get("pages", []):
        if not isinstance(entry, dict):
            continue
        pages.append(
            BookPageOutput(
                aligned=int(entry["aligned"]),
                original=int(entry["original"]),
                file=output_dir / str(entry["file"]),
            )
        )
    title = None
    reicat = manifest.get("reicat")
    if isinstance(reicat, dict):
        raw_title = reicat.get("titolo") or reicat.get("title")
        if raw_title:
            title = str(raw_title).strip()
    return (
        BookOutput(
            output_dir=output_dir,
            manifest_path=manifest_path,
            slug=slug,
            pages=pages,
        ),
        title,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data", type=Path)
    args = parser.parse_args()

    data_root: Path = args.data_root
    output_root = data_root / "output"
    polyindex_dir = data_root / "polyindex"
    if not output_root.is_dir():
        raise SystemExit(f"output dir not found: {output_root}")

    manifests = sorted(output_root.glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("no manifests found")

    for manifest_path in manifests:
        sha = manifest_path.parent.name
        book_output, title = _book_output_from_manifest(manifest_path)
        path, stats = sync_time_index_from_book(
            polyindex_dir,
            sha,
            book_output,
            book_title=title,
            request_id=f"backfill-{sha[:12]}",
        )
        print(
            f"{sha[:12]}…  years={stats['n_years']:5d}  dates={stats['n_dates']:5d}  "
            f"pages={stats['n_pages_scanned']:4d}  ({title or book_output.slug})"
        )
    print(f"written: {path}")


if __name__ == "__main__":
    main()

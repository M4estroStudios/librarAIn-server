#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.hashing import compute_file_sha256
from src.models.request import EnrichedIngestRequest, IngestRequest, PageRange, ReicatMetadata
from src.persistence.book_sqlite import init_books_schema, upsert_book_reicat
from src.persistence.pipeline_runs import _sqlite_connection


def _resolve_data_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from src.core.config import load_settings

        return Path(load_settings().data_root).resolve()
    except Exception:
        return (ROOT / "data").resolve()


def _find_raw_pdf_by_digest(data_root: Path, source_sha256: str) -> Path | None:
    raw_dir = data_root / "input" / "raw"
    if not raw_dir.is_dir():
        return None
    digest = source_sha256.strip().lower()
    for pdf_path in sorted(raw_dir.glob("*.pdf")):
        try:
            if compute_file_sha256(pdf_path).lower() == digest:
                return pdf_path
        except OSError:
            continue
    return None


def _count_pdf_pages(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path), strict=False).pages)


def _book_exists(sqlite_path: str, source_sha256: str) -> bool:
    with _sqlite_connection(sqlite_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM books WHERE source_sha256 = ?",
            (source_sha256.strip().lower(),),
        ).fetchone()
    return row is not None


def _enriched_from_manifest(
    manifest: dict[str, object],
    manifest_path: Path,
    data_root: Path,
) -> tuple[EnrichedIngestRequest, bool]:
    source_sha256 = str(manifest["source_sha256"]).strip().lower()
    reicat_raw = manifest.get("reicat")
    if not isinstance(reicat_raw, dict):
        raise ValueError("manifest missing reicat object")
    reicat = ReicatMetadata.model_validate(reicat_raw)
    schema_version = str(manifest.get("pipeline_version", "1.0"))
    pdf_path = _find_raw_pdf_by_digest(data_root, source_sha256)
    skip_digest = pdf_path is None
    if pdf_path is not None:
        source_pdf_path = str(pdf_path)
        page_count = _count_pdf_pages(pdf_path)
    else:
        source_pdf_path = str(manifest_path)
        aligned = manifest.get("aligned_page_count")
        original = manifest.get("original_page_count")
        if isinstance(aligned, int) and aligned > 0:
            page_count = aligned
        elif isinstance(original, int) and original > 0:
            page_count = original
        else:
            page_count = 1
    request = IngestRequest(
        schema_version=schema_version,  # type: ignore[arg-type]
        source_pdf_path=source_pdf_path,
        pages_to_remove=[],
        toc_range=PageRange(start=1, end=1),
        index_range=PageRange(start=1, end=1),
        reicat=reicat,
    )
    enriched = EnrichedIngestRequest(
        request=request,
        source_sha256=source_sha256,
        source_pdf_path=source_pdf_path,
        source_pdf_page_count=page_count,
    )
    return enriched, skip_digest


def _iter_manifest_paths(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    manifests: list[Path] = []
    for book_dir in sorted(output_dir.iterdir()):
        if not book_dir.is_dir():
            continue
        manifest_path = book_dir / "manifest.json"
        if manifest_path.is_file():
            manifests.append(manifest_path)
    return manifests


def _backfill_one(
    manifest_path: Path,
    *,
    data_root: Path,
    sqlite_path: str,
    dry_run: bool,
    force: bool,
) -> str:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"SKIP {manifest_path.parent.name}: unreadable manifest ({exc})"
    if not isinstance(manifest, dict):
        return f"SKIP {manifest_path.parent.name}: manifest root is not an object"
    try:
        enriched, skip_digest = _enriched_from_manifest(manifest, manifest_path, data_root)
    except (ValueError, TypeError) as exc:
        return f"SKIP {manifest_path.parent.name}: {exc}"
    digest = enriched.source_sha256
    title = enriched.request.reicat.title
    if _book_exists(sqlite_path, digest) and not force:
        return f"SKIP {digest[:16]}… {title!r}: already in books (use --force to update)"
    if dry_run:
        mode = "manifest-only" if skip_digest else "pdf-verified"
        return f"DRY-RUN {digest[:16]}… {title!r}: would upsert ({mode})"
    result = upsert_book_reicat(
        enriched,
        sqlite_path,
        skip_digest_verification=skip_digest,
    )
    action = "inserted" if result.was_inserted else "updated"
    mode = "manifest-only" if skip_digest else "pdf-verified"
    return f"OK {digest[:16]}… {title!r}: {action} ({mode})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill biblioteca.db books table from data/output/*/manifest.json",
    )
    parser.add_argument(
        "--data-root",
        help="DATA_ROOT override (default: from .env or ./data)",
    )
    parser.add_argument(
        "--sqlite-path",
        help="SQLite path override (default: <data-root>/db/biblioteca.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing to the database",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upsert even when the book row already exists",
    )
    args = parser.parse_args(argv)
    data_root = _resolve_data_root(args.data_root)
    sqlite_path = args.sqlite_path or str(data_root / "db" / "biblioteca.db")
    output_dir = data_root / "output"
    manifests = _iter_manifest_paths(output_dir)
    if not manifests:
        print(f"No manifest.json files under {output_dir}")
        return 1
    if not args.dry_run:
        init_books_schema(sqlite_path)
    print(f"data_root={data_root}")
    print(f"sqlite_path={sqlite_path}")
    print(f"manifests={len(manifests)} dry_run={args.dry_run} force={args.force}\n")
    ok = 0
    skipped = 0
    failed = 0
    for manifest_path in manifests:
        line = _backfill_one(
            manifest_path,
            data_root=data_root,
            sqlite_path=sqlite_path,
            dry_run=args.dry_run,
            force=args.force,
        )
        print(line)
        if line.startswith("OK ") or line.startswith("DRY-RUN "):
            ok += 1
        elif line.startswith("SKIP "):
            skipped += 1
        else:
            failed += 1
    print(f"\nSummary: ok={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

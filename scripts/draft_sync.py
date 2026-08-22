"""CLI sync bozze: export/import JSON e pack/unpack ZIP.

Esempi:
  python -m scripts.draft_sync export
  python -m scripts.draft_sync import
  python -m scripts.draft_sync pack
  python -m scripts.draft_sync unpack
  python -m scripts.draft_sync unpack path/to/librarain-drafts.zip
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.core.config import ConfigurationError, load_settings
from src.persistence.draft_sync import (
    default_bundle_path,
    export_all_active_drafts,
    import_sync_dir,
    pack_drafts_bundle,
    unpack_drafts_bundle,
)


def _settings():
    try:
        return load_settings()
    except ConfigurationError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync bozze ingest tra PC")
    parser.add_argument(
        "command",
        choices=("export", "import", "pack", "unpack"),
        help="export/import = solo JSON in data/sync; pack/unpack = ZIP con PDF",
    )
    parser.add_argument(
        "bundle",
        nargs="?",
        default="",
        help="Path ZIP per unpack (default: data/sync/bundles/librarain-drafts.zip)",
    )
    args = parser.parse_args(argv)
    settings = _settings()
    sqlite_path = settings.sqlite_path
    data_root = Path(settings.data_root)

    if args.command == "export":
        result = export_all_active_drafts(sqlite_path, data_root)
        print(f"OK export: {result['count']} bozze → data/sync/drafts/")
        print(f"  tipologies: {result['appendix_types_path']}")
        return 0

    if args.command == "import":
        result = import_sync_dir(sqlite_path, data_root)
        print(
            f"OK import: {result['imported_count']} aggiornate, "
            f"{result['skipped']} già aggiornate, "
            f"{result['appendix_types']} tipologies"
        )
        return 0

    if args.command == "pack":
        path = pack_drafts_bundle(sqlite_path, data_root)
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"OK pack: {path} ({size_mb:.1f} MB)")
        print("Copia questo ZIP sull’altro PC, poi: make drafts-unpack")
        return 0

    # unpack
    bundle = Path(args.bundle) if args.bundle else default_bundle_path(data_root)
    if not bundle.is_file():
        print(f"bundle non trovato: {bundle}", file=sys.stderr)
        print("Suggerimento: copia librarain-drafts.zip in data/sync/bundles/", file=sys.stderr)
        return 1
    result = unpack_drafts_bundle(sqlite_path, data_root, bundle)
    print(
        f"OK unpack: {result['imported_count']} bozze + "
        f"{result['appendix_types']} tipologies da {bundle.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

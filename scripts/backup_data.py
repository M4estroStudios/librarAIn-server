#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / "backup"
DATA_DIR_NAME = "data"


def _resolve_data_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from src.core.config import load_settings

        return Path(load_settings().data_root).resolve()
    except Exception:
        return (ROOT / DATA_DIR_NAME).resolve()


def _resolve_backup_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return DEFAULT_BACKUP_DIR.resolve()


def _next_backup_zip_path(backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    counter = 0
    while True:
        candidate = backup_dir / f"{stamp}.{counter}.zip"
        if not candidate.exists():
            return candidate
        counter += 1


def _should_skip_path(relative: Path, include_tmp: bool) -> bool:
    if include_tmp:
        return False
    return bool(relative.parts) and relative.parts[0] == "tmp"


def _iter_data_files(data_root: Path, include_tmp: bool) -> list[Path]:
    if not data_root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(data_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(data_root)
        if _should_skip_path(relative, include_tmp):
            continue
        files.append(path)
    return files


def _zip_has_data_root(members: list[str]) -> bool:
    for member in members:
        normalized = member.replace("\\", "/").lstrip("/")
        if normalized == DATA_DIR_NAME or normalized.startswith(f"{DATA_DIR_NAME}/"):
            return True
    return False


def create_backup(
    data_root: Path,
    backup_dir: Path,
    *,
    include_tmp: bool,
) -> Path:
    if not data_root.is_dir():
        raise FileNotFoundError(f"data directory not found: {data_root}")

    files = _iter_data_files(data_root, include_tmp)
    if not files:
        raise RuntimeError(f"no files to back up under {data_root}")

    zip_path = _next_backup_zip_path(backup_dir)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            relative = file_path.relative_to(data_root)
            arcname = Path(DATA_DIR_NAME) / relative
            archive.write(file_path, arcname.as_posix())
    return zip_path


def restore_backup(data_root: Path, zip_path: Path) -> int:
    if not zip_path.is_file():
        raise FileNotFoundError(f"backup zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.namelist()
        if not _zip_has_data_root(members):
            raise ValueError(f"zip does not contain a top-level {DATA_DIR_NAME}/ folder: {zip_path}")

        with tempfile.TemporaryDirectory() as temp_name:
            archive.extractall(temp_name)
            extracted_data = Path(temp_name) / DATA_DIR_NAME
            if not extracted_data.is_dir():
                raise ValueError(
                    f"extracted archive is missing {DATA_DIR_NAME}/ directory: {zip_path}"
                )

            if data_root.exists():
                shutil.rmtree(data_root)
            data_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(extracted_data, data_root)

    return len(_iter_data_files(data_root, include_tmp=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and restore zipped backups of the data directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/backup_data.py backup\n"
            "  python scripts/backup_data.py backup --include-tmp\n"
            "  python scripts/backup_data.py restore backup/2026-06-12T18-30-00.0.zip\n"
            "\n"
            "Backups are written to backup/ as ISO-timestamped zip files "
            "(YYYY-MM-DDTHH-MM-SS.<counter>.zip). Each archive contains a data/ "
            "folder at the root. By default tmp/ is excluded because it holds "
            "volatile pipeline scratch files; pass --include-tmp to include it.\n"
            "\n"
            "Restore replaces the entire target data directory with the contents "
            "of the selected backup zip."
        ),
    )
    parser.add_argument(
        "--data-root",
        help="DATA_ROOT override (default: from .env or ./data)",
    )
    parser.add_argument(
        "--backup-dir",
        help="Directory for backup zip files (default: ./backup)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser(
        "backup",
        help="zip the data directory into backup/",
    )
    backup_parser.add_argument(
        "--include-tmp",
        action="store_true",
        help="include data/tmp/ in the backup (excluded by default)",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="replace data/ from a backup zip",
    )
    restore_parser.add_argument(
        "zip_path",
        help="path to a backup .zip file (for example backup/2026-06-12T18-30-00.0.zip)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    data_root = _resolve_data_root(args.data_root)
    backup_dir = _resolve_backup_dir(args.backup_dir)

    if args.command == "backup":
        try:
            zip_path = create_backup(
                data_root,
                backup_dir,
                include_tmp=args.include_tmp,
            )
        except (FileNotFoundError, RuntimeError) as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"data_root={data_root}")
        print(f"include_tmp={args.include_tmp}")
        print(f"backup_created={zip_path}")
        return 0

    zip_path = Path(args.zip_path).expanduser().resolve()
    try:
        restored_files = restore_backup(data_root, zip_path)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"data_root={data_root}")
    print(f"restored_from={zip_path}")
    print(f"restored_files={restored_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

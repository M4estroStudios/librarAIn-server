from __future__ import annotations

import secrets
from pathlib import Path

from src.core.hashing import compute_file_sha256


def upload_staging_path(data_root: Path, prefix: str = "upload") -> Path:
    """Return a temporary upload path outside input/raw."""
    staging = data_root / "tmp" / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    return staging / f".{prefix}_{secrets.token_hex(8)}.part"


def find_raw_pdf_by_sha256(data_root: Path, digest: str) -> Path | None:
    raw_dir = data_root / "input" / "raw"
    if not raw_dir.is_dir():
        return None
    for candidate in raw_dir.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
            continue
        try:
            if compute_file_sha256(candidate) == digest:
                return candidate
        except OSError:
            continue
    return None

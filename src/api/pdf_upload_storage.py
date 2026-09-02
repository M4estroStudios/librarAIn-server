from __future__ import annotations

import secrets
import shutil
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


def draft_pdfs_dir(data_root: Path) -> Path:
    path = data_root / "input" / "drafts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def draft_pdf_path_for_sha(data_root: Path, digest: str) -> Path:
    return draft_pdfs_dir(data_root) / f"{digest.lower()}.pdf"


def find_draft_pdf_by_sha256(data_root: Path, digest: str) -> Path | None:
    path = draft_pdf_path_for_sha(data_root, digest)
    return path if path.is_file() else None


def find_processed_pdf_by_sha256(data_root: Path, digest: str) -> Path | None:
    path = data_root / "input" / "processed" / f"{digest.lower()}.pdf"
    return path if path.is_file() else None


def find_source_pdf_by_sha256(data_root: Path, digest: str) -> Path | None:
    """Locate a source PDF for an ingested/draft book (drafts → processed → raw)."""
    sha = digest.strip().lower()
    if not sha:
        return None
    for finder in (
        find_draft_pdf_by_sha256,
        find_processed_pdf_by_sha256,
        find_raw_pdf_by_sha256,
    ):
        found = finder(data_root, sha)
        if found is not None:
            return found
    return None


def save_draft_pdf(data_root: Path, digest: str, source_path: Path) -> Path:
    """Copy/replace the draft PDF keyed by source SHA-256."""
    dest = draft_pdf_path_for_sha(data_root, digest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() != dest.resolve():
        shutil.copy2(source_path, dest)
    return dest


def delete_draft_pdf(data_root: Path, digest: str) -> bool:
    path = draft_pdf_path_for_sha(data_root, digest)
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True

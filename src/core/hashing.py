from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


def compute_job_id(stem: str, started_at: str) -> str:
    payload = f"{stem.strip()}{started_at}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_job_id(stem: str) -> tuple[str, str]:
    started_at = datetime.now(timezone.utc).isoformat()
    return compute_job_id(stem, started_at), started_at


def compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as source_file:
        while True:
            chunk = source_file.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

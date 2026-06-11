from __future__ import annotations

import fcntl
import shutil
from dataclasses import dataclass
from pathlib import Path

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.models.settings import Settings

SKIP_REASON_KEEP = "tmp_keep_after_success"
SKIP_REASON_LOCKED = "tmp_files_locked"
SKIP_REASON_MISSING = "tmp_dir_missing"


@dataclass
class CleanupResult:
    skipped: bool
    reason: str | None
    files_removed: int
    bytes_freed: int


def _tmp_dir_for_sha(settings: Settings, source_sha256: str) -> Path:
    return Path(settings.data_root) / "tmp" / source_sha256


def _count_tree(path: Path) -> tuple[int, int]:
    files_removed = 0
    bytes_freed = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            files_removed += 1
            try:
                bytes_freed += entry.stat().st_size
            except OSError:
                pass
    return files_removed, bytes_freed


def _has_locked_tmp_files(tmp_dir: Path) -> bool:
    tmp_files = list(tmp_dir.rglob("*.tmp"))
    if not tmp_files:
        return False
    for tmp_file in tmp_files:
        try:
            with tmp_file.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (BlockingIOError, OSError):
            return True
    return False


def cleanup_tmp_after_success(source_sha256: str, settings: Settings) -> CleanupResult:
    if settings.tmp_keep_after_success:
        return CleanupResult(
            skipped=True,
            reason=SKIP_REASON_KEEP,
            files_removed=0,
            bytes_freed=0,
        )

    tmp_dir = _tmp_dir_for_sha(settings, source_sha256)
    if not tmp_dir.exists():
        return CleanupResult(
            skipped=True,
            reason=SKIP_REASON_MISSING,
            files_removed=0,
            bytes_freed=0,
        )

    if _has_locked_tmp_files(tmp_dir):
        Log(
            WARNING_LOG_LEVEL,
            "tmp cleanup skipped: locked .tmp files",
            {
                "stage": "tmp_cleanup",
                "event": "skipped",
                "reason": SKIP_REASON_LOCKED,
                "tmp_dir": str(tmp_dir),
            },
        )
        return CleanupResult(
            skipped=True,
            reason=SKIP_REASON_LOCKED,
            files_removed=0,
            bytes_freed=0,
        )

    files_removed, bytes_freed = _count_tree(tmp_dir)
    shutil.rmtree(tmp_dir)
    Log(
        INFO_LOG_LEVEL,
        "tmp cleanup completed",
        {
            "stage": "tmp_cleanup",
            "event": "completed",
            "tmp_dir": str(tmp_dir),
            "files_removed": files_removed,
            "bytes_freed": bytes_freed,
        },
    )
    return CleanupResult(
        skipped=False,
        reason=None,
        files_removed=files_removed,
        bytes_freed=bytes_freed,
    )

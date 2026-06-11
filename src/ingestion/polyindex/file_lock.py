from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _ensure_lock_byte(lock_path: Path) -> None:
    if not lock_path.is_file():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"\0")
        return
    if lock_path.stat().st_size == 0:
        lock_path.write_bytes(b"\0")


def _acquire_lock(fd: int) -> None:
    if sys.platform == "win32":
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


def _release_lock(fd: int) -> None:
    if sys.platform == "win32":
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    _ensure_lock_byte(lock_path)
    with lock_path.open("a+b") as lock_file:
        fd = lock_file.fileno()
        if sys.platform == "win32":
            lock_file.seek(0)
        _acquire_lock(fd)
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock_file.seek(0)
            _release_lock(fd)


@contextmanager
def polyindex_dir_lock(polyindex_dir: Path, lock_name: str) -> Iterator[None]:
    polyindex_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(polyindex_dir / lock_name):
        yield

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.ingestion.polyindex.file_lock import exclusive_file_lock, polyindex_dir_lock

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class TestPolyindexFileLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exclusive_file_lock_serializes_access(self) -> None:
        lock_path = self.tmp / ".test.lock"
        order: list[str] = []
        gate = threading.Event()

        def first() -> None:
            with exclusive_file_lock(lock_path):
                order.append("held")
                gate.set()
                time.sleep(0.05)

        def second() -> None:
            gate.wait()
            with exclusive_file_lock(lock_path):
                order.append("acquired")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(order, ["held", "acquired"])

    @unittest.skipIf(sys.platform == "win32", "non-blocking flock peer check is Unix-specific")
    def test_exclusive_file_lock_blocks_nonblocking_peer(self) -> None:
        lock_path = self.tmp / ".test.lock"
        peer_blocked = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with exclusive_file_lock(lock_path):
                release.set()
                time.sleep(0.2)

        def peer() -> None:
            release.wait()
            with lock_path.open("a+b") as handle:
                fd = handle.fileno()
                if sys.platform == "win32":
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    except OSError:
                        peer_blocked.set()
                        return
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    return
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    peer_blocked.set()
                    return
                fcntl.flock(fd, fcntl.LOCK_UN)

        holder_thread = threading.Thread(target=holder)
        peer_thread = threading.Thread(target=peer)
        holder_thread.start()
        peer_thread.start()
        holder_thread.join()
        peer_thread.join()
        self.assertTrue(peer_blocked.is_set())

    def test_polyindex_dir_lock_creates_lock_file(self) -> None:
        polyindex_dir = self.tmp / "polyindex"
        with polyindex_dir_lock(polyindex_dir, ".index.lock"):
            self.assertTrue((polyindex_dir / ".index.lock").is_file())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log, logInit
from src.ingestion.tmp_cleanup import (
    SKIP_REASON_KEEP,
    SKIP_REASON_LOCKED,
    cleanup_tmp_after_success,
)
from src.models.settings import Settings

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

SHA = "cafebabe" * 8


def _settings(data_root: str, *, tmp_keep: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "DATA_ROOT": data_root,
            "OPENAI_PROVIDER": "local",
            "TMP_KEEP_AFTER_SUCCESS": "true" if tmp_keep else "false",
        }
    )


def _seed_tmp_tree(tmp_dir: Path) -> None:
    (tmp_dir / "pages").mkdir(parents=True)
    (tmp_dir / "pages" / "p.0001.png").write_bytes(b"x" * 128)
    (tmp_dir / "pages" / "p.0001.txt").write_text("sample text", encoding="utf-8")


class TestTmpCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        logInit(INFO_LOG_LEVEL, log_dir=self.data_root / "log")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_keep_flag_true_preserves_tmp(self) -> None:
        tmp_dir = self.data_root / "tmp" / SHA
        _seed_tmp_tree(tmp_dir)
        settings = _settings(str(self.data_root), tmp_keep=True)

        result = cleanup_tmp_after_success(SHA, settings)

        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, SKIP_REASON_KEEP)
        self.assertEqual(result.files_removed, 0)
        self.assertEqual(result.bytes_freed, 0)
        self.assertTrue(tmp_dir.exists())
        self.assertTrue((tmp_dir / "pages" / "p.0001.png").exists())

    def test_keep_flag_false_removes_tmp_and_logs(self) -> None:
        tmp_dir = self.data_root / "tmp" / SHA
        _seed_tmp_tree(tmp_dir)
        settings = _settings(str(self.data_root), tmp_keep=False)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = cleanup_tmp_after_success(SHA, settings)

        self.assertFalse(result.skipped)
        self.assertIsNone(result.reason)
        self.assertGreater(result.files_removed, 0)
        self.assertGreater(result.bytes_freed, 0)
        self.assertFalse(tmp_dir.exists())

        payload = Log(
            INFO_LOG_LEVEL,
            "probe",
            {"stage": "tmp_cleanup"},
            json=True,
        )
        self.assertIsNotNone(payload)
        record = json.loads(payload or "")
        self.assertEqual(record["stage"], "tmp_cleanup")

        output = buffer.getvalue()
        self.assertIn("tmp cleanup completed", output)
        self.assertIn("bytes_freed", output)

    def test_locked_tmp_file_skips_cleanup_with_warning(self) -> None:
        tmp_dir = self.data_root / "tmp" / SHA
        _seed_tmp_tree(tmp_dir)
        locked_tmp = tmp_dir / "writing.tmp"
        locked_tmp.write_bytes(b"in progress")
        settings = _settings(str(self.data_root), tmp_keep=False)

        with locked_tmp.open("rb") as handle:
            fd = handle.fileno()
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = cleanup_tmp_after_success(SHA, settings)
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, SKIP_REASON_LOCKED)
        self.assertEqual(result.files_removed, 0)
        self.assertEqual(result.bytes_freed, 0)
        self.assertTrue(tmp_dir.exists())

        output = buffer.getvalue()
        self.assertIn("tmp cleanup skipped", output)
        self.assertIn("locked .tmp files", output)

        payload = Log(
            WARNING_LOG_LEVEL,
            "probe",
            {"stage": "tmp_cleanup", "reason": SKIP_REASON_LOCKED},
            json=True,
            override=True,
        )
        self.assertIsNotNone(payload)
        record = json.loads(payload or "")
        self.assertEqual(record["level"], "WARNING")
        self.assertEqual(record["reason"], SKIP_REASON_LOCKED)


if __name__ == "__main__":
    unittest.main()

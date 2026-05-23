from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from src.core.log import (
    DEBUG_LOG_LEVEL,
    INFO_LOG_LEVEL,
    Log,
    bind_log_context,
    logInit,
    reset_log_context,
    safe_text,
)


class TestLogging(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name) / "logs"
        logInit(INFO_LOG_LEVEL, log_dir=self.log_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_safe_text_truncates_long_values(self) -> None:
        text = "x" * 250
        self.assertEqual(safe_text(text, 200), ("x" * 200) + "...")

    def test_log_injects_bound_context_into_params(self) -> None:
        buffer = io.StringIO()
        request_token, sha_token = bind_log_context(
            request_id="req-log-001",
            source_sha256="deadbeef" * 8,
        )
        try:
            with redirect_stdout(buffer):
                Log(
                    INFO_LOG_LEVEL,
                    "configured logger message",
                    {"stage": "setup", "event": "init"},
                )
        finally:
            reset_log_context(request_token, sha_token)

        output = buffer.getvalue()
        self.assertIn("configured logger message", output)
        self.assertIn("request_id", output)
        self.assertIn("req-log-001", output)
        self.assertIn("source_sha256", output)
        self.assertIn("deadbeef" * 8, output)
        self.assertIn("setup", output)

    def test_log_without_context_omits_request_fields(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            Log(INFO_LOG_LEVEL, "plain message")
        output = buffer.getvalue()
        self.assertIn("plain message", output)
        self.assertNotIn("request_id", output)

    def test_log_json_returns_structured_record(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            payload = Log(
                INFO_LOG_LEVEL,
                "json message",
                {"stage": "setup", "event": "init"},
                json=True,
            )
        self.assertIsNotNone(payload)
        record = json.loads(payload or "")
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["message"], "json message")
        self.assertEqual(record["stage"], "setup")
        self.assertEqual(record["event"], "init")
        self.assertIn("ts", record)
        self.assertIn("file", record)
        self.assertIn("caller", record)
        self.assertIn("json message", buffer.getvalue())

    def test_log_to_file_appends_daily_json_line(self) -> None:
        with redirect_stdout(io.StringIO()):
            Log(INFO_LOG_LEVEL, "file message", {"stage": "persist"}, to_file=True)

        day = datetime.now().astimezone().date().isoformat()
        log_path = self.log_dir / f"{day}.log"
        self.assertTrue(log_path.is_file())
        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        record = json.loads(lines[-1])
        self.assertEqual(record["message"], "file message")
        self.assertEqual(record["stage"], "persist")

    def test_log_json_and_to_file_both_enabled(self) -> None:
        with redirect_stdout(io.StringIO()):
            payload = Log(
                INFO_LOG_LEVEL,
                "dual message",
                {"stage": "dual"},
                json=True,
                to_file=True,
            )
        self.assertIsNotNone(payload)
        record = json.loads(payload or "")
        self.assertEqual(record["message"], "dual message")

        day = datetime.now().astimezone().date().isoformat()
        log_path = self.log_dir / f"{day}.log"
        file_record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(file_record["message"], "dual message")
        self.assertEqual(file_record["stage"], "dual")

    def test_suppressed_log_does_not_return_json_or_write_file(self) -> None:
        day = datetime.now().astimezone().date().isoformat()
        log_path = self.log_dir / f"{day}.log"
        with redirect_stdout(io.StringIO()):
            payload = Log(DEBUG_LOG_LEVEL, "hidden message", json=True, to_file=True)
        self.assertIsNone(payload)
        if log_path.exists():
            self.assertNotIn("hidden message", log_path.read_text(encoding="utf-8"))


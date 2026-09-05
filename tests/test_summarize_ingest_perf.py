from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.summarize_ingest_perf import _estimate, _fmt_seconds, _print_run, main
from src.persistence.book_sqlite import init_books_schema
from src.persistence.pipeline_runs import create_pipeline_run, save_pipeline_run_timing


class TestSummarizeIngestPerf(unittest.TestCase):
    def test_fmt_seconds(self) -> None:
        self.assertEqual(_fmt_seconds(12.34), "12.34s")
        self.assertIn("min", _fmt_seconds(90))
        self.assertIn("h", _fmt_seconds(7200))
        self.assertEqual(_fmt_seconds(None), "-")

    def test_estimate_mentions_llm_call_count(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            _estimate(798, 4, 60)
        text = buffer.getvalue()
        self.assertIn("1596", text)
        self.assertIn("floor rate-limit", text)

    def test_print_run_ranks_phases(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            _print_run(
                {
                    "request_id": "abc",
                    "source_sha256": "a" * 64,
                    "status": "succeeded",
                    "total_pages": 10,
                    "compute_mode": "local",
                    "book_title": "Demo",
                    "timing": {
                        "total_seconds": 100,
                        "phases": {"render": 10.0, "stage2_vision": 80.0},
                    },
                }
            )
        text = buffer.getvalue()
        vision_at = text.find("stage2_vision")
        render_at = text.find("render")
        self.assertGreater(vision_at, 0)
        self.assertGreater(render_at, vision_at)

    def test_main_reads_empty_or_populated_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = str(Path(tmp) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id="req-perf",
                source_sha256="b" * 64,
                pipeline_version="1.0",
                total_pages=3,
            )
            save_pipeline_run_timing(
                sqlite_path,
                request_id="req-perf",
                timing={"total_seconds": 12.5, "phases": {"render": 4.0, "stage1_ocr": 8.5}},
            )
            settings = type(
                "S",
                (),
                {
                    "sqlite_path": sqlite_path,
                    "max_parallel_request": 4,
                    "rate_limit_per_minute": 60,
                    "time_index_use_llm": False,
                },
            )()
            buffer = io.StringIO()
            argv = [
                "summarize_ingest_perf",
                "--sqlite",
                sqlite_path,
                "--data-root",
                tmp,
                "--limit",
                "5",
                "--estimate-pages",
                "10",
            ]
            with (
                patch("sys.argv", argv),
                patch("sys.stdout", buffer),
                patch("src.core.config.load_settings", return_value=settings),
            ):
                code = main()
            self.assertEqual(code, 0)
            text = buffer.getvalue()
            self.assertIn("req-perf", text)
            self.assertIn("stage1_ocr", text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.persistence.book_sqlite import init_books_schema
from src.persistence.pipeline_runs import (
    _sqlite_connection,
    create_pipeline_run,
    get_pipeline_run_by_request_id,
    mark_pipeline_run_finished,
)

SHA = "deadbeef" * 8
REQUEST_ID = "req-pipeline-001"


class TestPipelineRuns(unittest.TestCase):
    def test_create_get_and_mark_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)

            row_id = create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=10,
            )
            self.assertIsInstance(row_id, int)

            running = get_pipeline_run_by_request_id(sqlite_path, REQUEST_ID)
            assert running is not None
            self.assertEqual(running["request_id"], REQUEST_ID)
            self.assertEqual(running["source_sha256"], SHA)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["pipeline_version"], "1.0")
            self.assertEqual(running["total_pages"], 10)
            self.assertEqual(running["succeeded_pages"], 0)
            self.assertEqual(running["failed_pages"], 0)
            self.assertIsNone(running["finished_at"])
            self.assertIsNone(running["last_error"])

            mark_pipeline_run_finished(
                sqlite_path,
                request_id=REQUEST_ID,
                status="succeeded",
                succeeded_pages=8,
                failed_pages=2,
            )

            finished = get_pipeline_run_by_request_id(sqlite_path, REQUEST_ID)
            assert finished is not None
            self.assertEqual(finished["status"], "succeeded")
            self.assertEqual(finished["succeeded_pages"], 8)
            self.assertEqual(finished["failed_pages"], 2)
            self.assertIsNotNone(finished["finished_at"])

    def test_duplicate_request_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=5,
            )
            with self.assertRaises(RuntimeError):
                create_pipeline_run(
                    sqlite_path,
                    request_id=REQUEST_ID,
                    source_sha256=SHA,
                    pipeline_version="1.0",
                    total_pages=5,
                )

    def test_init_books_schema_creates_pipeline_runs_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            row_id = create_pipeline_run(
                sqlite_path,
                request_id="req-schema-check",
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=1,
            )
            self.assertIsInstance(row_id, int)

    def test_init_books_schema_uses_wal_journal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            with _sqlite_connection(sqlite_path) as conn:
                row = conn.execute("PRAGMA journal_mode").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row[0]).lower(), "wal")


if __name__ == "__main__":
    unittest.main()

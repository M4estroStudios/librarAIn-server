from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.api.job_control import (
    pipeline_run_can_resume,
    pipeline_run_is_interrupted,
    try_handle_job_resume_post,
    try_handle_job_terminate_post,
)
from src.api.job_history import _historical_display_status, _history_row_from_pipeline
from src.api.job_registry import JobRegistry
from src.persistence.book_sqlite import init_books_schema
from src.persistence.pipeline_runs import (
    create_pipeline_run,
    get_pipeline_run_by_request_id,
)

SHA = "aabbccdd" * 8
REQUEST_ID = "req-interrupted-001"


class _FakeHandler:
    pass


class TestInterruptedJobControl(unittest.TestCase):
    def test_pipeline_run_is_interrupted(self) -> None:
        self.assertTrue(
            pipeline_run_is_interrupted({"status": "running", "finished_at": None})
        )
        self.assertFalse(
            pipeline_run_is_interrupted(
                {"status": "running", "finished_at": "2026-08-04T10:00:00+00:00"}
            )
        )
        self.assertFalse(
            pipeline_run_is_interrupted({"status": "failed", "finished_at": None})
        )

    def test_history_row_marks_interrupted_resumable(self) -> None:
        row = _history_row_from_pipeline(
            {
                "request_id": REQUEST_ID,
                "status": "running",
                "finished_at": None,
                "source_sha256": SHA,
                "started_at": "2026-08-04T10:00:00+00:00",
                "book_title": "Libro",
                "last_error": None,
            }
        )
        self.assertEqual(row["display_status"], "interrotto")
        self.assertTrue(row["resumable"])
        self.assertTrue(row["terminable"])

    def test_aborted_display_status(self) -> None:
        self.assertEqual(
            _historical_display_status("aborted", "2026-08-04T10:00:00+00:00"),
            "annullato",
        )

    @patch("src.api.job_control._book_has_missing_pages", return_value=True)
    def test_aborted_with_gaps_is_resumable(self, _mock_gaps: MagicMock) -> None:
        run = {
            "status": "aborted",
            "finished_at": "2026-08-04T10:00:00+00:00",
            "source_sha256": SHA,
            "last_error": "resumed as other-id",
        }
        self.assertTrue(pipeline_run_can_resume(run, Path(".")))
        row = _history_row_from_pipeline(run | {
            "request_id": REQUEST_ID,
            "started_at": "2026-08-04T09:00:00+00:00",
            "book_title": "Libro",
        }, data_root=Path("."))
        self.assertEqual(row["display_status"], "annullato")
        self.assertTrue(row["resumable"])
        self.assertFalse(row["terminable"])

    @patch("src.api.job_control._start_ingest_continue_job")
    @patch("src.api.job_control.pipeline_run_can_resume", return_value=True)
    def test_resume_allows_aborted_incomplete(
        self, _mock_can: MagicMock, mock_start: MagicMock
    ) -> None:
        mock_start.return_value = (
            "new-job-id",
            "gaps_repair",
            "/api/ingest/new-job-id/status",
            "/api/ingest/new-job-id/events",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=10,
            )
            from src.persistence.pipeline_runs import mark_pipeline_run_finished

            mark_pipeline_run_finished(
                sqlite_path,
                request_id=REQUEST_ID,
                status="aborted",
                succeeded_pages=3,
                failed_pages=0,
                last_error="resumed as old-child",
            )
            settings = MagicMock()
            settings.sqlite_path = sqlite_path
            responses: list[tuple[int, dict]] = []

            def send_json(_handler, status, payload):
                responses.append((status, payload))

            def read_body(_handler, _limit):
                return json.dumps({"job_id": REQUEST_ID}).encode("utf-8")

            handled = try_handle_job_resume_post(
                "/api/system/jobs/resume",
                _FakeHandler(),
                data_root=Path(tmp_dir),
                settings=settings,
                registry=JobRegistry(),
                job_semaphore=threading.Semaphore(1),
                max_concurrent_jobs=1,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[0][0], 202)
            mock_start.assert_called_once()

    def test_terminate_marks_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=10,
            )
            settings = MagicMock()
            settings.sqlite_path = sqlite_path
            responses: list[tuple[int, dict]] = []

            def send_json(_handler, status, payload):
                responses.append((status, payload))

            def read_body(_handler, _limit):
                return json.dumps({"job_id": REQUEST_ID}).encode("utf-8")

            handled = try_handle_job_terminate_post(
                "/api/system/jobs/terminate",
                _FakeHandler(),
                settings=settings,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[0][0], 200)
            finished = get_pipeline_run_by_request_id(sqlite_path, REQUEST_ID)
            assert finished is not None
            self.assertEqual(finished["status"], "aborted")
            self.assertIsNotNone(finished["finished_at"])
            self.assertEqual(finished["last_error"], "terminated by user")

    def test_terminate_rejects_finished_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=10,
            )
            from src.persistence.pipeline_runs import mark_pipeline_run_finished

            mark_pipeline_run_finished(
                sqlite_path,
                request_id=REQUEST_ID,
                status="succeeded",
                succeeded_pages=10,
                failed_pages=0,
            )
            settings = MagicMock()
            settings.sqlite_path = sqlite_path
            responses: list[tuple[int, dict]] = []

            def send_json(_handler, status, payload):
                responses.append((status, payload))

            def read_body(_handler, _limit):
                return json.dumps({"job_id": REQUEST_ID}).encode("utf-8")

            handled = try_handle_job_terminate_post(
                "/api/system/jobs/terminate",
                _FakeHandler(),
                settings=settings,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[0][0], 409)

    @patch("src.api.job_control._start_ingest_continue_job")
    def test_resume_starts_job_and_aborts_old(self, mock_start: MagicMock) -> None:
        mock_start.return_value = (
            "new-job-id",
            "resume",
            "/api/ingest/new-job-id/status",
            "/api/ingest/new-job-id/events",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id=REQUEST_ID,
                source_sha256=SHA,
                pipeline_version="1.0",
                total_pages=10,
            )
            settings = MagicMock()
            settings.sqlite_path = sqlite_path
            responses: list[tuple[int, dict]] = []

            def send_json(_handler, status, payload):
                responses.append((status, payload))

            def read_body(_handler, _limit):
                return json.dumps({"job_id": REQUEST_ID}).encode("utf-8")

            handled = try_handle_job_resume_post(
                "/api/system/jobs/resume",
                _FakeHandler(),
                data_root=Path(tmp_dir),
                settings=settings,
                registry=JobRegistry(),
                job_semaphore=threading.Semaphore(1),
                max_concurrent_jobs=1,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[0][0], 202)
            self.assertEqual(responses[0][1]["job_id"], "new-job-id")
            finished = get_pipeline_run_by_request_id(sqlite_path, REQUEST_ID)
            assert finished is not None
            self.assertEqual(finished["status"], "aborted")
            self.assertIn("resumed as new-job-id", str(finished["last_error"]))


if __name__ == "__main__":
    unittest.main()

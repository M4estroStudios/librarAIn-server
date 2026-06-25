from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.persistence.book_sqlite import init_books_schema
from src.persistence.research_runs import (
    create_research_run_accepted,
    get_research_run_by_request_id,
    mark_research_run_failed,
    mark_research_run_running,
    mark_research_run_succeeded,
)
from src.search.request_schema import ResearchPoh

REQUEST_ID = "req-research-001"


class TestResearchRuns(unittest.TestCase):
    def test_create_running_succeeded_and_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)

            create_research_run_accepted(
                sqlite_path,
                request_id=REQUEST_ID,
                query="Alpha Test",
                poh=ResearchPoh(id="subj_alpha", label="Alpha Test"),
                pipeline_version="2.0",
            )
            accepted = get_research_run_by_request_id(sqlite_path, REQUEST_ID)
            assert accepted is not None
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["poh_id"], "subj_alpha")
            self.assertEqual(json.loads(accepted["context_books_json"]), {})
            self.assertEqual(json.loads(accepted["subjects_matched_json"]), [])

            mark_research_run_running(sqlite_path, request_id=REQUEST_ID)
            running = get_research_run_by_request_id(sqlite_path, REQUEST_ID)
            assert running is not None
            self.assertEqual(running["status"], "running")

            mark_research_run_succeeded(
                sqlite_path,
                request_id=REQUEST_ID,
                context_books={"abc123": [1, 2, 3]},
                subjects_matched=[{"canonical_id": "subj_alpha", "method": "exact"}],
                citations_count=4,
            )
            succeeded = get_research_run_by_request_id(sqlite_path, REQUEST_ID)
            assert succeeded is not None
            self.assertEqual(succeeded["status"], "succeeded")
            self.assertEqual(json.loads(succeeded["context_books_json"]), {"abc123": [1, 2, 3]})
            self.assertEqual(
                json.loads(succeeded["subjects_matched_json"]),
                [{"canonical_id": "subj_alpha", "method": "exact"}],
            )
            self.assertEqual(succeeded["citations_count"], 4)
            self.assertIsNotNone(succeeded["finished_at"])

            failed_id = "req-research-failed"
            create_research_run_accepted(
                sqlite_path,
                request_id=failed_id,
                query="Beta Test",
                poh=None,
                pipeline_version="2.0",
            )
            mark_research_run_running(sqlite_path, request_id=failed_id)
            mark_research_run_failed(
                sqlite_path,
                request_id=failed_id,
                last_error="polyindex vuoto",
            )
            failed = get_research_run_by_request_id(sqlite_path, failed_id)
            assert failed is not None
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["last_error"], "polyindex vuoto")

    def test_init_books_schema_creates_research_runs_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_research_run_accepted(
                sqlite_path,
                request_id="req-schema-check",
                query="schema check",
                poh=None,
                pipeline_version="2.0",
            )
            row = get_research_run_by_request_id(sqlite_path, "req-schema-check")
            self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()

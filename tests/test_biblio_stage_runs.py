from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.api.job_history import list_job_history
from src.api.job_registry import JobRegistry
from src.api.research_batch_registry import ResearchBatchRegistry
from src.persistence.biblio_stage_runs import (
    create_biblio_stage_run,
    list_biblio_stage_runs,
    mark_biblio_stage_run_done,
    mark_biblio_stage_run_failed,
)
from src.persistence.book_sqlite import init_books_schema


SHA = "b" * 64
JOB_ID = "c" * 64


class BiblioStageRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sqlite_path = str(self.root / "db.sqlite")
        init_books_schema(self.sqlite_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_done_and_list(self) -> None:
        create_biblio_stage_run(
            self.sqlite_path,
            request_id=JOB_ID,
            source_sha256=SHA,
            stage="polyindex_index",
            compute_mode="local",
        )
        mark_biblio_stage_run_done(
            self.sqlite_path,
            request_id=JOB_ID,
            result={"n_new": 3, "n_match": 1},
        )
        rows = list_biblio_stage_runs(self.sqlite_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], JOB_ID)
        self.assertEqual(rows[0]["stage"], "polyindex_index")
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["result"], {"n_new": 3, "n_match": 1})

    def test_failed_run(self) -> None:
        create_biblio_stage_run(
            self.sqlite_path,
            request_id=JOB_ID,
            source_sha256=SHA,
            stage="polyindex_toc",
        )
        mark_biblio_stage_run_failed(
            self.sqlite_path,
            request_id=JOB_ID,
            last_error="boom",
        )
        rows = list_biblio_stage_runs(self.sqlite_path)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["last_error"], "boom")

    def test_list_job_history_includes_biblio_stage(self) -> None:
        create_biblio_stage_run(
            self.sqlite_path,
            request_id=JOB_ID,
            source_sha256=SHA,
            stage="polyindex_index",
        )
        mark_biblio_stage_run_done(
            self.sqlite_path,
            request_id=JOB_ID,
            result={"ok": True},
        )
        jobs = list_job_history(
            sqlite_path=self.sqlite_path,
            registry=JobRegistry(),
            batch_registry=ResearchBatchRegistry(self.root),
            data_root=self.root,
        )
        match = [job for job in jobs if job.get("job_id") == JOB_ID]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["job_kind"], "biblio")
        self.assertEqual(match[0]["stage"], "polyindex_index")
        self.assertEqual(match[0]["display_status"], "completato")
        self.assertEqual(match[0]["attempt_number"], 1)
        self.assertEqual(match[0]["attempt_total"], 1)


if __name__ == "__main__":
    unittest.main()

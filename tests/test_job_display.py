from __future__ import annotations

import unittest

from src.api.job_display import (
    build_batch_stage_segments,
    job_display_label,
    job_display_status,
)
from src.core.hashing import compute_job_id, new_job_id


class TestComputeJobId(unittest.TestCase):
    def test_deterministic_sha256(self) -> None:
        started = "2026-07-05T12:00:00+00:00"
        job_id = compute_job_id("i-rioni-di-roma", started)
        self.assertEqual(len(job_id), 64)
        self.assertEqual(job_id, compute_job_id("i-rioni-di-roma", started))

    def test_new_job_id_returns_pair(self) -> None:
        job_id, started_at = new_job_id("libro")
        self.assertEqual(job_id, compute_job_id("libro", started_at))
        self.assertIn("T", started_at)


class TestJobDisplayStatus(unittest.TestCase):
    def test_running_maps_to_in_corso(self) -> None:
        self.assertEqual(job_display_status("running"), "in_corso")
        self.assertEqual(job_display_label("in_corso"), "In corso")

    def test_done_maps_to_completato(self) -> None:
        self.assertEqual(job_display_status("done"), "completato")
        self.assertEqual(job_display_status("succeeded"), "completato")

    def test_queued_maps_to_in_coda(self) -> None:
        self.assertEqual(job_display_status("queued"), "in_coda")

    def test_stale_running_in_history_is_interrupted(self) -> None:
        from src.api.job_history import _historical_display_status

        self.assertEqual(_historical_display_status("running", None), "interrotto")
        self.assertEqual(_historical_display_status("accepted", None), "interrotto")
        self.assertEqual(_historical_display_status("succeeded", "2026-06-27T00:00:00+00:00"), "completato")

    def test_batch_stage_segments_sum_phases(self) -> None:
        child_a = {
            "phases": [
                {"phase": "research_collect", "status": "done"},
                {"phase": "research_filter", "status": "active"},
                {"phase": "research_article", "status": "pending"},
            ]
        }
        child_b = {
            "phases": [
                {"phase": "research_collect", "status": "done"},
                {"phase": "research_filter", "status": "pending"},
            ]
        }
        segments = build_batch_stage_segments([child_a, child_b])
        self.assertEqual(len(segments), 5)
        self.assertEqual(segments[0]["status"], "done")
        self.assertEqual(segments[1]["status"], "active")


if __name__ == "__main__":
    unittest.main()

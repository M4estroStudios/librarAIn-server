from __future__ import annotations

import threading
import unittest

from src.api.job_registry import JobRegistry
from src.ingestion.progress import make_event


class TestJobRegistryBasics(unittest.TestCase):
    def test_create_and_status(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        status = registry.get_status(job_id)
        assert status is not None
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["events"], [])
        self.assertIsNone(status["result"])

    def test_unknown_job_returns_none(self) -> None:
        registry = JobRegistry()
        self.assertIsNone(registry.get_status("missing"))

    def test_emit_updates_status_and_steps(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.set_global_total(job_id, 2)
        registry.emit(job_id, make_event("stage1_ocr", "started"))
        registry.emit(
            job_id, make_event("stage1_ocr", "page_progress", counts_as_step=True)
        )
        status = registry.get_status(job_id)
        assert status is not None
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["global_step"], 1)
        self.assertEqual(status["global_total"], 2)

    def test_terminal_done_event(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.emit(
            job_id, make_event("pipeline", "done", result={"book": "x"})
        )
        status = registry.get_status(job_id)
        assert status is not None
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["result"], {"book": "x"})

    def test_terminal_error_event(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.emit(job_id, make_event("pipeline", "error", message="boom"))
        status = registry.get_status(job_id)
        assert status is not None
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["error"], "boom")


class TestJobRegistrySubscribe(unittest.TestCase):
    def test_subscribe_replays_history_until_terminal(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.emit(job_id, make_event("pipeline", "started"))
        registry.emit(job_id, make_event("pipeline", "done", result={}))
        events = list(registry.subscribe(job_id))
        self.assertEqual([e["status"] for e in events], ["started", "done"])

    def test_subscribe_receives_live_events(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        received: list[str] = []

        def consumer() -> None:
            for ev in registry.subscribe(job_id):
                received.append(ev["status"])

        t = threading.Thread(target=consumer)
        t.start()
        registry.emit(job_id, make_event("pipeline", "started"))
        registry.emit(job_id, make_event("pipeline", "done", result={}))
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(received, ["started", "done"])

    def test_subscribe_unknown_job_yields_nothing(self) -> None:
        registry = JobRegistry()
        self.assertEqual(list(registry.subscribe("missing")), [])


class TestJobRegistryEviction(unittest.TestCase):
    def test_finished_job_evicted_after_ttl(self) -> None:
        registry = JobRegistry(ttl_seconds=0.0)
        old_job = registry.create_job()
        registry.emit(old_job, make_event("pipeline", "done", result={}))
        # Creating a new job triggers eviction of expired finished jobs.
        registry.create_job()
        self.assertIsNone(registry.get_status(old_job))

    def test_running_job_never_evicted(self) -> None:
        registry = JobRegistry(ttl_seconds=0.0)
        running = registry.create_job()
        registry.emit(running, make_event("pipeline", "started"))
        registry.create_job()
        self.assertIsNotNone(registry.get_status(running))

    def test_finished_jobs_capped(self) -> None:
        registry = JobRegistry(ttl_seconds=3600.0, max_finished_jobs=2)
        finished: list[str] = []
        for _ in range(4):
            job_id = registry.create_job()
            registry.emit(job_id, make_event("pipeline", "done", result={}))
            finished.append(job_id)
        registry.create_job()
        remaining = [j for j in finished if registry.get_status(j) is not None]
        self.assertLessEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()

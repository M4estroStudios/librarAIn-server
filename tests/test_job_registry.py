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

    def test_list_active_jobs_summarizes_phases(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.set_global_total(job_id, 3)
        registry.emit(job_id, make_event("page_repair", "started", aligned_page=7, source_sha256="a" * 64))
        registry.emit(job_id, make_event("stage2_vision", "started", page_total=1))
        registry.emit(
            job_id,
            make_event("stage2_vision", "page_progress", counts_as_step=True, page_total=1),
        )
        jobs = registry.list_active_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], job_id)
        self.assertIn("Riparazione pagina 7", jobs[0]["title"])
        self.assertEqual(jobs[0]["global_step"], 1)
        self.assertEqual(jobs[0]["global_total"], 3)
        phase_names = [phase["phase"] for phase in jobs[0]["phases"]]
        self.assertIn("page_repair", phase_names)
        self.assertIn("stage2_vision", phase_names)

    def test_list_active_jobs_research_headline(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job(job_kind="research")
        registry.emit(
            job_id,
            {
                "phase": "research",
                "status": "progress",
                "query": "Battaglia di Cannae",
                "poh_id": "subj_hannibal",
                "poh_label": "Annibale",
                "message": "Annibale",
            },
        )
        registry.emit(job_id, make_event("research_article", "started"))
        jobs = registry.list_active_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Articolo: Annibale")
        self.assertIn("subj_hannibal", jobs[0]["subtitle"] or "")
        self.assertEqual(jobs[0]["poh_label"], "Annibale")

    def test_list_active_jobs_research_phases(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job(job_kind="research")
        registry.emit(
            job_id,
            {
                "phase": "research_collect",
                "status": "started",
                "page_total": 15,
                "subject_pages": 12,
                "time_pages": 3,
            },
        )
        for _ in range(5):
            registry.emit(
                job_id,
                {
                    "phase": "research_collect",
                    "status": "progress",
                    "counts_as_step": True,
                    "page_total": 15,
                },
            )
        registry.emit(
            job_id,
            {
                "phase": "research_collect",
                "status": "completed",
                "page_total": 15,
                "subject_pages": 12,
                "time_pages": 3,
                "loaded_pages": 5,
            },
        )
        jobs = registry.list_active_jobs()
        self.assertEqual(len(jobs), 1)
        collect_phase = next(p for p in jobs[0]["phases"] if p["phase"] == "research_collect")
        self.assertEqual(collect_phase["total"], 15)
        self.assertEqual(collect_phase["done"], 15)
        self.assertEqual(collect_phase["status"], "done")

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

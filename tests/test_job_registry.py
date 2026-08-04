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

    def test_create_job_with_explicit_id(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job(job_id="a" * 64)
        self.assertEqual(registry.get_status(job_id)["job_id"], "a" * 64)

    def test_create_job_rejects_duplicate_id(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job(job_id="b" * 64)
        with self.assertRaises(ValueError):
            registry.create_job(job_id=job_id)

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

    def test_glm_ingest_phases_include_stage3_editor(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.emit(job_id, make_event("render", "completed", rendered_page_count=3))
        registry.emit(job_id, make_event("stage1_glm_ocr", "started", page_total=3))
        jobs = registry.list_active_jobs()
        self.assertEqual(len(jobs), 1)
        phase_ids = [p["phase"] for p in jobs[0]["phases"]]
        self.assertIn("stage1_glm_ocr", phase_ids)
        self.assertIn("stage3_editor", phase_ids)
        self.assertLess(phase_ids.index("stage1_glm_ocr"), phase_ids.index("stage3_editor"))

    def test_glm_gaps_repair_uses_glm_phase_order(self) -> None:
        registry = JobRegistry()
        job_id = registry.create_job()
        registry.set_global_progress(job_id, step=5, total=10)
        registry.emit(
            job_id,
            make_event("stage1_glm_ocr", "baseline", done=2, page_total=5),
        )
        registry.emit(
            job_id,
            make_event("stage3_editor", "baseline", done=1, page_total=5),
        )
        registry.emit(job_id, make_event("gaps_repair", "started", page_total=5))
        registry.emit(job_id, make_event("stage1_glm_ocr", "started", page_total=3))
        for _ in range(3):
            registry.emit(
                job_id,
                make_event(
                    "stage1_glm_ocr",
                    "page_skipped",
                    counts_as_step=False,
                    page_total=3,
                ),
            )
        registry.emit(job_id, make_event("stage1_glm_ocr", "completed"))
        registry.emit(job_id, make_event("stage3_editor", "started", page_total=3))
        registry.emit(
            job_id,
            make_event(
                "stage3_editor",
                "page_progress",
                counts_as_step=True,
                page_total=3,
            ),
        )
        jobs = registry.list_active_jobs()
        self.assertEqual(len(jobs), 1)
        phase_ids = [p["phase"] for p in jobs[0]["phases"]]
        self.assertIn("stage1_glm_ocr", phase_ids)
        self.assertIn("stage3_editor", phase_ids)
        self.assertNotIn("stage1_ocr", phase_ids)
        self.assertNotIn("stage2_vision", phase_ids)
        for index_phase in (
            "polyindex_toc",
            "polyindex_index",
            "time_index",
            "polyindex_biblio",
        ):
            self.assertIn(index_phase, phase_ids)
        glm_phase = next(p for p in jobs[0]["phases"] if p["phase"] == "stage1_glm_ocr")
        self.assertEqual(glm_phase["status"], "done")
        self.assertEqual(glm_phase["done"], 5)
        self.assertEqual(glm_phase["total"], 5)
        editor = next(p for p in jobs[0]["phases"] if p["phase"] == "stage3_editor")
        self.assertEqual(editor["done"], 2)
        self.assertEqual(editor["total"], 5)
        self.assertEqual(editor["status"], "active")
        self.assertEqual(jobs[0]["global_step"], 6)
        self.assertEqual(jobs[0]["global_total"], 10)


if __name__ == "__main__":
    unittest.main()

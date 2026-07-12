from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.api.research_batch_registry import ResearchBatchRegistry


class TestResearchBatchRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_running_batch_becomes_interrupted_after_reload(self) -> None:
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.create(total=3, book_sha=None, poh_ids=["alpha", "beta"])
        registry.set_targets(job_id, [{"poh_id": "alpha", "label": "Alpha"}, {"poh_id": "beta", "label": "Beta"}])
        registry.set_total(job_id, 2)
        registry.append_generated(
            job_id,
            {"poh_id": "alpha", "title": "Alpha", "url": "/articolo/alpha.html", "request_id": "req-a"},
        )

        reloaded = ResearchBatchRegistry(self.data_root)
        job = reloaded.get(job_id)
        assert job is not None
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["done"], 1)
        self.assertEqual(len(job["targets"]), 2)

    def test_resume_restarts_interrupted_batch(self) -> None:
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.create(total=1)
        registry.finish(job_id, "interrupted")

        reloaded = ResearchBatchRegistry(self.data_root)
        self.assertTrue(reloaded.resume(job_id))
        self.assertEqual(reloaded.get(job_id)["status"], "running")

    def test_abort_dismisses_interrupted_batch(self) -> None:
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.create(total=2)
        registry.finish(job_id, "interrupted")
        self.assertTrue(registry.abort(job_id))
        job = registry.get(job_id)
        assert job is not None
        self.assertEqual(job["status"], "aborted")
        summary = registry.get_job_summary(job_id)
        assert summary is not None
        self.assertFalse(summary["resumable"])
        self.assertFalse(summary["is_active"])
        self.assertEqual(summary["display_status"], "annullato")

    def test_abort_rejects_non_interrupted_batch(self) -> None:
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.create(total=1)
        self.assertFalse(registry.abort(job_id))
        registry.finish(job_id, "succeeded")
        self.assertFalse(registry.abort(job_id))

    def test_recover_skips_when_aborted_batch_exists(self) -> None:
        from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
        from src.search.article_catalog import publish_poh_article

        polyindex = self.data_root / "polyindex"
        polyindex.mkdir(parents=True)
        document = PolyindexIndexDocument(
            subjects={
                "alpha": PolyindexIndexSubjectEntry(
                    canonical_label="Alpha",
                    aliases=[],
                    books={"abc123": {"title": "Libro", "slug": "libro", "aligned_pages": [1]}},
                ),
                "beta": PolyindexIndexSubjectEntry(
                    canonical_label="Beta",
                    aliases=[],
                    books={"abc123": {"title": "Libro", "slug": "libro", "aligned_pages": [2]}},
                ),
            }
        )
        (polyindex / "INDEX.json").write_bytes(document.to_json_bytes())
        publish_poh_article(
            self.data_root,
            poh_id="alpha",
            title="Alpha",
            markdown="# Alpha\n\n" + ("Contenuto enciclopedico verificabile. " * 20),
            request_id="req-a",
        )
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.recover_interrupted_from_catalog()
        self.assertTrue(job_id)
        self.assertTrue(registry.abort(job_id))
        self.assertIsNone(registry.recover_interrupted_from_catalog())

    def test_recover_interrupted_from_catalog(self) -> None:
        from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
        from src.search.article_catalog import publish_poh_article

        polyindex = self.data_root / "polyindex"
        polyindex.mkdir(parents=True)
        document = PolyindexIndexDocument(
            subjects={
                "alpha": PolyindexIndexSubjectEntry(
                    canonical_label="Alpha",
                    aliases=[],
                    books={"abc123": {"title": "Libro", "slug": "libro", "aligned_pages": [1]}},
                ),
                "beta": PolyindexIndexSubjectEntry(
                    canonical_label="Beta",
                    aliases=[],
                    books={"abc123": {"title": "Libro", "slug": "libro", "aligned_pages": [2]}},
                ),
            }
        )
        (polyindex / "INDEX.json").write_bytes(document.to_json_bytes())
        publish_poh_article(
            self.data_root,
            poh_id="alpha",
            title="Alpha",
            markdown="# Alpha\n\n" + ("Contenuto enciclopedico verificabile. " * 20),
            request_id="req-a",
        )
        registry = ResearchBatchRegistry(self.data_root)
        job_id = registry.recover_interrupted_from_catalog()
        self.assertTrue(job_id)
        job = registry.get(job_id)
        assert job is not None
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["done"], 1)
        self.assertGreater(job["total"], 1)


if __name__ == "__main__":
    unittest.main()

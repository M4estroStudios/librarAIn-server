from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion.polyindex.subject_embeddings_backfill import (
    embedding_backfill_status,
    list_missing_subject_embeddings,
    run_subject_embedding_backfill,
)
from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import init_subject_matcher_schema, set_subject_embedding


def _write_index(path: Path, subject_ids: list[str]) -> None:
    document = PolyindexIndexDocument(
        schema_version="1.0",
        subjects={
            subject_id: PolyindexIndexSubjectEntry(
                canonical_label=subject_id.replace("-", " ").title(),
                aliases=[],
                books={},
            )
            for subject_id in subject_ids
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class TestSubjectEmbeddingsBackfill(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.polyindex_dir = self.root / "polyindex"
        self.db_path = self.root / "db" / "biblioteca.db"
        init_subject_matcher_schema(str(self.db_path))
        _write_index(self.polyindex_dir / "INDEX.json", ["alpha", "beta", "gamma"])
        self.settings = Settings.model_validate(
            {
                "DATA_ROOT": str(self.root),
                "OPENAI_PROVIDER": "local",
                "MATCHER_EMBEDDING_MODEL": "test-embed-model",
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_status_counts_missing_embeddings(self) -> None:
        set_subject_embedding(
            str(self.db_path),
            "alpha",
            "Alpha",
            [0.1, 0.2],
            "test-embed-model",
        )
        status = embedding_backfill_status(self.polyindex_dir, self.settings)
        self.assertEqual(status.total_subjects, 3)
        self.assertEqual(status.embedded_count, 1)
        self.assertEqual(status.missing_count, 2)

    def test_list_missing_subject_embeddings(self) -> None:
        document = PolyindexIndexDocument.load_file(self.polyindex_dir / "INDEX.json")
        missing = list_missing_subject_embeddings(document, {"alpha"})
        self.assertEqual([item[0] for item in missing], ["beta", "gamma"])

    @patch("src.ingestion.polyindex.subject_embeddings_backfill.fetch_embeddings_parallel")
    @patch("src.ingestion.polyindex.subject_embeddings_backfill.build_openai_client")
    def test_run_backfill_stores_missing_vectors(
        self,
        mock_build_client: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_build_client.return_value = MagicMock()
        mock_fetch.return_value = [[0.3, 0.4], [0.5, 0.6], [0.7, 0.8]]
        events: list[dict] = []

        result = run_subject_embedding_backfill(
            self.polyindex_dir,
            self.settings,
            request_id="req-1",
            progress=events.append,
        )

        self.assertEqual(result["generated"], 3)
        status = embedding_backfill_status(self.polyindex_dir, self.settings)
        self.assertEqual(status.missing_count, 0)
        self.assertTrue(any(ev.get("status") == "done" for ev in events))

    def test_run_backfill_noop_when_complete(self) -> None:
        document = PolyindexIndexDocument.load_file(self.polyindex_dir / "INDEX.json")
        for subject_id, entry in document.subjects.items():
            set_subject_embedding(
                str(self.db_path),
                subject_id,
                entry.canonical_label,
                [0.1],
                "test-embed-model",
            )
        result = run_subject_embedding_backfill(
            self.polyindex_dir,
            self.settings,
            request_id="req-2",
        )
        self.assertEqual(result["generated"], 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import math
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.models.polyindex_index import PolyindexIndexDocument
from src.models.settings import Settings
from src.search.request_schema import ResearchPoh
from src.search.subject_lookup import (
    METHOD_ALIAS,
    METHOD_EXACT,
    METHOD_POH_ID,
    METHOD_SEMANTIC,
    lookup_subjects,
)


def _settings(data_root: str, *, use_ai: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "DATA_ROOT": data_root,
            "OPENAI_PROVIDER": "local",
            "MATCHER_USE_AI": use_ai,
            "MATCHER_EMBEDDING_MODEL": "text-embedding-3-small",
            "MATCHER_SIMILARITY_THRESHOLD": 0.86,
        }
    )


def _hash_embedding(text: str, dim: int = 8) -> list[float]:
    digest = sha256(text.encode("utf-8")).digest()
    values = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create(self, *, model: str, input: str) -> MagicMock:
        del model
        self.calls.append(input)
        item = MagicMock()
        item.embedding = _hash_embedding(input)
        response = MagicMock()
        response.data = [item]
        return response


class RaisingEmbeddings:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create(self, *, model: str, input: str) -> MagicMock:
        del model
        self.calls.append(input)
        raise RuntimeError("endpoint unreachable")


def _fake_client(embeddings: object | None = None) -> MagicMock:
    client = MagicMock()
    client.embeddings = embeddings or FakeEmbeddings()
    return client


def _doc(subjects: dict) -> PolyindexIndexDocument:
    return PolyindexIndexDocument.model_validate(
        {"schema_version": "1.0", "subjects": subjects}
    )


def _subject(label: str, aligned: list[int], aliases: list[str] | None = None) -> dict:
    return {
        "canonical_label": label,
        "aliases": aliases or [],
        "books": {
            "sha1": {"aligned_pages": aligned, "original_pages": aligned},
        },
    }


class TestSubjectLookup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name) / "data"
        self.sqlite_path = str(self.data_root / "db" / "biblioteca.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exact_deterministic_match_aggregates_pages(self) -> None:
        document = _doc({"marco-polo": _subject("Marco Polo", [112, 114])})
        result = lookup_subjects(
            "Quale fu il ruolo di Marco Polo in Cina?",
            None,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-1",
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].canonical_id, "marco-polo")
        self.assertEqual(result.matches[0].method, METHOD_EXACT)
        self.assertEqual(result.pages, {"sha1": [112, 114]})
        self.assertFalse(result.ai_used)

    def test_alias_match(self) -> None:
        document = _doc(
            {"venezia": _subject("Venezia", [3], aliases=["Serenissima"])}
        )
        result = lookup_subjects(
            "La storia della Serenissima nel Mediterraneo",
            None,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-2",
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].canonical_id, "venezia")
        self.assertEqual(result.matches[0].method, METHOD_ALIAS)

    def test_poh_id_direct_match(self) -> None:
        document = _doc({"marco-polo": _subject("Marco Polo", [10])})
        poh = ResearchPoh(id="marco-polo", label="Marco Polo")
        result = lookup_subjects(
            "viaggi mercantili in oriente",
            poh,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-3",
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].method, METHOD_POH_ID)

    def test_no_match_returns_empty(self) -> None:
        document = _doc({"marco-polo": _subject("Marco Polo", [10])})
        result = lookup_subjects(
            "la rivoluzione industriale inglese",
            None,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-4",
        )
        self.assertEqual(result.matches, [])
        self.assertEqual(result.pages, {})

    @patch("src.search.subject_lookup._cosine_similarity", return_value=0.91)
    def test_semantic_match_above_threshold(self, _mock_sim: MagicMock) -> None:
        document = _doc({"kublai-khan": _subject("Kublai Khan", [50, 51])})
        result = lookup_subjects(
            "il grande imperatore mongolo",
            None,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-5",
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].method, METHOD_SEMANTIC)
        self.assertAlmostEqual(result.matches[0].similarity or 0.0, 0.91)
        self.assertTrue(result.ai_used)
        self.assertFalse(result.degraded)
        self.assertEqual(result.pages, {"sha1": [50, 51]})

    @patch("src.search.subject_lookup._cosine_similarity", return_value=0.50)
    def test_semantic_below_threshold_no_match(self, _mock_sim: MagicMock) -> None:
        document = _doc({"kublai-khan": _subject("Kublai Khan", [50])})
        result = lookup_subjects(
            "qualcosa di completamente diverso",
            None,
            document,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-6",
        )
        self.assertEqual(result.matches, [])
        self.assertTrue(result.ai_used)
        self.assertFalse(result.degraded)

    def test_endpoint_down_degrades_to_deterministic(self) -> None:
        document = _doc({"marco-polo": _subject("Marco Polo", [112])})
        result = lookup_subjects(
            "il viaggio di Marco Polo",
            None,
            document,
            _fake_client(RaisingEmbeddings()),
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-7",
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].method, METHOD_EXACT)
        self.assertTrue(result.ai_used)
        self.assertTrue(result.degraded)

    def test_use_ai_false_skips_embeddings(self) -> None:
        document = _doc({"kublai-khan": _subject("Kublai Khan", [50])})
        embeddings = FakeEmbeddings()
        lookup_subjects(
            "imperatore mongolo",
            None,
            document,
            _fake_client(embeddings),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-8",
        )
        self.assertEqual(embeddings.calls, [])


if __name__ == "__main__":
    unittest.main()

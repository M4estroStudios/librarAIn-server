from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion.polyindex.subject_dedup_suggest import (
    UnionFind,
    cluster_key,
    dismiss_cluster,
    list_open_suggestions,
    load_dismissed_pairs,
    pair_key,
    run_subject_dedup_scan,
)
from src.ingestion.polyindex.index_json import SubjectDeleteError, delete_polyindex_subject
from src.models.polyindex_index import (
    PolyindexIndexBookEntry,
    PolyindexIndexDocument,
    PolyindexIndexSubjectEntry,
)
from src.persistence.subject_matcher_sqlite import (
    delete_subject_embedding,
    get_subject_embedding,
    set_subject_embedding,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _settings(tmp: Path, *, use_ai: bool = False, threshold: float = 0.86) -> MagicMock:
    settings = MagicMock()
    settings.data_root = str(tmp)
    settings.sqlite_path = str(tmp / "db" / "biblioteca.db")
    settings.matcher_embedding_model = "text-embedding-3-small"
    settings.matcher_similarity_threshold = threshold
    settings.matcher_use_ai = use_ai
    settings.matcher_llm_model = None
    settings.editor_model = "editor-model"
    return settings


def _write_index(polyindex_dir: Path, subjects: dict) -> None:
    polyindex_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "subjects": subjects,
    }
    (polyindex_dir / "INDEX.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class TestUnionFindAndKeys(unittest.TestCase):
    def test_pair_and_cluster_keys_are_sorted(self) -> None:
        self.assertEqual(pair_key("b", "a"), "a|b")
        self.assertEqual(cluster_key(["c", "a", "b"]), "a|b|c")

    def test_union_find_groups(self) -> None:
        uf = UnionFind(["a", "b", "c", "d"])
        uf.union("a", "b")
        uf.union("b", "c")
        self.assertEqual(uf.find("a"), uf.find("c"))
        self.assertNotEqual(uf.find("a"), uf.find("d"))


class TestSubjectDedupSuggest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.settings = _settings(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fuzzy_scan_finds_near_duplicates_and_dismiss_filters(self) -> None:
        _write_index(
            self.polyindex_dir,
            {
                "augusto": {
                    "canonical_label": "Augusto",
                    "aliases": [],
                    "books": {
                        SHA_A: {
                            "title": "Libro A",
                            "slug": "libro-a",
                            "aligned_pages": [1],
                            "original_pages": [1],
                        }
                    },
                },
                "ottaviano": {
                    "canonical_label": "Ottaviano Augusto",
                    "aliases": ["Augusto"],
                    "books": {
                        SHA_B: {
                            "title": "Libro B",
                            "slug": "libro-b",
                            "aligned_pages": [2],
                            "original_pages": [2],
                        }
                    },
                },
                "venezia": {
                    "canonical_label": "Venezia",
                    "aliases": [],
                    "books": {
                        SHA_A: {
                            "title": "Libro A",
                            "slug": "libro-a",
                            "aligned_pages": [3],
                            "original_pages": [3],
                        }
                    },
                },
            },
        )
        result = run_subject_dedup_scan(
            self.polyindex_dir,
            self.settings,
            data_root=self.tmp,
            request_id="test-dedup",
            use_llm=False,
            min_similarity=0.8,
        )
        self.assertGreaterEqual(len(result["clusters"]), 1)
        keys = {cluster["cluster_key"] for cluster in result["clusters"]}
        self.assertTrue(any("augusto" in key and "ottaviano" in key for key in keys))

        open_before = list_open_suggestions(self.polyindex_dir)
        self.assertGreaterEqual(len(open_before["clusters"]), 1)
        cluster = open_before["clusters"][0]
        dismissed = dismiss_cluster(
            self.polyindex_dir,
            [m["canonical_id"] for m in cluster["members"]],
        )
        self.assertTrue(dismissed)
        open_after = list_open_suggestions(self.polyindex_dir)
        self.assertEqual(len(open_after["clusters"]), 0)
        self.assertTrue(load_dismissed_pairs(self.polyindex_dir))

    def test_embedding_high_similarity_proposes_without_llm(self) -> None:
        _write_index(
            self.polyindex_dir,
            {
                "alpha": {
                    "canonical_label": "Alpha",
                    "aliases": [],
                    "books": {
                        SHA_A: {
                            "aligned_pages": [1],
                            "original_pages": [1],
                        }
                    },
                },
                "beta": {
                    "canonical_label": "Beta Totally Different",
                    "aliases": [],
                    "books": {
                        SHA_B: {
                            "aligned_pages": [1],
                            "original_pages": [1],
                        }
                    },
                },
            },
        )
        vector = [1.0, 0.0, 0.0, 0.0]
        set_subject_embedding(
            self.settings.sqlite_path, "alpha", "Alpha", vector, self.settings.matcher_embedding_model
        )
        set_subject_embedding(
            self.settings.sqlite_path, "beta", "Beta", vector, self.settings.matcher_embedding_model
        )
        result = run_subject_dedup_scan(
            self.polyindex_dir,
            self.settings,
            data_root=self.tmp,
            request_id="emb-dedup",
            use_llm=False,
        )
        self.assertEqual(len(result["clusters"]), 1)
        members = {m["canonical_id"] for m in result["clusters"][0]["members"]}
        self.assertEqual(members, {"alpha", "beta"})
        self.assertIn("embedding", result["clusters"][0]["methods"])


class TestDeletePolyindexSubject(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.polyindex_dir.mkdir(parents=True)
        self.sqlite_path = str(self.tmp / "db" / "biblioteca.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_delete_removes_index_time_embedding_and_article(self) -> None:
        document = PolyindexIndexDocument(
            subjects={
                "augusto": PolyindexIndexSubjectEntry(
                    canonical_label="Augusto",
                    aliases=["Ottaviano"],
                    books={
                        SHA_A: PolyindexIndexBookEntry(
                            title="Libro A",
                            slug="libro-a",
                            aligned_pages=[1],
                            original_pages=[1],
                        )
                    },
                ),
                "roma": PolyindexIndexSubjectEntry(
                    canonical_label="Roma",
                    books={
                        SHA_A: PolyindexIndexBookEntry(
                            aligned_pages=[2],
                            original_pages=[2],
                        )
                    },
                ),
            }
        )
        document.write_atomic(self.polyindex_dir / "INDEX.json")
        (self.polyindex_dir / "TIME_INDEX.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "years": {
                        "27 a.C.": {
                            "books": {},
                            "subjects": ["augusto", "roma"],
                        }
                    },
                    "dates": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        set_subject_embedding(
            self.sqlite_path, "augusto", "Augusto", [0.1, 0.2], "text-embedding-3-small"
        )
        articles_dir = self.tmp / "research" / "articles"
        articles_dir.mkdir(parents=True)
        (articles_dir / "augusto.md").write_text("# Augusto", encoding="utf-8")
        (articles_dir / "augusto.html").write_text("<p>Augusto</p>", encoding="utf-8")
        catalog_path = self.tmp / "research" / "catalog.json"
        catalog_path.write_text(
            json.dumps({"articles": {"augusto": {"url": "/articolo/augusto.html"}}}),
            encoding="utf-8",
        )

        result = delete_polyindex_subject(
            self.polyindex_dir,
            "augusto",
            data_root=self.tmp,
            sqlite_path=self.sqlite_path,
        )
        self.assertEqual(result["canonical_id"], "augusto")
        self.assertTrue(result["embedding_removed"])
        self.assertGreaterEqual(result["time_refs_removed"], 1)
        self.assertTrue(result["article_cleanup"]["catalog_removed"])

        data = json.loads((self.polyindex_dir / "INDEX.json").read_text(encoding="utf-8"))
        self.assertNotIn("augusto", data["subjects"])
        self.assertIn("roma", data["subjects"])
        time_data = json.loads((self.polyindex_dir / "TIME_INDEX.json").read_text(encoding="utf-8"))
        self.assertEqual(time_data["years"]["27 a.C."]["subjects"], ["roma"])
        self.assertIsNone(get_subject_embedding(self.sqlite_path, "augusto", "text-embedding-3-small"))
        self.assertFalse((articles_dir / "augusto.md").exists())
        self.assertFalse((articles_dir / "augusto.html").exists())
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertNotIn("augusto", catalog["articles"])

        with self.assertRaises(SubjectDeleteError):
            delete_polyindex_subject(self.polyindex_dir, "augusto", data_root=self.tmp)

    def test_delete_subject_embedding_helper(self) -> None:
        set_subject_embedding(self.sqlite_path, "x", "X", [1.0], "m")
        self.assertTrue(delete_subject_embedding(self.sqlite_path, "x"))
        self.assertFalse(delete_subject_embedding(self.sqlite_path, "x"))


class TestAmbiguousLlmBand(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.polyindex_dir = self.tmp / "polyindex"
        self.settings = _settings(self.tmp, use_ai=True, threshold=0.86)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ambiguous_band_calls_llm(self) -> None:
        _write_index(
            self.polyindex_dir,
            {
                "a": {
                    "canonical_label": "Label A",
                    "aliases": [],
                    "books": {SHA_A: {"aligned_pages": [1], "original_pages": [1]}},
                },
                "b": {
                    "canonical_label": "Label B",
                    "aliases": [],
                    "books": {SHA_B: {"aligned_pages": [1], "original_pages": [1]}},
                },
            },
        )
        # Cosine ~0.866 for [1,0] vs [0.866, 0.5] roughly — craft exact vectors
        # Use identical-ish mid-band: score manually via patched cosine path
        # Vectors with cosine = 0.88 (between 0.82 and 0.92)
        import math

        left = [1.0, 0.0]
        # angle whose cos is 0.88
        angle = math.acos(0.88)
        right = [math.cos(angle), math.sin(angle)]
        set_subject_embedding(
            self.settings.sqlite_path, "a", "Label A", left, self.settings.matcher_embedding_model
        )
        set_subject_embedding(
            self.settings.sqlite_path, "b", "Label B", right, self.settings.matcher_embedding_model
        )

        with patch(
            "src.ingestion.polyindex.subject_dedup_suggest.build_openai_client",
            return_value=MagicMock(),
        ), patch(
            "src.ingestion.polyindex.subject_dedup_suggest._llm_arbitrate",
            return_value=(True, "stessa entità"),
        ) as llm_mock:
            result = run_subject_dedup_scan(
                self.polyindex_dir,
                self.settings,
                data_root=self.tmp,
                request_id="llm-band",
                use_llm=True,
            )
        llm_mock.assert_called()
        self.assertEqual(len(result["clusters"]), 1)
        self.assertIn("stessa entità", result["clusters"][0]["llm_reasons"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.models.settings import Settings
from src.search.poh_overlap import list_poh_overlaps


class PohOverlapTests(unittest.TestCase):
    def test_detects_similar_with_article(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            poly = data_root / "polyindex"
            poly.mkdir()
            doc = PolyindexIndexDocument(
                subjects={
                    "new_poh": PolyindexIndexSubjectEntry(
                        canonical_label="Alpha Test",
                        aliases=[],
                        books={"booknew": {"title": "Libro B", "aligned_pages": [1]}},
                    ),
                    "subj_alpha": PolyindexIndexSubjectEntry(
                        canonical_label="Alpha Tests",
                        aliases=["Alfa"],
                        books={"bookold": {"title": "Libro A", "aligned_pages": [2]}},
                    ),
                }
            )
            (poly / "INDEX.json").write_bytes(doc.to_json_bytes())
            articles = data_root / "research" / "articles"
            articles.mkdir(parents=True)
            (articles / "subj_alpha.html").write_text("<html></html>", encoding="utf-8")
            (articles / "subj_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (data_root / "research" / "catalog.json").write_text(
                json.dumps(
                    {
                        "articles": {
                            "subj_alpha": {
                                "poh_id": "subj_alpha",
                                "title": "Alpha",
                                "snippet": "x",
                                "url": "/articolo/subj_alpha.html",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings.model_validate(
                {
                    "DATA_ROOT": str(data_root),
                    "OPENAI_PROVIDER": "local",
                    "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                    "MATCHER_SIMILARITY_THRESHOLD": 0.86,
                }
            )
            overlaps = list_poh_overlaps(data_root, "booknew", settings=settings)
            self.assertEqual(len(overlaps), 1)
            self.assertEqual(overlaps[0]["poh_id"], "new_poh")
            self.assertGreaterEqual(len(overlaps[0]["similar_to"]), 1)

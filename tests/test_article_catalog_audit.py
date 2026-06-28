from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.models.polyindex_index import PolyindexIndexDocument, PolyindexIndexSubjectEntry
from src.search.article_catalog import publish_poh_article
from src.search.article_health_audit import audit_articles_health
from src.search.article_llm import build_no_material_article


class TestArticleCatalogAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmp.name)
        polyindex = self.data_root / "polyindex"
        polyindex.mkdir(parents=True)
        document = PolyindexIndexDocument(
            subjects={
                "hannibal": PolyindexIndexSubjectEntry(
                    canonical_label="Annibale",
                    aliases=["Annibale Barca"],
                    books={
                        "abc123": {
                            "title": "Libro",
                            "slug": "libro",
                            "aligned_pages": [1, 2],
                        }
                    },
                ),
                "empty_poh": PolyindexIndexSubjectEntry(
                    canonical_label="Vuoto",
                    aliases=[],
                    books={
                        "abc123": {
                            "title": "Libro",
                            "slug": "libro",
                            "aligned_pages": [3],
                        }
                    },
                ),
            }
        )
        (polyindex / "INDEX.json").write_bytes(document.to_json_bytes())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_audit_reports_missing(self) -> None:
        audit = audit_articles_health(self.data_root)
        self.assertGreaterEqual(audit["missing_count"], 2)
        issues = [item for item in audit["issues"] if item["issue"] == "missing"]
        self.assertEqual(issues, [])

    def test_audit_lists_generated_with_files(self) -> None:
        publish_poh_article(
            self.data_root,
            poh_id="hannibal",
            title="Annibale",
            markdown="# Annibale\n\n" + ("Contenuto enciclopedico verificabile. " * 20),
            request_id="req-ok",
        )
        audit = audit_articles_health(self.data_root)
        generated = audit.get("generated") or []
        self.assertTrue(any(item["poh_id"] == "hannibal" for item in generated))
        hannibal = next(item for item in generated if item["poh_id"] == "hannibal")
        self.assertTrue(hannibal["ok"])

    def test_audit_reports_no_material(self) -> None:
        publish_poh_article(
            self.data_root,
            poh_id="empty_poh",
            title="Vuoto",
            markdown=build_no_material_article("Vuoto"),
            request_id="req-stub",
        )
        audit = audit_articles_health(self.data_root)
        issues = [item for item in audit["issues"] if item["poh_id"] == "empty_poh"]
        self.assertTrue(any(item["issue"] == "no_material" for item in issues))

    def test_audit_reports_catalog_without_html(self) -> None:
        catalog_path = self.data_root / "research" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(
                {
                    "articles": {
                        "hannibal": {
                            "poh_id": "hannibal",
                            "title": "Annibale",
                            "no_material": False,
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        audit = audit_articles_health(self.data_root)
        issues = [item for item in audit["issues"] if item["poh_id"] == "hannibal"]
        self.assertTrue(any(item["issue"] == "damaged" for item in issues))

    def test_audit_reports_orphan_html(self) -> None:
        articles_dir = self.data_root / "research" / "articles"
        articles_dir.mkdir(parents=True)
        (articles_dir / "orphan.html").write_text(
            "<!DOCTYPE html><html><body><p>orphan</p></body></html>",
            encoding="utf-8",
        )
        audit = audit_articles_health(self.data_root)
        self.assertTrue(any(item["issue"] == "orphan_file" for item in audit["issues"]))


if __name__ == "__main__":
    unittest.main()

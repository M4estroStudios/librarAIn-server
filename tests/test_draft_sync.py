"""Tests for draft sync export/import/pack/unpack."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.api.page_guidance_http import save_ingest_draft
from src.persistence.draft_sync import (
    export_all_active_drafts,
    import_sync_dir,
    pack_drafts_bundle,
    unpack_drafts_bundle,
)


class DraftSyncTests(unittest.TestCase):
    def test_export_import_and_pack_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db" / "biblioteca.db"
            db.parent.mkdir(parents=True)
            sha = "a" * 64
            # Minimal PDF bytes for draft file.
            pdf_src = root / "book.pdf"
            pdf_src.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
            from src.api.pdf_upload_storage import save_draft_pdf

            save_draft_pdf(root, sha, pdf_src)
            save_ingest_draft(
                str(db),
                sha,
                {
                    "titolo": "Libro Sync",
                    "notes": "nota laptop",
                    "toc_range": "1-2",
                    "file_name": "libro.pdf",
                },
                file_name="libro.pdf",
                data_root=root,
            )
            exported = export_all_active_drafts(str(db), root)
            self.assertEqual(exported["count"], 1)
            sync_json = root / "sync" / "drafts" / f"{sha}.json"
            self.assertTrue(sync_json.is_file())
            payload = json.loads(sync_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["titolo"], "Libro Sync")
            self.assertTrue(payload["has_pdf"])

            # Second DB simulates the other PC.
            db2 = root / "db2" / "biblioteca.db"
            db2.parent.mkdir(parents=True)
            root2 = root / "pc2"
            root2.mkdir()
            # Copy sync JSON only (git-like), without PDF.
            (root2 / "sync" / "drafts").mkdir(parents=True)
            (root2 / "sync" / "drafts" / f"{sha}.json").write_text(
                sync_json.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            imported = import_sync_dir(str(db2), root2)
            self.assertEqual(imported["imported_count"], 1)

            # Pack from first root and unpack into third.
            bundle = pack_drafts_bundle(str(db), root, dest=root / "bundle.zip")
            self.assertTrue(bundle.is_file())
            root3 = root / "pc3"
            root3.mkdir()
            db3 = root3 / "biblioteca.db"
            result = unpack_drafts_bundle(str(db3), root3, bundle)
            self.assertEqual(result["imported_count"], 1)
            pdf = root3 / "input" / "drafts" / f"{sha}.pdf"
            self.assertTrue(pdf.is_file())


if __name__ == "__main__":
    unittest.main()

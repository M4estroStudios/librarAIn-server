from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.api.page_guidance_http import (
    list_ingest_drafts,
    load_ingest_notes_state,
    save_ingest_draft,
)


SHA = "a" * 64


class IngestDraftModeTests(unittest.TestCase):
    def test_index_only_mode_is_persisted_and_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "db" / "biblioteca.db"
            save_ingest_draft(
                str(sqlite_path),
                SHA,
                {
                    "titolo": "Indice di prova",
                    "autore": "Autore",
                    "index_range": "90-100",
                    "index_only": True,
                },
                file_name="indice.pdf",
                data_root=root,
            )

            state = load_ingest_notes_state(str(sqlite_path), SHA)
            drafts = list_ingest_drafts(str(sqlite_path), data_root=root)

        self.assertIsNotNone(state)
        self.assertTrue(state["index_only"])
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]["index_only"])
        self.assertEqual(drafts[0]["title"], "Indice di prova")

    def test_legacy_draft_without_toc_is_recognized_as_index_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "db" / "biblioteca.db"
            save_ingest_draft(
                str(sqlite_path),
                SHA,
                {
                    "titolo": "Indice legacy",
                    "autore": "Autore",
                    "index_range": "10-12",
                },
                data_root=root,
            )
            with sqlite3.connect(sqlite_path) as conn:
                raw = conn.execute(
                    "SELECT state_json FROM ingest_notes WHERE source_sha256 = ?",
                    (SHA,),
                ).fetchone()
                state = json.loads(raw[0])
                state.pop("index_only", None)
                conn.execute(
                    "UPDATE ingest_notes SET state_json = ? WHERE source_sha256 = ?",
                    (json.dumps(state), SHA),
                )

            drafts = list_ingest_drafts(str(sqlite_path), data_root=root)

        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]["index_only"])


if __name__ == "__main__":
    unittest.main()

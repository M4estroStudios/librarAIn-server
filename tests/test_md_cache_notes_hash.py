from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.ingestion.pipeline.md_cache import (
    read_stage_md,
    stage_md_marker_line,
    write_stage_md,
)


class MdCacheNotesHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "page.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_model_and_hash_hits(self) -> None:
        write_stage_md(self.path, "vision", "BODY", notes_hash="abcd1234")
        self.assertEqual(read_stage_md(self.path, "vision", notes_hash="abcd1234"), "BODY")

    def test_hash_change_misses(self) -> None:
        write_stage_md(self.path, "vision", "BODY", notes_hash="abcd1234")
        self.assertIsNone(read_stage_md(self.path, "vision", notes_hash="ffffffff"))

    def test_legacy_marker_hits_only_if_hash_empty(self) -> None:
        self.path.write_text(stage_md_marker_line("vision") + "OLD\n", encoding="utf-8")
        self.assertEqual(read_stage_md(self.path, "vision"), "OLD\n")
        self.assertIsNone(read_stage_md(self.path, "vision", notes_hash="abcd1234"))

    def test_unmarked_file_hits_only_if_hash_empty(self) -> None:
        self.path.write_text("plain", encoding="utf-8")
        self.assertEqual(read_stage_md(self.path, "vision"), "plain")
        self.assertIsNone(read_stage_md(self.path, "vision", notes_hash="abcd1234"))

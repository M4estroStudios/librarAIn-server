from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.markdown_artifacts import strip_lmstudio_channel_artifacts
from src.ingestion.toc_index_refine import (
    AggregateKind,
    refine_aggregate_markdown_file,
    refine_index_md,
    sort_index_md_file,
    sorted_index_md_text,
)
from src.models.settings import Settings

_SHA = "ab" * 32
_P_CHAT = "src.ingestion.toc_index_refine.chat_completion_with_retry"


def _settings(data_root: Path) -> Settings:
    return Settings.model_validate(
        {
            "DATA_ROOT": str(data_root),
            "OPENAI_PROVIDER": "local",
            "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
            "OPENAI_API_KEY": "test-key",
            "EDITOR_MODEL": "test-editor",
            "MAX_PARALLEL_REQUEST": 2,
        }
    )


class TestStripLmstudioChannelArtifacts(unittest.TestCase):
    def test_removes_thought_line_and_channel_prefix(self) -> None:
        raw = (
            "<|channel>thought\n"
            "<channel|>Presentazione, di Claudio Rendina 6\n"
            "Capitolo I. Monti 36\n"
        )
        cleaned = strip_lmstudio_channel_artifacts(raw)
        self.assertNotIn("<|channel>", cleaned)
        self.assertNotIn("<channel|>", cleaned)
        self.assertIn("Presentazione, di Claudio Rendina 6", cleaned)
        self.assertIn("Capitolo I. Monti 36", cleaned)


class TestTocIndexRefine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = self.tmp / "data"
        self.settings = _settings(self.data_root)
        self.client = MagicMock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch(_P_CHAT, new_callable=AsyncMock)
    def test_refine_index_strips_artifacts_and_preserves_header(self, mock_chat: AsyncMock) -> None:
        mock_chat.return_value = "Acquario, 988, 989\nArgentari, 456, 788-790"
        index_path = self.tmp / "INDEX.md"
        index_path.write_text(
            "# INDEX — Test Book\n\n"
            "<|channel>thought\n"
            "Acquario; 988, 989\n"
            "\n"
            "---\n\n"
            "Argentari; 456\n",
            encoding="utf-8",
        )

        asyncio.run(
            refine_index_md(
                index_path,
                self.client,
                self.settings,
                source_sha256=_SHA,
                request_id="req-1",
                cache_dir=self.tmp / "cache",
                force_recompute=True,
            )
        )

        self.assertEqual(mock_chat.await_count, 2)
        text = index_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# INDEX — Test Book\n\n"))
        self.assertNotIn("<|channel>", text)
        self.assertIn("Acquario, 988, 989", text)
        self.assertIn("Argentari, 456", text)
        body = text.removeprefix("# INDEX — Test Book\n\n")
        entry_lines = [line for line in body.splitlines() if line.strip()]
        self.assertEqual(entry_lines, sorted(entry_lines, key=str.casefold))

    @patch(_P_CHAT, new_callable=AsyncMock)
    def test_refine_skips_empty_body(self, mock_chat: AsyncMock) -> None:
        index_path = self.tmp / "INDEX.md"
        index_path.write_text("# INDEX — Empty\n\n", encoding="utf-8")

        asyncio.run(
            refine_aggregate_markdown_file(
                index_path,
                AggregateKind.INDEX,
                self.client,
                self.settings,
                source_sha256=_SHA,
                force_recompute=True,
            )
        )

        mock_chat.assert_not_awaited()

    @patch(_P_CHAT, new_callable=AsyncMock)
    def test_section_cache_hit_skips_second_call(self, mock_chat: AsyncMock) -> None:
        mock_chat.return_value = "Capitolo I 12"
        toc_path = self.tmp / "TOC.md"
        toc_path.write_text(
            "# TOC — Test Book\n\nCapitolo I 12\n",
            encoding="utf-8",
        )
        cache_dir = self.tmp / "cache"

        asyncio.run(
            refine_aggregate_markdown_file(
                toc_path,
                AggregateKind.TOC,
                self.client,
                self.settings,
                source_sha256=_SHA,
                cache_dir=cache_dir,
                force_recompute=True,
            )
        )
        asyncio.run(
            refine_aggregate_markdown_file(
                toc_path,
                AggregateKind.TOC,
                self.client,
                self.settings,
                source_sha256=_SHA,
                cache_dir=cache_dir,
                force_recompute=False,
            )
        )

        self.assertEqual(mock_chat.await_count, 1)


class TestSortIndexMdFile(unittest.TestCase):
    def test_sort_index_md_file_rewrites_unsorted_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            index_path = Path(tmp_name) / "INDEX.md"
            index_path.write_text(
                "# INDEX — Test Book\n\nVenezia, 4\nMarco Polo, 12\n",
                encoding="utf-8",
            )
            self.assertTrue(sort_index_md_file(index_path))
            text = index_path.read_text(encoding="utf-8")
            self.assertEqual(
                text,
                sorted_index_md_text("# INDEX — Test Book\n\nVenezia, 4\nMarco Polo, 12\n"),
            )

    def test_sort_index_md_file_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            index_path = Path(tmp_name) / "INDEX.md"
            index_path.write_text(
                "# INDEX — Test Book\n\nVenezia, 4\nMarco Polo, 12\n",
                encoding="utf-8",
            )
            sort_index_md_file(index_path)
            first = index_path.read_bytes()
            self.assertFalse(sort_index_md_file(index_path))
            self.assertEqual(index_path.read_bytes(), first)

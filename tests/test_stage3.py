from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.rate_limit import AsyncTokenBucket
from src.core.openai_client import _ClientState, _client_states
from src.ingestion.pipeline.stage2 import Stage2PageResult, Stage2Result
from src.ingestion.pipeline.stage2 import _read_stage_md
from src.ingestion.pipeline.stage3 import (
    Stage3Result,
    _load_editor_prompt,
    refine_with_editor,
    run_stage3_editor,
)

SHA = "deadbeef"


def _fake_client(content: str = "EDITED") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = content
    client.chat.completions.create.return_value = resp
    _client_states[client] = _ClientState(
        token_bucket=AsyncTokenBucket(60),
        retry_attempts=0,
    )
    return client


def _settings(data_root: str, editor_model: str = "editor-model-v1") -> MagicMock:
    s = MagicMock()
    s.data_root = data_root
    s.editor_model = editor_model
    s.vision_model = "vision-model-v1"
    s.max_parallel_request = 2
    s.reasoning_effort_editor = None
    s.reasoning_enable_thinking_editor = None
    return s


def _reasoning_settings(
    *,
    reasoning_effort_editor: str | None = None,
    reasoning_enable_thinking_editor: bool | None = None,
) -> MagicMock:
    s = MagicMock()
    s.reasoning_effort_editor = reasoning_effort_editor
    s.reasoning_enable_thinking_editor = reasoning_enable_thinking_editor
    return s


class TestRefineWithEditor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_edited_string(self) -> None:
        client = _fake_client()
        result = asyncio.run(
            refine_with_editor(
                client,
                model="test-model",
                stage2_md="# Title\n\nbody",
                request_id="req-001",
                page=1,
                settings=_reasoning_settings(),
            )
        )
        self.assertEqual(result, "EDITED")

    def test_system_message_is_editor_prompt_file(self) -> None:
        client = _fake_client()
        asyncio.run(
            refine_with_editor(
                client,
                model="test-model",
                stage2_md="raw md",
                request_id="req-001",
                page=1,
                settings=_reasoning_settings(),
            )
        )
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], _load_editor_prompt())

    def test_user_message_is_stage2_md(self) -> None:
        client = _fake_client()
        asyncio.run(
            refine_with_editor(
                client,
                model="test-model",
                stage2_md="stage two **markdown**",
                request_id="req-001",
                page=2,
                settings=_reasoning_settings(),
            )
        )
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "stage two **markdown**")


class TestRunStage3Editor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        data_root = self.tmp / "data"
        stage2_dir = data_root / "tmp" / SHA / "stage2Vision"
        stage2_dir.mkdir(parents=True, exist_ok=True)

        md1 = stage2_dir / "p.0001.test-book.md"
        md2 = stage2_dir / "p.0002.test-book.md"
        md1.write_text("md text page 1", encoding="utf-8")
        md2.write_text("md xx", encoding="utf-8")

        self.data_root = str(data_root)
        self.stage2_result = Stage2Result(
            pages=[
                Stage2PageResult(
                    aligned_page=1,
                    original_page=1,
                    md_path=str(md1),
                    char_count=len(md1.read_text(encoding="utf-8")),
                ),
                Stage2PageResult(
                    aligned_page=2,
                    original_page=2,
                    md_path=str(md2),
                    char_count=len(md2.read_text(encoding="utf-8")),
                ),
            ],
            skipped_existing=0,
            missing=[],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_run_creates_md_with_model_marker(self) -> None:
        client = _fake_client()
        settings = _settings(self.data_root)
        result = asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings, client)
        )
        self.assertIsInstance(result, Stage3Result)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(result.skipped_existing, 0)
        self.assertIsNone(result.last_error)
        for page in result.pages:
            self.assertTrue(Path(page.md_path).is_file())
            body = _read_stage_md(Path(page.md_path), "editor-model-v1")
            self.assertEqual(body, "EDITED")
            self.assertEqual(list(Path(page.md_path).parent.glob("*.json")), [])

    def test_second_run_same_model_skips(self) -> None:
        settings = _settings(self.data_root)
        client1 = _fake_client()
        asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings, client1)
        )
        client2 = _fake_client()
        result = asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings, client2)
        )
        self.assertEqual(result.skipped_existing, 2)
        self.assertEqual(len(result.pages), 2)
        client2.chat.completions.create.assert_not_called()

    def test_different_model_invalidates_cache(self) -> None:
        settings_a = _settings(self.data_root, editor_model="model-a")
        client1 = _fake_client()
        asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings_a, client1)
        )
        settings_b = _settings(self.data_root, editor_model="model-b")
        client2 = _fake_client()
        result = asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings_b, client2)
        )
        self.assertEqual(result.skipped_existing, 0)
        self.assertEqual(client2.chat.completions.create.call_count, 2)

    def test_force_recompute_bypasses_cache(self) -> None:
        settings = _settings(self.data_root)
        client1 = _fake_client()
        asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings, client1)
        )
        client2 = _fake_client("EDITED_AGAIN")
        result = asyncio.run(
            run_stage3_editor(
                self.stage2_result, SHA, settings, client2, force_recompute=True
            )
        )
        self.assertEqual(result.skipped_existing, 0)
        self.assertEqual(client2.chat.completions.create.call_count, 2)
        for page in result.pages:
            self.assertEqual(
                _read_stage_md(Path(page.md_path), "editor-model-v1"),
                "EDITED_AGAIN",
            )

    def test_char_counts_and_delta_on_page_results(self) -> None:
        settings = _settings(self.data_root)
        client = _fake_client("hi")
        result = asyncio.run(
            run_stage3_editor(self.stage2_result, SHA, settings, client)
        )
        for p in result.pages:
            self.assertEqual(p.char_count, 2)
            self.assertEqual(p.char_delta, 2 - p.stage2_char_count)


if __name__ == "__main__":
    unittest.main()

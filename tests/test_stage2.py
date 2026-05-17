from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.openai_client import _ClientState, _RateLimiter, _client_states
from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result
from src.ingestion.pipeline.stage2 import (
    Stage2Result,
    _load_vision_prompt,
    refine_with_vision,
    run_stage2_vision,
)

SHA = "deadbeef"


def _fake_client(content: str = "REFINED") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = content
    client.chat.completions.create.return_value = resp
    _client_states[client] = _ClientState(
        rate_limiter=_RateLimiter(60),
        retry_attempts=0,
    )
    return client


def _settings(data_root: str, vision_model: str = "vision-model-v1") -> MagicMock:
    s = MagicMock()
    s.data_root = data_root
    s.vision_model = vision_model
    return s


class TestRefineWithVision(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_fake_png(self, payload: bytes = b"\x89PNG") -> Path:
        png_path = self.tmp / "p.0001.png"
        png_path.write_bytes(payload)
        return png_path

    def test_returns_refined_string(self) -> None:
        client = _fake_client()
        png_path = self._write_fake_png()
        result = asyncio.run(
            refine_with_vision(
                client,
                model="test-model",
                page_image_path=png_path,
                raw_ocr_text="raw text",
                request_id="req-001",
                page=1,
            )
        )
        self.assertEqual(result, "REFINED")

    def test_system_message_is_vision_prompt_file(self) -> None:
        client = _fake_client()
        png_path = self._write_fake_png()
        asyncio.run(
            refine_with_vision(
                client,
                model="test-model",
                page_image_path=png_path,
                raw_ocr_text="raw text",
                request_id="req-001",
                page=1,
            )
        )
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], _load_vision_prompt())

    def test_user_message_contains_text_and_image(self) -> None:
        png_bytes = b"\x89PNG\xfake"
        client = _fake_client()
        png_path = self._write_fake_png(png_bytes)
        asyncio.run(
            refine_with_vision(
                client,
                model="test-model",
                page_image_path=png_path,
                raw_ocr_text="raw text here",
                request_id="req-001",
                page=1,
            )
        )
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        user_content = messages[1]["content"]
        text_parts = [p for p in user_content if p.get("type") == "text"]
        image_parts = [p for p in user_content if p.get("type") == "image_url"]
        self.assertEqual(len(text_parts), 1)
        self.assertEqual(text_parts[0]["text"], "raw text here")
        self.assertEqual(len(image_parts), 1)
        expected_b64 = base64.b64encode(png_bytes).decode("ascii")
        self.assertIn(expected_b64, image_parts[0]["image_url"]["url"])

    def test_image_url_uses_data_uri_scheme(self) -> None:
        client = _fake_client()
        png_path = self._write_fake_png()
        asyncio.run(
            refine_with_vision(
                client,
                model="test-model",
                page_image_path=png_path,
                raw_ocr_text="x",
                request_id="r",
                page=1,
            )
        )
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        image_parts = [p for p in messages[1]["content"] if p.get("type") == "image_url"]
        url = image_parts[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))


class TestRunStage2Vision(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        data_root = self.tmp / "data"
        render_dir = data_root / "tmp" / SHA / "render"
        ocr_dir = data_root / "tmp" / SHA / "stage1OCR"
        render_dir.mkdir(parents=True, exist_ok=True)
        ocr_dir.mkdir(parents=True, exist_ok=True)

        for page in [1, 2]:
            (render_dir / f"p.{page:04d}.png").write_bytes(b"\x89PNG")

        txt1 = ocr_dir / "p.0001.test-book.txt"
        txt2 = ocr_dir / "p.0002.test-book.txt"
        txt1.write_text("ocr text page 1", encoding="utf-8")
        txt2.write_text("ocr text page 2", encoding="utf-8")

        self.data_root = str(data_root)
        self.stage1_result = Stage1Result(
            pages=[
                Stage1PageResult(
                    aligned_page=1,
                    original_page=1,
                    txt_path=str(txt1),
                    char_count=15,
                ),
                Stage1PageResult(
                    aligned_page=2,
                    original_page=2,
                    txt_path=str(txt2),
                    char_count=15,
                ),
            ],
            skipped_existing=0,
            missing=[],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_run_creates_md_and_sidecar_files(self) -> None:
        client = _fake_client()
        settings = _settings(self.data_root)
        result = asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings, client)
        )
        self.assertIsInstance(result, Stage2Result)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(result.skipped_existing, 0)
        self.assertIsNone(result.last_error)
        for page in result.pages:
            self.assertTrue(Path(page.md_path).is_file())
            self.assertTrue(Path(page.sidecar_path).is_file())
            sidecar = json.loads(Path(page.sidecar_path).read_text(encoding="utf-8"))
            self.assertEqual(sidecar["model"], "vision-model-v1")

    def test_second_run_same_model_skips(self) -> None:
        settings = _settings(self.data_root)
        client1 = _fake_client()
        asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings, client1)
        )
        client2 = _fake_client()
        result = asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings, client2)
        )
        self.assertEqual(result.skipped_existing, 2)
        self.assertEqual(len(result.pages), 2)
        client2.chat.completions.create.assert_not_called()

    def test_different_model_invalidates_cache(self) -> None:
        settings_a = _settings(self.data_root, vision_model="model-a")
        client1 = _fake_client()
        asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings_a, client1)
        )
        settings_b = _settings(self.data_root, vision_model="model-b")
        client2 = _fake_client()
        result = asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings_b, client2)
        )
        self.assertEqual(result.skipped_existing, 0)
        self.assertEqual(client2.chat.completions.create.call_count, 2)

    def test_force_recompute_bypasses_cache(self) -> None:
        settings = _settings(self.data_root)
        client1 = _fake_client()
        asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings, client1)
        )
        client2 = _fake_client("REFINED_AGAIN")
        result = asyncio.run(
            run_stage2_vision(
                self.stage1_result, SHA, settings, client2, force_recompute=True
            )
        )
        self.assertEqual(result.skipped_existing, 0)
        self.assertEqual(client2.chat.completions.create.call_count, 2)
        for page in result.pages:
            self.assertEqual(Path(page.md_path).read_text(encoding="utf-8"), "REFINED_AGAIN")

    def test_char_count_reflects_md_content(self) -> None:
        settings = _settings(self.data_root)
        client = _fake_client("hello")
        result = asyncio.run(
            run_stage2_vision(self.stage1_result, SHA, settings, client)
        )
        for page in result.pages:
            self.assertEqual(page.char_count, len("hello"))


if __name__ == "__main__":
    unittest.main()

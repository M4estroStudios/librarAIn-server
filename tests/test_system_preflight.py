from __future__ import annotations

import unittest
from unittest.mock import patch

from src.api.system_preflight import evaluate_preflight, normalize_preflight_operation
from src.ingestion.pipeline.gpu_vram import GpuVramSnapshot


def _settings(**overrides):
    base = {
        "DATA_ROOT": "data",
        "OPENAI_PROVIDER": "local",
        "GPU_VRAM_CHECK_ENABLED": True,
        "GPU_VRAM_MAX_USED_GB": 4.0,
        "OCR_USE_GPU": False,
        "RESEARCH_MODEL": "research-model",
        "VISION_MODEL": "vision-model",
        "EDITOR_MODEL": "editor-model",
        "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
    }
    base.update(overrides)
    from src.models.settings import Settings

    return Settings.model_validate(base)


class SystemPreflightTests(unittest.TestCase):
    def test_normalize_chat_alias(self) -> None:
        self.assertEqual(normalize_preflight_operation("chat"), "research")

    def test_invalid_operation(self) -> None:
        self.assertIsNone(normalize_preflight_operation("unknown"))

    @patch("src.api.system_preflight.ensure_lmstudio_model_loaded")
    @patch("src.api.system_preflight._list_lmstudio_models", return_value=([], "http://lm"))
    @patch("src.api.system_preflight.collect_gpu_vram_snapshots")
    @patch("src.api.system_preflight._check_vram_for_operation", return_value=(True, "ok"))
    def test_preflight_ok_remote_provider(self, _vram, _snap, _lm, _ensure) -> None:
        _snap.return_value = [
            GpuVramSnapshot(device_index=0, used_bytes=0, total_bytes=24 * 1024**3)
        ]
        settings = _settings(OPENAI_PROVIDER="local", GPU_VRAM_CHECK_ENABLED=False)
        result = evaluate_preflight(settings, "research")
        self.assertTrue(result["ok"])

    @patch("src.api.system_preflight._list_lmstudio_models")
    @patch("src.api.system_preflight.collect_gpu_vram_snapshots")
    @patch("src.api.system_preflight._check_vram_for_operation", return_value=(False, "VRAM insufficiente"))
    def test_preflight_blocked_vram(self, _vram, snap, lm) -> None:
        snap.return_value = [
            GpuVramSnapshot(device_index=0, used_bytes=23 * 1024**3, total_bytes=24 * 1024**3)
        ]
        lm.return_value = ([], None)
        settings = _settings()
        result = evaluate_preflight(settings, "ingest")
        self.assertFalse(result["ok"])
        self.assertIn("VRAM", result["message"])

    @patch("src.api.system_preflight._model_loaded", return_value=True)
    @patch("src.api.system_preflight._list_lmstudio_models")
    @patch("src.api.system_preflight.collect_gpu_vram_snapshots")
    @patch("src.api.system_preflight._check_vram_for_operation", return_value=(True, "ok"))
    def test_preflight_model_loaded(self, _vram, snap, lm, _loaded) -> None:
        snap.return_value = [
            GpuVramSnapshot(device_index=0, used_bytes=10 * 1024**3, total_bytes=24 * 1024**3)
        ]
        lm.return_value = (
            [{"key": "research-model", "display_name": "Research", "loaded_instances": [{"id": "1"}]}],
            "http://lm",
        )
        result = evaluate_preflight(_settings(), "research")
        self.assertTrue(result["ok"])
        self.assertEqual(result["required_model"], "research-model")

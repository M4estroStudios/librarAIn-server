from __future__ import annotations

import unittest
from unittest.mock import patch

from src.api.system_preflight import evaluate_preflight
from src.ingestion.pipeline.gpu_vram import GpuVramSnapshot


def _settings():
    from src.models.settings import Settings

    return Settings.model_validate(
        {
            "DATA_ROOT": "data",
            "OPENAI_PROVIDER": "local",
            "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
            "GPU_VRAM_CHECK_ENABLED": True,
            "GPU_VRAM_MAX_USED_GB": 4.0,
            "OCR_USE_GPU": False,
            "RESEARCH_MODEL": "research-model",
            "VISION_MODEL": "vision-model",
        }
    )


class PreflightBlockRetryTests(unittest.TestCase):
    @patch("src.api.system_preflight._model_loaded", return_value=True)
    @patch("src.api.system_preflight._list_lmstudio_models", return_value=([], None))
    @patch("src.api.system_preflight.collect_gpu_vram_snapshots")
    @patch("src.api.system_preflight._check_vram_for_operation")
    def test_block_then_pass(self, vram_check, snap, _lm, _loaded) -> None:
        vram_check.side_effect = [
            (False, "VRAM insufficiente mock"),
            (True, "ok"),
        ]
        snap.return_value = [
            GpuVramSnapshot(device_index=0, used_bytes=23 * 1024**3, total_bytes=24 * 1024**3)
        ]
        settings = _settings()
        blocked = evaluate_preflight(settings, "research")
        self.assertFalse(blocked["ok"])
        ok = evaluate_preflight(settings, "research")
        self.assertTrue(ok["ok"])

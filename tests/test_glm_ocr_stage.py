from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.ingestion.pipeline.glm_ocr_stage import resolve_glm_ocr_model


class ResolveGlmOcrModelTests(unittest.TestCase):
    def test_prefers_explicit_glm_model(self) -> None:
        settings = MagicMock()
        settings.glm_ocr_model = "org/glm-ocr"
        settings.vision_model = "vision-model"
        self.assertEqual(resolve_glm_ocr_model(settings), "org/glm-ocr")

    def test_falls_back_to_vision_model(self) -> None:
        settings = MagicMock()
        settings.glm_ocr_model = None
        settings.vision_model = "vision-model"
        self.assertEqual(resolve_glm_ocr_model(settings), "vision-model")


class GpuVramGlmBackendTests(unittest.TestCase):
    def test_glm_backend_skips_easyocr_pool(self) -> None:
        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = MagicMock()
        settings.ocr_use_gpu = True
        settings.ocr_gpu_device = "0"
        settings.openai_provider = "local"
        settings.gpu_vram_check_enabled = True
        settings.gpu_vram_max_used_gb = 4.0
        settings.max_parallel_request = 4
        with patch("src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr") as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ) as mock_llm, patch(
            "src.ingestion.pipeline.gpu_vram._load_gpu_snapshots", return_value=[]
        ):
            require_gpu_vram_at_pipeline_start(
                settings,
                skip_vision_editor=False,
                ocr_backend="glm",
            )
        mock_ocr.assert_not_called()
        mock_llm.assert_called_once()


if __name__ == "__main__":
    unittest.main()

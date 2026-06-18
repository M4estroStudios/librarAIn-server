from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path


class FakeOCRPageEngine:
    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        return "page text"


class OCRPageEngineProtocolTests(unittest.TestCase):
    def test_fake_engine_satisfies_protocol(self) -> None:
        from src.ingestion.pipeline.engine import OCRPageEngine

        engine = FakeOCRPageEngine()
        self.assertIsInstance(engine, OCRPageEngine)

    def test_fake_engine_returns_string(self) -> None:
        engine = FakeOCRPageEngine()
        result = engine.ocr_page(Path("dummy.png"), lang=["en"])
        self.assertIsInstance(result, str)
        self.assertEqual(result, "page text")

    def test_fake_engine_accepts_multiple_languages(self) -> None:
        engine = FakeOCRPageEngine()
        result = engine.ocr_page(Path("dummy.png"), lang=["it", "en"])
        self.assertEqual(result, "page text")

    def test_easyocr_not_imported_at_module_load(self) -> None:
        self.assertNotIn("easyocr", sys.modules)

    def test_easyocr_engine_satisfies_protocol(self) -> None:
        from src.ingestion.pipeline.engine import EasyOCRPageEngine, OCRPageEngine

        self.assertTrue(issubclass(EasyOCRPageEngine, OCRPageEngine))

    def test_release_parallel_pool_clears_readers(self) -> None:
        from src.ingestion.pipeline.engine import EasyOCRPageEngine

        eng = EasyOCRPageEngine()
        eng._pool_readers = [object(), object()]
        eng._pool = queue.Queue()
        eng.release_parallel_pool()
        self.assertEqual(eng._pool_readers, [])
        self.assertIsNone(eng._pool)

    def test_ocr_page_requires_prepared_pool(self) -> None:
        from src.ingestion.pipeline.engine import EasyOCRPageEngine

        eng = EasyOCRPageEngine()
        with self.assertRaises(RuntimeError):
            eng.ocr_page(Path("x.png"), lang=["en"])

    def test_ocr_languages_default_in_settings(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "DATA_ROOT=data\nOPENAI_PROVIDER=local\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                from src.core.config import load_settings

                settings = load_settings(str(env_path))

        self.assertEqual(settings.ocr_languages, ["it", "en"])
        self.assertFalse(settings.ocr_use_gpu)

    def test_ocr_languages_parsed_from_string(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "DATA_ROOT=data\nOPENAI_PROVIDER=local\nOCR_LANGUAGES=fr,de\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                from src.core.config import load_settings

                settings = load_settings(str(env_path))

        self.assertEqual(settings.ocr_languages, ["fr", "de"])

    def test_ocr_use_gpu_parsed_from_env(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "DATA_ROOT=data\nOPENAI_PROVIDER=local\nOCR_USE_GPU=true\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                from src.core.config import load_settings

                settings = load_settings(str(env_path))

        self.assertTrue(settings.ocr_use_gpu)

    def test_gpu_vram_check_defaults_in_settings(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "DATA_ROOT=data\nOPENAI_PROVIDER=local\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                from src.core.config import load_settings

                settings = load_settings(str(env_path))

        self.assertTrue(settings.gpu_vram_check_enabled)
        self.assertEqual(settings.gpu_vram_max_used_gb, 4.0)

    def test_ensure_gpu_vram_available_skips_when_disabled(self) -> None:
        from src.ingestion.pipeline.engine import ensure_gpu_vram_available

        ensure_gpu_vram_available(max_used_gb=0.0, enabled=True)

    def test_ensure_gpu_vram_available_raises_when_free_space_insufficient(self) -> None:
        from unittest.mock import patch

        from src.ingestion.pipeline.gpu_vram import GpuVramSnapshot, ensure_gpu_vram_available
        from src.models.request import IngestInputErrorCode, IngestInputValidationException

        busy = [
            GpuVramSnapshot(device_index=0, used_bytes=int(22 * 1024**3), total_bytes=int(24 * 1024**3)),
            GpuVramSnapshot(device_index=1, used_bytes=int(23 * 1024**3), total_bytes=int(24 * 1024**3)),
        ]
        with patch("src.ingestion.pipeline.gpu_vram.collect_gpu_vram_snapshots", return_value=busy):
            with self.assertRaises(IngestInputValidationException) as ctx:
                ensure_gpu_vram_available(max_used_gb=4.0, gpu_device="all", enabled=True)

        self.assertEqual(ctx.exception.detail.code, IngestInputErrorCode.GPU_VRAM_BUSY)
        self.assertIn("VRAM GPU insufficiente", ctx.exception.detail.message)
        self.assertIn("GPU 0", ctx.exception.detail.message)

    def test_ensure_gpu_vram_available_allows_high_usage_with_enough_free(self) -> None:
        from unittest.mock import patch

        from src.ingestion.pipeline.gpu_vram import (
            GpuVramSnapshot,
            ensure_gpu_vram_available,
            ensure_gpu_vram_headroom_for_llm,
        )

        loaded = [
            GpuVramSnapshot(device_index=0, used_bytes=int(18.1 * 1024**3), total_bytes=int(24 * 1024**3)),
            GpuVramSnapshot(device_index=1, used_bytes=int(13.2 * 1024**3), total_bytes=int(24 * 1024**3)),
        ]
        with patch("src.ingestion.pipeline.gpu_vram.collect_gpu_vram_snapshots", return_value=loaded):
            ensure_gpu_vram_available(max_used_gb=4.0, gpu_device="all", enabled=True)
            ensure_gpu_vram_headroom_for_llm(loaded, load_free_gb=12.0)

    def test_collect_gpu_vram_snapshots_prefers_nvidia_smi(self) -> None:
        from unittest.mock import patch

        from src.ingestion.pipeline.gpu_vram import GpuVramSnapshot, collect_gpu_vram_snapshots

        smi = [
            GpuVramSnapshot(device_index=0, used_bytes=int(9 * 1024**3), total_bytes=int(24 * 1024**3)),
        ]
        with patch("src.ingestion.pipeline.gpu_vram._collect_gpu_vram_via_nvidia_smi", return_value=smi):
            with patch("src.ingestion.pipeline.gpu_vram._collect_gpu_vram_via_torch") as mock_torch:
                snapshots = collect_gpu_vram_snapshots(gpu_device="all")
        self.assertEqual(len(snapshots), 1)
        self.assertAlmostEqual(snapshots[0].used_gb, 9.0, places=1)
        mock_torch.assert_not_called()

    def test_parse_nvidia_smi_output(self) -> None:
        from unittest.mock import MagicMock, patch

        from src.ingestion.pipeline.gpu_vram import _collect_gpu_vram_via_nvidia_smi

        proc = MagicMock()
        proc.stdout = "0, 9277, 24564\n1, 17965, 24576\n"
        with patch("src.ingestion.pipeline.gpu_vram.shutil.which", return_value="/usr/bin/nvidia-smi"), patch(
            "src.ingestion.pipeline.gpu_vram.subprocess.run", return_value=proc
        ):
            snapshots = _collect_gpu_vram_via_nvidia_smi()
        self.assertIsNotNone(snapshots)
        assert snapshots is not None
        self.assertEqual(len(snapshots), 2)
        self.assertAlmostEqual(snapshots[1].used_gb, 17965 / 1024, places=1)


    def test_require_gpu_vram_at_pipeline_start_skips_llm_when_vision_editor_skipped(self) -> None:
        from unittest.mock import MagicMock, patch

        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = MagicMock()
        settings.ocr_use_gpu = True
        settings.ocr_gpu_device = "0"
        settings.openai_provider = "local"
        settings.gpu_vram_check_enabled = True
        settings.gpu_vram_max_used_gb = 4.0
        settings.max_parallel_request = 2
        with patch("src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr") as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ) as mock_llm, patch(
            "src.ingestion.pipeline.gpu_vram._load_gpu_snapshots", return_value=[]
        ):
            require_gpu_vram_at_pipeline_start(settings, skip_vision_editor=True, single_page=True)
        mock_ocr.assert_called_once()
        self.assertEqual(mock_ocr.call_args.kwargs["pool_size"], 1)
        self.assertEqual(mock_ocr.call_args.kwargs["per_instance_load_gb"], 4.0)
        mock_llm.assert_not_called()

    def test_require_gpu_vram_at_pipeline_start_checks_llm_with_single_page_ocr_pool(self) -> None:
        from unittest.mock import MagicMock, patch

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
            require_gpu_vram_at_pipeline_start(settings, skip_vision_editor=False, single_page=True)
        mock_ocr.assert_called_once()
        self.assertEqual(mock_ocr.call_args.kwargs["pool_size"], 1)
        self.assertEqual(mock_ocr.call_args.kwargs["per_instance_load_gb"], 4.0)
        mock_llm.assert_called_once()

    def test_require_gpu_vram_at_pipeline_start_scales_ocr_pool_for_full_book(self) -> None:
        from unittest.mock import MagicMock, patch

        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = MagicMock()
        settings.ocr_use_gpu = True
        settings.ocr_gpu_device = "0"
        settings.openai_provider = "local"
        settings.gpu_vram_check_enabled = True
        settings.gpu_vram_max_used_gb = 4.0
        settings.max_parallel_request = 2
        with patch("src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr") as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ), patch(
            "src.ingestion.pipeline.gpu_vram._load_gpu_snapshots", return_value=[]
        ):
            require_gpu_vram_at_pipeline_start(settings, skip_vision_editor=False, single_page=False)
        self.assertEqual(mock_ocr.call_args.kwargs["pool_size"], 2)
        self.assertEqual(mock_ocr.call_args.kwargs["per_instance_load_gb"], 4.0)

    def test_ensure_gpu_vram_headroom_for_ocr_allows_loaded_model_with_parallel_pool(self) -> None:
        from src.ingestion.pipeline.gpu_vram import (
            GpuVramSnapshot,
            ensure_gpu_vram_headroom_for_ocr,
        )

        loaded = [
            GpuVramSnapshot(device_index=0, used_bytes=int(17.8 * 1024**3), total_bytes=int(24 * 1024**3)),
            GpuVramSnapshot(device_index=1, used_bytes=int(13.2 * 1024**3), total_bytes=int(24 * 1024**3)),
        ]
        ensure_gpu_vram_headroom_for_ocr(
            loaded,
            pool_size=8,
            per_instance_load_gb=4.0,
        )

    def test_require_gpu_vram_at_pipeline_start_skips_ocr_when_entry_is_stage2(self) -> None:
        from unittest.mock import MagicMock, patch

        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = MagicMock()
        settings.ocr_use_gpu = True
        settings.ocr_gpu_device = "0"
        settings.openai_provider = "local"
        settings.gpu_vram_check_enabled = True
        settings.gpu_vram_max_used_gb = 4.0
        settings.max_parallel_request = 8
        with patch("src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr") as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ) as mock_llm, patch(
            "src.ingestion.pipeline.gpu_vram._load_gpu_snapshots", return_value=[]
        ):
            require_gpu_vram_at_pipeline_start(
                settings,
                skip_vision_editor=False,
                single_page=False,
                entry_stage="stage2Vision",
            )
        mock_ocr.assert_not_called()
        mock_llm.assert_called_once()

    def test_require_gpu_vram_at_pipeline_start_skips_gpu_when_entry_is_output_only(self) -> None:
        from unittest.mock import MagicMock, patch

        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = MagicMock()
        settings.ocr_use_gpu = True
        settings.openai_provider = "local"
        settings.gpu_vram_check_enabled = True
        settings.gpu_vram_max_used_gb = 4.0
        settings.max_parallel_request = 8
        with patch("src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr") as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ) as mock_llm:
            require_gpu_vram_at_pipeline_start(
                settings,
                skip_vision_editor=False,
                single_page=False,
                entry_stage="output",
            )
        mock_ocr.assert_not_called()
        mock_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()

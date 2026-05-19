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


if __name__ == "__main__":
    unittest.main()

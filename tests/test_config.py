from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config import ConfigurationError, load_settings


class ConfigLoaderTests(unittest.TestCase):
    def test_load_settings_valid_local_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                        "MAX_PARALLEL_REQUEST=4",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = load_settings(str(env_path))

        self.assertEqual(settings.data_root, "data")
        self.assertEqual(settings.sqlite_path, "data/db/biblioteca.db")
        self.assertEqual(settings.processed_pdf_input_dir, "data/input/processed")
        self.assertEqual(settings.openai_provider, "local")
        self.assertEqual(settings.max_parallel_request, 4)
        self.assertEqual(settings.timeout_seconds, 120)

    def test_load_settings_missing_required_vars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("OPENAI_PROVIDER=local\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(str(env_path))

        message = str(ctx.exception)
        self.assertIn("Missing required env vars", message)
        self.assertIn("DATA_ROOT", message)
        self.assertIn("See example.env", message)

    def test_load_settings_invalid_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                        "MAX_PARALLEL_REQUEST=zero",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(str(env_path))

        message = str(ctx.exception)
        self.assertIn("Invalid env vars", message)
        self.assertIn("MAX_PARALLEL_REQUEST", message)

    def test_load_settings_remote_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=remote",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(str(env_path))

        message = str(ctx.exception)
        self.assertIn("OPENAI_PROVIDER=remote requires", message)
        self.assertIn("OPENAI_BASE_URL", message)
        self.assertIn("OPENAI_API_KEY", message)

    def test_load_settings_uses_defaults_when_optional_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = load_settings(str(env_path))

        self.assertEqual(settings.retry_attempts, 2)
        self.assertEqual(settings.rate_limit_per_minute, 60)
        self.assertEqual(settings.page_range_per_thread, 10)

    def test_load_settings_reasoning_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                        "REASONING_EFFORT_VISION=medium",
                        "REASONING_ENABLE_THINKING_VISION=true",
                        "REASONING_EFFORT_EDITOR=low",
                        "REASONING_ENABLE_THINKING_EDITOR=false",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = load_settings(str(env_path))

        self.assertEqual(settings.reasoning_effort_vision, "medium")
        self.assertTrue(settings.reasoning_enable_thinking_vision)
        self.assertEqual(settings.reasoning_effort_editor, "low")
        self.assertFalse(settings.reasoning_enable_thinking_editor)

    def test_load_settings_reasoning_effort_off_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                        "REASONING_EFFORT_VISION=off",
                        "REASONING_EFFORT_EDITOR=off",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = load_settings(str(env_path))

        self.assertIsNone(settings.reasoning_effort_vision)
        self.assertIsNone(settings.reasoning_effort_editor)


if __name__ == "__main__":
    unittest.main()

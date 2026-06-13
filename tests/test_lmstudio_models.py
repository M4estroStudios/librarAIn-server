from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.core.lmstudio_models import (
    _find_loaded_instance_ids,
    lmstudio_api_root,
    should_swap_lmstudio_models,
    swap_lmstudio_vision_to_editor,
)


def _settings(**kwargs: object) -> MagicMock:
    s = MagicMock()
    s.openai_provider = kwargs.get("openai_provider", "local")
    s.openai_base_url = kwargs.get("openai_base_url", "http://localhost:1234/v1")
    s.openai_api_key = kwargs.get("openai_api_key", "dummy-key")
    s.vision_model = kwargs.get("vision_model", "org/vision-model")
    s.editor_model = kwargs.get("editor_model", "org/editor-model")
    s.lm_studio_swap_models = kwargs.get("lm_studio_swap_models", True)
    s.timeout_seconds = kwargs.get("timeout_seconds", 30)
    s.lm_studio_load_timeout_seconds = kwargs.get("lm_studio_load_timeout_seconds", 600)
    s.gpu_vram_check_enabled = kwargs.get("gpu_vram_check_enabled", False)
    s.gpu_vram_max_used_gb = kwargs.get("gpu_vram_max_used_gb", 4.0)
    return s


class TestLmStudioHelpers(unittest.TestCase):
    def test_lmstudio_api_root_strips_v1_suffix(self) -> None:
        self.assertEqual(
            lmstudio_api_root(_settings(openai_base_url="http://localhost:1234/v1")),
            "http://localhost:1234",
        )

    def test_should_swap_disabled_when_same_model(self) -> None:
        self.assertFalse(
            should_swap_lmstudio_models(
                _settings(vision_model="same", editor_model="same")
            )
        )

    def test_should_swap_false_for_remote(self) -> None:
        self.assertFalse(
            should_swap_lmstudio_models(_settings(openai_provider="remote"))
        )

    def test_find_loaded_instance_ids(self) -> None:
        payload = {
            "models": [
                {
                    "key": "org/vision-model",
                    "loaded_instances": [{"id": "org/vision-model"}],
                }
            ]
        }
        self.assertEqual(
            _find_loaded_instance_ids(payload, "org/vision-model"),
            ["org/vision-model"],
        )


class TestSwapLmStudioModels(unittest.TestCase):
    def test_swap_noop_when_disabled(self) -> None:
        swap_lmstudio_vision_to_editor(_settings(lm_studio_swap_models=False))

    @patch("src.core.lmstudio_models._request_json")
    def test_swap_unloads_vision_and_loads_editor(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            {"models": [{"key": "org/vision-model", "loaded_instances": [{"id": "org/vision-model"}]}]},
            {"instance_id": "org/vision-model"},
            {"status": "loaded", "instance_id": "org/editor-model"},
        ]
        swap_lmstudio_vision_to_editor(_settings())
        self.assertEqual(mock_request.call_count, 3)
        self.assertIn("/models/unload", mock_request.call_args_list[1].args[1])
        self.assertIn("/models/load", mock_request.call_args_list[2].args[1])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.core.lmstudio_models import (
    _find_loaded_instance_ids,
    ensure_lmstudio_model_loaded,
    lmstudio_api_root,
    should_swap_lmstudio_models,
    swap_lmstudio_model_to_editor,
    swap_lmstudio_vision_to_editor,
    unload_lmstudio_model,
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

    def test_find_loaded_matches_variant_config_to_base_instance(self) -> None:
        payload = {
            "models": [
                {
                    "key": "qwen/qwen3.6-27b-mtp",
                    "selected_variant": "qwen/qwen3.6-27b-mtp@q6_k",
                    "variants": ["qwen/qwen3.6-27b-mtp@q6_k", "qwen/qwen3.6-27b-mtp@q8_0"],
                    "loaded_instances": [{"id": "qwen/qwen3.6-27b-mtp"}],
                }
            ]
        }
        self.assertEqual(
            _find_loaded_instance_ids(payload, "qwen3.6-27b-mtp@q6_k"),
            ["qwen/qwen3.6-27b-mtp"],
        )

    def test_resolve_lmstudio_model_key_strips_variant(self) -> None:
        from src.core.lmstudio_models import _resolve_lmstudio_model_key

        payload = {
            "models": [
                {
                    "key": "qwen/qwen3.6-27b-mtp",
                    "variants": ["qwen/qwen3.6-27b-mtp@q6_k"],
                    "selected_variant": "qwen/qwen3.6-27b-mtp@q6_k",
                }
            ]
        }
        self.assertEqual(
            _resolve_lmstudio_model_key(payload, "qwen3.6-27b-mtp@q6_k"),
            "qwen/qwen3.6-27b-mtp",
        )


class TestSwapLmStudioModels(unittest.TestCase):
    def test_swap_noop_when_disabled(self) -> None:
        swap_lmstudio_vision_to_editor(_settings(lm_studio_swap_models=False))

    @patch("src.core.lmstudio_models._request_json")
    def test_swap_unloads_vision_and_loads_editor(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            {"models": [{"key": "org/vision-model", "loaded_instances": [{"id": "org/vision-model"}]}]},
            {"instance_id": "org/vision-model"},
            {"models": []},
            {"status": "loaded", "instance_id": "org/editor-model"},
        ]
        swap_lmstudio_vision_to_editor(_settings())
        self.assertEqual(mock_request.call_count, 4)
        self.assertIn("/models/unload", mock_request.call_args_list[1].args[1])
        self.assertIn("/models/load", mock_request.call_args_list[3].args[1])

    @patch("src.core.lmstudio_models._request_json")
    def test_swap_skips_editor_load_when_already_loaded(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            {
                "models": [
                    {
                        "key": "org/vision-model",
                        "loaded_instances": [{"id": "org/vision-model"}],
                    },
                    {
                        "key": "org/editor-model",
                        "loaded_instances": [{"id": "org/editor-model"}],
                    },
                ]
            },
            {"instance_id": "org/vision-model"},
            {
                "models": [
                    {
                        "key": "org/editor-model",
                        "loaded_instances": [{"id": "org/editor-model"}],
                    }
                ]
            },
        ]
        swap_lmstudio_vision_to_editor(_settings())
        self.assertEqual(mock_request.call_count, 3)
        self.assertIn("/models/unload", mock_request.call_args_list[1].args[1])
        self.assertTrue(
            all("/models/load" not in call.args[1] for call in mock_request.call_args_list)
        )

    @patch("src.core.lmstudio_models._request_json")
    def test_swap_from_model_unloads_glm_when_vision_equals_editor(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            {
                "models": [
                    {
                        "key": "org/glm-ocr",
                        "loaded_instances": [{"id": "org/glm-ocr"}],
                    }
                ]
            },
            {"instance_id": "org/glm-ocr"},
            {"models": []},
            {"status": "loaded", "instance_id": "org/editor-model"},
        ]
        swap_lmstudio_model_to_editor(
            _settings(vision_model="org/editor-model", editor_model="org/editor-model"),
            from_model="org/glm-ocr",
        )
        self.assertEqual(mock_request.call_count, 4)
        self.assertIn("/models/unload", mock_request.call_args_list[1].args[1])
        self.assertIn("/models/load", mock_request.call_args_list[3].args[1])


class TestUnloadLmStudioModel(unittest.TestCase):
    @patch("src.core.lmstudio_models._request_json")
    def test_unload_loaded_model(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            {
                "models": [
                    {
                        "key": "org/glm-ocr",
                        "loaded_instances": [{"id": "org/glm-ocr"}],
                    }
                ]
            },
            {"instance_id": "org/glm-ocr"},
        ]
        unloaded = unload_lmstudio_model(_settings(), "org/glm-ocr")
        self.assertEqual(unloaded, 1)
        self.assertIn("/models/unload", mock_request.call_args_list[1].args[1])
    @patch("src.core.lmstudio_models._load_model")
    @patch("src.core.lmstudio_models._request_json")
    def test_ensure_loads_when_not_loaded(self, request_json, load_model) -> None:
        request_json.return_value = {"models": []}
        load_model.return_value = {"status": "loaded", "instance_id": "inst-1"}
        from src.models.settings import Settings

        settings = Settings.model_validate(
            {
                "DATA_ROOT": "data",
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                "RESEARCH_MODEL": "research-model",
            }
        )
        ensure_lmstudio_model_loaded(settings, "research-model")
        load_model.assert_called_once_with(
            "http://127.0.0.1:1234",
            "research-model",
            settings,
        )

    @patch("src.core.lmstudio_models._load_model")
    @patch("src.core.lmstudio_models._request_json")
    def test_ensure_skips_when_loaded(self, request_json, load_model) -> None:
        request_json.return_value = {
            "models": [
                {
                    "key": "research-model",
                    "loaded_instances": [{"id": "inst-1"}],
                }
            ]
        }
        from src.models.settings import Settings

        settings = Settings.model_validate(
            {
                "DATA_ROOT": "data",
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                "RESEARCH_MODEL": "research-model",
            }
        )
        ensure_lmstudio_model_loaded(settings, "research-model")
        load_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()

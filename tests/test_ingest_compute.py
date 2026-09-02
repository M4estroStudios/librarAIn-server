from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.models.ingest_compute import (
    GLM_PIPELINE_STEPS,
    default_model_for_step,
    missing_cloud_config_for_plan,
    overlay_settings_for_step,
    parse_ingest_compute_plan,
    plan_needs_local_llm,
    resolve_ingest_compute_plan,
    with_step_override,
)
from src.models.settings import Settings


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "DATA_ROOT": "data",
        "OPENAI_PROVIDER": "local",
        "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
        "OPENAI_API_KEY": "local-key",
        "OPENAI_CLOUD_BASE_URL": "https://api.example.com/v1",
        "OPENAI_CLOUD_API_KEY": "cloud-key",
        "VISION_MODEL": "local-vision",
        "VISION_CLOUD_MODEL": "cloud-vision",
        "OCRVISION_MODEL": "local-ocr",
        "OCRVISION_CLOUD_MODEL": "cloud-ocr",
        "EDITOR_MODEL": "local-editor",
        "EDITOR_CLOUD_MODEL": "cloud-editor",
        "MATCHER_EMBEDDING_MODEL": "text-embedding-3-small",
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


class ParseIngestComputePlanTests(unittest.TestCase):
    def test_empty_raw_fills_default_mode(self) -> None:
        plan = parse_ingest_compute_plan(None, default_mode="cloud")
        self.assertEqual(plan.default_mode, "cloud")
        self.assertEqual(plan.summary_mode(), "cloud")
        self.assertEqual(plan.choice("stage3_editor").compute_mode, "cloud")
        self.assertIsNone(plan.choice("stage3_editor").model)

    def test_partial_steps_inherit_default(self) -> None:
        plan = parse_ingest_compute_plan(
            {
                "default_mode": "local",
                "steps": {
                    "stage3_editor": {"compute_mode": "cloud", "model": "gpt-4.1-mini"},
                },
            }
        )
        self.assertEqual(plan.choice("stage1_glm_ocr").compute_mode, "local")
        self.assertEqual(plan.choice("stage3_editor").compute_mode, "cloud")
        self.assertEqual(plan.choice("stage3_editor").model, "gpt-4.1-mini")
        self.assertEqual(plan.summary_mode(), "mixed")

    def test_json_string_and_invalid(self) -> None:
        plan = parse_ingest_compute_plan(
            json.dumps({"default_mode": "local", "steps": {}})
        )
        self.assertEqual(plan.default_mode, "local")
        with self.assertRaises(ValueError):
            parse_ingest_compute_plan("{not-json")

    def test_resolve_from_compute_mode_only(self) -> None:
        plan = resolve_ingest_compute_plan(compute_mode="cloud")
        self.assertEqual(plan.summary_mode(), "cloud")


class OverlayAndCloudValidationTests(unittest.TestCase):
    def test_overlay_applies_cloud_env_then_override(self) -> None:
        settings = _settings()
        plan = parse_ingest_compute_plan(
            {
                "default_mode": "local",
                "steps": {
                    "stage3_editor": {"compute_mode": "cloud", "model": "override-editor"},
                    "stage1_glm_ocr": {"compute_mode": "cloud"},
                },
            }
        )
        editor = overlay_settings_for_step(settings, plan, "stage3_editor")
        self.assertEqual(editor.editor_model, "override-editor")
        glm = overlay_settings_for_step(settings, plan, "stage1_glm_ocr")
        self.assertEqual(glm.ocrvision_model, "cloud-ocr")
        self.assertEqual(glm.glm_ocr_model, "cloud-ocr")

    def test_missing_cloud_only_for_cloud_steps_without_override(self) -> None:
        settings = _settings(OCRVISION_CLOUD_MODEL="", EDITOR_CLOUD_MODEL="cloud-editor")
        plan = parse_ingest_compute_plan(
            {
                "default_mode": "local",
                "steps": {
                    "stage1_glm_ocr": {"compute_mode": "cloud", "model": "ui-ocr"},
                    "stage3_editor": {"compute_mode": "cloud"},
                    "toc_refine": {"compute_mode": "local"},
                },
            }
        )
        missing = missing_cloud_config_for_plan(
            settings, plan, step_ids=GLM_PIPELINE_STEPS
        )
        self.assertEqual(missing, [])
        plan_no_override = parse_ingest_compute_plan(
            {
                "default_mode": "local",
                "steps": {"stage1_glm_ocr": {"compute_mode": "cloud"}},
            }
        )
        missing_ocr = missing_cloud_config_for_plan(
            settings, plan_no_override, step_ids=("stage1_glm_ocr",)
        )
        self.assertIn("OCRVISION_CLOUD_MODEL", missing_ocr)

    def test_all_local_skips_cloud_endpoint(self) -> None:
        settings = _settings(OPENAI_CLOUD_BASE_URL="", OPENAI_CLOUD_API_KEY="")
        plan = parse_ingest_compute_plan(None, default_mode="local")
        self.assertEqual(
            missing_cloud_config_for_plan(settings, plan, step_ids=GLM_PIPELINE_STEPS),
            [],
        )

    def test_defaults_and_step_override(self) -> None:
        settings = _settings()
        self.assertEqual(default_model_for_step(settings, "page_guidance", "local"), "local-vision")
        self.assertEqual(default_model_for_step(settings, "page_guidance", "cloud"), "cloud-vision")
        plan = resolve_ingest_compute_plan(compute_mode="local")
        updated = with_step_override(plan, "page_guidance", model="custom-vision")
        self.assertEqual(updated.choice("page_guidance").model, "custom-vision")

    def test_plan_needs_local_llm(self) -> None:
        all_cloud = parse_ingest_compute_plan(None, default_mode="cloud")
        self.assertFalse(plan_needs_local_llm(all_cloud, "glm_ocr"))
        mixed = parse_ingest_compute_plan(
            {"steps": {"stage3_editor": {"compute_mode": "local"}}}
        )
        self.assertTrue(plan_needs_local_llm(mixed, "glm_ocr"))
        self.assertFalse(
            plan_needs_local_llm(all_cloud, "classic", skip_vision_editor=True)
        )


class GpuVramNeedsLocalLlmTests(unittest.TestCase):
    def test_glm_skips_llm_when_needs_local_llm_false(self) -> None:
        from src.ingestion.pipeline.gpu_vram import require_gpu_vram_at_pipeline_start

        settings = _settings(GPU_VRAM_CHECK_ENABLED=True, GPU_VRAM_MAX_USED_GB=4.0)
        with patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_ocr"
        ) as mock_ocr, patch(
            "src.ingestion.pipeline.gpu_vram.ensure_gpu_vram_headroom_for_llm"
        ) as mock_llm, patch(
            "src.ingestion.pipeline.gpu_vram._load_gpu_snapshots", return_value=[]
        ):
            require_gpu_vram_at_pipeline_start(
                settings,
                skip_vision_editor=False,
                ocr_backend="glm",
                needs_local_llm=False,
            )
        mock_ocr.assert_not_called()
        mock_llm.assert_not_called()


class ComputeCatalogTests(unittest.TestCase):
    def test_build_catalog_survives_empty_lists(self) -> None:
        from src.api.ingest_compute_catalog import build_compute_catalog

        settings = _settings()
        with patch(
            "src.api.system_preflight._list_lmstudio_models",
            return_value=(
                [{"key": "local-vision", "display_name": "Vision", "loaded_instances": [{"id": "1"}]}],
                "http://127.0.0.1:1234",
            ),
        ), patch(
            "src.api.ingest_compute_catalog._list_cloud_models",
            return_value=[{"id": "cloud-vision", "display_name": "cloud-vision"}],
        ):
            catalog = build_compute_catalog(settings)
        self.assertTrue(catalog["cloud_configured"])
        self.assertEqual(catalog["local"][0]["id"], "local-vision")
        self.assertTrue(catalog["local"][0]["loaded"])
        self.assertEqual(catalog["defaults"]["stage3_editor"]["local"], "local-editor")
        self.assertEqual(len(catalog["steps"]), 8)

    def test_cloud_list_parses_openai_payload(self) -> None:
        from src.api.ingest_compute_catalog import _list_cloud_models

        class _Resp:
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"data": [{"id": "gpt-4.1-mini"}]}).encode("utf-8")

        settings = _settings()
        with patch("urllib.request.urlopen", return_value=_Resp()):
            models = _list_cloud_models(settings)
        self.assertEqual(models, [{"id": "gpt-4.1-mini", "display_name": "gpt-4.1-mini"}])


class IngestFormComputePlanTests(unittest.TestCase):
    def test_compute_plan_json_propagated(self) -> None:
        from src.api.ingest_form import build_ingest_payload_from_form

        fields = {
            "titolo": "Storia di Roma",
            "autore": "Mommsen, Theodor",
            "toc_range": "5-8",
            "index_range": "200-210",
            "compute_mode": "local",
            "compute_plan": json.dumps(
                {
                    "default_mode": "local",
                    "steps": {
                        "stage3_editor": {
                            "compute_mode": "cloud",
                            "model": "gpt-4.1-mini",
                        }
                    },
                }
            ),
        }
        payload = build_ingest_payload_from_form(fields)
        self.assertEqual(payload["compute_mode"], "local")
        self.assertEqual(
            payload["compute_plan"]["steps"]["stage3_editor"]["model"],
            "gpt-4.1-mini",
        )

    def test_invalid_compute_plan_raises(self) -> None:
        from src.api.ingest_form import build_ingest_payload_from_form

        fields = {
            "titolo": "Storia di Roma",
            "autore": "Mommsen, Theodor",
            "toc_range": "5-8",
            "index_range": "200-210",
            "compute_plan": "{bad",
        }
        with self.assertRaises(ValueError):
            build_ingest_payload_from_form(fields)


class PipelineRunComputePlanTests(unittest.TestCase):
    def test_stores_compute_plan_json(self) -> None:
        from src.persistence.book_sqlite import init_books_schema
        from src.persistence.pipeline_runs import (
            create_pipeline_run,
            get_pipeline_run_by_request_id,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            create_pipeline_run(
                sqlite_path,
                request_id="req-plan",
                source_sha256="deadbeef" * 8,
                pipeline_version="1.0",
                total_pages=2,
                compute_mode="mixed",
                compute_plan={"default_mode": "local", "steps": {}},
            )
            row = get_pipeline_run_by_request_id(sqlite_path, "req-plan")
            assert row is not None
            self.assertEqual(row["compute_mode"], "mixed")
            self.assertEqual(row["compute_plan"]["default_mode"], "local")


class SettingsChatRolesCloudTests(unittest.TestCase):
    def test_missing_cloud_config_uses_chat_roles(self) -> None:
        settings = _settings(VISION_CLOUD_MODEL="", EDITOR_CLOUD_MODEL="cloud-editor")
        missing = settings.missing_cloud_config(
            job_kind="ingest", chat_roles=("editor",), needs_embeddings=False
        )
        self.assertNotIn("VISION_CLOUD_MODEL", missing)
        self.assertNotIn("EDITOR_CLOUD_MODEL", missing)
        missing_vision = settings.missing_cloud_config(
            job_kind="ingest", chat_roles=("vision",), needs_embeddings=False
        )
        self.assertIn("VISION_CLOUD_MODEL", missing_vision)

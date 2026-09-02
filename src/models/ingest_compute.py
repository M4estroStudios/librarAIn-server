from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from src.models.settings import (
    _CHAT_MODEL_ATTRS,
    ChatModelRole,
    ComputeMode,
    Settings,
    normalize_compute_mode,
)

IngestComputeStepId = str

INGEST_COMPUTE_STEPS: tuple[str, ...] = (
    "page_guidance",
    "reicat_vision",
    "stage1_glm_ocr",
    "stage2_vision",
    "stage3_editor",
    "toc_refine",
    "index_refine",
    "biblio",
)

STEP_CHAT_ROLE: dict[str, ChatModelRole] = {
    "page_guidance": "vision",
    "reicat_vision": "vision",
    "stage1_glm_ocr": "ocrvision",
    "stage2_vision": "vision",
    "stage3_editor": "editor",
    "toc_refine": "editor",
    "index_refine": "editor",
    "biblio": "editor",
}

GLM_PIPELINE_STEPS: tuple[str, ...] = (
    "stage1_glm_ocr",
    "stage3_editor",
    "toc_refine",
    "index_refine",
    "biblio",
)
CLASSIC_PIPELINE_STEPS: tuple[str, ...] = (
    "stage2_vision",
    "stage3_editor",
    "toc_refine",
    "index_refine",
    "biblio",
)

INGEST_COMPUTE_STEP_META: tuple[dict[str, Any], ...] = (
    {
        "id": "page_guidance",
        "label": "Consiglio AI pagine",
        "pipelines": ["classic", "glm_ocr"],
    },
    {
        "id": "reicat_vision",
        "label": "REICAT",
        "pipelines": ["classic", "glm_ocr"],
    },
    {
        "id": "stage1_glm_ocr",
        "label": "OCR + Vision",
        "pipelines": ["glm_ocr"],
    },
    {
        "id": "stage2_vision",
        "label": "Vision",
        "pipelines": ["classic"],
    },
    {
        "id": "stage3_editor",
        "label": "Editor",
        "pipelines": ["classic", "glm_ocr"],
    },
    {
        "id": "toc_refine",
        "label": "Affinamento TOC",
        "pipelines": ["classic", "glm_ocr"],
    },
    {
        "id": "index_refine",
        "label": "Affinamento indice",
        "pipelines": ["classic", "glm_ocr"],
    },
    {
        "id": "biblio",
        "label": "Bibliografia",
        "pipelines": ["classic", "glm_ocr"],
    },
)


@dataclass(frozen=True)
class StepComputeChoice:
    compute_mode: ComputeMode
    model: str | None = None


@dataclass(frozen=True)
class IngestComputePlan:
    default_mode: ComputeMode
    steps: dict[str, StepComputeChoice]

    def choice(self, step_id: str) -> StepComputeChoice:
        found = self.steps.get(step_id)
        if found is not None:
            return found
        return StepComputeChoice(compute_mode=self.default_mode, model=None)

    def summary_mode(self) -> str:
        modes = {choice.compute_mode for choice in self.steps.values()}
        if not modes:
            return self.default_mode
        if modes == {"local"}:
            return "local"
        if modes == {"cloud"}:
            return "cloud"
        return "mixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_mode": self.default_mode,
            "steps": {
                step_id: {
                    "compute_mode": choice.compute_mode,
                    "model": choice.model,
                }
                for step_id, choice in self.steps.items()
            },
        }


def _coerce_model(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _parse_plan_mapping(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("compute_plan must be valid JSON") from exc
        raw = parsed
    if not isinstance(raw, dict):
        raise ValueError("compute_plan must be an object")
    return raw


def parse_ingest_compute_plan(
    raw: object,
    *,
    default_mode: ComputeMode | str | None = "local",
) -> IngestComputePlan:
    data = _parse_plan_mapping(raw)
    mode = normalize_compute_mode(data.get("default_mode") or default_mode)
    steps_raw = data.get("steps")
    if steps_raw is None:
        steps_raw = {}
    if not isinstance(steps_raw, dict):
        raise ValueError("compute_plan.steps must be an object")
    steps: dict[str, StepComputeChoice] = {}
    for step_id in INGEST_COMPUTE_STEPS:
        item = steps_raw.get(step_id)
        if not isinstance(item, dict):
            steps[step_id] = StepComputeChoice(compute_mode=mode, model=None)
            continue
        step_mode = normalize_compute_mode(item.get("compute_mode") or mode)
        steps[step_id] = StepComputeChoice(
            compute_mode=step_mode,
            model=_coerce_model(item.get("model")),
        )
    return IngestComputePlan(default_mode=mode, steps=steps)


def resolve_ingest_compute_plan(
    *,
    compute_mode: object = None,
    compute_plan: object = None,
) -> IngestComputePlan:
    default_mode = normalize_compute_mode(compute_mode)
    return parse_ingest_compute_plan(compute_plan, default_mode=default_mode)


def pipeline_llm_steps(
    pipeline_mode: str,
    *,
    skip_vision_editor: bool = False,
) -> tuple[str, ...]:
    if pipeline_mode == "glm_ocr":
        if skip_vision_editor:
            return ("stage1_glm_ocr",)
        return GLM_PIPELINE_STEPS
    if skip_vision_editor:
        return ()
    return CLASSIC_PIPELINE_STEPS


def plan_needs_local_llm(
    plan: IngestComputePlan | None,
    pipeline_mode: str,
    *,
    skip_vision_editor: bool = False,
) -> bool:
    if plan is None:
        return True
    for step_id in pipeline_llm_steps(
        pipeline_mode, skip_vision_editor=skip_vision_editor
    ):
        if plan.choice(step_id).compute_mode == "local":
            return True
    return False


def default_model_for_step(
    settings: Settings,
    step_id: str,
    mode: ComputeMode,
) -> str | None:
    role = STEP_CHAT_ROLE.get(step_id)
    if role is None:
        return None
    local_attr, cloud_attr = _CHAT_MODEL_ATTRS[role]
    if mode == "cloud":
        value = getattr(settings, cloud_attr, None)
        return str(value).strip() if isinstance(value, str) and value.strip() else None
    value = getattr(settings, local_attr, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if role == "ocrvision":
        fallback = getattr(settings, "glm_ocr_model", None)
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
    return None


def overlay_settings_for_step(
    settings: Settings,
    plan: IngestComputePlan,
    step_id: str,
) -> Settings:
    choice = plan.choice(step_id)
    job_settings = settings.for_compute_mode(choice.compute_mode)
    model = (choice.model or "").strip()
    if not model:
        return job_settings
    role = STEP_CHAT_ROLE.get(step_id)
    if role is None:
        return job_settings
    local_attr, _cloud_attr = _CHAT_MODEL_ATTRS[role]
    updates: dict[str, str | None] = {local_attr: model}
    if role == "ocrvision":
        updates["glm_ocr_model"] = model
    return job_settings.model_copy(update=updates)


def missing_cloud_config_for_plan(
    settings: Settings,
    plan: IngestComputePlan,
    *,
    step_ids: Sequence[str],
) -> list[str]:
    cloud_steps = [
        step_id for step_id in step_ids if plan.choice(step_id).compute_mode == "cloud"
    ]
    if not cloud_steps:
        return []
    missing: list[str] = []
    if not settings.openai_cloud_base_url:
        missing.append("OPENAI_CLOUD_BASE_URL")
    if not settings.openai_cloud_api_key:
        missing.append("OPENAI_CLOUD_API_KEY")
    seen_env: set[str] = set()
    for step_id in cloud_steps:
        choice = plan.choice(step_id)
        if choice.model:
            continue
        role = STEP_CHAT_ROLE.get(step_id)
        if role is None:
            continue
        _local_attr, cloud_attr = _CHAT_MODEL_ATTRS[role]
        if getattr(settings, cloud_attr):
            continue
        field = Settings.model_fields[cloud_attr]
        env_name = str(field.alias or cloud_attr)
        if env_name in seen_env:
            continue
        seen_env.add(env_name)
        missing.append(env_name)
    return missing


def resolved_model_for_step(settings: Settings, step_id: str) -> str:
    if step_id == "stage1_glm_ocr":
        from src.ingestion.pipeline.glm_ocr_stage import resolve_glm_ocr_model

        return resolve_glm_ocr_model(settings)
    role = STEP_CHAT_ROLE.get(step_id)
    if role is None:
        return ""
    local_attr, _cloud_attr = _CHAT_MODEL_ATTRS[role]
    value = getattr(settings, local_attr, None)
    return str(value).strip() if isinstance(value, str) else ""


@contextmanager
def apply_step_compute(
    settings: Settings,
    plan: IngestComputePlan,
    step_id: str,
) -> Iterator[Settings]:
    from src.core.openai_client import use_compute_mode

    step_settings = overlay_settings_for_step(settings, plan, step_id)
    choice = plan.choice(step_id)
    with use_compute_mode(choice.compute_mode, step_settings):
        yield step_settings


def step_defaults_payload(settings: Settings) -> dict[str, dict[str, str | None]]:
    defaults: dict[str, dict[str, str | None]] = {}
    for step_id in INGEST_COMPUTE_STEPS:
        defaults[step_id] = {
            "local": default_model_for_step(settings, step_id, "local"),
            "cloud": default_model_for_step(settings, step_id, "cloud"),
        }
    return defaults


def with_step_override(
    plan: IngestComputePlan,
    step_id: str,
    *,
    compute_mode: object = None,
    model: object = None,
) -> IngestComputePlan:
    choice = plan.choice(step_id)
    mode = choice.compute_mode
    if compute_mode not in (None, ""):
        mode = normalize_compute_mode(compute_mode)
    resolved_model = choice.model
    coerced = _coerce_model(model)
    if coerced:
        resolved_model = coerced
    steps = dict(plan.steps)
    steps[step_id] = StepComputeChoice(compute_mode=mode, model=resolved_model)
    return IngestComputePlan(default_mode=plan.default_mode, steps=steps)


def parse_compute_fields(fields: Mapping[str, Any]) -> IngestComputePlan:
    return resolve_ingest_compute_plan(
        compute_mode=fields.get("compute_mode"),
        compute_plan=fields.get("compute_plan"),
    )

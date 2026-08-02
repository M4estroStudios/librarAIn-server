from __future__ import annotations

from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
ComputeMode = Literal["local", "cloud"]
ChatModelRole = Literal[
    "vision",
    "ocrvision",
    "editor",
    "research",
    "matcher_llm",
    "time_index_llm",
]

_CHAT_MODEL_ATTRS: dict[ChatModelRole, tuple[str, str]] = {
    "vision": ("vision_model", "vision_cloud_model"),
    "ocrvision": ("ocrvision_model", "ocrvision_cloud_model"),
    "editor": ("editor_model", "editor_cloud_model"),
    "research": ("research_model", "research_cloud_model"),
    "matcher_llm": ("matcher_llm_model", "matcher_llm_cloud_model"),
    "time_index_llm": ("time_index_llm_model", "time_index_llm_cloud_model"),
}

JOB_CHAT_ROLES: dict[str, tuple[ChatModelRole, ...]] = {
    "ingest": ("vision", "editor", "matcher_llm", "time_index_llm", "ocrvision"),
    "ingest_glm": ("ocrvision", "editor", "matcher_llm", "time_index_llm"),
    "research": ("research", "editor", "matcher_llm"),
    "biblio": ("ocrvision",),
    "reicat": ("vision",),
    "repair": ("vision", "editor", "ocrvision"),
    "chat": ("research", "editor", "matcher_llm"),
    "merge_article": ("research", "editor", "matcher_llm"),
    "etaly": ("research", "editor", "matcher_llm"),
    "subject_dedup": ("matcher_llm", "editor"),
}

JOB_NEEDS_EMBEDDINGS: frozenset[str] = frozenset(
    {"ingest", "ingest_glm", "research", "subject_dedup", "embeddings"}
)

_REASONING_EFFORT_OFF = {"none", "off", "false", "0", "no", "disabled"}
_REASONING_EFFORT_ALLOWED: tuple[ReasoningEffort, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
)


def normalize_compute_mode(raw: object) -> ComputeMode:
    if raw is None:
        return "local"
    text = str(raw).strip().lower()
    if not text:
        return "local"
    if text in ("local", "cloud"):
        return text  # type: ignore[return-value]
    raise ValueError('compute_mode must be "local" or "cloud"')


def parse_compute_mode_field(raw: object) -> ComputeMode:
    try:
        return normalize_compute_mode(raw)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _parse_reasoning_effort(v: object, env_name: str) -> ReasoningEffort | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s or s in _REASONING_EFFORT_OFF:
        return None
    if s not in _REASONING_EFFORT_ALLOWED:
        raise ValueError(
            f"{env_name} must be one of: minimal, low, medium, high, or empty/off"
        )
    return s  # type: ignore[return-value]


def _parse_reasoning_enable_thinking(v: object, env_name: str) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if not s:
        return None
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name} must be true/false or empty")


class Settings(BaseModel):
    data_root: str = Field(min_length=1, alias="DATA_ROOT")
    openai_provider: Literal["local", "remote"] = Field(alias="OPENAI_PROVIDER")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_cloud_base_url: str | None = Field(default=None, alias="OPENAI_CLOUD_BASE_URL")
    openai_cloud_api_key: str | None = Field(default=None, alias="OPENAI_CLOUD_API_KEY")
    vision_model: str | None = Field(default=None, alias="VISION_MODEL")
    vision_cloud_model: str | None = Field(default=None, alias="VISION_CLOUD_MODEL")
    ocrvision_model: str | None = Field(default=None, alias="OCRVISION_MODEL")
    ocrvision_cloud_model: str | None = Field(default=None, alias="OCRVISION_CLOUD_MODEL")
    glm_ocr_model: str | None = Field(default=None, alias="GLM_OCR_MODEL")
    editor_model: str | None = Field(default=None, alias="EDITOR_MODEL")
    editor_cloud_model: str | None = Field(default=None, alias="EDITOR_CLOUD_MODEL")
    max_parallel_request: int = Field(default=2, gt=0, alias="MAX_PARALLEL_REQUEST")
    timeout_seconds: int = Field(default=120, gt=0, alias="TIMEOUT_SECONDS")
    research_timeout_seconds: int = Field(
        default=3600, gt=0, alias="RESEARCH_TIMEOUT_SECONDS"
    )
    retry_attempts: int = Field(default=2, ge=0, alias="RETRY_ATTEMPTS")
    rate_limit_per_minute: int = Field(default=60, gt=0, alias="RATE_LIMIT_PER_MINUTE")
    page_range_per_thread: int = Field(
        default=10, ge=1, alias="PAGE_RANGE_PER_THREAD"
    )
    ocr_languages: list[str] = Field(default_factory=lambda: ["it", "en"], alias="OCR_LANGUAGES")
    ocr_use_gpu: bool = Field(default=False, alias="OCR_USE_GPU")
    ocr_gpu_device: str = Field(default="all", alias="OCR_GPU_DEVICE")
    gpu_vram_check_enabled: bool = Field(default=True, alias="GPU_VRAM_CHECK_ENABLED")
    gpu_vram_max_used_gb: float = Field(default=4.0, ge=0, alias="GPU_VRAM_MAX_USED_GB")
    lm_studio_swap_models: bool = Field(default=True, alias="LM_STUDIO_SWAP_MODELS")
    lm_studio_load_timeout_seconds: int = Field(
        default=600, gt=0, alias="LM_STUDIO_LOAD_TIMEOUT_SECONDS"
    )
    reasoning_effort_vision: ReasoningEffort | None = Field(
        default=None, alias="REASONING_EFFORT_VISION"
    )
    reasoning_enable_thinking_vision: bool | None = Field(
        default=None, alias="REASONING_ENABLE_THINKING_VISION"
    )
    reasoning_effort_editor: ReasoningEffort | None = Field(
        default=None, alias="REASONING_EFFORT_EDITOR"
    )
    reasoning_enable_thinking_editor: bool | None = Field(
        default=None, alias="REASONING_ENABLE_THINKING_EDITOR"
    )
    matcher_embedding_model: str = Field(
        default="text-embedding-3-small", alias="MATCHER_EMBEDDING_MODEL"
    )
    matcher_llm_model: str | None = Field(default=None, alias="MATCHER_LLM_MODEL")
    matcher_llm_cloud_model: str | None = Field(
        default=None, alias="MATCHER_LLM_CLOUD_MODEL"
    )
    matcher_similarity_threshold: float = Field(
        default=0.86, ge=0.0, le=1.0, alias="MATCHER_SIMILARITY_THRESHOLD"
    )
    matcher_use_ai: bool = Field(default=True, alias="MATCHER_USE_AI")
    time_index_llm_model: str | None = Field(default=None, alias="TIME_INDEX_LLM_MODEL")
    time_index_llm_cloud_model: str | None = Field(
        default=None, alias="TIME_INDEX_LLM_CLOUD_MODEL"
    )
    time_index_use_llm: bool = Field(default=True, alias="TIME_INDEX_USE_LLM")
    research_model: str | None = Field(default=None, alias="RESEARCH_MODEL")
    research_cloud_model: str | None = Field(default=None, alias="RESEARCH_CLOUD_MODEL")
    research_temperature: float = Field(default=0.3, ge=0.0, le=2.0, alias="RESEARCH_TEMPERATURE")
    reasoning_effort_research: ReasoningEffort | None = Field(
        default=None, alias="REASONING_EFFORT_RESEARCH"
    )
    reasoning_enable_thinking_research: bool | None = Field(
        default=None, alias="REASONING_ENABLE_THINKING_RESEARCH"
    )
    tmp_keep_after_success: bool = Field(default=True, alias="TMP_KEEP_AFTER_SUCCESS")

    @field_validator("tmp_keep_after_success", mode="before")
    @classmethod
    def parse_tmp_keep_after_success(cls, v: object) -> bool:
        parsed = _parse_reasoning_enable_thinking(v, "TMP_KEEP_AFTER_SUCCESS")
        return True if parsed is None else parsed

    @field_validator("time_index_use_llm", mode="before")
    @classmethod
    def parse_time_index_use_llm(cls, v: object) -> bool:
        parsed = _parse_reasoning_enable_thinking(v, "TIME_INDEX_USE_LLM")
        return True if parsed is None else parsed

    @field_validator("matcher_use_ai", mode="before")
    @classmethod
    def parse_matcher_use_ai(cls, v: object) -> bool:
        parsed = _parse_reasoning_enable_thinking(v, "MATCHER_USE_AI")
        return True if parsed is None else parsed

    @field_validator("reasoning_effort_vision", mode="before")
    @classmethod
    def parse_reasoning_effort_vision(cls, v: object) -> ReasoningEffort | None:
        return _parse_reasoning_effort(v, "REASONING_EFFORT_VISION")

    @field_validator("reasoning_enable_thinking_vision", mode="before")
    @classmethod
    def parse_reasoning_enable_thinking_vision(cls, v: object) -> bool | None:
        return _parse_reasoning_enable_thinking(v, "REASONING_ENABLE_THINKING_VISION")

    @field_validator("reasoning_effort_editor", mode="before")
    @classmethod
    def parse_reasoning_effort_editor(cls, v: object) -> ReasoningEffort | None:
        return _parse_reasoning_effort(v, "REASONING_EFFORT_EDITOR")

    @field_validator("reasoning_enable_thinking_editor", mode="before")
    @classmethod
    def parse_reasoning_enable_thinking_editor(cls, v: object) -> bool | None:
        return _parse_reasoning_enable_thinking(v, "REASONING_ENABLE_THINKING_EDITOR")

    @field_validator("reasoning_effort_research", mode="before")
    @classmethod
    def parse_reasoning_effort_research(cls, v: object) -> ReasoningEffort | None:
        return _parse_reasoning_effort(v, "REASONING_EFFORT_RESEARCH")

    @field_validator("reasoning_enable_thinking_research", mode="before")
    @classmethod
    def parse_reasoning_enable_thinking_research(cls, v: object) -> bool | None:
        return _parse_reasoning_enable_thinking(v, "REASONING_ENABLE_THINKING_RESEARCH")

    @field_validator("gpu_vram_check_enabled", mode="before")
    @classmethod
    def parse_gpu_vram_check_enabled(cls, v: object) -> bool:
        parsed = _parse_reasoning_enable_thinking(v, "GPU_VRAM_CHECK_ENABLED")
        return True if parsed is None else parsed

    @field_validator("ocr_gpu_device", mode="before")
    @classmethod
    def parse_ocr_gpu_device(cls, v: object) -> str:
        s = str(v).strip().lower()
        if s == "all":
            return "all"
        if s.isdigit():
            return s
        raise ValueError("OCR_GPU_DEVICE must be 'all' or a non-negative integer (e.g. 0, 1)")

    @field_validator("ocr_languages", mode="before")
    @classmethod
    def parse_ocr_languages(cls, v: object) -> list[str]:
        if isinstance(v, list):
            return [str(lang).strip().lower() for lang in v if str(lang).strip()]
        if isinstance(v, str):
            return [lang.strip().lower() for lang in v.split(",") if lang.strip()]
        return v

    @property
    def sqlite_path(self) -> str:
        return str(Path(self.data_root) / "db" / "biblioteca.db")

    @property
    def processed_pdf_input_dir(self) -> str:
        return str(Path(self.data_root) / "input" / "processed")

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "Settings":
        self.data_root = self.data_root.strip()
        if not self.data_root:
            raise ValueError("DATA_ROOT must be non-empty")

        def _strip_optional(value: str | None) -> str | None:
            if value is None:
                return None
            return value.strip() or None

        self.openai_base_url = _strip_optional(self.openai_base_url)
        self.openai_api_key = _strip_optional(self.openai_api_key)
        self.openai_cloud_base_url = _strip_optional(self.openai_cloud_base_url)
        self.openai_cloud_api_key = _strip_optional(self.openai_cloud_api_key)
        self.vision_model = _strip_optional(self.vision_model)
        self.vision_cloud_model = _strip_optional(self.vision_cloud_model)
        self.ocrvision_model = _strip_optional(self.ocrvision_model)
        self.ocrvision_cloud_model = _strip_optional(self.ocrvision_cloud_model)
        self.glm_ocr_model = _strip_optional(self.glm_ocr_model)
        self.editor_model = _strip_optional(self.editor_model)
        self.editor_cloud_model = _strip_optional(self.editor_cloud_model)
        self.matcher_embedding_model = self.matcher_embedding_model.strip()
        if not self.matcher_embedding_model:
            raise ValueError("MATCHER_EMBEDDING_MODEL must be non-empty")
        self.matcher_llm_model = _strip_optional(self.matcher_llm_model)
        self.matcher_llm_cloud_model = _strip_optional(self.matcher_llm_cloud_model)
        self.time_index_llm_model = _strip_optional(self.time_index_llm_model)
        self.time_index_llm_cloud_model = _strip_optional(self.time_index_llm_cloud_model)
        self.research_model = _strip_optional(self.research_model)
        self.research_cloud_model = _strip_optional(self.research_cloud_model)

        if not self.ocrvision_model and self.glm_ocr_model:
            self.ocrvision_model = self.glm_ocr_model

        if self.openai_provider == "remote":
            missing_fields: list[str] = []
            if not self.openai_base_url:
                missing_fields.append("OPENAI_BASE_URL")
            if not self.openai_api_key:
                missing_fields.append("OPENAI_API_KEY")
            if missing_fields:
                raise ValueError(
                    "OPENAI_PROVIDER=remote requires: " + ", ".join(missing_fields)
                )
        return self

    def cloud_endpoint_configured(self) -> bool:
        return bool(self.openai_cloud_base_url and self.openai_cloud_api_key)

    def missing_cloud_config(
        self,
        *,
        job_kind: str,
        chat_roles: Sequence[ChatModelRole] | None = None,
        needs_embeddings: bool | None = None,
    ) -> list[str]:
        del chat_roles
        check_embeddings = (
            bool(needs_embeddings)
            if needs_embeddings is not None
            else job_kind in JOB_NEEDS_EMBEDDINGS
        )
        missing: list[str] = []
        if not self.openai_cloud_base_url:
            missing.append("OPENAI_CLOUD_BASE_URL")
        if not self.openai_cloud_api_key:
            missing.append("OPENAI_CLOUD_API_KEY")

        def _require_any(cloud_attrs: Sequence[str]) -> None:
            if any(getattr(self, attr) for attr in cloud_attrs):
                return
            field = Settings.model_fields[cloud_attrs[0]]
            missing.append(str(field.alias or cloud_attrs[0]))

        if job_kind in {"ingest", "repair"}:
            _require_any(("vision_cloud_model",))
            _require_any(("editor_cloud_model",))
        elif job_kind == "ingest_glm":
            _require_any(("ocrvision_cloud_model",))
            _require_any(("editor_cloud_model",))
        elif job_kind in {"research", "chat", "merge_article", "etaly"}:
            _require_any(
                ("research_cloud_model", "editor_cloud_model", "matcher_llm_cloud_model")
            )
        elif job_kind == "biblio":
            _require_any(("ocrvision_cloud_model",))
        elif job_kind == "reicat":
            _require_any(("vision_cloud_model",))
        elif job_kind == "subject_dedup":
            _require_any(("matcher_llm_cloud_model", "editor_cloud_model"))
        else:
            for role in JOB_CHAT_ROLES.get(job_kind, ()):
                _local_attr, cloud_attr = _CHAT_MODEL_ATTRS[role]
                if getattr(self, cloud_attr):
                    continue
                field = Settings.model_fields[cloud_attr]
                missing.append(str(field.alias or cloud_attr))

        if check_embeddings:
            if not self.openai_base_url:
                missing.append("OPENAI_BASE_URL")
            if not self.matcher_embedding_model:
                missing.append("MATCHER_EMBEDDING_MODEL")
        return missing

    def for_compute_mode(self, mode: ComputeMode) -> "Settings":
        if mode == "local":
            return self
        updates: dict[str, str | None] = {}
        for local_attr, cloud_attr in _CHAT_MODEL_ATTRS.values():
            cloud_value = getattr(self, cloud_attr)
            if cloud_value:
                updates[local_attr] = cloud_value
        if self.ocrvision_cloud_model:
            updates["glm_ocr_model"] = self.ocrvision_cloud_model
        if not updates:
            return self
        return self.model_copy(update=updates)

    def cloud_config_missing_env_vars(self) -> list[str]:
        missing: list[str] = []
        if not self.openai_cloud_base_url:
            missing.append("OPENAI_CLOUD_BASE_URL")
        if not self.openai_cloud_api_key:
            missing.append("OPENAI_CLOUD_API_KEY")
        for _local_attr, cloud_attr in _CHAT_MODEL_ATTRS.values():
            if not getattr(self, cloud_attr):
                field = Settings.model_fields[cloud_attr]
                missing.append(str(field.alias or cloud_attr))
        return missing

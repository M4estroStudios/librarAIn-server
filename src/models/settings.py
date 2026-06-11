from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ReasoningEffort = Literal["minimal", "low", "medium", "high"]

_REASONING_EFFORT_OFF = {"none", "off", "false", "0", "no", "disabled"}
_REASONING_EFFORT_ALLOWED: tuple[ReasoningEffort, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
)


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
    vision_model: str | None = Field(default=None, alias="VISION_MODEL")
    editor_model: str | None = Field(default=None, alias="EDITOR_MODEL")
    max_parallel_request: int = Field(default=2, gt=0, alias="MAX_PARALLEL_REQUEST")
    timeout_seconds: int = Field(default=120, gt=0, alias="TIMEOUT_SECONDS")
    retry_attempts: int = Field(default=2, ge=0, alias="RETRY_ATTEMPTS")
    rate_limit_per_minute: int = Field(default=60, gt=0, alias="RATE_LIMIT_PER_MINUTE")
    page_range_per_thread: int = Field(
        default=10, ge=1, alias="PAGE_RANGE_PER_THREAD"
    )
    ocr_languages: list[str] = Field(default_factory=lambda: ["it", "en"], alias="OCR_LANGUAGES")
    ocr_use_gpu: bool = Field(default=False, alias="OCR_USE_GPU")
    ocr_gpu_device: str = Field(default="all", alias="OCR_GPU_DEVICE")
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
    matcher_similarity_threshold: float = Field(
        default=0.86, ge=0.0, le=1.0, alias="MATCHER_SIMILARITY_THRESHOLD"
    )
    matcher_use_ai: bool = Field(default=True, alias="MATCHER_USE_AI")
    time_index_llm_model: str | None = Field(default=None, alias="TIME_INDEX_LLM_MODEL")
    time_index_use_llm: bool = Field(default=True, alias="TIME_INDEX_USE_LLM")

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

        if self.openai_base_url is not None:
            self.openai_base_url = self.openai_base_url.strip() or None
        if self.openai_api_key is not None:
            self.openai_api_key = self.openai_api_key.strip() or None
        if self.vision_model is not None:
            self.vision_model = self.vision_model.strip() or None
        if self.editor_model is not None:
            self.editor_model = self.editor_model.strip() or None
        self.matcher_embedding_model = self.matcher_embedding_model.strip()
        if not self.matcher_embedding_model:
            raise ValueError("MATCHER_EMBEDDING_MODEL must be non-empty")
        if self.matcher_llm_model is not None:
            self.matcher_llm_model = self.matcher_llm_model.strip() or None
        if self.time_index_llm_model is not None:
            self.time_index_llm_model = self.time_index_llm_model.strip() or None

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

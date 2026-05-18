from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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

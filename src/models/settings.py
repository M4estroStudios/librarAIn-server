from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    data_root: str = Field(min_length=1, alias="DATA_ROOT")
    openai_provider: Literal["local", "remote"] = Field(alias="OPENAI_PROVIDER")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    vision_model: str | None = Field(default=None, alias="VISION_MODEL")
    editor_model: str | None = Field(default=None, alias="EDITOR_MODEL")
    max_parallel: int = Field(default=2, gt=0, alias="MAX_PARALLEL")
    timeout_seconds: int = Field(default=120, gt=0, alias="TIMEOUT_SECONDS")
    retry_attempts: int = Field(default=2, ge=0, alias="RETRY_ATTEMPTS")
    rate_limit_per_minute: int = Field(default=60, gt=0, alias="RATE_LIMIT_PER_MINUTE")

    @property
    def sqlite_path(self) -> str:
        return str(Path(self.data_root) / "db" / "library.db")

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

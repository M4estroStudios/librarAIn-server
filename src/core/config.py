from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from src.core.log import ERROR_LOG_LEVEL, Log
from src.models.settings import Settings


class ConfigurationError(ValueError):
    pass


def _parse_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    parsed: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _format_settings_validation_error(exc: ValidationError) -> str:
    missing: list[str] = []
    invalid: list[str] = []

    def _to_env_name(raw_name: str) -> str:
        field = Settings.model_fields.get(raw_name)
        if field is not None and field.alias is not None:
            return field.alias
        return raw_name

    for error in exc.errors():
        loc = error.get("loc", ())
        raw_field_name = str(loc[0]) if loc else "unknown"
        field_name = _to_env_name(raw_field_name)
        if error.get("type") == "missing":
            missing.append(field_name)
        else:
            invalid.append(f"{field_name}: {error.get('msg', 'invalid value')}")

    chunks: list[str] = []
    if missing:
        chunks.append(
            "Missing required env vars: " + ", ".join(sorted(set(missing))) + "."
        )
    if invalid:
        chunks.append("Invalid env vars: " + "; ".join(invalid) + ".")
    chunks.append("See example.env")
    return " ".join(chunks)


def load_settings(env_file: str = ".env") -> Settings:
    env_path = Path(env_file)
    file_values = _parse_env_file(env_path)
    merged_values = {**file_values, **os.environ}

    try:
        return Settings.model_validate(merged_values)
    except ValidationError as exc:
        msg = _format_settings_validation_error(exc)
        Log(ERROR_LOG_LEVEL, "settings validation failed", {"error": msg})
        raise ConfigurationError(msg) from exc

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

from src.core.log import INFO_LOG_LEVEL, Log
from src.models.ingest_compute import INGEST_COMPUTE_STEP_META, step_defaults_payload
from src.models.settings import Settings


def _list_cloud_models(settings: Settings) -> list[dict[str, Any]]:
    base = str(getattr(settings, "openai_cloud_base_url", None) or "").strip()
    if not base:
        return []
    url = base.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    api_key = getattr(settings, "openai_cloud_api_key", None)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = float(getattr(settings, "timeout_seconds", 8) or 8)
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        Log(INFO_LOG_LEVEL, "cloud models list failed", {"error": repr(exc)})
        return []
    entries: list[Any]
    if isinstance(payload, dict):
        raw_entries = payload.get("data")
        if raw_entries is None:
            raw_entries = payload.get("models")
        entries = raw_entries if isinstance(raw_entries, list) else []
    elif isinstance(payload, list):
        entries = payload
    else:
        return []
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        model_id = ""
        display_name = ""
        if isinstance(entry, dict):
            model_id = str(entry.get("id") or entry.get("key") or "").strip()
            display_name = str(entry.get("display_name") or entry.get("id") or model_id).strip()
        elif isinstance(entry, str):
            model_id = entry.strip()
            display_name = model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, "display_name": display_name or model_id})
    return models


def _normalize_local_models(raw_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("key") or entry.get("id") or "").strip()
        if not model_id:
            continue
        display_name = str(entry.get("display_name") or model_id).strip() or model_id
        loaded = bool(entry.get("loaded_instances"))
        models.append(
            {
                "id": model_id,
                "display_name": display_name,
                "loaded": loaded,
            }
        )
    return models


def _empty_catalog() -> dict[str, Any]:
    return {
        "local": [],
        "cloud": [],
        "defaults": {},
        "steps": [dict(item) for item in INGEST_COMPUTE_STEP_META],
        "lmstudio_root": None,
        "cloud_endpoint": None,
        "cloud_configured": False,
    }


def build_compute_catalog(settings: Settings) -> dict[str, Any]:
    from src.api.system_preflight import _list_lmstudio_models

    try:
        local_raw, lm_root = _list_lmstudio_models(settings)
    except Exception as exc:
        Log(INFO_LOG_LEVEL, "local models list failed", {"error": repr(exc)})
        local_raw, lm_root = [], None
    cloud_url = str(getattr(settings, "openai_cloud_base_url", None) or "").strip()
    parsed = urlparse(cloud_url)
    cloud_host = parsed.netloc or None
    try:
        cloud_configured = bool(settings.cloud_endpoint_configured())
    except Exception:
        cloud_configured = bool(
            getattr(settings, "openai_cloud_base_url", None)
            and getattr(settings, "openai_cloud_api_key", None)
        )
    try:
        defaults = step_defaults_payload(settings)
    except Exception as exc:
        Log(INFO_LOG_LEVEL, "compute catalog defaults failed", {"error": repr(exc)})
        defaults = {}
    return {
        "local": _normalize_local_models(local_raw),
        "cloud": _list_cloud_models(settings),
        "defaults": defaults,
        "steps": [dict(item) for item in INGEST_COMPUTE_STEP_META],
        "lmstudio_root": lm_root,
        "cloud_endpoint": cloud_host,
        "cloud_configured": cloud_configured,
    }


def try_handle_compute_catalog_get(
    path: str,
    handler: Any,
    *,
    settings: Settings,
    send_json: Callable[..., None],
) -> bool:
    if path != "/api/ingest/compute-catalog":
        return False
    try:
        catalog = build_compute_catalog(settings)
    except Exception as exc:
        Log(INFO_LOG_LEVEL, "compute catalog failed", {"error": repr(exc)})
        catalog = _empty_catalog()
    send_json(handler, 200, {"ok": True, **catalog})
    return True

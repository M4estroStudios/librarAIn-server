from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse, urlunparse

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.models.settings import Settings


def lmstudio_api_root(settings: Settings) -> str | None:
    base = (settings.openai_base_url or "").strip()
    if not base:
        return None
    parsed = urlparse(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    elif path.endswith("/v1/"):
        path = path[: -len("/v1/")]
    return urlunparse((parsed.scheme, parsed.netloc, path or "", "", "", "")).rstrip("/")


def should_swap_lmstudio_models(settings: Settings) -> bool:
    if not settings.lm_studio_swap_models:
        return False
    if settings.openai_provider != "local":
        return False
    if not lmstudio_api_root(settings):
        return False
    vision = (settings.vision_model or "").strip()
    editor = (settings.editor_model or "").strip()
    if not vision or not editor:
        return False
    return vision != editor


def _model_matches(candidate: str, target: str) -> bool:
    c = candidate.strip().lower()
    t = target.strip().lower()
    if not c or not t:
        return False
    return c == t or c.endswith("/" + t) or t.endswith("/" + c)


def _find_loaded_instance_ids(models_payload: dict[str, Any], model_name: str) -> list[str]:
    ids: list[str] = []
    for entry in models_payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        keys = [
            str(entry.get("key", "")),
            str(entry.get("selected_variant", "")),
            str(entry.get("display_name", "")),
        ]
        if not any(_model_matches(k, model_name) for k in keys if k):
            continue
        for inst in entry.get("loaded_instances", []):
            if not isinstance(inst, dict):
                continue
            inst_id = str(inst.get("id", "")).strip()
            if inst_id:
                ids.append(inst_id)
    return ids


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str | None,
    body: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read().decode("utf-8")
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _unload_model(root: str, instance_id: str, settings: Settings) -> None:
    url = f"{root}/api/v1/models/unload"
    _request_json(
        "POST",
        url,
        api_key=settings.openai_api_key,
        body={"instance_id": instance_id},
        timeout_seconds=float(settings.timeout_seconds),
    )


def _load_model(root: str, model_name: str, settings: Settings) -> dict[str, Any]:
    url = f"{root}/api/v1/models/load"
    load_timeout = max(float(settings.timeout_seconds), float(settings.lm_studio_load_timeout_seconds))
    return _request_json(
        "POST",
        url,
        api_key=settings.openai_api_key,
        body={"model": model_name},
        timeout_seconds=load_timeout,
    )


def swap_lmstudio_vision_to_editor(settings: Settings) -> None:
    if not should_swap_lmstudio_models(settings):
        return

    root = lmstudio_api_root(settings)
    vision = (settings.vision_model or "").strip()
    editor = (settings.editor_model or "").strip()
    assert root and vision and editor

    Log(INFO_LOG_LEVEL, "lmstudio model swap begin", {"vision_model": vision, "editor_model": editor})

    try:
        listed = _request_json(
            "GET",
            f"{root}/api/v1/models",
            api_key=settings.openai_api_key,
            timeout_seconds=float(settings.timeout_seconds),
        )
    except urllib.error.URLError as exc:
        Log(ERROR_LOG_LEVEL, "lmstudio list models failed", {"error": repr(exc)})
        raise RuntimeError(f"LM Studio list models failed: {exc}") from exc

    instance_ids = _find_loaded_instance_ids(listed, vision)
    if not instance_ids:
        instance_ids = [vision]

    unloaded = 0
    for instance_id in dict.fromkeys(instance_ids):
        try:
            _unload_model(root, instance_id, settings)
            unloaded += 1
            Log(INFO_LOG_LEVEL, "lmstudio vision model unloaded", {"instance_id": instance_id})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                Log(WARNING_LOG_LEVEL, "lmstudio vision unload skipped (not loaded)", {"instance_id": instance_id})
                continue
            Log(ERROR_LOG_LEVEL, "lmstudio vision unload failed", {"instance_id": instance_id, "error": repr(exc)})
            raise RuntimeError(f"LM Studio unload failed for {instance_id}: {exc}") from exc
        except urllib.error.URLError as exc:
            Log(ERROR_LOG_LEVEL, "lmstudio vision unload failed", {"instance_id": instance_id, "error": repr(exc)})
            raise RuntimeError(f"LM Studio unload failed for {instance_id}: {exc}") from exc

    try:
        load_result = _load_model(root, editor, settings)
    except urllib.error.URLError as exc:
        Log(ERROR_LOG_LEVEL, "lmstudio editor load failed", {"editor_model": editor, "error": repr(exc)})
        raise RuntimeError(f"LM Studio load failed for {editor}: {exc}") from exc

    Log(
        INFO_LOG_LEVEL,
        "lmstudio model swap done",
        {
            "vision_model": vision,
            "editor_model": editor,
            "unloaded_instances": unloaded,
            "load_status": load_result.get("status"),
            "load_instance_id": load_result.get("instance_id"),
        },
    )

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


def _model_base_name(name: str) -> str:
    return name.split("@", 1)[0].strip().lower()


def _variant_suffix(name: str) -> str | None:
    if "@" not in name:
        return None
    suffix = name.split("@", 1)[1].strip().lower()
    return suffix or None


def _model_tail(name: str) -> str:
    base = _model_base_name(name)
    return base.rsplit("/", 1)[-1]


def _model_matches(candidate: str, target: str) -> bool:
    c = candidate.strip().lower()
    t = target.strip().lower()
    if not c or not t:
        return False
    if c == t:
        return True
    if c.endswith("/" + t) or t.endswith("/" + c):
        return True
    cb = _model_base_name(c)
    tb = _model_base_name(t)
    if cb == tb:
        return _variants_compatible(c, t)
    if cb.endswith("/" + tb) or tb.endswith("/" + cb):
        return _variants_compatible(c, t)
    if _model_tail(c) == _model_tail(t):
        return _variants_compatible(c, t)
    return False


def _variants_compatible(candidate: str, target: str) -> bool:
    c_var = _variant_suffix(candidate)
    t_var = _variant_suffix(target)
    if t_var and c_var:
        return c_var == t_var
    return True


def _entry_model_keys(entry: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("key", "selected_variant", "display_name"):
        value = str(entry.get(field, "")).strip()
        if value:
            keys.append(value)
    variants = entry.get("variants")
    if isinstance(variants, list):
        for item in variants:
            if isinstance(item, str) and item.strip():
                keys.append(item.strip())
    return keys


def _resolve_lmstudio_model_key(models_payload: dict[str, Any], model_name: str) -> str:
    target = (model_name or "").strip()
    if not target:
        return target
    for entry in models_payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        if any(_model_matches(key, target) for key in _entry_model_keys(entry)):
            base_key = str(entry.get("key", "")).strip()
            if base_key:
                return base_key
    if "@" in target:
        return target.split("@", 1)[0].strip()
    return target


def _find_loaded_instance_ids(models_payload: dict[str, Any], model_name: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for entry in models_payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        keys = _entry_model_keys(entry)
        entry_matches = any(_model_matches(key, model_name) for key in keys)
        for inst in entry.get("loaded_instances", []):
            if not isinstance(inst, dict):
                continue
            inst_id = str(inst.get("id", "")).strip()
            if not inst_id or inst_id in seen:
                continue
            if entry_matches or _model_matches(inst_id, model_name):
                ids.append(inst_id)
                seen.add(inst_id)
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


def ensure_lmstudio_model_loaded(settings: Settings, model_name: str) -> None:
    name = (model_name or "").strip()
    if not name or settings.openai_provider != "local":
        return
    root = lmstudio_api_root(settings)
    if not root:
        raise RuntimeError("LM Studio base URL non configurato")
    try:
        listed = _request_json(
            "GET",
            f"{root}/api/v1/models",
            api_key=settings.openai_api_key,
            timeout_seconds=float(settings.timeout_seconds),
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LM Studio list models failed: {exc}") from exc
    if _find_loaded_instance_ids(listed, name):
        Log(INFO_LOG_LEVEL, "lmstudio model already loaded", {"model": name})
        return
    load_key = _resolve_lmstudio_model_key(listed, name)
    Log(INFO_LOG_LEVEL, "lmstudio model load begin", {"model": name, "load_key": load_key})
    try:
        load_result = _load_model(root, load_key, settings)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        Log(ERROR_LOG_LEVEL, "lmstudio model load failed", {"model": name, "error": detail})
        raise RuntimeError(f"Caricamento modello {name} fallito: {detail}") from exc
    except urllib.error.URLError as exc:
        Log(ERROR_LOG_LEVEL, "lmstudio model load failed", {"model": name, "error": repr(exc)})
        raise RuntimeError(f"Caricamento modello {name} fallito: {exc}") from exc
    Log(
        INFO_LOG_LEVEL,
        "lmstudio model load done",
        {
            "model": name,
            "load_status": load_result.get("status"),
            "load_instance_id": load_result.get("instance_id"),
        },
    )


def _lmstudio_management_enabled(settings: Settings) -> bool:
    if not settings.lm_studio_swap_models:
        return False
    if settings.openai_provider != "local":
        return False
    return bool(lmstudio_api_root(settings))


def _unload_loaded_model_instances(
    root: str,
    listed: dict[str, Any],
    model_name: str,
    settings: Settings,
) -> int:
    source = (model_name or "").strip()
    if not source:
        return 0
    instance_ids = _find_loaded_instance_ids(listed, source)
    if not instance_ids:
        instance_ids = [source]
    unloaded = 0
    for instance_id in dict.fromkeys(instance_ids):
        try:
            _unload_model(root, instance_id, settings)
            unloaded += 1
            Log(INFO_LOG_LEVEL, "lmstudio model unloaded", {"model": source, "instance_id": instance_id})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                Log(
                    WARNING_LOG_LEVEL,
                    "lmstudio model unload skipped (not loaded)",
                    {"model": source, "instance_id": instance_id},
                )
                continue
            Log(
                ERROR_LOG_LEVEL,
                "lmstudio model unload failed",
                {"model": source, "instance_id": instance_id, "error": repr(exc)},
            )
            raise RuntimeError(f"LM Studio unload failed for {instance_id}: {exc}") from exc
        except urllib.error.URLError as exc:
            Log(
                ERROR_LOG_LEVEL,
                "lmstudio model unload failed",
                {"model": source, "instance_id": instance_id, "error": repr(exc)},
            )
            raise RuntimeError(f"LM Studio unload failed for {instance_id}: {exc}") from exc
    return unloaded


def unload_lmstudio_model(settings: Settings, model_name: str) -> int:
    if not _lmstudio_management_enabled(settings):
        return 0
    source = (model_name or "").strip()
    if not source:
        return 0
    root = lmstudio_api_root(settings)
    assert root
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
    return _unload_loaded_model_instances(root, listed, source, settings)


def swap_lmstudio_model_to_editor(
    settings: Settings,
    *,
    from_model: str | None = None,
) -> None:
    if not _lmstudio_management_enabled(settings):
        return
    root = lmstudio_api_root(settings)
    source = (from_model or settings.vision_model or "").strip()
    editor = (settings.editor_model or "").strip()
    if not root or not source or not editor or source == editor:
        return

    Log(INFO_LOG_LEVEL, "lmstudio model swap begin", {"from_model": source, "editor_model": editor})

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

    unloaded = _unload_loaded_model_instances(root, listed, source, settings)

    try:
        load_key = _resolve_lmstudio_model_key(listed, editor)
        load_result = _load_model(root, load_key, settings)
    except urllib.error.URLError as exc:
        Log(ERROR_LOG_LEVEL, "lmstudio editor load failed", {"editor_model": editor, "error": repr(exc)})
        raise RuntimeError(f"LM Studio load failed for {editor}: {exc}") from exc

    Log(
        INFO_LOG_LEVEL,
        "lmstudio model swap done",
        {
            "from_model": source,
            "editor_model": editor,
            "unloaded_instances": unloaded,
            "load_status": load_result.get("status"),
            "load_instance_id": load_result.get("instance_id"),
        },
    )


def swap_lmstudio_vision_to_editor(settings: Settings) -> None:
    if not should_swap_lmstudio_models(settings):
        return
    swap_lmstudio_model_to_editor(settings)

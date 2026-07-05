from __future__ import annotations

import json
import urllib.error
from typing import Any, Literal

from src.core.lmstudio_models import _find_loaded_instance_ids, ensure_lmstudio_model_loaded, lmstudio_api_root
from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.pipeline.gpu_vram import (
    _LLM_LOAD_MIN_FREE_GB,
    _MODEL_LOADED_USED_THRESHOLD_GB,
    _load_gpu_snapshots,
    _required_free_gb_for_snapshot,
    collect_gpu_vram_snapshots,
    ensure_gpu_vram_headroom_for_llm,
    ensure_gpu_vram_headroom_for_ocr,
)
from src.models.settings import Settings
from src.search.article_llm import research_model

PreflightOperation = Literal[
    "ingest",
    "research",
    "research-merge",
    "repair",
    "repair-all",
    "chat",
]

_PREFLIGHT_OPERATIONS: frozenset[str] = frozenset(
    {"ingest", "research", "research-merge", "repair", "repair-all", "chat"}
)


def normalize_preflight_operation(raw: str) -> str | None:
    op = (raw or "").strip().lower()
    if op == "chat":
        return "research"
    if op in _PREFLIGHT_OPERATIONS:
        return op
    return None


def _optional_model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _first_model_for_operation(settings: Settings, operation: str) -> str | None:
    if operation in ("research", "research-merge"):
        return research_model(settings)
    if operation in ("repair", "repair-all"):
        if settings.openai_provider != "local":
            return None
        for attr in ("vision_model", "editor_model"):
            model = _optional_model_name(getattr(settings, attr, None))
            if model:
                return model
        return None
    if operation == "ingest":
        if settings.openai_provider != "local":
            return None
        for attr in ("vision_model", "editor_model"):
            model = _optional_model_name(getattr(settings, attr, None))
            if model:
                return model
        return None
    return None


def _list_lmstudio_models(settings: Settings) -> tuple[list[dict[str, Any]], str | None]:
    root = lmstudio_api_root(settings)
    if not root:
        return [], None
    url = f"{root}/api/v1/models"
    headers = {"Accept": "application/json"}
    if settings.openai_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"
    try:
        import urllib.request

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=float(settings.timeout_seconds)) as resp:
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return [], root
        models: list[dict[str, Any]] = []
        for entry in payload.get("models", []):
            if not isinstance(entry, dict):
                continue
            loaded = []
            for inst in entry.get("loaded_instances", []):
                if isinstance(inst, dict) and inst.get("id"):
                    loaded.append(
                        {
                            "id": str(inst.get("id")),
                            "vram_gb": inst.get("vram_gb"),
                        }
                    )
            models.append(
                {
                    "key": str(entry.get("key", "")),
                    "display_name": str(entry.get("display_name", "")),
                    "loaded_instances": loaded,
                }
            )
        return models, root
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        Log(INFO_LOG_LEVEL, "preflight lmstudio list failed", {"error": repr(exc)})
        return [], root


def _model_loaded(models_payload: list[dict[str, Any]], model_name: str) -> bool:
    listed = {"models": models_payload}
    return bool(_find_loaded_instance_ids(listed, model_name))


def _vram_payload(snapshots: list) -> list[dict[str, Any]]:
    return [
        {
            "device_index": item.device_index,
            "used_gb": round(item.used_gb, 2),
            "total_gb": round(item.total_gb, 2),
            "free_gb": round(item.free_gb, 2),
        }
        for item in snapshots
    ]


def _check_vram_for_operation(settings: Settings, operation: str) -> tuple[bool, str]:
    if not bool(settings.gpu_vram_check_enabled):
        return True, "GPU VRAM check disabilitato"
    per_instance_gb = float(settings.gpu_vram_max_used_gb)
    if per_instance_gb <= 0:
        return True, "Soglia VRAM non configurata"

    try:
        if operation == "ingest":
            needs_ocr = bool(settings.ocr_use_gpu)
            needs_llm = settings.openai_provider == "local"
            if needs_ocr:
                pool = max(1, int(settings.max_parallel_request))
                ocr_device = str(settings.ocr_gpu_device or "all")
                ocr_snapshots = _load_gpu_snapshots(ocr_device)
                ensure_gpu_vram_headroom_for_ocr(
                    ocr_snapshots,
                    pool_size=pool,
                    per_instance_load_gb=per_instance_gb,
                )
            if needs_llm:
                llm_snapshots = _load_gpu_snapshots("all")
                ensure_gpu_vram_headroom_for_llm(llm_snapshots, load_free_gb=_LLM_LOAD_MIN_FREE_GB)
            return True, "VRAM sufficiente per ingest"
        if operation in ("research", "research-merge"):
            if settings.openai_provider != "local":
                return True, "Provider remoto: VRAM LLM non richiesta"
            snapshots = _load_gpu_snapshots("all")
            ensure_gpu_vram_headroom_for_llm(snapshots, load_free_gb=_LLM_LOAD_MIN_FREE_GB)
            return True, "VRAM sufficiente per research"
        if operation in ("repair", "repair-all"):
            needs_ocr = bool(settings.ocr_use_gpu)
            needs_llm = settings.openai_provider == "local"
            if needs_ocr:
                pool = max(1, int(settings.max_parallel_request))
                ocr_device = str(settings.ocr_gpu_device or "all")
                ocr_snapshots = _load_gpu_snapshots(ocr_device)
                ensure_gpu_vram_headroom_for_ocr(
                    ocr_snapshots,
                    pool_size=pool,
                    per_instance_load_gb=per_instance_gb,
                )
            if needs_llm:
                llm_snapshots = _load_gpu_snapshots("all")
                ensure_gpu_vram_headroom_for_llm(llm_snapshots, load_free_gb=_LLM_LOAD_MIN_FREE_GB)
            return True, "VRAM sufficiente per repair"
    except Exception as exc:
        detail = str(exc)
        if hasattr(exc, "detail") and getattr(exc, "detail", None):
            detail = str(getattr(exc.detail, "message", exc))
        return False, detail
    return True, "OK"


def _ensure_model_for_operation(
    settings: Settings,
    operation: str,
    models_payload: list[dict[str, Any]],
) -> tuple[bool, str]:
    required = _first_model_for_operation(settings, operation)
    if not required:
        return True, "Nessun modello locale richiesto"
    if _model_loaded(models_payload, required):
        return True, f"Modello {required} già caricato"
    snapshots = collect_gpu_vram_snapshots(gpu_device="all")
    if not snapshots:
        return False, "Impossibile verificare VRAM GPU"
    for snapshot in snapshots:
        needed = _required_free_gb_for_snapshot(
            snapshot,
            load_free_gb=_LLM_LOAD_MIN_FREE_GB,
            loaded_threshold_gb=_MODEL_LOADED_USED_THRESHOLD_GB,
            inference_free_gb=2.0,
        )
        if snapshot.free_gb >= needed:
            try:
                ensure_lmstudio_model_loaded(settings, required)
            except RuntimeError as exc:
                return False, str(exc)
            return True, f"Modello {required} caricato"
    details = ", ".join(
        f"GPU {s.device_index}: {s.free_gb:.1f} GB liberi" for s in snapshots
    )
    return False, (
        f"Modello {required} non caricato e VRAM insufficiente ({details}). "
        f"Servono circa {_LLM_LOAD_MIN_FREE_GB:g} GB liberi."
    )


def evaluate_preflight(settings: Settings, operation: str) -> dict[str, Any]:
    vram_ok, vram_msg = _check_vram_for_operation(settings, operation)
    models_payload, lm_root = _list_lmstudio_models(settings)
    model_ok, model_msg = _ensure_model_for_operation(settings, operation, models_payload)
    required_model = _first_model_for_operation(settings, operation)
    if model_ok and required_model:
        models_payload, lm_root = _list_lmstudio_models(settings)
    snapshots = collect_gpu_vram_snapshots(gpu_device="all")
    ok = vram_ok and model_ok
    parts = [vram_msg, model_msg]
    message = " · ".join(parts) if ok else next((p for p in parts if "insufficient" in p.lower() or "impossibile" in p.lower() or not vram_ok or not model_ok), parts[0])
    if not ok:
        message = model_msg if not model_ok else vram_msg
    Log(
        INFO_LOG_LEVEL if ok else WARNING_LOG_LEVEL,
        "preflight evaluated",
        {
            "operation": operation,
            "ok": ok,
            "required_model": required_model,
            "vram_ok": vram_ok,
            "model_ok": model_ok,
        },
    )
    loaded_models = [
        {
            "key": m.get("key"),
            "display_name": m.get("display_name"),
            "instances": m.get("loaded_instances"),
        }
        for m in models_payload
        if m.get("loaded_instances")
    ]
    return {
        "ok": ok,
        "message": message,
        "operation": operation,
        "vram": _vram_payload(snapshots),
        "loaded_models": loaded_models,
        "required_model": required_model,
        "lmstudio_root": lm_root,
    }

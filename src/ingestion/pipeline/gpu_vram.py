from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.models.request import IngestInputErrorCode, IngestInputValidationError, IngestInputValidationException
from src.models.settings import Settings

_BYTES_PER_GB = 1024**3
_MIB = 1024 * 1024
_MODEL_LOADED_USED_THRESHOLD_GB = 8.0
_INFERENCE_MIN_FREE_GB = 2.0
_LLM_LOAD_MIN_FREE_GB = 12.0


@dataclass(frozen=True)
class GpuVramSnapshot:
    device_index: int
    used_bytes: int
    total_bytes: int

    @property
    def used_gb(self) -> float:
        return self.used_bytes / _BYTES_PER_GB

    @property
    def total_gb(self) -> float:
        return self.total_bytes / _BYTES_PER_GB

    @property
    def free_bytes(self) -> int:
        return max(0, self.total_bytes - self.used_bytes)

    @property
    def free_gb(self) -> float:
        return self.free_bytes / _BYTES_PER_GB


def _import_torch():
    import torch  # noqa: PLC0415

    return torch


def _cuda_is_available() -> bool:
    try:
        return _import_torch().cuda.is_available()
    except ImportError:
        return False


def _cuda_device_count() -> int:
    return _import_torch().cuda.device_count()


def _cuda_mem_get_info(device_index: int) -> tuple[int, int]:
    return _import_torch().cuda.mem_get_info(device_index)


def _resolve_gpu_device_indices(gpu_device: str) -> list[int]:
    count = _cuda_device_count()
    if count <= 0:
        return []
    if gpu_device == "all":
        return list(range(count))
    index = int(gpu_device)
    if index < 0 or index >= count:
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.GPU_VRAM_BUSY,
                message=(
                    f"OCR_GPU_DEVICE={gpu_device} non disponibile "
                    f"(schede CUDA rilevate: {count})"
                ),
                field="OCR_GPU_DEVICE",
            )
        )
    return [index]


def _collect_gpu_vram_via_nvidia_smi() -> list[GpuVramSnapshot] | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        Log(WARNING_LOG_LEVEL, "nvidia-smi vram query failed", {"error": repr(exc)})
        return None

    snapshots: list[GpuVramSnapshot] = []
    for line in proc.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
            used_mib = float(parts[1])
            total_mib = float(parts[2])
        except ValueError:
            continue
        snapshots.append(
            GpuVramSnapshot(
                device_index=index,
                used_bytes=int(used_mib * _MIB),
                total_bytes=int(total_mib * _MIB),
            )
        )
    return snapshots or None


def _filter_snapshots_by_gpu_device(
    snapshots: list[GpuVramSnapshot],
    gpu_device: str,
) -> list[GpuVramSnapshot]:
    if gpu_device == "all":
        return snapshots
    index = int(gpu_device)
    matched = [item for item in snapshots if item.device_index == index]
    if not matched:
        known = ", ".join(str(item.device_index) for item in snapshots)
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.GPU_VRAM_BUSY,
                message=(
                    f"OCR_GPU_DEVICE={gpu_device} non disponibile "
                    f"(schede GPU rilevate: {known or 'nessuna'})"
                ),
                field="OCR_GPU_DEVICE",
            )
        )
    return matched


def _collect_gpu_vram_via_torch(*, gpu_device: str) -> list[GpuVramSnapshot]:
    if not _cuda_is_available():
        return []
    snapshots: list[GpuVramSnapshot] = []
    for index in _resolve_gpu_device_indices(gpu_device):
        free_bytes, total_bytes = _cuda_mem_get_info(index)
        used_bytes = total_bytes - free_bytes
        snapshots.append(
            GpuVramSnapshot(
                device_index=index,
                used_bytes=used_bytes,
                total_bytes=total_bytes,
            )
        )
    return snapshots


def collect_gpu_vram_snapshots(*, gpu_device: str = "all") -> list[GpuVramSnapshot]:
    smi_snapshots = _collect_gpu_vram_via_nvidia_smi()
    if smi_snapshots is not None:
        return _filter_snapshots_by_gpu_device(smi_snapshots, gpu_device)
    return _collect_gpu_vram_via_torch(gpu_device=gpu_device)


def _required_free_gb_for_snapshot(
    snapshot: GpuVramSnapshot,
    *,
    load_free_gb: float,
    loaded_threshold_gb: float,
    inference_free_gb: float,
) -> float:
    if snapshot.used_gb >= loaded_threshold_gb:
        return inference_free_gb
    return load_free_gb


def _raise_gpu_vram_busy(message: str, *, log_key: str, **log_fields: object) -> None:
    Log(ERROR_LOG_LEVEL, log_key, log_fields)
    raise IngestInputValidationException(
        IngestInputValidationError(
            code=IngestInputErrorCode.GPU_VRAM_BUSY,
            message=message,
        )
    )


def _load_gpu_snapshots(gpu_device: str) -> list[GpuVramSnapshot]:
    snapshots = collect_gpu_vram_snapshots(gpu_device=gpu_device)
    if not snapshots:
        message = (
            "Impossibile verificare la VRAM GPU: nvidia-smi non disponibile "
            "e CUDA non rilevata. Installa i driver NVIDIA o disabilita GPU_VRAM_CHECK_ENABLED."
        )
        Log(ERROR_LOG_LEVEL, "gpu vram preflight unavailable", {"gpu_device": gpu_device})
        raise IngestInputValidationException(
            IngestInputValidationError(
                code=IngestInputErrorCode.GPU_VRAM_BUSY,
                message=message,
            )
        )
    return snapshots


def ensure_gpu_vram_headroom_on_devices(
    snapshots: list[GpuVramSnapshot],
    *,
    required_free_gb: float,
    operation_label: str,
) -> None:
    if required_free_gb <= 0:
        return
    short = [item for item in snapshots if item.free_gb < required_free_gb]
    if not short:
        details = ", ".join(
            f"GPU {item.device_index}: {item.free_gb:.1f} GB liberi "
            f"({item.used_gb:.1f}/{item.total_gb:.1f} GB in uso)"
            for item in snapshots
        )
        Log(
            INFO_LOG_LEVEL,
            "gpu vram headroom ok",
            {"operation": operation_label, "required_free_gb": required_free_gb, "devices": details},
        )
        return
    details = ", ".join(
        f"GPU {item.device_index}: {item.free_gb:.1f} GB liberi "
        f"({item.used_gb:.1f}/{item.total_gb:.1f} GB in uso)"
        for item in short
    )
    message = (
        f"VRAM GPU insufficiente per {operation_label}: servono almeno "
        f"{required_free_gb:g} GB liberi per scheda ({details}). "
        "Libera VRAM o sposta OCR/LLM su un'altra GPU."
    )
    _raise_gpu_vram_busy(
        message,
        log_key="gpu vram headroom failed",
        operation=operation_label,
        required_free_gb=required_free_gb,
        short_devices=details,
    )


def ensure_gpu_vram_headroom_for_ocr(
    snapshots: list[GpuVramSnapshot],
    *,
    pool_size: int,
    per_instance_load_gb: float,
    loaded_threshold_gb: float = _MODEL_LOADED_USED_THRESHOLD_GB,
    inference_free_gb: float = _INFERENCE_MIN_FREE_GB,
) -> None:
    pool = max(1, pool_size)
    short: list[tuple[GpuVramSnapshot, float]] = []
    for snapshot in snapshots:
        needed = _required_free_gb_for_snapshot(
            snapshot,
            load_free_gb=per_instance_load_gb * pool,
            loaded_threshold_gb=loaded_threshold_gb,
            inference_free_gb=inference_free_gb,
        )
        if snapshot.free_gb < needed:
            short.append((snapshot, needed))
    if not short:
        details = ", ".join(
            f"GPU {item.device_index}: {item.free_gb:.1f} GB liberi "
            f"({item.used_gb:.1f}/{item.total_gb:.1f} GB in uso)"
            for item in snapshots
        )
        Log(
            INFO_LOG_LEVEL,
            "gpu vram ocr headroom ok",
            {"devices": details, "pool_size": pool, "per_instance_load_gb": per_instance_load_gb},
        )
        return
    details = ", ".join(
        f"GPU {item.device_index}: {item.free_gb:.1f} GB liberi "
        f"({item.used_gb:.1f}/{item.total_gb:.1f} GB in uso, servono {needed:.1f} GB)"
        for item, needed in short
    )
    message = (
        "VRAM GPU insufficiente per OCR: "
        f"{details}. "
        "Se il modello OCR è già caricato, bastano circa "
        f"{inference_free_gb:g} GB liberi; altrimenti servono circa "
        f"{per_instance_load_gb:g} GB per istanza (pool {pool}). "
        "Libera VRAM o sposta OCR/LLM su un'altra GPU."
    )
    _raise_gpu_vram_busy(
        message,
        log_key="gpu vram ocr headroom failed",
        short_devices=details,
        pool_size=pool,
    )


def ensure_gpu_vram_headroom_for_llm(
    snapshots: list[GpuVramSnapshot],
    *,
    load_free_gb: float,
    loaded_threshold_gb: float = _MODEL_LOADED_USED_THRESHOLD_GB,
    inference_free_gb: float = _INFERENCE_MIN_FREE_GB,
) -> None:
    for snapshot in snapshots:
        needed = _required_free_gb_for_snapshot(
            snapshot,
            load_free_gb=load_free_gb,
            loaded_threshold_gb=loaded_threshold_gb,
            inference_free_gb=inference_free_gb,
        )
        if snapshot.free_gb >= needed:
            details = (
                f"GPU {snapshot.device_index}: {snapshot.free_gb:.1f} GB liberi "
                f"({snapshot.used_gb:.1f}/{snapshot.total_gb:.1f} GB in uso)"
            )
            Log(
                INFO_LOG_LEVEL,
                "gpu vram llm headroom ok",
                {"devices": details, "required_free_gb": needed},
            )
            return
    details = ", ".join(
        f"GPU {item.device_index}: {item.free_gb:.1f} GB liberi "
        f"({item.used_gb:.1f}/{item.total_gb:.1f} GB in uso, "
        f"servono {_required_free_gb_for_snapshot(item, load_free_gb=load_free_gb, loaded_threshold_gb=loaded_threshold_gb, inference_free_gb=inference_free_gb):.1f} GB)"
        for item in snapshots
    )
    message = (
        "VRAM GPU insufficiente per Vision/Editor: nessuna scheda ha spazio libero "
        f"sufficiente ({details}). Se il modello è già caricato, libera almeno "
        f"{inference_free_gb:g} GB; altrimenti servono circa {load_free_gb:g} GB liberi."
    )
    _raise_gpu_vram_busy(
        message,
        log_key="gpu vram llm headroom failed",
        short_devices=details,
    )


def ensure_gpu_vram_available(
    *,
    max_used_gb: float,
    gpu_device: str = "all",
    enabled: bool = True,
) -> None:
    if not enabled or max_used_gb <= 0:
        return
    snapshots = _load_gpu_snapshots(gpu_device)
    ensure_gpu_vram_headroom_on_devices(
        snapshots,
        required_free_gb=max_used_gb,
        operation_label="OCR",
    )


GpuVramOperation = Literal["ocr", "llm"]

_REPAIR_ENTRY_NEEDS_OCR = frozenset({"stage1OCR"})
_REPAIR_ENTRY_NEEDS_LLM = frozenset({"stage1OCR", "stage2Vision", "stage3Editor"})


def _gpu_device_for_operation(settings: Settings, operation: GpuVramOperation) -> str:
    if operation == "ocr":
        return str(getattr(settings, "ocr_gpu_device", "all"))
    return "all"


def _operation_needs_vram_check(settings: Settings, operation: GpuVramOperation) -> bool:
    if not bool(getattr(settings, "gpu_vram_check_enabled", True)):
        return False
    try:
        max_used_gb = float(getattr(settings, "gpu_vram_max_used_gb", 4.0))
    except (TypeError, ValueError):
        return False
    if max_used_gb <= 0:
        return False
    if operation == "ocr":
        return bool(getattr(settings, "ocr_use_gpu", False))
    return getattr(settings, "openai_provider", "") == "local"


def require_gpu_vram(settings: Settings, operation: GpuVramOperation) -> None:
    if not _operation_needs_vram_check(settings, operation):
        return
    per_instance_gb = float(getattr(settings, "gpu_vram_max_used_gb", 4.0))
    if operation == "ocr":
        pool_size = max(1, int(getattr(settings, "max_parallel_request", 1)))
        snapshots = _load_gpu_snapshots(_gpu_device_for_operation(settings, operation))
        ensure_gpu_vram_headroom_for_ocr(
            snapshots,
            pool_size=pool_size,
            per_instance_load_gb=per_instance_gb,
        )
        return
    snapshots = _load_gpu_snapshots("all")
    ensure_gpu_vram_headroom_for_llm(snapshots, load_free_gb=_LLM_LOAD_MIN_FREE_GB)


def require_gpu_vram_at_pipeline_start(
    settings: Settings,
    *,
    skip_vision_editor: bool,
    single_page: bool = False,
    entry_stage: str | None = None,
) -> None:
    if not bool(getattr(settings, "gpu_vram_check_enabled", True)):
        return
    per_instance_gb = float(getattr(settings, "gpu_vram_max_used_gb", 4.0))
    if per_instance_gb <= 0:
        return
    if entry_stage is None:
        needs_ocr = bool(getattr(settings, "ocr_use_gpu", False))
        needs_llm = not skip_vision_editor and getattr(settings, "openai_provider", "") == "local"
    else:
        needs_ocr = bool(getattr(settings, "ocr_use_gpu", False)) and entry_stage in _REPAIR_ENTRY_NEEDS_OCR
        needs_llm = (
            not skip_vision_editor
            and getattr(settings, "openai_provider", "") == "local"
            and entry_stage in _REPAIR_ENTRY_NEEDS_LLM
        )
    if not needs_ocr and not needs_llm:
        return
    ocr_pool = 1 if single_page else max(1, int(getattr(settings, "max_parallel_request", 1)))
    if needs_ocr:
        ocr_device = str(getattr(settings, "ocr_gpu_device", "all"))
        ocr_snapshots = _load_gpu_snapshots(ocr_device)
        ensure_gpu_vram_headroom_for_ocr(
            ocr_snapshots,
            pool_size=ocr_pool,
            per_instance_load_gb=per_instance_gb,
        )
    if needs_llm:
        llm_snapshots = _load_gpu_snapshots("all")
        ensure_gpu_vram_headroom_for_llm(llm_snapshots, load_free_gb=_LLM_LOAD_MIN_FREE_GB)

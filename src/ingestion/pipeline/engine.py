from __future__ import annotations

import gc
import queue
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.models.request import IngestInputErrorCode, IngestInputValidationError, IngestInputValidationException
from src.models.settings import Settings

_BYTES_PER_GB = 1024**3
_MIB = 1024 * 1024


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


def ensure_gpu_vram_available(
    *,
    max_used_gb: float,
    gpu_device: str = "all",
    enabled: bool = True,
) -> None:
    if not enabled or max_used_gb <= 0:
        return

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

    limit_bytes = int(max_used_gb * _BYTES_PER_GB)
    busy: list[GpuVramSnapshot] = []
    for snapshot in snapshots:
        if snapshot.used_bytes > limit_bytes:
            busy.append(snapshot)
    if not busy:
        details = ", ".join(
            f"GPU {item.device_index}: {item.used_gb:.1f}/{item.total_gb:.1f} GB in uso"
            for item in snapshots
        )
        Log(INFO_LOG_LEVEL, "gpu vram preflight ok", {"devices": details, "limit_gb": max_used_gb})
        return

    details = ", ".join(
        f"GPU {item.device_index}: {item.used_gb:.1f}/{item.total_gb:.1f} GB in uso"
        for item in busy
    )
    message = (
        f"VRAM GPU insufficiente: superato il limite di {max_used_gb:g} GB per scheda ({details}). "
        "Chiudi gli altri processi che usano la GPU (es. LM Studio) e libera VRAM prima di continuare."
    )
    Log(ERROR_LOG_LEVEL, "gpu vram preflight failed", {"busy_devices": details, "limit_gb": max_used_gb})
    raise IngestInputValidationException(
        IngestInputValidationError(
            code=IngestInputErrorCode.GPU_VRAM_BUSY,
            message=message,
        )
    )


GpuVramOperation = Literal["ocr", "llm"]


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
    ensure_gpu_vram_available(
        max_used_gb=float(getattr(settings, "gpu_vram_max_used_gb", 4.0)),
        gpu_device=_gpu_device_for_operation(settings, operation),
        enabled=True,
    )


def require_gpu_vram_at_pipeline_start(settings: Settings, *, skip_vision_editor: bool) -> None:
    needs_ocr = bool(getattr(settings, "ocr_use_gpu", False))
    needs_llm = not skip_vision_editor and getattr(settings, "openai_provider", "") == "local"
    if not needs_ocr and not needs_llm:
        return
    gpu_device = "all"
    if needs_ocr and not needs_llm:
        gpu_device = str(getattr(settings, "ocr_gpu_device", "all"))
    ensure_gpu_vram_available(
        max_used_gb=float(getattr(settings, "gpu_vram_max_used_gb", 4.0)),
        gpu_device=gpu_device,
        enabled=bool(getattr(settings, "gpu_vram_check_enabled", True)),
    )


@runtime_checkable
class OCRPageEngine(Protocol):
    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str: ...


def _gpu_enabled(gpu_arg: bool | str) -> bool:
    return gpu_arg is not False


def _move_module_to_cpu(module: object) -> None:
    target = getattr(module, "module", module)
    to_fn = getattr(target, "to", None)
    if callable(to_fn):
        to_fn("cpu")


def _dispose_easyocr_reader(reader: object, *, used_gpu: bool) -> None:
    if used_gpu:
        for attr in ("detector", "recognizer"):
            net = getattr(reader, attr, None)
            if net is None:
                continue
            try:
                _move_module_to_cpu(net)
            except Exception:
                pass
            try:
                delattr(reader, attr)
            except Exception:
                pass
    try:
        del reader
    except Exception:
        pass


def _release_cuda_memory() -> None:
    gc.collect()
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            ipc_collect = getattr(torch.cuda, "ipc_collect", None)
            if callable(ipc_collect):
                ipc_collect()
    except ImportError:
        pass
    gc.collect()


class EasyOCRPageEngine:
    def __init__(self, *, gpu: bool = False, gpu_device: str = "all") -> None:
        if not gpu:
            self._gpu_arg: bool | str = False
        elif gpu_device == "all":
            self._gpu_arg = True
        else:
            self._gpu_arg = f"cuda:{gpu_device}"
        self._pool: queue.Queue[object] | None = None
        self._pool_lang_key: tuple[str, ...] | None = None
        self._pool_readers: list[object] = []

    def prepare_parallel_pool(self, lang: list[str], *, pool_size: int) -> None:
        lang_key = tuple(lang)
        size = max(1, pool_size)
        if self._pool is not None and self._pool_lang_key == lang_key and len(self._pool_readers) == size:
            return
        self.release_parallel_pool()
        Log(
            INFO_LOG_LEVEL,
            "EasyOCR prepare parallel reader pool",
            {"lang": list(lang), "gpu": self._gpu_arg, "pool_size": size},
        )
        import easyocr  # noqa: PLC0415

        self._pool = queue.Queue()
        self._pool_lang_key = lang_key
        for index in range(size):
            Log(
                INFO_LOG_LEVEL,
                "EasyOCR instantiate Reader for pool",
                {"index": index + 1, "pool_size": size, "lang": list(lang), "gpu": self._gpu_arg},
            )
            reader = easyocr.Reader(list(lang), gpu=self._gpu_arg)
            self._pool_readers.append(reader)
            self._pool.put(reader)

    def release_parallel_pool(self) -> None:
        if not self._pool_readers and self._pool is None:
            return
        count = len(self._pool_readers)
        used_gpu = _gpu_enabled(self._gpu_arg)
        readers: list[object] = list(self._pool_readers)
        if self._pool is not None:
            while True:
                try:
                    readers.append(self._pool.get_nowait())
                except queue.Empty:
                    break
        unique: list[object] = []
        seen: set[int] = set()
        for reader in readers:
            rid = id(reader)
            if rid in seen:
                continue
            seen.add(rid)
            unique.append(reader)
        Log(INFO_LOG_LEVEL, "EasyOCR release parallel reader pool", {"count": count})
        for reader in unique:
            _dispose_easyocr_reader(reader, used_gpu=used_gpu)
        self._pool_readers = []
        self._pool = None
        self._pool_lang_key = None
        _release_cuda_memory()

    @classmethod
    def release_cached_readers(cls) -> None:
        _release_cuda_memory()

    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        if self._pool is None:
            raise RuntimeError(
                "EasyOCR reader pool not prepared; call prepare_parallel_pool() before ocr_page()"
            )
        Log(
            INFO_LOG_LEVEL,
            "EasyOCR readtext begin",
            {"path": str(image_path), "lang": lang},
        )
        reader = self._pool.get()
        try:
            results = reader.readtext(str(image_path), detail=0)
        finally:
            self._pool.put(reader)
        out = "\n".join(results)
        Log(
            INFO_LOG_LEVEL,
            "EasyOCR readtext done",
            {"path": str(image_path), "line_count": len(results)},
        )
        return out

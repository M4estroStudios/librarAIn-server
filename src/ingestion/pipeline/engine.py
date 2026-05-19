from __future__ import annotations

import gc
import queue
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.log import INFO_LOG_LEVEL, Log


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

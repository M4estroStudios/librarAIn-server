from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.log import INFO_LOG_LEVEL, Log


@runtime_checkable
class OCRPageEngine(Protocol):
    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str: ...


class EasyOCRPageEngine:
    _readers: dict[tuple[tuple[str, ...], bool | str], object] = {}

    def __init__(self, *, gpu: bool = False, gpu_device: str = "all") -> None:
        if not gpu:
            self._gpu_arg: bool | str = False
        elif gpu_device == "all":
            self._gpu_arg = True
        else:
            self._gpu_arg = f"cuda:{gpu_device}"

    def _get_reader(self, lang: list[str]) -> object:
        key = (tuple(lang), self._gpu_arg)
        if key not in self._readers:
            Log(
                INFO_LOG_LEVEL,
                "EasyOCR instantiate Reader",
                {"lang": list(lang), "gpu": self._gpu_arg},
            )
            import easyocr  # noqa: PLC0415

            self._readers[key] = easyocr.Reader(list(lang), gpu=self._gpu_arg)
        else:
            Log(
                INFO_LOG_LEVEL,
                "EasyOCR reuse cached Reader",
                {"lang": list(lang), "gpu": self._gpu_arg},
            )
        return self._readers[key]

    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        Log(
            INFO_LOG_LEVEL,
            "EasyOCR readtext begin",
            {"path": str(image_path), "lang": lang},
        )
        reader = self._get_reader(lang)
        results = reader.readtext(str(image_path), detail=0)
        out = "\n".join(results)
        Log(
            INFO_LOG_LEVEL,
            "EasyOCR readtext done",
            {"path": str(image_path), "line_count": len(results)},
        )
        return out

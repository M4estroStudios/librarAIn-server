from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class OCRPageEngine(Protocol):
    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str: ...


class EasyOCRPageEngine:
    _readers: dict[tuple[tuple[str, ...], bool], object] = {}

    def __init__(self, *, gpu: bool = False) -> None:
        self._gpu = gpu

    def _get_reader(self, lang: list[str]) -> object:
        key = (tuple(lang), self._gpu)
        if key not in self._readers:
            import easyocr  # noqa: PLC0415

            self._readers[key] = easyocr.Reader(list(lang), gpu=self._gpu)
        return self._readers[key]

    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        reader = self._get_reader(lang)
        results = reader.readtext(str(image_path), detail=0)
        return "\n".join(results)

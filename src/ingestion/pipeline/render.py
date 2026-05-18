from __future__ import annotations

import binascii
import json
import struct
import zlib
from pathlib import Path
from typing import Any

from src.core.hashing import compute_file_sha256
from src.core.log import INFO_LOG_LEVEL, Log

PNG_RENDERER_MARKER_VERSION = 1


def _sidecar_path(png_path: Path) -> Path:
    return png_path.with_suffix(png_path.suffix + ".json")


def _expected_marker(
    pdf_path: Path, source_sha256: str, page_index_zero: int, dpi: int
) -> dict[str, Any]:
    return {
        "version": PNG_RENDERER_MARKER_VERSION,
        "renderer": "pypdfium2",
        "source_pdf_path": str(pdf_path.resolve()),
        "source_sha256": source_sha256,
        "page_index_zero": page_index_zero,
        "dpi": dpi,
    }


def _marker_matches(sidecar_path: Path, expected: dict[str, Any]) -> bool:
    try:
        observed = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(observed.get(key) == value for key, value in expected.items())


def _write_marker(sidecar_path: Path, marker: dict[str, Any]) -> None:
    sidecar_path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _bitmap_row_to_rgb(row: bytes, width: int, mode: str) -> bytes:
    if mode == "RGB":
        return row[: width * 3]
    if mode == "BGR":
        return b"".join(
            row[offset + 2 : offset + 3]
            + row[offset + 1 : offset + 2]
            + row[offset : offset + 1]
            for offset in range(0, width * 3, 3)
        )
    if mode == "RGBA":
        return b"".join(row[offset : offset + 3] for offset in range(0, width * 4, 4))
    if mode == "BGRA":
        return b"".join(
            row[offset + 2 : offset + 3]
            + row[offset + 1 : offset + 2]
            + row[offset : offset + 1]
            for offset in range(0, width * 4, 4)
        )
    raise ValueError(f"unsupported pypdfium2 bitmap mode: {mode}")


def _write_bitmap_png(bitmap: Any, target_path: Path) -> None:
    width = int(bitmap.width)
    height = int(bitmap.height)
    stride = int(bitmap.stride)
    mode = str(bitmap.mode)
    raw = bytes(bitmap.buffer)
    rows = []
    for y in range(height):
        row_start = y * stride
        row = raw[row_start : row_start + stride]
        rows.append(b"\x00" + _bitmap_row_to_rgb(row, width, mode))
    payload = b"".join(rows)

    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            ),
            _png_chunk(b"IDAT", zlib.compress(payload)),
            _png_chunk(b"IEND", b""),
        ]
    )
    target_path.write_bytes(png)


def _load_pdfium() -> Any:
    import pypdfium2 as pdfium  # noqa: PLC0415

    return pdfium


def _render_pdf_page_to_png(
    pdf_path: Path,
    page_index_zero: int,
    target_path: Path,
    *,
    dpi: int,
    source_sha256: str,
) -> Path:
    if page_index_zero < 0:
        raise ValueError("page_index_zero must be >= 0")
    if dpi < 1:
        raise ValueError("dpi must be >= 1")

    marker = _expected_marker(pdf_path, source_sha256, page_index_zero, dpi)
    sidecar_path = _sidecar_path(target_path)
    if target_path.is_file() and _marker_matches(sidecar_path, marker):
        Log(
            INFO_LOG_LEVEL,
            "pdf render PNG cache hit",
            {
                "page_index_zero": page_index_zero,
                "path": str(target_path),
            },
        )
        return target_path

    Log(
        INFO_LOG_LEVEL,
        "pdf render PNG rasterize begin",
        {
            "page_index_zero": page_index_zero,
            "dpi": dpi,
            "path": str(target_path),
        },
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pdfium = _load_pdfium()
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = None
    bitmap = None
    try:
        if page_index_zero >= len(pdf):
            raise ValueError(
                f"page_index_zero {page_index_zero} exceeds pdf page count {len(pdf)}"
            )
        page = pdf[page_index_zero]
        bitmap = page.render(scale=dpi / 72)
        _write_bitmap_png(bitmap, target_path)
        _write_marker(sidecar_path, marker)
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        pdf.close()
    Log(
        INFO_LOG_LEVEL,
        "pdf render PNG rasterize done",
        {"page_index_zero": page_index_zero, "path": str(target_path)},
    )
    return target_path


def render_pdf_page_to_png(
    pdf_path: Path, page_index_zero: int, target_path: Path, *, dpi: int = 200
) -> Path:
    pdf_path = Path(pdf_path)
    target_path = Path(target_path)
    Log(
        INFO_LOG_LEVEL,
        "render_pdf_page_to_png compute digest begin",
        {"pdf": str(pdf_path)},
    )
    source_sha256 = compute_file_sha256(pdf_path)
    Log(
        INFO_LOG_LEVEL,
        "render_pdf_page_to_png compute digest done",
        {"sha256_prefix": source_sha256[:16]},
    )
    return _render_pdf_page_to_png(
        pdf_path,
        page_index_zero,
        target_path,
        dpi=dpi,
        source_sha256=source_sha256,
    )


def render_aligned_pdf_pages(
    aligned_pdf_path: Path, target_dir: Path, dpi: int
) -> list[tuple[int, Path]]:
    aligned_pdf_path = Path(aligned_pdf_path)
    target_dir = Path(target_dir)
    if dpi < 1:
        raise ValueError("dpi must be >= 1")

    Log(
        INFO_LOG_LEVEL,
        "render_aligned_pdf_pages compute digest begin",
        {"pdf": str(aligned_pdf_path)},
    )
    source_sha256 = compute_file_sha256(aligned_pdf_path)
    Log(
        INFO_LOG_LEVEL,
        "render_aligned_pdf_pages compute digest done",
        {"sha256_prefix": source_sha256[:16]},
    )
    render_dir = target_dir / source_sha256 / "render"
    pdfium = _load_pdfium()
    pdf = pdfium.PdfDocument(str(aligned_pdf_path))
    try:
        page_count = len(pdf)
    finally:
        pdf.close()

    rendered: list[tuple[int, Path]] = []
    for page_index_zero in range(page_count):
        aligned_page_1based = page_index_zero + 1
        Log(
            INFO_LOG_LEVEL,
            "render aligned PDF batch page begin",
            {"aligned_page_1based": aligned_page_1based, "page_count": page_count},
        )
        png_path = render_dir / f"p.{aligned_page_1based:04d}.png"
        rendered_path = _render_pdf_page_to_png(
            aligned_pdf_path,
            page_index_zero,
            png_path,
            dpi=dpi,
            source_sha256=source_sha256,
        )
        rendered.append((aligned_page_1based, rendered_path))
        Log(
            INFO_LOG_LEVEL,
            "render aligned PDF batch page done",
            {"aligned_page_1based": aligned_page_1based, "path": str(rendered_path)},
        )
    return rendered

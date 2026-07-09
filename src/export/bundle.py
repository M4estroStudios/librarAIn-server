"""Package finalized :class:`EtalyArticle` objects into an E-TALY import ``.zip``.

The bundle produced here is the deliverable a reviewer imports into the E-TALY Flutter
app. It carries, for one or more articles:

* the article Markdown under ``text/ITA/{poh_id}.md``;
* every *cited* source page rendered to a small WebP under ``sources/<sha>/p<page>.webp``
  (deduplicated across all articles);
* optional covers under ``covers/{poh_id}.webp``;
* CSV patch rows (``patch/poh_{p,o,m}.csv``) for the poh that are **new** relative to
  the E-TALY catalog (decision D-16/D-22);
* a ``patch/registry.json`` snapshot of the registry entries used;
* a top-level ``MANIFEST.json`` describing every poh, the source books and the
  ``new`` vs ``overwrite`` action per poh.

Page rendering reuses the ingestion conventions (see
:mod:`src.ingestion.pipeline.render`): the aligned/normalized PDF lives at
``data/input/processed/<sha>.pdf`` and *aligned* pages are 1-based within it, so the
zero-based PDF page index for an aligned page ``N`` is ``N - 1``. Pre-rendered PNGs (if
present at ``data/tmp/<sha>/render/p.NNNN.png``) are reused instead of re-rasterizing.

The bundler never crashes on a missing/unrenderable page: such pages are collected in
:attr:`BundleResult.missing_pages` (this matters for the downstream lint slice).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.export.etaly_adapter import EtalyArticle
from src.export.registry import EtalyRegistry, PohType, default_etaly_assets_path
from src.export.time_range import parse_time_range

DEFAULT_RENDER_SCALE = 2.0
DEFAULT_MAX_LONG_EDGE = 1240
DEFAULT_WEBP_QUALITY = 80

_CSV_COLUMNS = ("name", "id_code", "beginning", "end", "shelf")

_pdfium_lock = threading.Lock()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_data_root() -> Path:
    """Repo-local data directory holding ``output/``, ``input/processed/`` and ``tmp/``."""
    return _repo_root() / "data"


@dataclass
class BundleItem:
    """One article plus the human-approved metadata needed to package it.

    ``poh_type`` selects the CSV patch file, ``time_range`` (a polyindex string, may be
    ``None``) feeds the CSV ``beginning``/``end`` columns and ``cover_path`` is an optional
    image rendered to ``covers/{poh_id}.webp``.
    """

    article: EtalyArticle
    poh_type: PohType
    name: str | None = None
    time_range: str | None = None
    cover_path: Path | None = None


@dataclass
class BundleResult:
    """Outcome of :func:`build_bundle`."""

    zip_path: Path
    poh_count: int
    rendered_pages: int
    missing_pages: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_pdfium() -> Any:
    import pypdfium2 as pdfium  # noqa: PLC0415

    return pdfium


def _load_pil_image() -> Any:
    from PIL import Image  # noqa: PLC0415

    return Image


@dataclass
class _BookManifest:
    sha: str
    title: str | None
    aligned_pages: set[int]


def _read_manifest(data_root: Path, sha: str) -> _BookManifest | None:
    manifest_path = data_root / "output" / sha / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    aligned_pages: set[int] = set()
    for page in raw.get("pages", []) or []:
        if isinstance(page, dict) and isinstance(page.get("aligned"), int):
            aligned_pages.add(int(page["aligned"]))
    reicat = raw.get("reicat")
    title = reicat.get("titolo") if isinstance(reicat, dict) else None
    return _BookManifest(sha=sha, title=title, aligned_pages=aligned_pages)


def _prerendered_png_path(data_root: Path, sha: str, aligned_page: int) -> Path:
    return data_root / "tmp" / sha / "render" / f"p.{aligned_page:04d}.png"


def _processed_pdf_path(data_root: Path, sha: str) -> Path:
    return data_root / "input" / "processed" / f"{sha}.pdf"


def _to_webp_bytes(image: Any, *, max_long_edge: int, webp_quality: int) -> bytes:
    rgb = image.convert("RGB")
    if max_long_edge > 0 and max(rgb.size) > max_long_edge:
        rgb.thumbnail((max_long_edge, max_long_edge))
    buffer = io.BytesIO()
    rgb.save(buffer, format="WEBP", quality=webp_quality, method=6)
    return buffer.getvalue()


def _render_pdf_page_image(pdf_path: Path, page_index_zero: int, render_scale: float) -> Any:
    pdfium = _load_pdfium()
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(str(pdf_path))
        page = None
        bitmap = None
        try:
            if page_index_zero < 0 or page_index_zero >= len(pdf):
                raise ValueError(
                    f"page_index_zero {page_index_zero} exceeds pdf page count {len(pdf)}"
                )
            page = pdf[page_index_zero]
            bitmap = page.render(scale=render_scale)
            return bitmap.to_pil()
        finally:
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            pdf.close()


def _render_cited_page_webp(
    data_root: Path,
    manifest: _BookManifest | None,
    sha: str,
    aligned_page: int,
    *,
    render_scale: float,
    max_long_edge: int,
    webp_quality: int,
) -> tuple[bytes | None, str | None]:
    """Return ``(webp_bytes, error)``; exactly one of the two is non-``None``."""
    image_module = _load_pil_image()

    png_path = _prerendered_png_path(data_root, sha, aligned_page)
    if png_path.is_file():
        try:
            with image_module.open(png_path) as image:
                return _to_webp_bytes(
                    image, max_long_edge=max_long_edge, webp_quality=webp_quality
                ), None
        except OSError as exc:
            return None, f"prerendered PNG unreadable ({png_path}): {exc}"

    if manifest is None:
        return None, f"manifest not found for source {sha}"
    if manifest.aligned_pages and aligned_page not in manifest.aligned_pages:
        return None, f"aligned page {aligned_page} not listed in manifest for {sha}"

    pdf_path = _processed_pdf_path(data_root, sha)
    if not pdf_path.is_file():
        return None, f"processed PDF not found for source {sha}"

    try:
        image = _render_pdf_page_image(pdf_path, aligned_page - 1, render_scale)
    except Exception as exc:  # noqa: BLE001 - never crash the whole bundle on one page
        return None, f"failed to render {sha} aligned page {aligned_page}: {exc}"
    return _to_webp_bytes(image, max_long_edge=max_long_edge, webp_quality=webp_quality), None


def _cover_webp_bytes(
    cover_path: Path, *, max_long_edge: int, webp_quality: int
) -> tuple[bytes | None, str | None]:
    image_module = _load_pil_image()
    try:
        with image_module.open(cover_path) as image:
            return _to_webp_bytes(
                image, max_long_edge=max_long_edge, webp_quality=webp_quality
            ), None
    except OSError as exc:
        return None, f"cover unreadable ({cover_path}): {exc}"


def _etaly_asset_exists(etaly_assets_path: Path, poh_id: str) -> bool:
    asset_path = etaly_assets_path / "timeline" / "data" / "text" / "ITA" / f"{poh_id}.md"
    return asset_path.is_file()


def _csv_rows_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(_CSV_COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _registry_snapshot_bytes(
    registry: EtalyRegistry | None, poh_ids: set[str]
) -> bytes:
    entries: dict[str, Any] = {}
    if registry is not None:
        for slug, entry in registry.document.entries.items():
            if entry.poh_id in poh_ids:
                entries[slug] = entry.model_dump(mode="json")
    payload = {"schema_version": "1.0", "entries": entries}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def build_bundle(
    items: list[BundleItem],
    output_zip: Path,
    *,
    etaly_assets_path: str | Path | None = None,
    data_root: str | Path | None = None,
    registry: EtalyRegistry | None = None,
    render_scale: float = DEFAULT_RENDER_SCALE,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    webp_quality: int = DEFAULT_WEBP_QUALITY,
) -> BundleResult:
    """Package ``items`` into an E-TALY import ``.zip`` at ``output_zip``.

    Cited pages are rendered to WebP once each (deduplicated across articles), CSV patch
    rows are emitted only for poh that are *new* relative to the E-TALY assets tree, and a
    ``MANIFEST.json`` records the ``new``/``overwrite`` action per poh.
    """
    output_zip = Path(output_zip)
    data_root_path = Path(data_root) if data_root else default_data_root()
    assets_path = Path(etaly_assets_path) if etaly_assets_path else default_etaly_assets_path()

    warnings: list[str] = []
    missing_pages: list[tuple[str, int]] = []
    poh_ids = {item.article.poh_id for item in items}

    manifests: dict[str, _BookManifest | None] = {}
    books: dict[str, dict[str, Any]] = {}

    def _manifest_for(sha: str) -> _BookManifest | None:
        if sha not in manifests:
            manifest = _read_manifest(data_root_path, sha)
            manifests[sha] = manifest
            books[sha] = {"title": manifest.title if manifest is not None else None}
        return manifests[sha]

    unique_pages: set[tuple[str, int]] = set()
    for item in items:
        for sha, page in item.article.cited_pages:
            unique_pages.add((sha.lower(), int(page)))
            _manifest_for(sha.lower())

    rendered_webp: dict[tuple[str, int], bytes] = {}
    for sha, page in sorted(unique_pages):
        webp_bytes, error = _render_cited_page_webp(
            data_root_path,
            _manifest_for(sha),
            sha,
            page,
            render_scale=render_scale,
            max_long_edge=max_long_edge,
            webp_quality=webp_quality,
        )
        if webp_bytes is None:
            missing_pages.append((sha, page))
            if error:
                warnings.append(error)
            continue
        rendered_webp[(sha, page)] = webp_bytes

    manifest_poh: list[dict[str, Any]] = []
    csv_rows: dict[PohType, list[dict[str, str]]] = {"p": [], "o": [], "m": []}
    covers: dict[str, bytes] = {}

    for item in items:
        poh_id = item.article.poh_id
        name = (item.name or "").strip() or poh_id
        md_bytes = item.article.markdown.encode("utf-8")
        action = "overwrite" if _etaly_asset_exists(assets_path, poh_id) else "new"

        sources = sorted(
            {(sha.lower(), int(page)) for sha, page in item.article.cited_pages}
        )
        manifest_poh.append(
            {
                "poh_id": poh_id,
                "name": name,
                "action": action,
                "md_sha256": _sha256_hex(md_bytes),
                "sources": [{"sha": sha, "page": page} for sha, page in sources],
            }
        )

        if action == "new":
            beginning, end = parse_time_range(item.time_range)
            csv_rows[item.poh_type].append(
                {
                    "name": name,
                    "id_code": poh_id,
                    "beginning": beginning,
                    "end": end,
                    "shelf": "",
                }
            )

        if item.cover_path is not None:
            cover_bytes, cover_error = _cover_webp_bytes(
                Path(item.cover_path),
                max_long_edge=max_long_edge,
                webp_quality=webp_quality,
            )
            if cover_bytes is None:
                if cover_error:
                    warnings.append(cover_error)
            else:
                covers[poh_id] = cover_bytes

        warnings.extend(item.article.warnings)

    manifest_payload = {
        "generated_at": _utc_now_iso(),
        "books": books,
        "poh": manifest_poh,
    }

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            archive.writestr(
                f"text/ITA/{item.article.poh_id}.md", item.article.markdown
            )
        for (sha, page), webp_bytes in sorted(rendered_webp.items()):
            archive.writestr(f"sources/{sha}/p{page}.webp", webp_bytes)
        for poh_id, cover_bytes in sorted(covers.items()):
            archive.writestr(f"covers/{poh_id}.webp", cover_bytes)
        for poh_type in ("p", "o", "m"):
            rows = csv_rows[poh_type]
            if rows:
                archive.writestr(f"patch/poh_{poh_type}.csv", _csv_rows_bytes(rows))
        archive.writestr(
            "patch/registry.json", _registry_snapshot_bytes(registry, poh_ids)
        )
        archive.writestr(
            "MANIFEST.json",
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
        )

    Log(
        INFO_LOG_LEVEL if not missing_pages else WARNING_LOG_LEVEL,
        "etaly bundle built",
        {
            "zip_path": str(output_zip),
            "poh_count": len(items),
            "rendered_pages": len(rendered_webp),
            "missing_pages": len(missing_pages),
        },
    )

    return BundleResult(
        zip_path=output_zip,
        poh_count=len(items),
        rendered_pages=len(rendered_webp),
        missing_pages=missing_pages,
        warnings=warnings,
    )

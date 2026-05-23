from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.hashing import compute_file_sha256
from src.core.log import INFO_LOG_LEVEL, Log
from src.ingestion.pipeline.stage1 import _slugify
from src.ingestion.pipeline.stage3 import Stage3Result
from src.models.request import EnrichedIngestRequest, UsefulPagesEnumeration
from src.models.settings import Settings


@dataclass
class BookPageOutput:
    aligned: int
    original: int
    file: Path


@dataclass
class BookOutput:
    output_dir: Path
    manifest_path: Path
    slug: str
    pages: list[BookPageOutput]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page_filename(aligned_page: int, slug: str) -> str:
    return f"p.{aligned_page:04d}.{slug}.md"


def _atomic_copy_or_skip(source: Path, dest: Path) -> bool:
    if not source.is_file():
        raise FileNotFoundError(f"stage3 md not found: {source}")
    if dest.is_file():
        if compute_file_sha256(dest) == compute_file_sha256(source):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_bytes()
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)
    return True


def _atomic_write_bytes(dest: Path, content: bytes) -> bool:
    if dest.is_file() and dest.read_bytes() == content:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.is_file():
            tmp_path.unlink(missing_ok=True)
    return True


def _manifest_core(data: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if key != "generated_at"}


def materialize_book_pages(
    stage3_result: Stage3Result,
    enriched: EnrichedIngestRequest,
    source_sha256: str,
    useful_pages: UsefulPagesEnumeration,
    settings: Settings,
    *,
    request_id: str = "",
) -> BookOutput:
    slug = _slugify(enriched.request.reicat.title)
    output_dir = Path(settings.data_root) / "output" / source_sha256
    pages_dir = output_dir / "pages"
    manifest_path = output_dir / "manifest.json"

    sorted_pages = sorted(stage3_result.pages, key=lambda page: page.aligned_page)
    book_pages: list[BookPageOutput] = []
    manifest_page_entries: list[dict[str, object]] = []
    pages_written = False

    for page in sorted_pages:
        source = Path(page.md_path)
        filename = _page_filename(page.aligned_page, slug)
        rel_path = f"pages/{filename}"
        dest = pages_dir / filename
        if _atomic_copy_or_skip(source, dest):
            pages_written = True
        book_pages.append(
            BookPageOutput(
                aligned=page.aligned_page,
                original=page.original_page,
                file=dest,
            )
        )
        manifest_page_entries.append(
            {
                "aligned": page.aligned_page,
                "original": page.original_page,
                "file": rel_path,
            }
        )

    manifest_data: dict[str, object] = {
        "source_sha256": source_sha256,
        "slug": slug,
        "original_page_count": useful_pages.original_page_count,
        "aligned_page_count": useful_pages.aligned_page_count,
        "pages": manifest_page_entries,
        "reicat": enriched.request.reicat.model_dump(by_alias=True),
        "pipeline_version": enriched.request.schema_version,
        "generated_at": _utc_now_iso(),
    }

    should_write_manifest = True
    if manifest_path.is_file() and not pages_written:
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if _manifest_core(existing) == _manifest_core(manifest_data):
                should_write_manifest = False
        except (json.JSONDecodeError, OSError):
            pass

    if should_write_manifest:
        manifest_bytes = json.dumps(manifest_data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(manifest_path, manifest_bytes)

    Log(
        INFO_LOG_LEVEL,
        "output_writer materialize_book_pages done",
        {
            "request_id": request_id,
            "source_sha256": source_sha256[:16],
            "output_dir": str(output_dir),
            "page_count": len(book_pages),
        },
    )

    return BookOutput(
        output_dir=output_dir,
        manifest_path=manifest_path,
        slug=slug,
        pages=book_pages,
    )

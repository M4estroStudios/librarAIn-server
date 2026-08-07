from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from src.api.prompts_http import snapshot_book_prompts
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.core.text import slugify as _slugify
from src.ingestion.markdown_artifacts import clean_markdown_channel_artifacts
from src.ingestion.pipeline.stage3 import Stage3Result
from src.models.polyindex_index import BookIndexDocument
from src.models.polyindex_toc import PolyindexTocChapter
from src.models.request import EnrichedIngestRequest, PageRange, UsefulPagesEnumeration
from src.models.settings import Settings

_FRONTMATTER_FENCE = "---"
_CHAPTER_NUM_FROM_LABEL = re.compile(
    r"(?is)^Cap(?:itolo|\.)\s*([IVXLCDM]+|\d+)\s*(?:[—\-]\s*(.+))?$"
)


def _range_dump(page_range: PageRange | None) -> dict[str, int] | None:
    if page_range is None:
        return None
    return page_range.model_dump()


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
    raw_text = source.read_text(encoding="utf-8")
    cleaned_text = clean_markdown_channel_artifacts(raw_text)
    if not cleaned_text.endswith("\n"):
        cleaned_text += "\n"
    content = cleaned_text.encode("utf-8")
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


def split_page_frontmatter(text: str) -> tuple[str | None, str]:
    if not text.startswith(_FRONTMATTER_FENCE):
        return None, text
    parts = text.split(_FRONTMATTER_FENCE, 2)
    if len(parts) < 3:
        return None, text
    body = parts[2].lstrip("\n")
    return parts[1], body


def strip_page_frontmatter(text: str) -> str:
    _frontmatter, body = split_page_frontmatter(text)
    return body


def _yaml_scalar(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _chapter_number_and_name(label: str, position: int) -> tuple[str, str]:
    stripped = label.strip()
    match = _CHAPTER_NUM_FROM_LABEL.match(stripped)
    if match:
        number = match.group(1)
        title = (match.group(2) or "").strip()
        return number, title or stripped
    return str(position), stripped


def _find_chapter_for_page(
    chapters: list[PolyindexTocChapter],
    aligned_page: int,
) -> tuple[int, PolyindexTocChapter] | None:
    for position, chapter in enumerate(chapters, start=1):
        if chapter.aligned_page_start <= aligned_page <= chapter.aligned_page_end:
            return position, chapter
    return None


def _subject_linked_in_body(body: str, key: str) -> bool:
    return f"INDEX.md#{key}" in body or f'<a id="{key}"></a>' in body


def _append_connection_entries(lines: list[str], entries: list[str]) -> None:
    if not entries:
        lines.append("    []")
        return
    for entry in entries:
        lines.append(f"    - {_yaml_scalar(entry)}")


def build_page_frontmatter(
    *,
    aligned_page: int,
    original_page: int,
    chapter_number: str | None,
    chapter_name: str | None,
    index_success: list[str],
    index_failed: list[str],
) -> str:
    lines = [
        _FRONTMATTER_FENCE,
        f"aligned_page: {aligned_page}",
        f"original_page: {original_page}",
    ]
    if chapter_number is not None and chapter_name is not None:
        lines.append(f"chapter_number: {_yaml_scalar(chapter_number)}")
        lines.append(f"chapter_name: {_yaml_scalar(chapter_name)}")
    lines.append("index_connections:")
    lines.append("  success:")
    _append_connection_entries(lines, index_success)
    lines.append("  failed:")
    _append_connection_entries(lines, index_failed)
    lines.append(_FRONTMATTER_FENCE)
    return "\n".join(lines) + "\n"


def load_book_index_document(path: Path) -> BookIndexDocument:
    if not path.is_file():
        return BookIndexDocument()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return BookIndexDocument()
    try:
        return BookIndexDocument.model_validate(raw)
    except ValidationError:
        return BookIndexDocument()


def stamp_page_metadata(
    book_output: BookOutput,
    chapters: list[PolyindexTocChapter],
    index_document: BookIndexDocument,
    *,
    request_id: str = "",
) -> dict[str, int]:
    pages_updated = 0
    for page in book_output.pages:
        if not page.file.is_file():
            continue
        original_text = page.file.read_text(encoding="utf-8")
        body = strip_page_frontmatter(original_text)
        chapter_number: str | None = None
        chapter_name: str | None = None
        found = _find_chapter_for_page(chapters, page.aligned)
        if found is not None:
            position, chapter = found
            chapter_number, chapter_name = _chapter_number_and_name(chapter.label, position)
        subject_keys = index_document.page_subjects.get(str(page.aligned), [])
        index_success: list[str] = []
        index_failed: list[str] = []
        for key in subject_keys:
            entry = index_document.subjects.get(key)
            label = entry.canonical_label if entry is not None else key
            if _subject_linked_in_body(body, key):
                index_success.append(label)
            else:
                index_failed.append(label)
        frontmatter = build_page_frontmatter(
            aligned_page=page.aligned,
            original_page=page.original,
            chapter_number=chapter_number,
            chapter_name=chapter_name,
            index_success=index_success,
            index_failed=index_failed,
        )
        updated = frontmatter + body
        if not updated.endswith("\n"):
            updated += "\n"
        if updated == original_text:
            continue
        if _atomic_write_bytes(page.file, updated.encode("utf-8")):
            pages_updated += 1
    stats = {"pages_updated": pages_updated, "page_count": len(book_output.pages)}
    Log(
        INFO_LOG_LEVEL,
        "output_writer stamp_page_metadata done",
        {"request_id": request_id, **stats},
    )
    return stats


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
    expected_aligned = int(useful_pages.aligned_page_count)
    if len(sorted_pages) != expected_aligned:
        Log(
            ERROR_LOG_LEVEL,
            "output_writer page count mismatch vs aligned_page_count",
            {
                "request_id": request_id,
                "source_sha256": source_sha256[:16],
                "stage3_pages": len(sorted_pages),
                "aligned_page_count": expected_aligned,
                "missing_aligned": list(stage3_result.missing or []),
            },
        )
        raise ValueError(
            "stage3 page count "
            f"({len(sorted_pages)}) does not match aligned_page_count ({expected_aligned})"
        )

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

    existing_manifest: dict[str, object] | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_manifest = loaded
        except (json.JSONDecodeError, OSError):
            existing_manifest = None

    existing_prompts = existing_manifest.get("prompts_used") if existing_manifest else None
    if isinstance(existing_prompts, dict) and isinstance(existing_prompts.get("items"), list):
        prompts_used: dict[str, object] = existing_prompts
    else:
        prompts_used = snapshot_book_prompts(output_dir)

    manifest_data: dict[str, object] = {
        "source_sha256": source_sha256,
        "slug": slug,
        "original_page_count": useful_pages.original_page_count,
        "aligned_page_count": expected_aligned,
        "pages_to_remove": list(enriched.request.pages_to_remove),
        "toc_range": enriched.request.toc_range.model_dump(),
        "index_range": enriched.request.index_range.model_dump(),
        "toc_range_aligned": useful_pages.toc_range_aligned.model_dump(),
        "index_range_aligned": useful_pages.index_range_aligned.model_dump(),
        "pages": manifest_page_entries,
        "reicat": enriched.request.reicat.model_dump(by_alias=True),
        "md_formatting": enriched.request.md_formatting.resolved(),
        "pipeline_version": enriched.request.schema_version,
        "prompts_used": prompts_used,
        "generated_at": _utc_now_iso(),
    }
    biblio_range = _range_dump(enriched.request.biblio_range)
    if biblio_range is not None:
        manifest_data["biblio_range"] = biblio_range
    biblio_range_aligned = _range_dump(useful_pages.biblio_range_aligned)
    if biblio_range_aligned is not None:
        manifest_data["biblio_range_aligned"] = biblio_range_aligned

    should_write_manifest = True
    if existing_manifest is not None and not pages_written:
        if _manifest_core(existing_manifest) == _manifest_core(manifest_data):
            should_write_manifest = False

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

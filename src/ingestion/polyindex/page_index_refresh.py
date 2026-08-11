from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import openai

from src.core.hashing import validate_source_sha256
from src.core.openai_client import build_openai_client
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.file_lock import polyindex_dir_lock
from src.ingestion.polyindex.time_index import (
    SCHEMA_VERSION,
    _atomic_write_json,
    _empty_book_time_index_document,
    _empty_time_index_document,
    _merge_entry_pages,
    _merge_local_entry_pages,
    _sort_book_time_index_document,
    _sort_time_index_document,
    book_time_index_json_path,
)
from src.ingestion.polyindex.time_index_llm import extract_time_references_for_page
from src.models.request import PageRange
from src.models.settings import Settings
from src.persistence.polyindex_deprecated import (
    add_deprecated_item,
    clear_deprecated_item_if_matches,
)


def _book_output_from_disk(data_root: Path, source_sha256: str) -> BookOutput:
    from src.api.biblio_handlers import _book_output_from_disk as load_book

    return load_book(data_root, source_sha256)


def _book_title(manifest: dict[str, Any]) -> str | None:
    reicat = manifest.get("reicat")
    if not isinstance(reicat, dict):
        return None
    raw = reicat.get("title") or reicat.get("titolo")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def book_has_time_index(data_root: Path, source_sha256: str, slug: str) -> bool:
    if book_time_index_json_path(data_root / "output" / source_sha256, slug).is_file():
        return True
    global_path = data_root / "polyindex" / "TIME_INDEX.json"
    if not global_path.is_file():
        return False
    try:
        document = json.loads(global_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(document, dict):
        return False
    for section_name in ("years", "dates"):
        section = document.get(section_name)
        if not isinstance(section, dict):
            continue
        for entry in section.values():
            if not isinstance(entry, dict):
                continue
            books = entry.get("books")
            if isinstance(books, dict) and source_sha256 in books:
                return True
    return False


def page_in_manifest_range(manifest: dict[str, Any], key: str, original_page: int) -> bool:
    raw = manifest.get(key)
    if not isinstance(raw, dict):
        return False
    start, end = raw.get("start"), raw.get("end")
    return isinstance(start, int) and isinstance(end, int) and start <= original_page <= end


def indices_for_page(
    data_root: Path,
    *,
    source_sha256: str,
    original_page: int,
    slug: str,
    manifest: dict[str, Any],
) -> list[str]:
    linked: list[str] = []
    if book_has_time_index(data_root, source_sha256, slug):
        linked.append("time_index")
    if page_in_manifest_range(manifest, "toc_range", original_page):
        linked.append("polyindex_toc")
    if page_in_manifest_range(manifest, "index_range", original_page):
        linked.append("polyindex_index")
    if page_in_manifest_range(manifest, "biblio_range", original_page):
        linked.append("polyindex_biblio")
    return linked


def _labels_for_page(book_document: dict[str, Any], aligned_page: int) -> tuple[set[str], set[str]]:
    years: set[str] = set()
    dates: set[str] = set()
    page_years = book_document.get("page_years")
    page_dates = book_document.get("page_dates")
    if isinstance(page_years, dict):
        raw = page_years.get(str(aligned_page))
        if isinstance(raw, list):
            years.update(str(item) for item in raw if str(item).strip())
    if isinstance(page_dates, dict):
        raw = page_dates.get(str(aligned_page))
        if isinstance(raw, list):
            dates.update(str(item) for item in raw if str(item).strip())
    for section_name, bucket in (("years", years), ("dates", dates)):
        section = book_document.get(section_name)
        if not isinstance(section, dict):
            continue
        for label, entry in section.items():
            if isinstance(entry, dict) and isinstance(entry.get("aligned_pages"), list):
                if aligned_page in entry["aligned_pages"]:
                    bucket.add(str(label))
    return years, dates


def _mark_missing_deprecated(
    data_root: Path,
    *,
    source_sha256: str,
    page: BookPageOutput,
    missing_years: set[str],
    missing_dates: set[str],
) -> int:
    added = 0
    for section, labels in (("years", missing_years), ("dates", missing_dates)):
        for label in sorted(labels):
            item = add_deprecated_item(
                data_root,
                source_sha256=source_sha256,
                index_kind="time_index",
                label=label,
                aligned_page=page.aligned,
                original_page=page.original,
                section=section,
                detail="reference missing after page transcript update",
            )
            if item is not None:
                added += 1
    return added


def _clear_active_deprecated(
    data_root: Path,
    *,
    source_sha256: str,
    page: BookPageOutput,
    new_years: set[str],
    new_dates: set[str],
) -> int:
    cleared = 0
    for section, labels in (("years", new_years), ("dates", new_dates)):
        for label in sorted(labels):
            if clear_deprecated_item_if_matches(
                data_root,
                source_sha256=source_sha256,
                index_kind="time_index",
                label=label,
                aligned_page=page.aligned,
                section=section,
            ):
                cleared += 1
    return cleared


async def _refresh_time_pages_async(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    pages: list[BookPageOutput],
    *,
    book_output: BookOutput,
    book_title: str | None,
    client: openai.OpenAI | None,
    request_id: str,
    prompt_notes: str | None,
) -> dict[str, Any]:
    polyindex_dir = Path(data_root) / "polyindex"
    book_path = book_time_index_json_path(book_output.output_dir, book_output.slug)
    if book_path.is_file():
        try:
            book_document = json.loads(book_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            book_document = _empty_book_time_index_document()
        if not isinstance(book_document, dict):
            book_document = _empty_book_time_index_document()
    else:
        book_document = _empty_book_time_index_document()
    book_document.setdefault("schema_version", SCHEMA_VERSION)
    years_section = book_document.setdefault("years", {})
    dates_section = book_document.setdefault("dates", {})
    page_years = book_document.setdefault("page_years", {})
    page_dates = book_document.setdefault("page_dates", {})
    if not isinstance(years_section, dict):
        years_section = {}
        book_document["years"] = years_section
    if not isinstance(dates_section, dict):
        dates_section = {}
        book_document["dates"] = dates_section
    if not isinstance(page_years, dict):
        page_years = {}
        book_document["page_years"] = page_years
    if not isinstance(page_dates, dict):
        page_dates = {}
        book_document["page_dates"] = page_dates

    sem = asyncio.Semaphore(settings.max_parallel_request)
    deprecated_added = 0
    deprecated_cleared = 0
    pages_updated = 0
    llm_pages = 0

    async def _scan(page: BookPageOutput) -> tuple[BookPageOutput, set[str], set[str], bool]:
        text = page.file.read_text(encoding="utf-8") if page.file.is_file() else ""
        async with sem:
            years, dates, _via, used_llm = await extract_time_references_for_page(
                text,
                client=client,
                settings=settings,
                request_id=request_id,
                aligned_page=page.aligned,
                prompt_notes=prompt_notes,
                source_sha256=source_sha256,
                book_slug=book_output.slug,
            )
        return page, years, dates, used_llm

    results = await asyncio.gather(*(_scan(page) for page in pages))
    merge_refs: list[tuple[int, int, set[str], set[str]]] = []
    for page, new_years, new_dates, used_llm in results:
        prev_years, prev_dates = _labels_for_page(book_document, page.aligned)
        missing_years = prev_years - new_years
        missing_dates = prev_dates - new_dates
        deprecated_added += _mark_missing_deprecated(
            data_root,
            source_sha256=source_sha256,
            page=page,
            missing_years=missing_years,
            missing_dates=missing_dates,
        )
        deprecated_cleared += _clear_active_deprecated(
            data_root,
            source_sha256=source_sha256,
            page=page,
            new_years=new_years,
            new_dates=new_dates,
        )
        for label in sorted(new_years):
            _merge_local_entry_pages(years_section, label, page.aligned, page.original)
        for label in sorted(new_dates):
            _merge_local_entry_pages(dates_section, label, page.aligned, page.original)
        page_years[str(page.aligned)] = sorted(prev_years | new_years)
        page_dates[str(page.aligned)] = sorted(prev_dates | new_dates)
        if not page_years[str(page.aligned)]:
            page_years.pop(str(page.aligned), None)
        if not page_dates[str(page.aligned)]:
            page_dates.pop(str(page.aligned), None)
        merge_refs.append((page.aligned, page.original, new_years, new_dates))
        pages_updated += 1
        if used_llm:
            llm_pages += 1

    _atomic_write_json(book_path, _sort_book_time_index_document(book_document))

    global_path = polyindex_dir / "TIME_INDEX.json"
    with polyindex_dir_lock(polyindex_dir, ".time_index.lock"):
        if global_path.is_file():
            try:
                document = json.loads(global_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                document = _empty_time_index_document()
            if not isinstance(document, dict):
                document = _empty_time_index_document()
        else:
            document = _empty_time_index_document()
        document["schema_version"] = SCHEMA_VERSION
        g_years = document.setdefault("years", {})
        g_dates = document.setdefault("dates", {})
        if not isinstance(g_years, dict):
            g_years = {}
            document["years"] = g_years
        if not isinstance(g_dates, dict):
            g_dates = {}
            document["dates"] = g_dates
        for aligned_page, original_page, years, dates in merge_refs:
            for year_label in years:
                _merge_entry_pages(
                    g_years,
                    year_label,
                    source_sha256,
                    aligned_page,
                    original_page,
                    book_title=book_title,
                    book_slug=book_output.slug,
                )
            for date_label in dates:
                _merge_entry_pages(
                    g_dates,
                    date_label,
                    source_sha256,
                    aligned_page,
                    original_page,
                    book_title=book_title,
                    book_slug=book_output.slug,
                )
        _atomic_write_json(global_path, _sort_time_index_document(document))

    return {
        "ok": True,
        "index_kind": "time_index",
        "pages_updated": pages_updated,
        "deprecated_added": deprecated_added,
        "deprecated_cleared": deprecated_cleared,
        "n_llm_pages": llm_pages,
        "book_time_index_path": str(book_path),
    }


def _manifest_page_range(manifest: dict[str, Any], key: str) -> PageRange | None:
    raw = manifest.get(key)
    if not isinstance(raw, dict):
        return None
    start, end = raw.get("start"), raw.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return None
    return PageRange(start=start, end=end)


def refresh_polyindex_for_pages(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    aligned_pages: list[int],
    *,
    client: openai.OpenAI | None = None,
    request_id: str = "",
    prompt_notes: str | None = None,
) -> dict[str, Any]:
    from src.api.biblio_handlers import run_biblio_only_job
    from src.api.biblio_polyindex_jobs import run_polyindex_index_job, run_polyindex_toc_job

    sha = validate_source_sha256(source_sha256)
    wanted = sorted({int(page) for page in aligned_pages if int(page) > 0})
    if not wanted:
        return {"ok": False, "error": "aligned_pages required"}
    book_output = _book_output_from_disk(data_root, sha)
    try:
        manifest = json.loads(book_output.manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    page_by_aligned = {page.aligned: page for page in book_output.pages}
    time_pages: list[BookPageOutput] = []
    membership: dict[str, list[int]] = {
        "time_index": [],
        "polyindex_toc": [],
        "polyindex_index": [],
        "polyindex_biblio": [],
    }
    for aligned in wanted:
        page = page_by_aligned.get(aligned)
        if page is None:
            continue
        linked = indices_for_page(
            data_root,
            source_sha256=sha,
            original_page=page.original,
            slug=book_output.slug,
            manifest=manifest,
        )
        for kind in linked:
            membership.setdefault(kind, []).append(page.aligned)
        if "time_index" in linked:
            time_pages.append(page)

    openai_client = client or build_openai_client(settings)
    rid = request_id or sha
    updates: list[dict[str, Any]] = []
    if time_pages:
        updates.append(
            asyncio.run(
                _refresh_time_pages_async(
                    data_root,
                    settings,
                    sha,
                    time_pages,
                    book_output=book_output,
                    book_title=_book_title(manifest),
                    client=openai_client,
                    request_id=rid,
                    prompt_notes=prompt_notes,
                )
            )
        )
    if membership.get("polyindex_toc"):
        updates.append(
            run_polyindex_toc_job(
                data_root,
                settings,
                sha,
                client=openai_client,
                request_id=rid,
                prompt_notes=prompt_notes,
            )
        )
    if membership.get("polyindex_index"):
        updates.append(
            run_polyindex_index_job(
                data_root,
                settings,
                sha,
                client=openai_client,
                request_id=rid,
                prompt_notes=prompt_notes,
            )
        )
    if membership.get("polyindex_biblio"):
        biblio_range = _manifest_page_range(manifest, "biblio_range")
        if biblio_range is None:
            updates.append(
                {
                    "ok": False,
                    "index_kind": "polyindex_biblio",
                    "error": "biblio_range missing in manifest",
                }
            )
        else:
            result = run_biblio_only_job(
                data_root,
                settings,
                sha,
                biblio_range,
                client=openai_client,
                request_id=rid,
                prompt_notes=prompt_notes,
            )
            result = dict(result)
            result["index_kind"] = "polyindex_biblio"
            updates.append(result)
    return {
        "ok": True,
        "source_sha256": sha,
        "aligned_pages": wanted,
        "membership": membership,
        "updates": updates,
        "skipped": [],
    }

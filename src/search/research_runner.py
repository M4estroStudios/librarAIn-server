from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client
from src.models.polyindex_index import PolyindexIndexDocument
from src.models.polyindex_toc import PolyindexTocDocument
from src.models.settings import Settings
from src.search.article_finalize_llm import finalize_article
from src.search.article_llm import query_log_fields
from src.search.article_llm import generate_article
from src.search.chapter_expansion import expand_chapters
from src.search.page_relevance import filter_relevant_pages
from src.search.pages_loader import load_pages
from src.search.poh_links_llm import (
    _CHUNK_OVERLAP,
    _CHUNK_SIZE,
    add_poh_links,
    chunk_article_text,
    discover_poh_link_tasks,
    link_paragraph_count,
    poh_link_phase_total,
)
from src.search.postprocess import PostprocessResult, postprocess_markdown
from src.search.request_schema import ResearchPoh, ResearchRequest
from src.search.subject_lookup import lookup_subjects
from src.search.time_lookup import load_time_index, lookup_time
from src.search.timeline_llm import add_timeline

RESEARCH_PIPELINE_VERSION = "2.0"
PHASE_COLLECT = "research_collect"
PHASE_FILTER = "research_filter"
PHASE_ARTICLE = "research_article"
PHASE_POH_LINKS = "research_poh_links"
PHASE_TIMELINE = "research_timeline"
PHASE_VERIFY = "research_verify"
STATUS_STARTED = "started"
STATUS_PROGRESS = "progress"
STATUS_PLAN = "plan"
STATUS_WAITING = "waiting"
STATUS_COMPLETED = "completed"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

ProgressReporter = Callable[[dict[str, Any]], None]
SetGlobalTotal = Callable[[int], None]


@dataclass(frozen=True)
class ResearchContextAudit:
    context_books_loaded: dict[str, list[int]]
    context_books: dict[str, list[int]]
    subjects_matched: list[dict[str, Any]]


@dataclass
class ResearchRunResult:
    markdown: str
    markdown_path: str
    postprocess: PostprocessResult
    audit: ResearchContextAudit
    skipped_llm: bool = False


class ResearchConcurrencyLimiter:
    def __init__(self, max_concurrent: int) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._running = 0
        self._condition = threading.Condition()

    def try_acquire(self) -> bool:
        with self._condition:
            if self._running >= self._max_concurrent:
                return False
            self._running += 1
            return True

    def acquire(self) -> None:
        with self._condition:
            while self._running >= self._max_concurrent:
                self._condition.wait()
            self._running += 1

    def release(self) -> None:
        with self._condition:
            self._running = max(0, self._running - 1)
            self._condition.notify()


class ResearchDedupIndex:
    def __init__(self, *, ttl_seconds: float = 3600.0) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float]] = {}
        self._ttl_seconds = ttl_seconds

    def lookup(self, dedup_key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(dedup_key)
            if entry is None:
                return None
            request_id, created = entry
            if now - created > self._ttl_seconds:
                del self._entries[dedup_key]
                return None
            return request_id

    def register(self, dedup_key: str, request_id: str) -> None:
        with self._lock:
            self._entries[dedup_key] = (request_id, time.monotonic())


def compute_dedup_key(
    request: ResearchRequest,
    *,
    index_path: Path,
) -> str:
    poh_id = request.poh.id if request.poh and request.poh.id else ""
    normalized_query = request.query.strip().casefold()
    if index_path.is_file():
        digest_material = f"{index_path.stat().st_mtime_ns}:{index_path.stat().st_size}"
    else:
        digest_material = "missing"
    payload = f"{normalized_query}|{poh_id}|{digest_material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _emit(reporter: ProgressReporter | None, event: dict[str, Any]) -> None:
    if reporter is not None:
        reporter(event)


async def _heartbeat_loop(
    reporter: ProgressReporter,
    *,
    phase: str,
    message: str,
    interval_seconds: float = 15.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        _emit(
            reporter,
            {
                "phase": phase,
                "status": STATUS_WAITING,
                "message": message,
                "counts_as_step": False,
            },
        )


async def _run_with_heartbeat(
    reporter: ProgressReporter | None,
    *,
    phase: str,
    message: str,
    coro: Any,
    interval_seconds: float = 15.0,
) -> Any:
    if reporter is None:
        return await coro
    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            reporter,
            phase=phase,
            message=message,
            interval_seconds=interval_seconds,
        )
    )
    try:
        return await coro
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


def _research_event(phase: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"phase": phase, "status": status, **fields}


def _step_event(phase: str, *, page_total: int | None = None, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase": phase,
        "status": STATUS_PROGRESS,
        "counts_as_step": True,
    }
    if page_total is not None:
        payload["page_total"] = page_total
    payload.update(fields)
    return payload


def _research_global_total(
    *,
    collect_total: int,
    filter_total: int,
    poh_chunk_count: int,
    poh_link_paragraph_count: int = 0,
) -> int:
    poh_total = poh_link_phase_total(poh_chunk_count, poh_link_paragraph_count)
    return collect_total + filter_total + 1 + poh_total + 1 + 2


def _estimate_poh_chunks(*, total_chars: int, markdown_chars: int = 0) -> int:
    basis = markdown_chars or total_chars
    if basis <= 0:
        return 1
    step = max(1, _CHUNK_SIZE - _CHUNK_OVERLAP)
    return max(1, (basis + step - 1) // step)


def _research_plan_totals(
    *,
    collect_total: int,
    filter_total: int,
    poh_chunk_count: int,
    poh_link_paragraph_count: int = 0,
) -> dict[str, int]:
    poh_total = poh_link_phase_total(poh_chunk_count, poh_link_paragraph_count)
    return {
        PHASE_COLLECT: max(1, collect_total),
        PHASE_FILTER: max(1, filter_total),
        PHASE_ARTICLE: 1,
        PHASE_POH_LINKS: poh_total,
        PHASE_TIMELINE: 1,
        PHASE_VERIFY: 2,
    }


def _research_plan_event(phase_totals: dict[str, int]) -> dict[str, Any]:
    return {
        "phase": "research",
        "status": STATUS_PLAN,
        "phase_totals": phase_totals,
        "counts_as_step": False,
    }


def _merge_page_maps(*maps: dict[str, list[int]]) -> dict[str, list[int]]:
    merged: dict[str, set[int]] = {}
    for page_map in maps:
        for sha, pages in page_map.items():
            merged.setdefault(sha, set()).update(pages)
    return {sha: sorted(page_set) for sha, page_set in sorted(merged.items())}


def _count_pages(page_map: dict[str, list[int]]) -> int:
    return sum(len(pages) for pages in page_map.values())


def _pages_added(before: dict[str, list[int]], after: dict[str, list[int]]) -> int:
    before_set: dict[str, set[int]] = {
        sha: set(pages) for sha, pages in before.items()
    }
    added = 0
    for sha, pages in after.items():
        known = before_set.get(sha, set())
        added += sum(1 for page in pages if page not in known)
    return added


def _polyindex_empty(data_root: Path) -> bool:
    index_path = data_root / "polyindex" / "INDEX.json"
    if not index_path.is_file():
        return True
    try:
        document = PolyindexIndexDocument.load_file(index_path)
    except (json.JSONDecodeError, OSError, ValueError):
        return True
    return not document.subjects


async def run_research_async(
    request: ResearchRequest,
    *,
    data_root: Path,
    settings: Settings,
    request_id: str,
    reporter: ProgressReporter | None = None,
    set_global_total: SetGlobalTotal | None = None,
) -> ResearchRunResult:
    if _polyindex_empty(data_root):
        raise RuntimeError("polyindex vuoto")

    log_fields = query_log_fields(request.query, request.poh)
    subject = log_fields["research_subject"]
    Log(
        INFO_LOG_LEVEL,
        f"research run started: {subject}",
        {
            "request_id": request_id,
            "pipeline_version": RESEARCH_PIPELINE_VERSION,
            **log_fields,
        },
    )

    polyindex_dir = data_root / "polyindex"
    index_document = PolyindexIndexDocument.load_file(polyindex_dir / "INDEX.json")
    toc_document = PolyindexTocDocument.load_file(polyindex_dir / "TOC.json")
    time_index = load_time_index(polyindex_dir / "TIME_INDEX.json")

    client = build_openai_client(settings)

    subject_result = lookup_subjects(
        request.query,
        request.poh,
        index_document,
        client,
        settings.sqlite_path,
        settings,
        request_id,
    )
    subject_pages = _count_pages(subject_result.pages)

    expanded = expand_chapters(
        subject_result.pages,
        toc_document,
        max_books=request.options.max_books,
        max_pages_per_book=request.options.max_pages_per_book,
        request_id=request_id,
    )
    expanded_pages = _count_pages(expanded.pages)
    collect_subject_pages = expanded_pages

    time_result = lookup_time(
        request.query,
        request.poh,
        expanded.pages,
        time_index,
        request_id=request_id,
    )
    time_pages = _count_pages(time_result.pages)
    collect_time_pages = _pages_added(expanded.pages, time_result.pages)
    collect_total = collect_subject_pages + collect_time_pages

    candidate_pages = _merge_page_maps(subject_result.pages, time_result.pages)
    merged_pages = _count_pages(candidate_pages)

    _emit(
        reporter,
        _research_event(
            PHASE_COLLECT,
            STATUS_STARTED,
            page_total=max(1, collect_total),
            subject_pages=collect_subject_pages,
            time_pages=collect_time_pages,
        ),
    )
    _emit(
        reporter,
        _research_plan_event(
            _research_plan_totals(
                collect_total=collect_total,
                filter_total=merged_pages,
                poh_chunk_count=_estimate_poh_chunks(total_chars=0),
            )
        ),
    )

    pages_result = load_pages(
        candidate_pages,
        data_root,
        max_books=request.options.max_books,
        max_pages_per_book=request.options.max_pages_per_book,
        request_id=request_id,
    )
    loaded_pages = len(pages_result.pages)
    collect_steps = min(loaded_pages, collect_total) if collect_total else loaded_pages
    for _ in range(collect_steps):
        _emit(
            reporter,
            _step_event(PHASE_COLLECT, page_total=max(1, collect_total)),
        )
    _emit(
        reporter,
        _research_event(
            PHASE_COLLECT,
            STATUS_COMPLETED,
            page_total=max(1, collect_total),
            subject_pages=collect_subject_pages,
            time_pages=collect_time_pages,
            loaded_pages=loaded_pages,
            merged_pages=merged_pages,
        ),
    )

    filter_total = max(1, loaded_pages)
    _emit(
        reporter,
        _research_event(PHASE_FILTER, STATUS_STARTED, page_total=filter_total),
    )
    relevant_pages = filter_relevant_pages(
        pages_result.pages,
        query=request.query,
        poh=request.poh,
        document=index_document,
        on_page_checked=lambda: _emit(
            reporter,
            _step_event(PHASE_FILTER, page_total=filter_total),
        ),
    )
    dropped_pages = len(pages_result.pages) - len(relevant_pages)
    if dropped_pages:
        Log(
            INFO_LOG_LEVEL,
            f"research page filter: {subject} ({len(relevant_pages)}/{len(pages_result.pages)} pages kept)",
            {
                "request_id": request_id,
                "input_pages": len(pages_result.pages),
                "relevant_pages": len(relevant_pages),
                "dropped_pages": dropped_pages,
                **log_fields,
            },
        )
    _emit(
        reporter,
        _research_event(
            PHASE_FILTER,
            STATUS_COMPLETED,
            page_total=filter_total,
            input_pages=loaded_pages,
            kept_pages=len(relevant_pages),
            dropped_pages=dropped_pages,
        ),
    )

    estimated_poh_chunks = _estimate_poh_chunks(total_chars=pages_result.total_chars)
    plan_totals = _research_plan_totals(
        collect_total=collect_total,
        filter_total=filter_total,
        poh_chunk_count=estimated_poh_chunks,
    )
    _emit(reporter, _research_plan_event(plan_totals))
    if set_global_total is not None:
        set_global_total(_research_global_total(
            collect_total=max(1, collect_total),
            filter_total=filter_total,
            poh_chunk_count=estimated_poh_chunks,
        ))

    relevant_loaded: dict[str, set[int]] = {}
    for page in relevant_pages:
        relevant_loaded.setdefault(page.source_sha256, set()).add(page.aligned_page)

    audit = ResearchContextAudit(
        context_books_loaded={
            sha: sorted(pages)
            for sha, pages in sorted(pages_result.loaded_pages.items())
        },
        context_books={
            sha: sorted(pages) for sha, pages in sorted(relevant_loaded.items())
        },
        subjects_matched=[
            {
                "canonical_id": match.canonical_id,
                "canonical_label": match.canonical_label,
                "method": match.method,
                "similarity": match.similarity,
            }
            for match in subject_result.matches
        ],
    )

    _emit(
        reporter,
        _research_event(
            PHASE_ARTICLE,
            STATUS_STARTED,
            page_total=1,
            input_pages=len(relevant_pages),
            context_books=len(relevant_loaded),
        ),
    )
    _emit(
        reporter,
        _research_event(
            PHASE_ARTICLE,
            STATUS_WAITING,
            message="Generazione bozza con il modello…",
        ),
    )
    article_result = await _run_with_heartbeat(
        reporter,
        phase=PHASE_ARTICLE,
        message="Generazione bozza con il modello…",
        coro=generate_article(
            query=request.query,
            pages=relevant_pages,
            client=client,
            settings=settings,
            poh=request.poh,
            request_id=request_id,
        ),
    )
    poh_chunk_count = len(chunk_article_text(article_result.markdown))
    plan_totals = _research_plan_totals(
        collect_total=collect_total,
        filter_total=filter_total,
        poh_chunk_count=poh_chunk_count,
    )
    _emit(reporter, _research_plan_event(plan_totals))
    if set_global_total is not None:
        set_global_total(
            _research_global_total(
                collect_total=max(1, collect_total),
                filter_total=filter_total,
                poh_chunk_count=poh_chunk_count,
            )
        )
    _emit(
        reporter,
        _research_event(
            PHASE_ARTICLE,
            STATUS_COMPLETED,
            counts_as_step=True,
            page_total=1,
            skipped_llm=article_result.skipped_llm,
            input_pages=len(relevant_pages),
            markdown_chars=len(article_result.markdown),
        ),
    )

    poh_phase_total = poh_link_phase_total(poh_chunk_count, 0)
    _emit(
        reporter,
        _research_event(
            PHASE_POH_LINKS,
            STATUS_STARTED,
            page_total=poh_phase_total,
            chunk_count=poh_chunk_count,
        ),
    )
    link_tasks = await discover_poh_link_tasks(
        article_markdown=article_result.markdown,
        document=index_document,
        client=client,
        settings=settings,
        sqlite_path=settings.sqlite_path,
        request_id=request_id,
        reporter=reporter,
    )
    link_task_count = len(link_tasks)
    link_paragraphs = link_paragraph_count(link_tasks)
    poh_phase_total = poh_link_phase_total(poh_chunk_count, link_paragraphs)
    plan_totals = _research_plan_totals(
        collect_total=collect_total,
        filter_total=filter_total,
        poh_chunk_count=poh_chunk_count,
        poh_link_paragraph_count=link_paragraphs,
    )
    _emit(reporter, _research_plan_event(plan_totals))
    if set_global_total is not None:
        set_global_total(
            _research_global_total(
                collect_total=max(1, collect_total),
                filter_total=filter_total,
                poh_chunk_count=poh_chunk_count,
                poh_link_paragraph_count=link_paragraphs,
            )
        )
    _emit(
        reporter,
        _research_event(
            PHASE_POH_LINKS,
            STATUS_PROGRESS,
            page_total=poh_phase_total,
            chunk_count=poh_chunk_count,
            link_tasks=link_task_count,
            link_paragraphs=link_paragraphs,
        ),
    )
    poh_result = await add_poh_links(
        query=request.query,
        article_markdown=article_result.markdown,
        document=index_document,
        client=client,
        settings=settings,
        link_tasks=link_tasks,
        poh=request.poh,
        request_id=request_id,
        sqlite_path=settings.sqlite_path,
        reporter=reporter,
        poh_phase_total=poh_phase_total,
    )
    _emit(
        reporter,
        _research_event(
            PHASE_POH_LINKS,
            STATUS_COMPLETED,
            page_total=poh_phase_total,
            skipped_llm=poh_result.skipped_llm,
            candidates=len(link_tasks),
            link_tasks=link_task_count,
            link_paragraphs=link_paragraphs,
            chunk_count=poh_chunk_count,
        ),
    )

    _emit(
        reporter,
        _research_event(PHASE_TIMELINE, STATUS_STARTED, page_total=1,
            timeline_candidates=len(time_result.timeline_candidates),
        ),
    )
    _emit(
        reporter,
        _research_event(
            PHASE_TIMELINE,
            STATUS_WAITING,
            message="Generazione cronologia…",
        ),
    )
    timeline_result = await _run_with_heartbeat(
        reporter,
        phase=PHASE_TIMELINE,
        message="Generazione cronologia…",
        coro=add_timeline(
            query=request.query,
            article_markdown=poh_result.markdown,
            timeline_candidates=time_result.timeline_candidates,
            pages=relevant_pages,
            client=client,
            settings=settings,
            poh=request.poh,
            request_id=request_id,
        ),
    )
    _emit(
        reporter,
        _research_event(
            PHASE_TIMELINE,
            STATUS_COMPLETED,
            counts_as_step=True,
            page_total=1,
            skipped_llm=timeline_result.skipped_llm,
            timeline_candidates=len(time_result.timeline_candidates),
        ),
    )

    _emit(reporter, _research_event(PHASE_VERIFY, STATUS_STARTED, page_total=2))
    postprocessed = postprocess_markdown(
        timeline_result.markdown,
        data_root=data_root,
        index_document=index_document,
        request_id=request_id,
    )
    _emit(
        reporter,
        _step_event(PHASE_VERIFY, page_total=2, verify_step="postprocess"),
    )
    _emit(
        reporter,
        _research_event(
            PHASE_VERIFY,
            STATUS_WAITING,
            message="Revisione finale con il modello…",
        ),
    )
    finalize_result = await _run_with_heartbeat(
        reporter,
        phase=PHASE_VERIFY,
        message="Revisione finale con il modello…",
        coro=finalize_article(
            query=request.query,
            draft_markdown=article_result.markdown,
            enriched_markdown=postprocessed.markdown,
            client=client,
            settings=settings,
            poh=request.poh,
            request_id=request_id,
        ),
    )
    _emit(
        reporter,
        _step_event(PHASE_VERIFY, page_total=2, verify_step="finalize"),
    )
    _emit(
        reporter,
        _research_event(
            PHASE_VERIFY,
            STATUS_COMPLETED,
            page_total=2,
            citations=len(postprocessed.citations),
            poh_links=len(postprocessed.pohs_referenced),
            timeline_rows=len(postprocessed.timeline_rows),
            skipped_llm=finalize_result.skipped_llm,
            markdown_chars=len(finalize_result.markdown),
        ),
    )

    final_postprocessed = postprocess_markdown(
        finalize_result.markdown,
        data_root=data_root,
        index_document=index_document,
        request_id=request_id,
    )

    skipped_llm = (
        article_result.skipped_llm
        and poh_result.skipped_llm
        and timeline_result.skipped_llm
        and finalize_result.skipped_llm
    )
    Log(
        INFO_LOG_LEVEL,
        f"research run completed: {subject}",
        {
            "request_id": request_id,
            "skipped_llm": skipped_llm,
            "pages_used": len(relevant_pages),
            "books_used": len(audit.context_books),
            "citations": len(final_postprocessed.citations),
            "poh_links": len(final_postprocessed.pohs_referenced),
            "timeline_rows": len(final_postprocessed.timeline_rows),
            **log_fields,
        },
    )
    return ResearchRunResult(
        markdown=final_postprocessed.markdown,
        markdown_path="",
        postprocess=final_postprocessed,
        audit=audit,
        skipped_llm=skipped_llm,
    )


def run_research(
    request: ResearchRequest,
    *,
    data_root: Path,
    settings: Settings,
    request_id: str,
    reporter: ProgressReporter | None = None,
    set_global_total: SetGlobalTotal | None = None,
) -> ResearchRunResult:
    return asyncio.run(
        run_research_async(
            request,
            data_root=data_root,
            settings=settings,
            request_id=request_id,
            reporter=reporter,
            set_global_total=set_global_total,
        )
    )


def persist_query_markdown(data_root: Path, request_id: str, markdown: str) -> Path:
    out_dir = data_root / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{request_id}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(markdown, encoding="utf-8")
    tmp.replace(path)
    return path


def build_article_response(result: ResearchRunResult) -> dict[str, Any]:
    post = result.postprocess
    return {
        "markdown": post.markdown,
        "skipped_llm": result.skipped_llm,
        "citations": [
            {
                "source_sha256": item.source_sha256,
                "aligned_page": item.aligned_page,
                "label": item.label,
            }
            for item in post.citations
        ],
        "pohs_referenced": [
            {
                "poh_id": item.poh_id,
                "label": item.label,
                "linked_from_count": item.linked_from_count,
            }
            for item in post.pohs_referenced
        ],
        "timeline_rows": [
            {
                "period": item.period,
                "event": item.event,
                "source_links": list(item.source_links),
            }
            for item in post.timeline_rows
        ],
    }


def build_poh_research_request(poh_id: str, label: str) -> ResearchRequest:
    return ResearchRequest(
        query=label,
        poh=ResearchPoh(id=poh_id, label=label),
    )

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
from src.search.article_llm import query_log_fields
from src.search.article_llm import generate_article
from src.search.chapter_expansion import expand_chapters
from src.search.page_relevance import filter_relevant_pages
from src.search.pages_loader import load_pages
from src.search.poh_links_llm import add_poh_links, build_poh_candidates
from src.search.postprocess import PostprocessResult, postprocess_markdown
from src.search.request_schema import ResearchPoh, ResearchRequest
from src.search.subject_lookup import lookup_subjects
from src.search.time_lookup import load_time_index, lookup_time
from src.search.timeline_llm import add_timeline

RESEARCH_PIPELINE_VERSION = "2.0"
PHASE_PREFILTER = "research_prefilter"
PHASE_ARTICLE = "research_article"
PHASE_POH_LINKS = "research_poh_links"
PHASE_TIMELINE = "research_timeline"
PHASE_POSTPROCESS = "research_postprocess"
STATUS_STARTED = "started"
STATUS_COMPLETED = "completed"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

ProgressReporter = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ResearchContextAudit:
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


def _research_event(phase: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"phase": phase, "status": status, **fields}


def _merge_page_maps(*maps: dict[str, list[int]]) -> dict[str, list[int]]:
    merged: dict[str, set[int]] = {}
    for page_map in maps:
        for sha, pages in page_map.items():
            merged.setdefault(sha, set()).update(pages)
    return {sha: sorted(page_set) for sha, page_set in sorted(merged.items())}


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

    _emit(reporter, _research_event(PHASE_PREFILTER, STATUS_STARTED))
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
    expanded = expand_chapters(
        subject_result.pages,
        toc_document,
        max_books=request.options.max_books,
        max_pages_per_book=request.options.max_pages_per_book,
        request_id=request_id,
    )
    time_result = lookup_time(
        request.query,
        request.poh,
        expanded.pages,
        time_index,
        request_id=request_id,
    )
    candidate_pages = _merge_page_maps(subject_result.pages, time_result.pages)
    pages_result = load_pages(
        candidate_pages,
        data_root,
        max_books=request.options.max_books,
        max_pages_per_book=request.options.max_pages_per_book,
        request_id=request_id,
    )
    relevant_pages = filter_relevant_pages(
        pages_result.pages,
        query=request.query,
        poh=request.poh,
        document=index_document,
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
            PHASE_PREFILTER,
            STATUS_COMPLETED,
            books=len(pages_result.loaded_pages),
            pages=len(relevant_pages),
            dropped_pages=dropped_pages,
        ),
    )

    relevant_loaded: dict[str, set[int]] = {}
    for page in relevant_pages:
        relevant_loaded.setdefault(page.source_sha256, set()).add(page.aligned_page)

    audit = ResearchContextAudit(
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

    _emit(reporter, _research_event(PHASE_ARTICLE, STATUS_STARTED))
    article_result = await generate_article(
        query=request.query,
        pages=relevant_pages,
        client=client,
        settings=settings,
        poh=request.poh,
        request_id=request_id,
    )
    _emit(
        reporter,
        _research_event(
            PHASE_ARTICLE,
            STATUS_COMPLETED,
            skipped_llm=article_result.skipped_llm,
        ),
    )

    poh_candidates = build_poh_candidates(
        document=index_document,
        subject_matches=subject_result.matches,
        article_markdown=article_result.markdown,
        query=request.query,
    )

    _emit(reporter, _research_event(PHASE_POH_LINKS, STATUS_STARTED))
    poh_result = await add_poh_links(
        query=request.query,
        article_markdown=article_result.markdown,
        poh_candidates=poh_candidates,
        client=client,
        settings=settings,
        poh=request.poh,
        request_id=request_id,
    )
    _emit(
        reporter,
        _research_event(
            PHASE_POH_LINKS,
            STATUS_COMPLETED,
            skipped_llm=poh_result.skipped_llm,
        ),
    )

    _emit(reporter, _research_event(PHASE_TIMELINE, STATUS_STARTED))
    timeline_result = await add_timeline(
        query=request.query,
        article_markdown=poh_result.markdown,
        timeline_candidates=time_result.timeline_candidates,
        pages=relevant_pages,
        client=client,
        settings=settings,
        poh=request.poh,
        request_id=request_id,
    )
    _emit(
        reporter,
        _research_event(
            PHASE_TIMELINE,
            STATUS_COMPLETED,
            skipped_llm=timeline_result.skipped_llm,
        ),
    )

    _emit(reporter, _research_event(PHASE_POSTPROCESS, STATUS_STARTED))
    postprocessed = postprocess_markdown(
        timeline_result.markdown,
        data_root=data_root,
        index_document=index_document,
        request_id=request_id,
    )
    _emit(
        reporter,
        _research_event(
            PHASE_POSTPROCESS,
            STATUS_COMPLETED,
            citations=len(postprocessed.citations),
        ),
    )

    skipped_llm = (
        article_result.skipped_llm
        and poh_result.skipped_llm
        and timeline_result.skipped_llm
    )
    Log(
        INFO_LOG_LEVEL,
        f"research run completed: {subject}",
        {
            "request_id": request_id,
            "skipped_llm": skipped_llm,
            "pages_used": len(relevant_pages),
            "books_used": len(audit.context_books),
            "citations": len(postprocessed.citations),
            "poh_links": len(postprocessed.pohs_referenced),
            "timeline_rows": len(postprocessed.timeline_rows),
            **log_fields,
        },
    )
    return ResearchRunResult(
        markdown=postprocessed.markdown,
        markdown_path="",
        postprocess=postprocessed,
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
) -> ResearchRunResult:
    return asyncio.run(
        run_research_async(
            request,
            data_root=data_root,
            settings=settings,
            request_id=request_id,
            reporter=reporter,
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

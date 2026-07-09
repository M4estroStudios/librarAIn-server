from __future__ import annotations

import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Generator

from src.api.job_display import job_display_label, job_display_status

_INGEST_TERMINAL_STATUSES = frozenset({"done", "error"})
_RESEARCH_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
_TERMINAL_STATUSES = _INGEST_TERMINAL_STATUSES | _RESEARCH_TERMINAL_STATUSES

DEFAULT_JOB_TTL_SECONDS = 2 * 60 * 60
DEFAULT_MAX_FINISHED_JOBS = 200


class JobState:
    __slots__ = (
        "job_id",
        "job_kind",
        "status",
        "events",
        "result",
        "error",
        "pipeline_version",
        "created_at",
        "updated_at",
        "finished_at_monotonic",
        "global_total",
        "global_step",
        "_subscribers",
    )

    def __init__(self, job_id: str, *, job_kind: str = "ingest") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.job_id = job_id
        self.job_kind = job_kind
        self.status: str = "accepted" if job_kind == "research" else "queued"
        self.pipeline_version: str | None = None
        self.events: list[dict[str, Any]] = []
        self.result: Any | None = None
        self.error: str | None = None
        self.created_at = now
        self.updated_at = now
        self.finished_at_monotonic: float | None = None
        self.global_total: int | None = None
        self.global_step: int = 0
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []


class JobRegistry:
    """Thread-safe in-memory registry of ingest jobs.

    Each job has an append-only event history and a set of active SSE
    subscribers.  Subscribers receive a replay of the history on connect
    followed by live events pushed by the worker thread.

    Finished jobs are evicted after ``ttl_seconds`` (and the number of
    finished jobs retained is capped) so memory stays bounded on a
    long-running server.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_JOB_TTL_SECONDS,
        max_finished_jobs: int = DEFAULT_MAX_FINISHED_JOBS,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}
        self._ttl_seconds = ttl_seconds
        self._max_finished_jobs = max_finished_jobs

    def _evict_finished_locked(self) -> None:
        now = time.monotonic()
        finished = [
            state
            for state in self._jobs.values()
            if state.finished_at_monotonic is not None and not state._subscribers
        ]
        for state in finished:
            assert state.finished_at_monotonic is not None
            if now - state.finished_at_monotonic >= self._ttl_seconds:
                del self._jobs[state.job_id]

        remaining = [
            state
            for state in self._jobs.values()
            if state.finished_at_monotonic is not None and not state._subscribers
        ]
        overflow = len(remaining) - self._max_finished_jobs
        if overflow > 0:
            remaining.sort(key=lambda state: state.finished_at_monotonic or 0.0)
            for state in remaining[:overflow]:
                del self._jobs[state.job_id]

    def create_job(
        self,
        *,
        job_id: str | None = None,
        job_kind: str = "ingest",
        pipeline_version: str | None = None,
    ) -> str:
        """Allocate a new job and return its opaque job_id."""
        if job_id is None:
            job_id = uuid.uuid4().hex
        with self._lock:
            self._evict_finished_locked()
            if job_id in self._jobs:
                raise ValueError(f"job_id already exists: {job_id}")
            state = JobState(job_id, job_kind=job_kind)
            state.pipeline_version = pipeline_version
            self._jobs[job_id] = state
        return job_id

    def set_global_total(self, job_id: str, total: int) -> None:
        """Declare or update the total number of countable work-steps for the job.

        Emits a ``pipeline_total`` event when the total is first set or changes
        so clients can update their progress bars.
        """
        with self._lock:
            state = self._jobs[job_id]
            previous = state.global_total
            state.global_total = total
            if previous == total:
                return
            ev: dict[str, Any] = {
                "phase": "pipeline",
                "status": "pipeline_total",
                "global_total": total,
                "global_step": state.global_step,
                "counts_as_step": False,
                "ts": datetime.now(timezone.utc).isoformat(),
                "seq": len(state.events),
            }
            state.events.append(ev)
            state.updated_at = ev["ts"]
            for q in state._subscribers:
                q.put(ev)

    def emit(self, job_id: str, event: dict[str, Any]) -> None:
        """Append *event* to the job history and push it to all subscribers.

        If ``event["counts_as_step"]`` is ``True`` the registry atomically
        increments ``global_step`` and injects ``global_step`` /
        ``global_total`` into the event before delivery.
        """
        with self._lock:
            state = self._jobs[job_id]
            now = datetime.now(timezone.utc).isoformat()
            ev = dict(event)
            ev["ts"] = now
            ev["seq"] = len(state.events)

            if ev.get("counts_as_step"):
                state.global_step += 1
                ev["global_step"] = state.global_step
                ev["global_total"] = state.global_total

            status = ev.get("status", "")
            if status == "done":
                state.status = "done"
                state.result = ev.get("result")
                state.finished_at_monotonic = time.monotonic()
            elif status == "error":
                state.status = "error"
                state.error = ev.get("message")
                state.finished_at_monotonic = time.monotonic()
            elif status == "succeeded":
                state.status = "succeeded"
                state.result = ev.get("result")
                state.finished_at_monotonic = time.monotonic()
            elif status == "failed":
                state.status = "failed"
                state.error = ev.get("message")
                state.finished_at_monotonic = time.monotonic()
            elif status == "started":
                if state.job_kind == "research" and state.status == "accepted":
                    state.status = "running"
                elif state.status == "queued":
                    state.status = "running"

            state.events.append(ev)
            state.updated_at = now
            for q in state._subscribers:
                q.put(ev)

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """Return a JSON-serialisable snapshot of the job state.

        Returns ``None`` if the job_id is unknown.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return None
            payload: dict[str, Any] = {
                "job_id": state.job_id,
                "job_kind": state.job_kind,
                "status": state.status,
                "global_step": state.global_step,
                "global_total": state.global_total,
                "events": list(state.events),
                "result": state.result,
                "error": state.error,
                "created_at": state.created_at,
                "updated_at": state.updated_at,
            }
            if state.job_kind == "research":
                payload["request_id"] = state.job_id
                payload["pipeline_version"] = state.pipeline_version
                payload["last_error"] = state.error
            return payload

    def running_job_count(self) -> int:
        with self._lock:
            return sum(
                1
                for state in self._jobs.values()
                if state.status not in _TERMINAL_STATUSES
            )

    def list_active_jobs(self) -> list[dict[str, Any]]:
        return self.list_jobs(include_finished=False)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        include_finished: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            selected: list[JobState] = []
            for state in self._jobs.values():
                if state.status in _TERMINAL_STATUSES and not include_finished:
                    continue
                selected.append(state)
            selected.sort(key=lambda item: item.updated_at, reverse=True)
            selected.sort(
                key=lambda item: 0 if item.status not in _TERMINAL_STATUSES else 1
            )
            summaries: list[dict[str, Any]] = []
            for state in selected:
                if state.status in _TERMINAL_STATUSES and not include_finished:
                    continue
                if state.job_kind == "research":
                    summaries.append(summarize_research_job_state(state))
                else:
                    summaries.append(summarize_job_state(state))
                if len(summaries) >= max(1, limit):
                    break
            return summaries

    def get_job_summary(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return None
            if state.job_kind == "research":
                return summarize_research_job_state(state)
            return summarize_job_state(state)

    def subscribe(
        self, job_id: str, last_seq: int = -1
    ) -> Generator[dict[str, Any], None, None]:
        """Yield events for *job_id*, starting after *last_seq*.

        First replays history (events with seq > last_seq), then blocks
        waiting for new events until a terminal event (``done`` / ``error``)
        is received.

        If the job does not exist, yields nothing.

        The caller owns the generator and must consume or close it; the
        subscriber queue is cleaned up in the finally block.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            history = list(state.events)
            q: queue.Queue[dict[str, Any]] = queue.Queue()
            state._subscribers.append(q)

        try:
            for ev in history:
                if ev["seq"] > last_seq:
                    yield ev
                terminal = (
                    _RESEARCH_TERMINAL_STATUSES
                    if state.job_kind == "research"
                    else _INGEST_TERMINAL_STATUSES
                )
                if ev.get("status") in terminal:
                    return

            while True:
                ev = q.get()
                yield ev
                terminal = (
                    _RESEARCH_TERMINAL_STATUSES
                    if state.job_kind == "research"
                    else _INGEST_TERMINAL_STATUSES
                )
                if ev.get("status") in terminal:
                    return
        finally:
            with self._lock:
                st = self._jobs.get(job_id)
                if st is not None:
                    try:
                        st._subscribers.remove(q)
                    except ValueError:
                        pass


_INGEST_PHASE_ORDER = [
    "validation",
    "gate_hash",
    "pdf_alignment",
    "page_enumeration",
    "render",
    "stage1_ocr",
    "stage2_vision",
    "stage3_editor",
    "polyindex_toc",
    "polyindex_index",
    "time_index",
]
_GLM_INGEST_PHASE_ORDER = [
    "validation",
    "gate_hash",
    "pdf_alignment",
    "page_enumeration",
    "render",
    "stage1_glm_ocr",
    "polyindex_toc",
    "polyindex_index",
    "time_index",
]
_REPAIR_PHASE_ORDER = [
    "page_repair",
    "gaps_repair",
    "stage1_ocr",
    "stage2_vision",
    "stage3_editor",
]
_RESEARCH_DISPLAY_PHASE_ORDER = [
    "research_collect",
    "research_filter",
    "research_article",
    "research_poh_links",
    "research_timeline",
    "research_verify",
]
_RESEARCH_INTERNAL_PHASES = frozenset({"queue", "research", "pipeline"})
_PAGE_STEP_STATUSES = frozenset(
    {"page_progress", "page_skipped", "page_failed", "progress"}
)
_ACTIVITY_STATUSES = frozenset(
    {"started", "progress", "page_progress", "page_skipped", "page_failed", "waiting"}
)
_PHASE_LABELS = {
    "validation": "Validazione metadati",
    "gate_hash": "Controllo hash",
    "pdf_alignment": "Allineamento PDF",
    "page_enumeration": "Enumerazione pagine",
    "render": "Render PDF",
    "stage1_ocr": "Stage 1 — OCR",
    "stage1_glm_ocr": "Stage 1 — GLM OCR",
    "stage2_vision": "Stage 2 — Vision",
    "stage3_editor": "Stage 3 — Editor",
    "polyindex_toc": "Polyindex TOC",
    "polyindex_index": "Polyindex INDEX",
    "time_index": "Polyindex TIME_INDEX",
    "page_repair": "Preparazione riparazione",
    "gaps_repair": "Riparazione lacune",
    "subject_embeddings": "Embedding soggetti",
    "queue": "In coda",
    "research_collect": "Raccolta fonti",
    "research_filter": "Sfoltimento fonti",
    "research_article": "Generazione bozza",
    "research_poh_links": "Collegamenti POH",
    "research_timeline": "Cronologia",
    "research_verify": "Verifica",
    "research_prefilter": "Prefiltro contesto",
    "research_postprocess": "Post-process articolo",
    "research_finalize": "Revisione finale",
    "research": "Pipeline research",
    "research_batch": "Generazione batch articoli",
    "pipeline": "Pipeline",
}


def _clip_text(text: str, limit: int = 96) -> str:
    stripped = str(text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _current_activity(events: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    for ev in reversed(events):
        phase = ev.get("phase")
        status = ev.get("status")
        if not phase or phase in _RESEARCH_INTERNAL_PHASES:
            continue
        if status not in _ACTIVITY_STATUSES:
            continue
        parts: list[str] = []
        if ev.get("aligned_page") is not None:
            parts.append(f"pagina {ev['aligned_page']}")
        missing = ev.get("missing_in")
        if isinstance(missing, list) and missing:
            parts.append("lacune: " + ", ".join(str(item) for item in missing))
        if ev.get("message"):
            parts.append(str(ev["message"]))
        if ev.get("prefilter_step"):
            parts.append(str(ev["prefilter_step"]).replace("_", " "))
        label = _PHASE_LABELS.get(str(phase), str(phase))
        return str(phase), label, " · ".join(parts) if parts else None
    return None, None, None


def _extract_job_context(state: JobState) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "poh_id": None,
        "poh_label": None,
        "query": None,
        "book_title": None,
        "source_sha256": None,
        "aligned_page": None,
        "missing_in": None,
    }
    for ev in state.events:
        if ev.get("poh_label"):
            ctx["poh_label"] = str(ev["poh_label"]).strip()
        if ev.get("poh_id"):
            ctx["poh_id"] = str(ev["poh_id"]).strip()
        if ev.get("query"):
            ctx["query"] = str(ev["query"]).strip()
        for title_field in ("book_title", "titolo"):
            if ev.get(title_field):
                ctx["book_title"] = str(ev[title_field]).strip()
        if ev.get("source_sha256"):
            ctx["source_sha256"] = str(ev["source_sha256"]).strip().lower()
        if ev.get("aligned_page") is not None:
            ctx["aligned_page"] = ev["aligned_page"]
        if ev.get("missing_in"):
            ctx["missing_in"] = ev["missing_in"]
        result = ev.get("result")
        if isinstance(result, dict):
            if result.get("poh_id") and not ctx["poh_id"]:
                ctx["poh_id"] = str(result["poh_id"]).strip()
    phase, phase_label, activity_detail = _current_activity(state.events)
    ctx["current_phase"] = phase
    ctx["current_phase_label"] = phase_label
    ctx["activity_detail"] = activity_detail
    return ctx


def _is_repair_job(state: JobState) -> bool:
    return any(
        ev.get("phase") in {"page_repair", "gaps_repair"} for ev in state.events
    )


def _build_job_headline(state: JobState, ctx: dict[str, Any]) -> tuple[str, str | None, str | None]:
    poh_label = ctx.get("poh_label")
    poh_id = ctx.get("poh_id")
    query = ctx.get("query")
    activity = ctx.get("activity_detail")
    phase_label = ctx.get("current_phase_label")

    if state.job_kind == "research" or _is_research_job(state):
        if poh_label:
            title = f"Articolo: {poh_label}"
            subtitle_parts: list[str] = []
            if poh_id and poh_id != poh_label:
                subtitle_parts.append(f"POH {poh_id}")
            if query and query != poh_label:
                subtitle_parts.append(f"Ricerca: {_clip_text(query)}")
            subtitle = " · ".join(subtitle_parts) if subtitle_parts else None
        elif query:
            title = f"Research: {_clip_text(query)}"
            subtitle = f"POH {poh_id}" if poh_id else None
        else:
            title = "Generazione articolo"
            subtitle = f"POH {poh_id}" if poh_id else None
        detail = activity or phase_label
        return title, subtitle, detail

    if _is_repair_job(state):
        sha = ctx.get("source_sha256")
        sha_hint = f"{sha[:16]}…" if sha else None
        if any(ev.get("phase") == "gaps_repair" for ev in state.events):
            title = "Riparazione lacune libro"
        else:
            page = ctx.get("aligned_page")
            title = f"Riparazione pagina {page}" if page is not None else "Riparazione pagina"
        subtitle = sha_hint
        detail = activity or phase_label
        return title, subtitle, detail

    book_title = ctx.get("book_title")
    sha = ctx.get("source_sha256")
    sha_hint = f"{sha[:16]}…" if sha else None
    if book_title:
        title = f"Ingest: {_clip_text(book_title, 72)}"
        subtitle = sha_hint or "Pipeline OCR, vision e polyindex"
    elif sha_hint:
        title = "Ingestione libro"
        subtitle = sha_hint
    else:
        title = "Ingestione libro"
        subtitle = "Avvio pipeline"
    detail = activity or phase_label
    return title, subtitle, detail


def _job_public_fields(state: JobState) -> dict[str, Any]:
    ctx = _extract_job_context(state)
    title, subtitle, detail = _build_job_headline(state, ctx)
    return {
        "title": title,
        "subtitle": subtitle,
        "detail": detail,
        "poh_id": ctx.get("poh_id"),
        "poh_label": ctx.get("poh_label"),
        "query": ctx.get("query"),
        "book_title": ctx.get("book_title"),
        "source_sha256": ctx.get("source_sha256"),
        "aligned_page": ctx.get("aligned_page"),
        "current_phase": ctx.get("current_phase"),
        "current_phase_label": ctx.get("current_phase_label"),
    }


def _is_research_job(state: JobState) -> bool:
    if state.job_kind == "research":
        return True
    phases = {ev.get("phase") for ev in state.events if ev.get("phase")}
    return bool(phases.intersection(set(_RESEARCH_DISPLAY_PHASE_ORDER) | {"research_prefilter"}))


def _latest_research_plan_totals(events: list[dict[str, Any]]) -> dict[str, int]:
    for ev in reversed(events):
        if ev.get("phase") != "research" or ev.get("status") != "plan":
            continue
        totals = ev.get("phase_totals")
        if not isinstance(totals, dict):
            continue
        parsed: dict[str, int] = {}
        for phase_id, total in totals.items():
            key = str(phase_id)
            if key in _RESEARCH_DISPLAY_PHASE_ORDER:
                parsed[key] = max(1, int(total))
        if parsed:
            return parsed
    return {}


def _phase_order_for_job(state: JobState) -> list[str]:
    if _is_research_job(state):
        return _RESEARCH_DISPLAY_PHASE_ORDER
    phases = {ev.get("phase") for ev in state.events if ev.get("phase")}
    if phases.intersection({"page_repair", "gaps_repair"}):
        return _REPAIR_PHASE_ORDER
    if "stage1_glm_ocr" in phases:
        return _GLM_INGEST_PHASE_ORDER
    return _INGEST_PHASE_ORDER


def _phase_detail_from_event(ev: dict[str, Any]) -> str | None:
    phase = ev.get("phase")
    status = ev.get("status")
    if not phase:
        return None
    if status == "failed" and ev.get("message"):
        return str(ev["message"])
    if phase == "research_article" and status == "started":
        pages = ev.get("input_pages")
        books = ev.get("context_books")
        if pages is not None:
            book_part = f" · {books} libri" if books is not None else ""
            return f"{pages} pag. in contesto{book_part}"
    if phase == "research_collect" and status == "started":
        subject_pages = ev.get("subject_pages")
        time_pages = ev.get("time_pages")
        if subject_pages is not None and time_pages is not None:
            return f"{subject_pages} pag. soggetto · {time_pages} pag. temporali"
    if phase == "research_poh_links" and status == "started":
        chunk_count = ev.get("chunk_count")
        link_tasks = ev.get("link_tasks")
        if chunk_count is not None and link_tasks is not None:
            return f"{chunk_count} chunk · {link_tasks} link da applicare"
        if chunk_count is not None:
            return f"{chunk_count} chunk · discovery in corso"
    if phase == "research_timeline" and status == "started":
        candidates = ev.get("timeline_candidates")
        if candidates is not None:
            return f"{candidates} candidati cronologia"
    if status != "completed":
        return None
    if phase == "research_collect":
        subject_pages = ev.get("subject_pages")
        time_pages = ev.get("time_pages")
        loaded = ev.get("loaded_pages")
        if loaded is not None:
            return f"{loaded} pag. caricate · {subject_pages or 0}+{time_pages or 0} candidate"
    if phase == "research_filter":
        kept = ev.get("kept_pages")
        dropped = ev.get("dropped_pages", 0)
        if kept is not None:
            return f"{kept} tenute · {dropped} scartate"
    if phase == "research_article":
        if ev.get("skipped_llm"):
            return "LLM non chiamato (nessun materiale)"
        parts: list[str] = []
        if ev.get("input_pages") is not None:
            parts.append(f"{ev['input_pages']} pag.")
        if ev.get("markdown_chars") is not None:
            parts.append(f"{ev['markdown_chars']} caratteri")
        return " · ".join(parts) if parts else None
    if phase == "research_poh_links":
        if ev.get("skipped_llm"):
            return "LLM non chiamato"
        chunk_count = ev.get("chunk_count")
        link_paragraphs = ev.get("link_paragraphs")
        link_tasks = ev.get("link_tasks")
        if chunk_count is not None and link_paragraphs is not None:
            return f"{chunk_count} chunk · {link_paragraphs} paragrafi linkati"
        if chunk_count is not None and link_tasks is not None:
            return f"{chunk_count} chunk · {link_tasks} candidati"
        if chunk_count is not None:
            return f"{chunk_count} chunk elaborati"
    if phase == "research_timeline":
        if ev.get("skipped_llm"):
            return "LLM non chiamato"
        if ev.get("timeline_candidates") is not None:
            return f"{ev['timeline_candidates']} candidati temporali"
    if phase == "research_verify":
        parts: list[str] = []
        if ev.get("citations") is not None:
            parts.append(f"{ev['citations']} citazioni")
        if ev.get("poh_links") is not None:
            parts.append(f"{ev['poh_links']} POH")
        if ev.get("timeline_rows") is not None:
            parts.append(f"{ev['timeline_rows']} righe cronologia")
        if ev.get("skipped_llm"):
            parts.append("revisione LLM saltata")
        return " · ".join(parts) if parts else None
    if phase == "research_prefilter":
        pages = ev.get("pages")
        dropped = ev.get("dropped_pages", 0)
        if pages is not None:
            return f"{pages} pag. finali · {dropped} scartate nel filtro"
    if phase == "research_postprocess":
        parts: list[str] = []
        if ev.get("citations") is not None:
            parts.append(f"{ev['citations']} citazioni")
        if ev.get("poh_links") is not None:
            parts.append(f"{ev['poh_links']} POH")
        if ev.get("timeline_rows") is not None:
            parts.append(f"{ev['timeline_rows']} righe cronologia")
        return " · ".join(parts) if parts else None
    if phase == "polyindex_index":
        parts: list[str] = []
        if ev.get("n_match") is not None:
            parts.append(f"{ev['n_match']} match")
        if ev.get("n_new") is not None:
            parts.append(f"{ev['n_new']} nuovi")
        if ev.get("n_alias") is not None:
            parts.append(f"{ev['n_alias']} alias")
        return " · ".join(parts) if parts else None
    if phase == "page_enumeration" and ev.get("n_pages") is not None:
        return f"{ev['n_pages']} pagine utili"
    if phase == "pdf_alignment":
        if ev.get("skipped"):
            return "saltato"
        if ev.get("aligned_useful_pages"):
            return f"{len(ev['aligned_useful_pages'])} pagine allineate"
    if ev.get("message"):
        return str(ev["message"])
    return None


def _summarize_phases(events: list[dict[str, Any]], phase_order: list[str]) -> list[dict[str, Any]]:
    plan_totals = _latest_research_plan_totals(events)
    states: dict[str, dict[str, Any]] = {
        phase: {
            "phase": phase,
            "status": "pending",
            "done": 0,
            "total": plan_totals.get(phase, 1),
        }
        for phase in phase_order
    }
    for ev in events:
        phase = ev.get("phase")
        status = ev.get("status")
        if not phase or phase == "pipeline" or phase not in states:
            continue
        item = states[phase]
        page_total = ev.get("page_total")
        if status == "started":
            item["status"] = "active"
            item["done"] = 0
            if page_total:
                item["total"] = max(1, int(page_total))
            detail = _phase_detail_from_event(ev)
            if detail:
                item["detail"] = detail
        elif status in _PAGE_STEP_STATUSES:
            item["status"] = "active"
            item["done"] = min(item["done"] + 1, int(item["total"]))
            if page_total:
                item["total"] = max(1, int(page_total))
        elif status == "completed":
            item["status"] = "done"
            rendered = ev.get("rendered_page_count")
            if page_total:
                item["total"] = max(1, int(page_total))
                item["done"] = item["total"]
            elif rendered is not None:
                item["total"] = max(1, int(rendered))
                item["done"] = item["total"]
            elif item["done"] < item["total"]:
                item["done"] = item["total"]
            detail = _phase_detail_from_event(ev)
            if detail:
                item["detail"] = detail
        elif status == "failed":
            item["status"] = "failed"
            detail = _phase_detail_from_event(ev)
            if detail:
                item["detail"] = detail
        elif status == "waiting":
            item["status"] = "active"
            detail = _phase_detail_from_event(ev)
            if detail:
                item["detail"] = detail
            elif ev.get("message"):
                item["detail"] = str(ev["message"])
    return [states[phase] for phase in phase_order]


def summarize_job_state(state: JobState) -> dict[str, Any]:
    phase_order = _phase_order_for_job(state)
    phases = _summarize_phases(state.events, phase_order)
    public = _job_public_fields(state)
    payload: dict[str, Any] = {
        "job_id": state.job_id,
        "job_kind": state.job_kind,
        "status": state.status,
        "display_status": job_display_status(state.status, state.events),
        "display_status_label": job_display_label(
            job_display_status(state.status, state.events)
        ),
        "is_active": state.status not in _TERMINAL_STATUSES,
        "error": state.error,
        **public,
        "global_step": state.global_step,
        "global_total": state.global_total,
        "phases": phases,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "status_url": f"/api/ingest/{state.job_id}/status",
        "events_url": f"/api/ingest/{state.job_id}/events",
        "system_status_url": f"/api/system/jobs/{state.job_id}",
        "system_events_url": f"/api/system/jobs/{state.job_id}/events",
    }
    plan_totals = _latest_research_plan_totals(state.events)
    if plan_totals:
        payload["research_phase_totals"] = plan_totals
    return payload


def summarize_research_job_state(state: JobState) -> dict[str, Any]:
    payload = summarize_job_state(state)
    payload["status_url"] = f"/api/research/{state.job_id}"
    payload["events_url"] = f"/api/research/{state.job_id}/events"
    payload["system_status_url"] = f"/api/system/jobs/{state.job_id}"
    payload["system_events_url"] = f"/api/system/jobs/{state.job_id}/events"
    if state.status == "succeeded" and isinstance(state.result, dict):
        url = state.result.get("url")
        if url:
            payload["article_url"] = url
    return payload

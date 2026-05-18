from __future__ import annotations

import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Generator

_TERMINAL_STATUSES = frozenset({"done", "error"})


class JobState:
    __slots__ = (
        "job_id",
        "status",
        "events",
        "result",
        "error",
        "created_at",
        "updated_at",
        "global_total",
        "global_step",
        "_subscribers",
    )

    def __init__(self, job_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.job_id = job_id
        self.status: str = "queued"
        self.events: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = now
        self.updated_at = now
        self.global_total: int | None = None
        self.global_step: int = 0
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []


class JobRegistry:
    """Thread-safe in-memory registry of ingest jobs.

    Each job has an append-only event history and a set of active SSE
    subscribers.  Subscribers receive a replay of the history on connect
    followed by live events pushed by the worker thread.

    TODO: add TTL-based cleanup for finished jobs (e.g. remove jobs older
    than 2 h after reaching a terminal status).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobState] = {}

    def create_job(self) -> str:
        """Allocate a new job and return its opaque job_id."""
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = JobState(job_id)
        return job_id

    def set_global_total(self, job_id: str, total: int) -> None:
        """Declare the total number of countable work-steps for the job.

        Must be called once, after the useful-page count is known.  Emits a
        ``pipeline_total`` event so clients can initialise their progress bars.
        """
        with self._lock:
            state = self._jobs[job_id]
            state.global_total = total
            ev: dict[str, Any] = {
                "phase": "pipeline",
                "status": "pipeline_total",
                "global_total": total,
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
            elif status == "error":
                state.status = "error"
                state.error = ev.get("message")
            elif status == "started" and state.status == "queued":
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
            return {
                "job_id": state.job_id,
                "status": state.status,
                "global_step": state.global_step,
                "global_total": state.global_total,
                "events": list(state.events),
                "result": state.result,
                "error": state.error,
                "created_at": state.created_at,
                "updated_at": state.updated_at,
            }

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
                if ev.get("status") in _TERMINAL_STATUSES:
                    return

            while True:
                ev = q.get()
                yield ev
                if ev.get("status") in _TERMINAL_STATUSES:
                    return
        finally:
            with self._lock:
                st = self._jobs.get(job_id)
                if st is not None:
                    try:
                        st._subscribers.remove(q)
                    except ValueError:
                        pass

from __future__ import annotations

import time
from typing import Any, Callable

ProgressReporter = Callable[[dict[str, Any]], None]

PHASE_VALIDATION = "validation"
PHASE_GATE_HASH = "gate_hash"
PHASE_PDF_ALIGNMENT = "pdf_alignment"
PHASE_PAGE_ENUMERATION = "page_enumeration"
PHASE_STAGE1_OCR = "stage1_ocr"
PHASE_STAGE2_VISION = "stage2_vision"
PHASE_STAGE3_EDITOR = "stage3_editor"

STATUS_STARTED = "started"
STATUS_PROGRESS = "progress"
STATUS_PAGE_PROGRESS = "page_progress"
STATUS_PAGE_SKIPPED = "page_skipped"
STATUS_PAGE_FAILED = "page_failed"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PIPELINE_TOTAL = "pipeline_total"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def make_event(
    phase: str,
    status: str,
    *,
    counts_as_step: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    """Build a progress event dict with a consistent schema.

    Args:
        phase: Pipeline phase identifier (use PHASE_* constants).
        status: Event status (use STATUS_* constants).
        counts_as_step: If True the event will be counted as one global work
            unit when emitted through JobRegistry.emit().  Set this to True
            for per-page events in any stage and for the pdf_alignment
            completed event.
        **fields: Additional key/value pairs to include in the event.

    Returns:
        Dict ready to pass to ``ProgressReporter`` or ``JobRegistry.emit``.

    Schema example (stage1 per-page progress)::

        {
          "phase": "stage1_ocr",
          "status": "page_progress",
          "counts_as_step": true,
          "page_index": 12,
          "page_total": 33,
          "aligned_page": 47,
          "original_page": 50,
          "char_count": 1873
        }

    The ``ts``, ``seq``, ``global_step`` and ``global_total`` fields are
    injected by JobRegistry.emit() at delivery time; do not set them here.
    """
    ev: dict[str, Any] = {
        "phase": phase,
        "status": status,
        "counts_as_step": counts_as_step,
    }
    ev.update(fields)
    return ev


_PHASE_TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_DONE, STATUS_ERROR}
)


class PipelineTiming:
    def __init__(self) -> None:
        self._pipeline_start = time.monotonic()
        self._phase_started: dict[str, float] = {}
        self.phase_seconds: dict[str, float] = {}

    def elapsed_total(self) -> float:
        return time.monotonic() - self._pipeline_start

    def enrich(self, event: dict[str, Any]) -> dict[str, Any]:
        ev = dict(event)
        phase = ev.get("phase")
        status = ev.get("status")
        if phase and status == STATUS_STARTED and phase not in self._phase_started:
            self._phase_started[phase] = time.monotonic()
        elif phase and status in _PHASE_TERMINAL_STATUSES:
            start = self._phase_started.pop(phase, None)
            if start is not None:
                duration = time.monotonic() - start
                self.phase_seconds[phase] = duration
                ev["phase_duration_seconds"] = round(duration, 2)
        ev["elapsed_seconds"] = round(self.elapsed_total(), 2)
        return ev

    def summary(self) -> dict[str, Any]:
        return {
            "total_seconds": round(self.elapsed_total(), 2),
            "phases": {phase: round(sec, 2) for phase, sec in self.phase_seconds.items()},
        }


def timed_progress_reporter(
    reporter: ProgressReporter | None,
    timing: PipelineTiming,
) -> ProgressReporter | None:
    if reporter is None:
        return None

    def _emit(event: dict[str, Any]) -> None:
        reporter(timing.enrich(event))

    return _emit

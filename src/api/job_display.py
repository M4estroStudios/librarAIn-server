from __future__ import annotations

from typing import Any

_TERMINAL_STATUSES = frozenset({"done", "error", "succeeded", "failed"})
_DISPLAY_LABELS = {
    "in_coda": "In coda",
    "in_pausa": "In pausa",
    "in_corso": "In corso",
    "completato": "Completato",
    "errore": "Errore",
    "interrotto": "Interrotto",
}


def job_display_status(status: str, events: list[dict[str, Any]] | None = None) -> str:
    if status in ("done", "succeeded"):
        return "completato"
    if status in ("error", "failed"):
        return "errore"
    if status == "paused":
        return "in_pausa"
    if status == "running":
        return "in_corso"
    if status == "accepted":
        return "in_coda"
    if status == "queued":
        for ev in reversed(events or []):
            if ev.get("phase") == "queue":
                return "in_coda"
        return "in_coda"
    return "in_coda"


def job_display_label(display_status: str) -> str:
    return _DISPLAY_LABELS.get(display_status, display_status)


def flatten_job_stage_segments(job: dict[str, Any]) -> list[dict[str, str]]:
    phases = job.get("phases")
    if not isinstance(phases, list):
        return []
    segments: list[dict[str, str]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("phase") or "")
        if not phase_id or phase_id in {"pipeline", "research", "research_batch"}:
            continue
        status = str(phase.get("status") or "pending")
        if status not in {"done", "active", "failed", "pending"}:
            status = "pending"
        segments.append({"phase": phase_id, "status": status})
    return segments


def build_batch_stage_segments(child_jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    for child in child_jobs:
        segments.extend(flatten_job_stage_segments(child))
    return segments


def enrich_batch_summary(
    batch: dict[str, Any],
    child_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    del child_jobs
    return dict(batch)

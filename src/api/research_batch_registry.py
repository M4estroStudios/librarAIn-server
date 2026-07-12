from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.api.job_display import job_display_label, job_display_status


def _batch_state_path(data_root: Path) -> Path:
    return data_root / "research" / "batch_state.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchBatchRegistry:
    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        path = _batch_state_path(self._data_root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        jobs = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(jobs, dict):
            return
        restored: dict[str, dict[str, Any]] = {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            snapshot = dict(job)
            snapshot["job_id"] = str(snapshot.get("job_id") or job_id)
            if snapshot.get("status") == "running":
                snapshot["status"] = "interrupted"
                snapshot["current_poh_id"] = None
                snapshot["current_poh_label"] = None
                snapshot["current_phase"] = None
                snapshot["current_request_id"] = None
            restored[snapshot["job_id"]] = snapshot
        self._jobs = restored

    def _persist_locked(self) -> None:
        path = _batch_state_path(self._data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "jobs": self._jobs}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def recover_interrupted_from_catalog(self) -> str | None:
        with self._lock:
            if any(job.get("status") in {"interrupted", "running"} for job in self._jobs.values()):
                return None
            if any(job.get("status") == "aborted" for job in self._jobs.values()):
                return None
        from src.search.article_catalog import list_batch_scope_targets, partition_batch_targets

        targets = list_batch_scope_targets(self._data_root)
        completed, pending = partition_batch_targets(self._data_root, targets)
        if not completed or not pending:
            return None
        preview = ", ".join(
            f"{item.get('label') or item['poh_id']} ({item['poh_id']})"
            for item in pending[:5]
        )
        if len(pending) > 5:
            preview += f", +{len(pending) - 5} more"
        job_id = self.create(total=len(targets), book_sha=None, poh_ids=None)
        self.set_targets(job_id, targets)
        self.set_total(job_id, len(targets))
        for item in completed:
            self.append_generated(job_id, item)
        self.set_targets_preview(job_id, preview)
        self.finish(job_id, "interrupted")
        return job_id

    def _touch(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _utc_now()

    def create(
        self,
        *,
        total: int,
        book_sha: str | None = None,
        poh_ids: list[str] | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "total": total,
                "done": 0,
                "generated": [],
                "errors": [],
                "request_ids": [],
                "targets": [],
                "scope_book_sha": book_sha,
                "scope_poh_ids": list(poh_ids) if poh_ids else None,
                "created_at": now,
                "updated_at": now,
            }
            self._persist_locked()
        return job_id

    def set_targets(self, job_id: str, targets: list[dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["targets"] = [dict(item) for item in targets]
            self._touch(job)
            self._persist_locked()

    def set_total(self, job_id: str, total: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["total"] = total
            self._touch(job)
            self._persist_locked()

    def set_targets_preview(self, job_id: str, preview: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["targets_preview"] = preview
            self._touch(job)
            self._persist_locked()

    def set_current(
        self,
        job_id: str,
        *,
        poh_id: str | None = None,
        poh_label: str | None = None,
        current_phase: str | None = None,
        current_request_id: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["current_poh_id"] = poh_id
            job["current_poh_label"] = poh_label
            job["current_phase"] = current_phase
            if current_request_id is not None:
                job["current_request_id"] = current_request_id
            self._touch(job)
            self._persist_locked()

    def append_generated(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["generated"].append(item)
            request_id = item.get("request_id")
            if request_id:
                job["request_ids"].append(request_id)
            job["done"] = len(job["generated"]) + len(job["errors"])
            self._touch(job)
            self._persist_locked()

    def append_error(self, job_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["errors"].append(item)
            job["done"] = len(job["generated"]) + len(job["errors"])
            self._touch(job)
            self._persist_locked()

    def finish(self, job_id: str, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["current_poh_id"] = None
            job["current_poh_label"] = None
            job["current_phase"] = None
            job["current_request_id"] = None
            self._touch(job)
            self._persist_locked()

    def resume(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "interrupted":
                return False
            job["status"] = "running"
            job["current_poh_id"] = None
            job["current_poh_label"] = None
            job["current_phase"] = None
            job["current_request_id"] = None
            self._touch(job)
            self._persist_locked()
            return True

    def abort(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "interrupted":
                return False
            job["status"] = "aborted"
            job["current_poh_id"] = None
            job["current_poh_label"] = None
            job["current_phase"] = None
            job["current_request_id"] = None
            self._touch(job)
            self._persist_locked()
            return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job["status"] == "running")

    def list_active_jobs(self) -> list[dict[str, Any]]:
        return self.list_jobs(include_finished=False)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        include_finished: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda item: item["updated_at"], reverse=True)
        jobs.sort(key=lambda item: 0 if item["status"] in {"running", "interrupted"} else 1)
        summaries: list[dict[str, Any]] = []
        for job in jobs:
            if job["status"] not in {"running", "interrupted"} and not include_finished:
                continue
            if job["status"] == "aborted" and not include_finished:
                continue
            summaries.append(self._summarize_job(job))
            if len(summaries) >= max(1, limit):
                break
        return summaries

    def get_job_summary(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return self._summarize_job(job)

    def _summarize_job(self, job: dict[str, Any]) -> dict[str, Any]:
        total = max(1, int(job.get("total") or 1))
        done = int(job.get("done") or 0)
        current_label = str(job.get("current_poh_label") or "").strip()
        current_id = str(job.get("current_poh_id") or "").strip()
        preview = str(job.get("targets_preview") or "").strip()
        status = str(job.get("status") or "running")
        if status == "interrupted":
            title = f"Batch interrotto ({done}/{total})"
            subtitle = preview or None
            detail = "Riprendi per continuare dal prossimo articolo"
        elif status == "aborted":
            title = f"Batch annullato ({done}/{total})"
            subtitle = preview or None
            detail = "Batch chiuso manualmente"
        elif current_label:
            title = f"Articolo: {current_label}"
            subtitle = f"POH {current_id}" if current_id and current_id != current_label else None
            detail = f"Progresso batch {done}/{total}"
        else:
            title = f"Generazione articoli ({done}/{total})"
            subtitle = preview or None
            detail = "In attesa del prossimo POH" if done < total else None
        current_phase = job.get("current_phase")
        if current_phase and status == "running":
            phase_labels = {
                "research": "Pipeline research",
                "research_article": "Generazione bozza",
                "research_collect": "Raccolta fonti",
                "research_filter": "Sfoltimento fonti",
            }
            phase_label = phase_labels.get(str(current_phase), str(current_phase))
            detail = (detail + " · " if detail else "") + phase_label
        errors = job.get("errors") or []
        generated = job.get("generated") or []
        if errors and status not in {"running", "interrupted"}:
            detail = (detail + " · " if detail else "") + f"{len(errors)} errori"
        if status == "interrupted":
            display_status = "interrotto"
        elif status == "aborted":
            display_status = "annullato"
        else:
            display_status = job_display_status(status)
        return {
            "job_id": job["job_id"],
            "job_kind": "research_batch",
            "status": status,
            "display_status": display_status,
            "display_status_label": job_display_label(display_status),
            "is_active": status == "running",
            "resumable": status == "interrupted",
            "title": title,
            "subtitle": subtitle,
            "detail": detail,
            "poh_id": current_id or None,
            "poh_label": current_label or None,
            "global_step": done,
            "global_total": total,
            "phases": [
                {
                    "phase": "research_batch",
                    "status": "active" if done < total and status == "running" else "done",
                    "done": done,
                    "total": total,
                    "detail": f"{len(generated)} ok · {len(errors)} errori" if done else None,
                }
            ],
            "batch_generated": len(generated),
            "batch_errors": len(errors),
            "current_request_id": job.get("current_request_id"),
            "request_ids": list(job.get("request_ids") or []),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "status_url": f"/api/research/generate/status?job_id={job['job_id']}",
            "events_url": None,
            "system_status_url": f"/api/system/jobs/{job['job_id']}",
        }

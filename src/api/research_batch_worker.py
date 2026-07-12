from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from src.api.job_registry import JobRegistry
from src.api.research_batch_registry import ResearchBatchRegistry
from src.core.hashing import new_job_id
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, bind_log_context, reset_log_context
from src.models.settings import Settings
from src.persistence.research_runs import mark_research_run_running
from src.search.article_catalog import (
    generate_article_for_poh,
    partition_batch_targets,
    resolve_batch_targets,
)
from src.search.research_runner import RESEARCH_PIPELINE_VERSION, ResearchConcurrencyLimiter

RecordAccepted = Callable[..., None]
RecordSucceeded = Callable[..., None]
RecordFailed = Callable[..., None]
ResearchJobStem = Callable[..., str]


def _done_poh_ids(job: dict[str, Any]) -> set[str]:
    done: set[str] = set()
    for item in job.get("generated") or []:
        if isinstance(item, dict) and item.get("poh_id"):
            done.add(str(item["poh_id"]))
    return done


def resolve_persisted_batch_pending(
    data_root: Path,
    job: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets = job.get("targets")
    if not isinstance(targets, list) or not targets:
        targets = resolve_batch_targets(
            data_root,
            book_sha=job.get("scope_book_sha"),
            poh_ids=job.get("scope_poh_ids"),
        )
    done_poh = _done_poh_ids(job)
    remaining = [
        {"poh_id": str(item["poh_id"]), "label": str(item.get("label") or item["poh_id"])}
        for item in targets
        if str(item.get("poh_id") or "") not in done_poh
    ]
    completed, pending = partition_batch_targets(data_root, remaining)
    return completed, pending


def _preview_targets(targets: list[dict[str, Any]]) -> str:
    preview = ", ".join(
        f"{item.get('label') or item['poh_id']} ({item['poh_id']})"
        for item in targets[:5]
    )
    if len(targets) > 5:
        preview += f", +{len(targets) - 5} more"
    return preview


def run_research_batch_worker(
    job_id: str,
    *,
    data_root: Path,
    settings: Settings,
    registry: JobRegistry,
    batch_registry: ResearchBatchRegistry,
    concurrency_limiter: ResearchConcurrencyLimiter,
    record_accepted: RecordAccepted,
    record_succeeded: RecordSucceeded,
    record_failed: RecordFailed,
    research_job_stem: ResearchJobStem,
    resume: bool = False,
    book_sha: str | None = None,
    poh_ids: list[str] | None = None,
) -> None:
    try:
        if resume:
            snapshot = batch_registry.get(job_id)
            if snapshot is None:
                return
            targets = snapshot.get("targets")
            if not isinstance(targets, list) or not targets:
                targets = resolve_batch_targets(
                    data_root,
                    book_sha=snapshot.get("scope_book_sha"),
                    poh_ids=snapshot.get("scope_poh_ids"),
                )
                batch_registry.set_targets(job_id, targets)
            extra_completed, pending = resolve_persisted_batch_pending(data_root, snapshot)
            for item in extra_completed:
                batch_registry.append_generated(job_id, item)
            if not pending:
                batch_registry.finish(job_id, "succeeded")
                return
            batch_registry.set_targets_preview(job_id, _preview_targets(pending))
            Log(
                INFO_LOG_LEVEL,
                f"research batch resumed: {len(pending)} remaining",
                {
                    "job_id": job_id,
                    "remaining": len(pending),
                    "already_done": int(snapshot.get("done") or 0),
                },
            )
        else:
            targets = resolve_batch_targets(data_root, book_sha=book_sha, poh_ids=poh_ids)
            if not targets:
                batch_registry.set_total(job_id, 0)
                batch_registry.finish(job_id, "succeeded")
                return
            batch_registry.set_targets(job_id, targets)
            completed, pending = partition_batch_targets(data_root, targets)
            batch_registry.set_total(job_id, len(targets))
            for item in completed:
                batch_registry.append_generated(job_id, item)
            if not pending:
                batch_registry.finish(job_id, "succeeded")
                return
            batch_registry.set_targets_preview(job_id, _preview_targets(pending))
            Log(
                INFO_LOG_LEVEL,
                f"research batch started: {len(pending)} remaining, {len(completed)} already done",
                {
                    "job_id": job_id,
                    "total": len(targets),
                    "remaining": len(pending),
                    "already_done": len(completed),
                },
            )

        for item in pending:
            poh_id = str(item["poh_id"])
            poh_label = str(item.get("label") or poh_id)
            request_id, _started_at = new_job_id(research_job_stem(poh_id=poh_id, query=poh_label))
            batch_registry.set_current(
                job_id,
                poh_id=poh_id,
                poh_label=poh_label,
                current_phase="research",
                current_request_id=request_id,
            )
            registry.create_job(
                job_id=request_id,
                job_kind="research",
                pipeline_version=RESEARCH_PIPELINE_VERSION,
            )
            record_accepted(
                settings,
                request_id=request_id,
                query=poh_label,
                poh_id=poh_id,
                poh_label=poh_label,
            )
            registry.emit(request_id, {"phase": "queue", "status": "waiting"})
            concurrency_limiter.acquire()

            def reporter(event: dict[str, Any], *, rid: str = request_id) -> None:
                registry.emit(rid, event)

            request_token, _sha_token = bind_log_context(request_id=request_id)
            try:
                registry.emit(request_id, {"phase": "research", "status": "started"})
                registry.emit(
                    request_id,
                    {
                        "phase": "research",
                        "status": "info",
                        "query": poh_label,
                        "poh_id": poh_id,
                        "poh_label": poh_label,
                        "message": f"Generazione articolo per {poh_label}",
                    },
                )
                mark_research_run_running(settings.sqlite_path, request_id=request_id)
                Log(
                    INFO_LOG_LEVEL,
                    f"research batch item started: {poh_label} ({poh_id})",
                    {"job_id": job_id, "request_id": request_id, "poh_id": poh_id, "poh_label": poh_label},
                )
                catalog_result, research_result = generate_article_for_poh(
                    data_root,
                    poh_id,
                    settings=settings,
                    request_id=request_id,
                    reporter=reporter,
                )
                record_succeeded(settings, research_result, request_id=request_id)
                registry.emit(
                    request_id,
                    {
                        "phase": "research",
                        "status": "succeeded",
                        "result": {
                            "poh_id": poh_id,
                            "url": catalog_result["url"],
                            "skipped_llm": catalog_result.get("skipped_llm"),
                            "no_material": catalog_result.get("no_material"),
                        },
                    },
                )
                batch_registry.append_generated(job_id, catalog_result)
            except Exception as exc:
                record_failed(settings, request_id=request_id, last_error=str(exc))
                registry.emit(
                    request_id,
                    {"phase": "research", "status": "failed", "message": str(exc)},
                )
                batch_registry.append_error(
                    job_id,
                    {"poh_id": poh_id, "request_id": request_id, "error": str(exc)},
                )
                Log(
                    ERROR_LOG_LEVEL,
                    f"research batch item failed: {poh_label} ({poh_id})",
                    {
                        "job_id": job_id,
                        "request_id": request_id,
                        "poh_id": poh_id,
                        "poh_label": poh_label,
                        "error": str(exc),
                    },
                )
            finally:
                reset_log_context(request_token, None)
                concurrency_limiter.release()

        snapshot = batch_registry.get(job_id)
        errors = len(snapshot["errors"]) if snapshot else 0
        status = "failed" if errors else "succeeded"
        batch_registry.finish(job_id, status)
    except Exception as exc:
        Log(ERROR_LOG_LEVEL, "research batch worker failed", {"job_id": job_id, "error": str(exc)})
        batch_registry.append_error(job_id, {"error": str(exc)})
        batch_registry.finish(job_id, "failed")


def spawn_research_batch_worker(job_id: str, **kwargs: Any) -> None:
    resume = bool(kwargs.pop("resume", False))
    threading.Thread(
        target=run_research_batch_worker,
        kwargs={"job_id": job_id, **kwargs, "resume": resume},
        daemon=True,
        name=f"research-batch-{'resume' if resume else 'new'}-{job_id[:8]}",
    ).start()

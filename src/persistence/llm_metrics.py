from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from src.persistence.pipeline_runs import _sqlite_connection

_READY_PATHS: set[str] = set()
_READY_LOCK = threading.Lock()
_SOURCE_CACHE: dict[tuple[str, str], str | None] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def ensure_llm_call_metrics_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_call_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            source_sha256 TEXT,
            stage TEXT NOT NULL,
            operation TEXT NOT NULL,
            compute_mode TEXT NOT NULL,
            requested_compute_mode TEXT NOT NULL,
            model TEXT NOT NULL,
            unit_index INTEGER,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            input_items INTEGER,
            input_image_count INTEGER,
            input_chars INTEGER,
            output_chars INTEGER,
            batch_size INTEGER,
            result_count INTEGER,
            output_dimensions INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            max_tokens INTEGER,
            temperature REAL,
            reasoning_effort TEXT,
            reasoning_enable_thinking INTEGER,
            response_id TEXT,
            error_type TEXT,
            error_message TEXT,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_metrics_group "
        "ON llm_call_metrics(stage, compute_mode, model, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_metrics_request "
        "ON llm_call_metrics(request_id, source_sha256)"
    )


def record_llm_call_metric(
    sqlite_path: str | None,
    *,
    call_id: str,
    request_id: str,
    source_sha256: str | None,
    stage: str,
    operation: str,
    compute_mode: str,
    requested_compute_mode: str,
    model: str,
    unit_index: int | None,
    attempt: int,
    status: str,
    started_at: str,
    finished_at: str,
    latency_ms: float,
    input_items: int | None = None,
    input_image_count: int | None = None,
    input_chars: int | None = None,
    output_chars: int | None = None,
    batch_size: int | None = None,
    result_count: int | None = None,
    output_dimensions: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    reasoning_enable_thinking: bool | None = None,
    response_id: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Persist one provider attempt without allowing telemetry to break the job."""
    if not sqlite_path:
        return False
    try:
        normalized_path = str(sqlite_path)
        with _READY_LOCK:
            if normalized_path not in _READY_PATHS:
                with _sqlite_connection(normalized_path) as conn:
                    ensure_llm_call_metrics_table(conn)
                _READY_PATHS.add(normalized_path)
        if not source_sha256:
            cache_key = (normalized_path, str(request_id))
            if cache_key not in _SOURCE_CACHE:
                try:
                    with _sqlite_connection(normalized_path) as conn:
                        source_row = conn.execute(
                            """
                            SELECT source_sha256 FROM pipeline_runs WHERE request_id = ?
                            UNION ALL
                            SELECT source_sha256 FROM biblio_stage_runs WHERE request_id = ?
                            LIMIT 1
                            """,
                            (request_id, request_id),
                        ).fetchone()
                except sqlite3.Error:
                    source_row = None
                _SOURCE_CACHE[cache_key] = (
                    str(source_row[0]) if source_row and source_row[0] else None
                )
            source_sha256 = _SOURCE_CACHE[cache_key]
        with _sqlite_connection(sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO llm_call_metrics (
                    call_id, request_id, source_sha256, stage, operation,
                    compute_mode, requested_compute_mode, model, unit_index,
                    attempt, status, started_at, finished_at, latency_ms,
                    input_items, input_image_count, input_chars, output_chars,
                    batch_size, result_count, output_dimensions,
                    prompt_tokens, completion_tokens, total_tokens,
                    max_tokens, temperature, reasoning_effort,
                    reasoning_enable_thinking, response_id, error_type,
                    error_message, metadata_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    str(call_id),
                    str(request_id),
                    str(source_sha256) if source_sha256 else None,
                    str(stage),
                    str(operation),
                    str(compute_mode),
                    str(requested_compute_mode),
                    str(model),
                    unit_index,
                    int(attempt),
                    str(status),
                    str(started_at),
                    str(finished_at),
                    max(0.0, float(latency_ms)),
                    input_items,
                    input_image_count,
                    input_chars,
                    output_chars,
                    batch_size,
                    result_count,
                    output_dimensions,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    max_tokens,
                    temperature,
                    reasoning_effort,
                    int(reasoning_enable_thinking)
                    if reasoning_enable_thinking is not None
                    else None,
                    response_id,
                    error_type,
                    str(error_message)[:1000] if error_message else None,
                    _json_dumps(metadata),
                ),
            )
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def _decode_metric_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("metadata_json", None)
    metadata: dict[str, Any] | None = None
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    data["metadata"] = metadata
    if data.get("reasoning_enable_thinking") is not None:
        data["reasoning_enable_thinking"] = bool(data["reasoning_enable_thinking"])
    return data


def list_llm_call_metrics(
    sqlite_path: str,
    *,
    request_id: str | None = None,
    source_sha256: str | None = None,
    stage: str | None = None,
    compute_mode: str | None = None,
    model: str | None = None,
    limit: int = 50000,
) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 100000))
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("request_id", request_id),
        ("source_sha256", source_sha256),
        ("stage", stage),
        ("compute_mode", compute_mode),
        ("model", model),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with _sqlite_connection(sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM llm_call_metrics {where} "
                "ORDER BY started_at ASC, id ASC LIMIT ?",
                (*params, cap),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_decode_metric_row(row) for row in rows]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 2)
    weight = index - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * weight
    return round(value, 2)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _wall_span_ms(rows: list[dict[str, Any]]) -> float | None:
    starts: list[datetime] = []
    finishes: list[datetime] = []
    for row in rows:
        try:
            starts.append(datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00")))
            finishes.append(datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            continue
    if not starts or not finishes:
        return None
    return round((max(finishes) - min(starts)).total_seconds() * 1000.0, 2)


def _sum_request_wall_spans(rows: list[dict[str, Any]]) -> float | None:
    """Add elapsed spans per request, without counting idle time between runs."""
    by_request: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_request.setdefault(str(row.get("request_id") or ""), []).append(row)
    spans = [
        span
        for request_rows in by_request.values()
        if (span := _wall_span_ms(request_rows)) is not None
    ]
    return round(sum(spans), 2) if spans else None


def _sum_int(rows: list[dict[str, Any]], key: str) -> int | None:
    values = [int(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return sum(values) if values else None


def aggregate_llm_call_metrics(
    sqlite_path: str,
    *,
    request_id: str | None = None,
    source_sha256: str | None = None,
    stage: str | None = None,
    compute_mode: str | None = None,
    model: str | None = None,
    limit: int = 50000,
) -> list[dict[str, Any]]:
    rows = list_llm_call_metrics(
        sqlite_path,
        request_id=request_id,
        source_sha256=source_sha256,
        stage=stage,
        compute_mode=compute_mode,
        model=model,
        limit=limit,
    )
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("stage") or ""),
            str(row.get("operation") or ""),
            str(row.get("compute_mode") or ""),
            str(row.get("model") or ""),
        )
        groups.setdefault(key, []).append(row)

    result: list[dict[str, Any]] = []
    for (group_stage, operation, mode, group_model), group_rows in groups.items():
        by_call: dict[str, list[dict[str, Any]]] = {}
        for row in group_rows:
            by_call.setdefault(str(row.get("call_id") or row.get("id")), []).append(row)
        attempts_per_call = [len(call_rows) for call_rows in by_call.values()]
        successful_call_rows = [
            next(
                (
                    row
                    for row in reversed(call_rows)
                    if row.get("status") == "success"
                ),
                call_rows[-1],
            )
            for call_rows in by_call.values()
            if call_rows
        ]
        successful_calls = sum(
            1 for call_rows in by_call.values() if any(row.get("status") == "success" for row in call_rows)
        )
        failed_calls = len(by_call) - successful_calls
        latencies = [
            float(row["latency_ms"])
            for row in group_rows
            if isinstance(row.get("latency_ms"), (int, float))
        ]
        success_latencies = [
            float(row["latency_ms"])
            for row in group_rows
            if row.get("status") == "success"
            and isinstance(row.get("latency_ms"), (int, float))
        ]
        output_chars = _sum_int(successful_call_rows, "output_chars")
        completion_tokens = _sum_int(successful_call_rows, "completion_tokens")
        total_tokens = _sum_int(successful_call_rows, "total_tokens")
        successful_attempts = sum(1 for row in group_rows if row.get("status") == "success")
        failed_attempts = len(group_rows) - successful_attempts
        total_latency_seconds = sum(latencies) / 1000.0 if latencies else 0.0
        wall_span_ms = _sum_request_wall_spans(group_rows)
        page_keys = {
            (str(row.get("request_id") or ""), int(row["unit_index"]))
            for row in successful_call_rows
            if isinstance(row.get("unit_index"), (int, float))
            and int(row["unit_index"]) > 0
        }
        pages_processed = len(page_keys) or None
        if operation == "embedding":
            units_processed = _sum_int(successful_call_rows, "result_count")
            if units_processed is None:
                units_processed = _sum_int(successful_call_rows, "batch_size")
        else:
            units_processed = len(successful_call_rows) or None
        item: dict[str, Any] = {
            "stage": group_stage,
            "operation": operation,
            "compute_mode": mode,
            "model": group_model,
            "requested_compute_modes": sorted(
                {str(row.get("requested_compute_mode") or "") for row in group_rows}
            ),
            "logical_calls": len(by_call),
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "attempts": len(group_rows),
            "retry_calls": sum(1 for count in attempts_per_call if count > 1),
            "retry_rate": round(
                sum(1 for count in attempts_per_call if count > 1) / len(by_call), 4
            )
            if by_call
            else None,
            "success_rate": round(successful_calls / len(by_call), 4) if by_call else None,
            "attempt_success_rate": round(successful_attempts / len(group_rows), 4)
            if group_rows
            else None,
            "failed_attempts": failed_attempts,
            "latency_ms_avg": _mean(latencies),
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "successful_latency_ms_avg": _mean(success_latencies),
            "successful_latency_ms_p50": _percentile(success_latencies, 0.50),
            "successful_latency_ms_p95": _percentile(success_latencies, 0.95),
            "input_chars": _sum_int(group_rows, "input_chars"),
            "output_chars": output_chars,
            "prompt_tokens": _sum_int(group_rows, "prompt_tokens"),
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "batch_items": _sum_int(group_rows, "batch_size"),
            "result_items": _sum_int(group_rows, "result_count"),
            "output_dimensions": next(
                (
                    int(row["output_dimensions"])
                    for row in group_rows
                    if isinstance(row.get("output_dimensions"), (int, float))
                ),
                None,
            ),
            "output_chars_per_second": (
                round(output_chars / total_latency_seconds, 2)
                if output_chars is not None and total_latency_seconds > 0
                else None
            ),
            "completion_tokens_per_second": (
                round(completion_tokens / total_latency_seconds, 2)
                if completion_tokens is not None and total_latency_seconds > 0
                else None
            ),
            "successful_attempts": successful_attempts,
            "wall_span_ms": wall_span_ms,
            "pages_processed": pages_processed,
            "pages_per_second": (
                round(pages_processed / (wall_span_ms / 1000.0), 4)
                if pages_processed is not None and wall_span_ms and wall_span_ms > 0
                else None
            ),
            "units_processed": units_processed,
            "units_per_second": (
                round(units_processed / (wall_span_ms / 1000.0), 4)
                if units_processed is not None and wall_span_ms and wall_span_ms > 0
                else None
            ),
            "logical_calls_per_second": (
                round(len(by_call) / (wall_span_ms / 1000.0), 4)
                if wall_span_ms is not None and wall_span_ms > 0
                else None
            ),
        }
        result.append(item)
    result.sort(key=lambda item: (str(item["stage"]), str(item["compute_mode"]), str(item["model"])))
    return result


def _legacy_stage_model(settings: Any, stage: str, compute_mode: str) -> str:
    role_attrs = {
        "stage1_glm_ocr": ("ocrvision_model", "ocrvision_cloud_model", "glm_ocr_model"),
        "stage2_vision": ("vision_model", "vision_cloud_model", "vision_model"),
        "stage3_editor": ("editor_model", "editor_cloud_model", "editor_model"),
    }
    attrs = role_attrs.get(stage)
    if attrs is not None and settings is not None:
        attr = attrs[1] if compute_mode == "cloud" else attrs[0]
        value = getattr(settings, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        fallback = getattr(settings, attrs[2], None)
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
    return "modello storico non registrato"


def aggregate_legacy_pipeline_stage_metrics(
    sqlite_path: str,
    *,
    settings: Any = None,
    limit: int = 50000,
) -> list[dict[str, Any]]:
    """Adapt old pipeline timing rows to the same stage/mode table shape.

    Old runs predate per-call telemetry.  They are useful for wall-time and
    pages/s only when a mode was explicitly backfilled; token and percentile
    fields remain unavailable instead of being guessed.
    """
    from src.persistence.pipeline_runs import list_pipeline_runs

    try:
        runs = list_pipeline_runs(sqlite_path, limit=limit)
    except Exception:
        return []
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        compute_mode = run.get("compute_mode")
        if compute_mode not in {"local", "cloud"}:
            continue
        timing = run.get("timing")
        phases = timing.get("phases") if isinstance(timing, dict) else None
        if not isinstance(phases, dict):
            continue
        total_pages = run.get("total_pages") or run.get("page_count") or 0
        completed_pages = run.get("succeeded_pages") or total_pages
        try:
            total_pages_i = max(0, int(total_pages))
            completed_pages_i = max(0, int(completed_pages))
        except (TypeError, ValueError):
            continue
        for stage, raw_seconds in phases.items():
            if not str(stage).startswith("stage"):
                continue
            try:
                seconds = float(raw_seconds)
            except (TypeError, ValueError):
                continue
            if seconds <= 0 or completed_pages_i <= 0:
                continue
            stage_name = str(stage)
            model = _legacy_stage_model(settings, stage_name, str(compute_mode))
            key = (stage_name, str(compute_mode), model)
            group = groups.setdefault(
                key,
                {
                    "stage": stage_name,
                    "operation": "pipeline_stage",
                    "compute_mode": str(compute_mode),
                    "model": model,
                    "requested_compute_modes": [str(compute_mode)],
                    "logical_calls": 0,
                    "successful_calls": 0,
                    "failed_calls": 0,
                    "attempts": 0,
                    "retry_calls": 0,
                    "retry_rate": None,
                    "success_rate": None,
                    "attempt_success_rate": None,
                    "failed_attempts": 0,
                    "latency_ms_avg": None,
                    "latency_ms_p50": None,
                    "latency_ms_p95": None,
                    "successful_latency_ms_avg": None,
                    "successful_latency_ms_p50": None,
                    "successful_latency_ms_p95": None,
                    "input_chars": None,
                    "output_chars": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "batch_items": None,
                    "result_items": None,
                    "output_dimensions": None,
                    "output_chars_per_second": None,
                    "completion_tokens_per_second": None,
                    "successful_attempts": 0,
                    "wall_span_ms": 0.0,
                    "pages_processed": 0,
                    "pages_per_second": None,
                    "units_processed": 0,
                    "units_per_second": None,
                    "legacy": True,
                    "legacy_cached": False,
                    "legacy_runs": 0,
                },
            )
            group["legacy_runs"] += 1
            group["logical_calls"] += total_pages_i or completed_pages_i
            group["successful_calls"] += min(completed_pages_i, total_pages_i or completed_pages_i)
            group["attempts"] += 1
            group["wall_span_ms"] += seconds * 1000.0
            group["pages_processed"] += completed_pages_i
            group["units_processed"] += completed_pages_i
            # Sub-second timings for hundreds of pages are cache timings, not
            # model throughput. Keep the row visible but do not publish a
            # fabricated pages/s value.
            if completed_pages_i >= 10 and seconds < 10.0:
                group["legacy_cached"] = True

    result: list[dict[str, Any]] = []
    for group in groups.values():
        wall_span_ms = float(group["wall_span_ms"])
        pages = int(group["pages_processed"])
        if not group["legacy_cached"] and wall_span_ms > 0:
            group["pages_per_second"] = round(pages / (wall_span_ms / 1000.0), 4)
            group["units_per_second"] = group["pages_per_second"]
        group["wall_span_ms"] = round(wall_span_ms, 2)
        group["success_rate"] = round(
            group["successful_calls"] / group["logical_calls"], 4
        ) if group["logical_calls"] else None
        result.append(group)
    result.sort(key=lambda item: (str(item["stage"]), str(item["compute_mode"]), str(item["model"])))
    return result

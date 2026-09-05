"""Print ingest timing + LLM metrics from SQLite (read-only diagnosis).

Usage:
  python -m scripts.summarize_ingest_perf [--data-root data] [--limit 10]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _fmt_seconds(value: object) -> str:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "-"
    if seconds >= 3600:
        return f"{seconds / 3600:.2f}h ({seconds:.1f}s)"
    if seconds >= 60:
        return f"{seconds / 60:.1f}min ({seconds:.1f}s)"
    return f"{seconds:.2f}s"


def _print_run(run: dict[str, Any]) -> None:
    timing = run.get("timing") if isinstance(run.get("timing"), dict) else {}
    phases = timing.get("phases") if isinstance(timing.get("phases"), dict) else {}
    sha = str(run.get("source_sha256") or "")[:16]
    title = run.get("book_title") or "-"
    print(
        f"  request_id={run.get('request_id')}  sha={sha}…  "
        f"status={run.get('status')}  pages={run.get('total_pages')}  "
        f"mode={run.get('compute_mode')}  title={title}"
    )
    print(
        f"    wall={_fmt_seconds(timing.get('total_seconds'))}  "
        f"started={run.get('started_at')}  finished={run.get('finished_at')}"
    )
    if not phases:
        print("    phases: (nessun timing.phases — run pre-strumentazione o skip)")
        return
    ranked = sorted(phases.items(), key=lambda item: float(item[1] or 0), reverse=True)
    for name, seconds in ranked:
        print(f"    {name:24} {_fmt_seconds(seconds)}")


def _print_llm_group(group: dict[str, Any]) -> None:
    wall = group.get("wall_span_ms")
    wall_s = _fmt_seconds(wall / 1000.0) if isinstance(wall, (int, float)) else "-"
    print(
        f"  {group.get('stage')}  op={group.get('operation')}  "
        f"mode={group.get('compute_mode')}  model={group.get('model')}"
    )
    print(
        f"    calls={group.get('logical_calls')} ok={group.get('successful_calls')}  "
        f"retry_rate={group.get('retry_rate')}  "
        f"p50={group.get('successful_latency_ms_p50')}ms  "
        f"p95={group.get('successful_latency_ms_p95')}ms  "
        f"pages/s={group.get('pages_per_second')}  wall={wall_s}"
    )
    if group.get("legacy"):
        print("    (legacy pipeline_runs timing, no per-call tokens)")


def _estimate(pages: int, parallel: int, rate: int) -> None:
    vision_calls = pages
    editor_calls = pages
    llm_calls = vision_calls + editor_calls
    rate_floor_min = llm_calls / rate if rate else 0.0
    print("  Stima ordine di grandezza (classic, cache fredda, 1 call/pagina/stage):")
    print(f"    pagine={pages}  MAX_PARALLEL={parallel}  RATE_LIMIT={rate}/min")
    print(f"    LLM ingest ≈ {llm_calls} (vision {vision_calls} + editor {editor_calls})")
    if rate:
        print(f"    floor rate-limit ≈ {rate_floor_min:.1f} min (se il modello è più veloce del bucket)")
    print(
        f"    se vision=20s e editor=10s @ P={parallel}: "
        f"≈ {(pages / parallel * 20 + pages / parallel * 10) / 60:.0f} min solo LLM"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Riassunto timing ingest + metriche LLM")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--sqlite", default=None, help="Override path SQLite")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--estimate-pages",
        type=int,
        default=798,
        help="Pagine per la stima statica (default: libro campione 798)",
    )
    args = parser.parse_args()

    from src.core.config import load_settings
    from src.persistence.llm_metrics import aggregate_llm_call_metrics
    from src.persistence.pipeline_runs import list_pipeline_runs

    settings = load_settings()
    sqlite_path = args.sqlite or settings.sqlite_path
    print(f"sqlite={sqlite_path}")
    print(f"MAX_PARALLEL_REQUEST={settings.max_parallel_request}")
    print(f"RATE_LIMIT_PER_MINUTE={settings.rate_limit_per_minute}")
    print(f"TIME_INDEX_USE_LLM={settings.time_index_use_llm}")
    print()
    _estimate(args.estimate_pages, settings.max_parallel_request, settings.rate_limit_per_minute)
    print()

    db = Path(sqlite_path)
    if not db.is_file():
        print(f"Nessun database in {sqlite_path}. Avvia un ingest e rilancia.")
        return 0

    print("== pipeline_runs (più recenti) ==")
    try:
        runs = list_pipeline_runs(sqlite_path, limit=args.limit)
    except Exception as exc:
        print(f"  errore lettura pipeline_runs: {exc}")
        runs = []
    if not runs:
        print("  (vuoto)")
    for run in runs:
        _print_run(run)
        print()

    print("== llm_call_metrics (aggregati) ==")
    try:
        groups = aggregate_llm_call_metrics(sqlite_path, settings=settings)
    except Exception as exc:
        print(f"  errore lettura llm_call_metrics: {exc}")
        groups = []
    if not groups:
        print("  (vuoto — ingest senza telemetry o DB nuovo)")
    for group in groups:
        _print_llm_group(group)
    print()
    print("Dettaglio: docs/perf-ingest-analysis.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

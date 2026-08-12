from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src.api.biblio_handlers import _book_output_from_disk, _run_async
from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import build_openai_client
from src.ingestion.index_builder import build_index_md
from src.ingestion.index_cross_links import (
    allocate_subject_keys,
    apply_index_cross_links,
    audit_index_cross_links_readiness,
    book_index_json_path,
    _subjects_by_content_page,
)
from src.ingestion.index_md_links import build_first_index_source_page_by_label
from src.ingestion.polyindex.index_md_parser import parse_index_md
from src.ingestion.progress import PHASE_POLYINDEX_INDEX, ProgressReporter, make_event
from src.models.settings import Settings

PREFLIGHT_PHASE_TIMEOUT_SECONDS = 600
PREFLIGHT_SANDBOX_MAX_SUBJECTS = 75
_INDEX_CROSS_LINKS_TEST_MODULE = "tests/test_index_cross_links.py"


class IndexCrossLinksPreflightError(Exception):
    pass


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _phase_result(
    name: str,
    *,
    ok: bool,
    seconds: float,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "ok": ok,
        "seconds": round(seconds, 3),
        "message": message,
    }
    payload.update(extra)
    return payload


def _load_book_context(data_root: Path, source_sha256: str) -> dict[str, Any]:
    from src.api.biblio_polyindex_jobs import _load_book_context

    return _load_book_context(data_root, source_sha256)


def _resolve_index_md_path(ctx: dict[str, Any]) -> Path:
    book_output = ctx["book_output"]
    index_md = book_output.output_dir / "INDEX.md"
    if index_md.is_file():
        return index_md
    built = build_index_md(book_output, ctx["useful"])
    if built.is_file():
        return built
    raise IndexCrossLinksPreflightError("INDEX.md missing and build_index_md produced no file")


def _collect_aligned_pages_for_probe(
    index_md_path: Path,
    book_output,
    useful_pages,
    *,
    max_subjects: int,
) -> set[int]:
    all_subjects = [
        subject
        for subject in parse_index_md(index_md_path, useful_pages)
        if subject.aligned_pages
    ]
    probe_subjects = all_subjects[:max_subjects]
    index_page_set = useful_pages.index_range_aligned.as_set()
    first_index_page = build_first_index_source_page_by_label(book_output, useful_pages)
    subject_keys = allocate_subject_keys(probe_subjects)
    by_page = _subjects_by_content_page(
        subject_keys,
        first_index_page,
        book_output.slug,
        index_page_set,
    )
    aligned_pages = set(index_page_set)
    aligned_pages.update(by_page.keys())
    return aligned_pages


def _prepare_sandbox_book(
    data_root: Path,
    ctx: dict[str, Any],
    index_md_path: Path,
    sandbox_root: Path,
    *,
    max_subjects: int,
) -> tuple[Path, set[int]]:
    book_output = ctx["book_output"]
    sha = ctx["sha"]
    dest_dir = sandbox_root / "output" / sha
    pages_dest = dest_dir / "pages"
    pages_dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(book_output.manifest_path, dest_dir / "manifest.json")
    shutil.copy2(index_md_path, dest_dir / "INDEX.md")
    index_json = book_index_json_path(book_output.output_dir, book_output.slug)
    if index_json.is_file():
        shutil.copy2(index_json, dest_dir / index_json.name)
    aligned_pages = _collect_aligned_pages_for_probe(
        index_md_path,
        book_output,
        ctx["useful"],
        max_subjects=max_subjects,
    )
    pages_by_aligned = {page.aligned: page for page in book_output.pages}
    copied = 0
    for aligned in sorted(aligned_pages):
        page = pages_by_aligned.get(aligned)
        if page is None or not page.file.is_file():
            continue
        shutil.copy2(page.file, pages_dest / page.file.name)
        copied += 1
    if copied < 1:
        raise IndexCrossLinksPreflightError("sandbox probe: no page files copied")
    return dest_dir, aligned_pages


def _run_unit_tests_phase(timeout_seconds: int) -> dict[str, Any]:
    start = time.perf_counter()
    test_path = _project_root() / _INDEX_CROSS_LINKS_TEST_MODULE
    if not test_path.is_file():
        return _phase_result(
            "unit_tests",
            ok=False,
            seconds=time.perf_counter() - start,
            message=f"missing test module: {test_path}",
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=no"],
            cwd=str(_project_root()),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _phase_result(
            "unit_tests",
            ok=False,
            seconds=time.perf_counter() - start,
            message=f"pytest timed out after {timeout_seconds}s",
        )
    ok = completed.returncode == 0
    tail = (completed.stdout or completed.stderr or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit code {completed.returncode}"
    return _phase_result(
        "unit_tests",
        ok=ok,
        seconds=time.perf_counter() - start,
        message=summary,
        exit_code=completed.returncode,
    )


async def _run_sandbox_probe_phase(
    data_root: Path,
    ctx: dict[str, Any],
    index_md_path: Path,
    *,
    client: Any | None,
    settings: Settings,
    request_id: str,
    max_subjects: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    start = time.perf_counter()
    sandbox_parent = data_root / "tmp" / ctx["sha"] / "index_cross_links_preflight"
    sandbox_parent.mkdir(parents=True, exist_ok=True)
    sandbox_root = sandbox_parent / f"run-{int(time.time())}"
    try:
        dest_dir, _ = _prepare_sandbox_book(
            data_root,
            ctx,
            index_md_path,
            sandbox_root,
            max_subjects=max_subjects,
        )
        sandbox_book = _book_output_from_disk(sandbox_root, ctx["sha"])
        sandbox_index = dest_dir / "INDEX.md"
        stats = await apply_index_cross_links(
            sandbox_index,
            sandbox_book,
            ctx["useful"],
            client=client,
            settings=settings,
            request_id=request_id or "preflight-sandbox",
            max_subjects=max_subjects,
            parallel_pages=True,
        )
        elapsed = time.perf_counter() - start
        if elapsed > timeout_seconds:
            return _phase_result(
                "sandbox_probe",
                ok=False,
                seconds=elapsed,
                message=f"sandbox probe exceeded {timeout_seconds}s",
                stats=stats,
            )
        subjects = int(stats.get("subjects") or 0)
        regex_links = int(stats.get("regex_links") or 0)
        llm_links = int(stats.get("llm_links") or 0)
        parallel_pages = int(stats.get("parallel_pages") or 0)
        pages_updated = int(stats.get("pages_updated") or 0)
        index_pages_updated = int(stats.get("index_pages_updated") or 0)
        index_lines_linked = int(stats.get("index_lines_linked") or 0)
        issues: list[str] = []
        if subjects < 1:
            issues.append("no subjects processed")
        work_done = (
            regex_links + llm_links > 0
            or pages_updated > 0
            or index_pages_updated > 0
            or index_lines_linked > 0
        )
        if not work_done:
            issues.append("no linking work produced")
        if parallel_pages != 1:
            issues.append("parallel_pages not enabled")
        ok = not issues
        message = "sandbox subset run ok" if ok else "; ".join(issues)
        return _phase_result(
            "sandbox_probe",
            ok=ok,
            seconds=elapsed,
            message=message,
            stats=stats,
            reversible=True,
            sandbox_path=str(sandbox_root),
        )
    except Exception as exc:
        return _phase_result(
            "sandbox_probe",
            ok=False,
            seconds=time.perf_counter() - start,
            message=str(exc),
            reversible=True,
        )
    finally:
        if sandbox_root.is_dir():
            shutil.rmtree(sandbox_root, ignore_errors=True)


async def run_index_cross_links_preflight(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: Any | None = None,
    request_id: str = "",
    progress: ProgressReporter | None = None,
    sandbox_max_subjects: int = PREFLIGHT_SANDBOX_MAX_SUBJECTS,
    phase_timeout_seconds: int = PREFLIGHT_PHASE_TIMEOUT_SECONDS,
    run_unit_tests: bool = True,
    run_sandbox_probe: bool = True,
) -> dict[str, Any]:
    def emit(message: str) -> None:
        if progress is None:
            return
        progress(
            make_event(
                PHASE_POLYINDEX_INDEX,
                "preflight",
                message=message,
            )
        )

    phases: dict[str, dict[str, Any]] = {}
    ctx = _load_book_context(data_root, source_sha256)
    index_md_path = _resolve_index_md_path(ctx)

    emit("Preflight statico INDEX cross-links")
    static_start = time.perf_counter()
    static_issues: list[str] = []
    try:
        build_openai_client(settings)
    except Exception as exc:
        static_issues.append(f"openai client: {exc}")
    if settings.max_parallel_request < 1:
        static_issues.append("max_parallel_request must be >= 1")
    audit = audit_index_cross_links_readiness(
        index_md_path,
        ctx["book_output"],
        ctx["useful"],
    )
    if not audit["ok"]:
        static_issues.extend(audit.get("issues") or [])
    phases["static"] = _phase_result(
        "static",
        ok=not static_issues,
        seconds=time.perf_counter() - static_start,
        message="static checks ok" if not static_issues else "; ".join(static_issues),
        audit=audit,
        max_parallel_request=settings.max_parallel_request,
        parallel_pages_default=True,
        refine_index_not_probed=True,
    )

    if run_unit_tests:
        emit("Preflight unit tests index_cross_links")
        phases["unit_tests"] = _run_unit_tests_phase(phase_timeout_seconds)

    if run_sandbox_probe and phases.get("static", {}).get("ok"):
        emit(f"Preflight sandbox ({sandbox_max_subjects} soggetti, reversibile)")
        openai_client = client or build_openai_client(settings)
        phases["sandbox_probe"] = await _run_sandbox_probe_phase(
            data_root,
            ctx,
            index_md_path,
            client=openai_client,
            settings=settings,
            request_id=request_id,
            max_subjects=sandbox_max_subjects,
            timeout_seconds=phase_timeout_seconds,
        )
    elif run_sandbox_probe:
        phases["sandbox_probe"] = _phase_result(
            "sandbox_probe",
            ok=False,
            seconds=0.0,
            message="skipped: static preflight failed",
            skipped=True,
        )

    ok = all(phase.get("ok") for phase in phases.values())
    failed = [name for name, phase in phases.items() if not phase.get("ok")]
    message = "index cross links preflight ok" if ok else f"failed phases: {', '.join(failed)}"
    result = {
        "ok": ok,
        "message": message,
        "reversible": True,
        "phases": phases,
        "full_run_ready": ok,
        "subjects_with_aligned_pages": audit.get("stats", {}).get("subjects_with_aligned_pages"),
    }
    Log(
        INFO_LOG_LEVEL if ok else WARNING_LOG_LEVEL,
        "index cross links preflight completed",
        {"request_id": request_id, "ok": ok, "failed_phases": failed},
    )
    return result


def ensure_index_cross_links_preflight(
    data_root: Path,
    settings: Settings,
    source_sha256: str,
    *,
    client: Any | None = None,
    request_id: str = "",
    progress: ProgressReporter | None = None,
    **preflight_kwargs: Any,
) -> dict[str, Any]:
    result = _run_async(
        run_index_cross_links_preflight(
            data_root,
            settings,
            source_sha256,
            client=client,
            request_id=request_id,
            progress=progress,
            **preflight_kwargs,
        )
    )
    if not result.get("ok"):
        raise IndexCrossLinksPreflightError(str(result.get("message") or "preflight failed"))
    return result

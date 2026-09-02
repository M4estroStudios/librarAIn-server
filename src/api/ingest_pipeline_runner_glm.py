from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from src.api.ingest_pipeline_runner import (
    _emit,
    _emit_error,
    _extract_validation_error,
    build_pipeline_skipped_payload,
    persist_pipeline_timing,
)
from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.orchestrator import NullOrchestratorRegistry, OrchestratorStageError, run_pipeline
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import maybe_run_pdf_alignment
from src.ingestion.pipeline.engine import require_gpu_vram_at_pipeline_start
from src.ingestion.progress import (
    PHASE_GATE_HASH,
    PHASE_PAGE_ENUMERATION,
    PHASE_PDF_ALIGNMENT,
    PHASE_STAGE1_GLM_OCR,
    PHASE_STAGE3_EDITOR,
    PHASE_VALIDATION,
    PipelineTiming,
    STATUS_COMPLETED,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
    timed_progress_reporter,
)
from src.ingestion.request_validation import validate_and_enrich_request
from src.models.request import IngestInputValidationException
from src.models.settings import Settings
from src.models.ingest_compute import IngestComputePlan, plan_needs_local_llm
from src.persistence.book_sqlite import run_ingest_gate_phase

_GLM_ACTIVE_PAGE_STAGES = 3


def run_glm_ingest_pipeline(
    ingest_payload: dict[str, Any],
    saved_pdf_path: Path,
    settings: Settings,
    reporter: ProgressReporter | None,
    set_global_total: Callable[[int], None] | None,
    compute_plan: IngestComputePlan | None = None,
) -> dict[str, Any]:
    ingest_payload = dict(ingest_payload)
    ingest_payload["source_pdf_path"] = str(saved_pdf_path)

    timing = PipelineTiming()
    reporter = timed_progress_reporter(reporter, timing)

    _emit(reporter, make_event(PHASE_VALIDATION, STATUS_STARTED))
    Log(INFO_LOG_LEVEL, "glm pipeline validate_and_enrich_request begin")
    try:
        enriched = validate_and_enrich_request(ingest_payload)
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        Log(WARNING_LOG_LEVEL, "glm pipeline validation failed", {"error": str(exc)})
        _emit_error(
            reporter,
            PHASE_VALIDATION,
            err_detail["message"],
            code=err_detail.get("code"),
            field=err_detail.get("field"),
        )
        raise

    Log(
        INFO_LOG_LEVEL,
        "glm pipeline validate_and_enrich_request done",
        {"source_sha256": enriched.source_sha256[:16]},
    )
    _emit(
        reporter,
        make_event(PHASE_VALIDATION, STATUS_COMPLETED, source_sha256=enriched.source_sha256),
    )

    _emit(reporter, make_event(PHASE_GATE_HASH, STATUS_STARTED))
    ingest_gate_phase = run_ingest_gate_phase(enriched, settings.sqlite_path)
    _emit(
        reporter,
        make_event(
            PHASE_GATE_HASH,
            STATUS_COMPLETED,
            pipeline_skipped=ingest_gate_phase.pipeline_skipped,
            gate_status=ingest_gate_phase.gate.status.value,
        ),
    )

    request_id = enriched.request.request_id
    if ingest_gate_phase.pipeline_skipped:
        if set_global_total is not None:
            set_global_total(0)
        payload_out = build_pipeline_skipped_payload(
            enriched=enriched, ingest_gate_phase=ingest_gate_phase,
            timing=timing, pipeline_mode="glm_ocr",
        )
        persist_pipeline_timing(settings, request_id, timing)
        Log(INFO_LOG_LEVEL, "glm pipeline completed (pipeline_skipped, metadata only)",
            {"source_sha256": enriched.source_sha256[:16]})
        return payload_out

    try:
        require_gpu_vram_at_pipeline_start(
            settings,
            skip_vision_editor=False,
            ocr_backend="glm",
            needs_local_llm=(
                plan_needs_local_llm(compute_plan, "glm_ocr") if compute_plan is not None else None
            ),
        )
    except IngestInputValidationException as exc:
        err_detail = _extract_validation_error(exc)
        _emit_error(
            reporter,
            PHASE_STAGE1_GLM_OCR,
            err_detail["message"],
            code=err_detail.get("code"),
            field=err_detail.get("field"),
        )
        raise

    _emit(
        reporter,
        make_event(
            PHASE_PDF_ALIGNMENT,
            STATUS_STARTED,
            will_run=True,
        ),
    )
    try:
        pdf_alignment = maybe_run_pdf_alignment(
            enriched,
            ingest_gate_phase,
            settings.processed_pdf_input_dir,
            page_range_per_thread=settings.page_range_per_thread,
        )
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        _emit_error(reporter, PHASE_PDF_ALIGNMENT, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    _emit(
        reporter,
        make_event(
            PHASE_PDF_ALIGNMENT,
            STATUS_COMPLETED,
            counts_as_step=True,
            skipped=pdf_alignment is None,
        ),
    )

    _emit(reporter, make_event(PHASE_PAGE_ENUMERATION, STATUS_STARTED))
    try:
        useful_pages_enumeration = build_useful_pages_enumeration(enriched, pdf_alignment)
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        _emit_error(reporter, PHASE_PAGE_ENUMERATION, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    n_pages = len(useful_pages_enumeration.useful_original_pages)
    aligned_useful_pages = sorted(
        useful_pages_enumeration.original_page_to_aligned_page[p]
        for p in useful_pages_enumeration.useful_original_pages
    )
    _emit(
        reporter,
        make_event(
            PHASE_PAGE_ENUMERATION,
            STATUS_COMPLETED,
            n_pages=n_pages,
            aligned_useful_pages=aligned_useful_pages,
        ),
    )

    total_steps = 1 + n_pages * _GLM_ACTIVE_PAGE_STAGES
    if set_global_total is not None:
        set_global_total(total_steps)

    try:
        try:
            orchestrator_result = asyncio.run(
                run_pipeline(
                    enriched,
                    pdf_alignment,
                    useful_pages_enumeration,
                    settings,
                    settings.sqlite_path,
                    NullOrchestratorRegistry(),
                    request_id,
                    progress=reporter,
                    skip_vision_editor=False,
                    pipeline_mode="glm_ocr",
                    compute_plan=compute_plan,
                )
            )
        except OrchestratorStageError as exc:
            err_detail = _extract_validation_error(exc.cause)
            _emit_error(reporter, PHASE_STAGE3_EDITOR, err_detail["message"],
                        code=err_detail.get("code"), field=err_detail.get("field"))
            raise exc.cause from exc
        except (ValueError, IngestInputValidationException) as exc:
            err_detail = _extract_validation_error(exc)
            _emit_error(reporter, PHASE_STAGE1_GLM_OCR, err_detail["message"],
                        code=err_detail.get("code"), field=err_detail.get("field"))
            raise
        except Exception as exc:
            err_detail = _extract_validation_error(exc)
            _emit_error(reporter, PHASE_STAGE1_GLM_OCR, err_detail["message"],
                        code=err_detail.get("code"), field=err_detail.get("field"))
            raise
    finally:
        persist_pipeline_timing(settings, request_id, timing)

    stage1_result = orchestrator_result.stage1_result
    stage2_result = orchestrator_result.stage2_result

    def _build_payload(
        stage2_dump: dict[str, Any] | None,
        stage3_dump: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "pipeline_mode": "glm_ocr",
            "enriched": enriched.model_dump(mode="json", by_alias=True),
            "ingest_gate_phase": ingest_gate_phase.model_dump(mode="json", by_alias=True),
            "pdf_alignment": (
                pdf_alignment.model_dump(mode="json", by_alias=True)
                if pdf_alignment is not None
                else None
            ),
            "useful_pages_enumeration": useful_pages_enumeration.model_dump(
                mode="json", by_alias=True
            ),
            "stage1": stage1_result.model_dump(mode="json"),
            "stage2": stage2_dump,
            "stage3": stage3_dump,
            "timing": timing.summary(),
        }

    stage3_result = orchestrator_result.stage3_result
    if stage2_result is None or stage3_result is None:
        raise RuntimeError("orchestrator returned incomplete glm stage results")

    payload_out = _build_payload(
        stage2_result.model_dump(mode="json"),
        stage3_result.model_dump(mode="json"),
    )
    _emit(
        reporter,
        make_event(
            PHASE_STAGE3_EDITOR,
            STATUS_COMPLETED,
            timing=payload_out["timing"],
        ),
    )
    persist_pipeline_timing(settings, request_id, timing)
    return payload_out

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.orchestrator import (
    NullOrchestratorRegistry,
    OrchestratorStageError,
    run_pipeline,
)
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import maybe_run_pdf_alignment
from src.ingestion.progress import (
    PHASE_GATE_HASH,
    PHASE_PAGE_ENUMERATION,
    PHASE_PDF_ALIGNMENT,
    PHASE_STAGE1_OCR,
    PHASE_STAGE2_VISION,
    PHASE_STAGE3_EDITOR,
    PHASE_VALIDATION,
    PipelineTiming,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_STARTED,
    ProgressReporter,
    make_event,
    timed_progress_reporter,
)
from src.ingestion.request_validation import (
    run_ingest_gate_phase,
    validate_and_enrich_request,
)
from src.models.request import IngestInputValidationError, IngestInputValidationException
from src.models.settings import Settings

_ACTIVE_PAGE_STAGES = 3


def _emit(reporter: ProgressReporter | None, event: dict[str, Any]) -> None:
    if reporter is not None:
        reporter(event)


def _emit_error(
    reporter: ProgressReporter | None,
    phase: str,
    message: str,
    *,
    code: str | None = None,
    field: str | None = None,
) -> None:
    _emit(
        reporter,
        make_event(
            phase,
            STATUS_ERROR,
            message=message,
            code=code,
            field=field,
        ),
    )


def run_full_pipeline(
    ingest_payload: dict[str, Any],
    saved_pdf_path: Path,
    settings: Settings,
    reporter: ProgressReporter | None,
    set_global_total: Callable[[int], None] | None,
) -> dict[str, Any]:
    """Run the full ingest pipeline and return the response payload dict.

    Emits structured progress events through *reporter* at every phase.
    Calls *set_global_total* once the useful-page count is known so the
    caller's registry can announce the total work units.

    On validation / pipeline errors, emits a terminal ``error`` event and
    raises ``IngestInputValidationException`` so the caller can clean up.

    Returns the same ``payload_out`` dict that was previously returned
    directly by the HTTP handler.
    """
    ingest_payload = dict(ingest_payload)
    ingest_payload["source_pdf_path"] = str(saved_pdf_path)

    timing = PipelineTiming()
    reporter = timed_progress_reporter(reporter, timing)

    _emit(reporter, make_event(PHASE_VALIDATION, STATUS_STARTED))
    Log(INFO_LOG_LEVEL, "pipeline validate_and_enrich_request begin")
    try:
        enriched = validate_and_enrich_request(ingest_payload)
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        Log(WARNING_LOG_LEVEL, "pipeline validation failed", {"error": str(exc)})
        _emit_error(reporter, PHASE_VALIDATION, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    Log(INFO_LOG_LEVEL, "pipeline validate_and_enrich_request done",
        {"source_sha256": enriched.source_sha256[:16]})
    _emit(reporter, make_event(PHASE_VALIDATION, STATUS_COMPLETED,
                               source_sha256=enriched.source_sha256[:16]))

    _emit(reporter, make_event(PHASE_GATE_HASH, STATUS_STARTED))
    Log(INFO_LOG_LEVEL, "pipeline run_ingest_gate_phase begin")
    ingest_gate_phase = run_ingest_gate_phase(enriched, settings.sqlite_path)
    Log(INFO_LOG_LEVEL, "pipeline gate phase done",
        {"pipeline_skipped": ingest_gate_phase.pipeline_skipped})
    _emit(reporter, make_event(PHASE_GATE_HASH, STATUS_COMPLETED,
                               pipeline_skipped=ingest_gate_phase.pipeline_skipped,
                               gate_status=ingest_gate_phase.gate.status.value))

    alignment_counts_as_step = not ingest_gate_phase.pipeline_skipped
    _emit(reporter, make_event(PHASE_PDF_ALIGNMENT, STATUS_STARTED,
                               will_run=alignment_counts_as_step))
    Log(INFO_LOG_LEVEL, "pipeline maybe_run_pdf_alignment begin")
    try:
        pdf_alignment = maybe_run_pdf_alignment(
            enriched,
            ingest_gate_phase,
            settings.processed_pdf_input_dir,
            page_range_per_thread=settings.page_range_per_thread,
        )
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        Log(WARNING_LOG_LEVEL, "pipeline pdf alignment failed", {"error": str(exc)})
        _emit_error(reporter, PHASE_PDF_ALIGNMENT, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    Log(INFO_LOG_LEVEL, "pipeline maybe_run_pdf_alignment done",
        {"returned_alignment": pdf_alignment is not None})
    _emit(reporter, make_event(PHASE_PDF_ALIGNMENT, STATUS_COMPLETED,
                               counts_as_step=alignment_counts_as_step,
                               skipped=pdf_alignment is None))

    _emit(reporter, make_event(PHASE_PAGE_ENUMERATION, STATUS_STARTED))
    Log(INFO_LOG_LEVEL, "pipeline build_useful_pages_enumeration begin",
        {"pdf_alignment_present": pdf_alignment is not None})
    try:
        useful_pages_enumeration = build_useful_pages_enumeration(enriched, pdf_alignment)
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        Log(WARNING_LOG_LEVEL, "pipeline page enumeration failed", {"error": str(exc)})
        _emit_error(reporter, PHASE_PAGE_ENUMERATION, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    n_pages = len(useful_pages_enumeration.useful_original_pages)
    Log(INFO_LOG_LEVEL, "pipeline build_useful_pages_enumeration done", {"n_pages": n_pages})
    _emit(reporter, make_event(PHASE_PAGE_ENUMERATION, STATUS_COMPLETED, n_pages=n_pages))

    alignment_step = 1 if alignment_counts_as_step else 0
    total_steps = alignment_step + n_pages * _ACTIVE_PAGE_STAGES
    if set_global_total is not None:
        set_global_total(total_steps)

    Log(INFO_LOG_LEVEL, "pipeline run_pipeline begin")
    try:
        orchestrator_result = asyncio.run(
            run_pipeline(
                enriched,
                pdf_alignment,
                useful_pages_enumeration,
                settings,
                settings.sqlite_path,
                NullOrchestratorRegistry(),
                enriched.request.request_id,
                progress=reporter,
                skip_vision_editor=ingest_gate_phase.pipeline_skipped,
            )
        )
    except OrchestratorStageError as exc:
        err_detail = _extract_validation_error(exc.cause)
        phase = (
            PHASE_STAGE2_VISION
            if exc.stage == "stage2_vision"
            else PHASE_STAGE3_EDITOR
        )
        Log(ERROR_LOG_LEVEL, "pipeline orchestrator stage failed",
            {"error": str(exc.cause), "phase": phase})
        _emit_error(reporter, phase, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise exc.cause from exc
    except (ValueError, IngestInputValidationException) as exc:
        err_detail = _extract_validation_error(exc)
        Log(WARNING_LOG_LEVEL, "pipeline orchestrator failed", {"error": str(exc)})
        _emit_error(reporter, PHASE_STAGE1_OCR, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise
    except Exception as exc:
        err_detail = _extract_validation_error(exc)
        Log(ERROR_LOG_LEVEL, "pipeline orchestrator failed", {"error": str(exc)})
        _emit_error(reporter, PHASE_STAGE1_OCR, err_detail["message"],
                    code=err_detail.get("code"), field=err_detail.get("field"))
        raise

    stage1_result = orchestrator_result.stage1_result
    Log(INFO_LOG_LEVEL, "pipeline run_pipeline done",
        {"pages": len(stage1_result.pages)})

    def _build_payload(stage2_dump: dict[str, Any] | None, stage3_dump: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "ok": True,
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

    if ingest_gate_phase.pipeline_skipped:
        payload_out = _build_payload(None, None)
        _emit(reporter, make_event(
            PHASE_STAGE1_OCR, STATUS_DONE, result=payload_out, timing=payload_out["timing"]
        ))
        Log(INFO_LOG_LEVEL, "pipeline completed (pipeline_skipped)",
            {"source_sha256": enriched.source_sha256[:16],
             "stage1_pages": len(stage1_result.pages)})
        return payload_out

    stage2_result = orchestrator_result.stage2_result
    stage3_result = orchestrator_result.stage3_result
    if stage2_result is None or stage3_result is None:
        raise RuntimeError("orchestrator returned incomplete stage results")

    Log(INFO_LOG_LEVEL, "pipeline stage2 done",
        {"pages": len(stage2_result.pages),
         "skipped_cached": stage2_result.skipped_existing,
         "failed": len(stage2_result.missing)})
    Log(INFO_LOG_LEVEL, "pipeline stage3 done",
        {"pages": len(stage3_result.pages),
         "skipped_cached": stage3_result.skipped_existing,
         "failed": len(stage3_result.missing)})

    payload_out = _build_payload(
        stage2_result.model_dump(mode="json"),
        stage3_result.model_dump(mode="json"),
    )
    _emit(reporter, make_event(
        PHASE_STAGE3_EDITOR, STATUS_DONE, result=payload_out, timing=payload_out["timing"]
    ))

    Log(INFO_LOG_LEVEL, "pipeline completed",
        {"source_sha256": enriched.source_sha256[:16],
         "stage1_pages": len(stage1_result.pages),
         "stage2_pages": len(stage2_result.pages),
         "stage3_pages": len(stage3_result.pages)})
    return payload_out


def _extract_validation_error(exc: Exception) -> dict[str, Any]:
    """Pull code/message/field out of a validation exception."""
    if isinstance(exc, IngestInputValidationException):
        d = exc.detail.model_dump(mode="json")
        return {"message": d.get("message", str(exc)),
                "code": d.get("code"), "field": d.get("field")}
    try:
        err_model = IngestInputValidationError.model_validate_json(str(exc))
        d = err_model.model_dump(mode="json")
        return {"message": d.get("message", str(exc)),
                "code": d.get("code"), "field": d.get("field")}
    except Exception:
        return {"message": str(exc), "code": None, "field": None}

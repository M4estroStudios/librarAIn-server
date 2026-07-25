from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.core.log import INFO_LOG_LEVEL, Log
from src.core.openai_client import build_openai_client
from src.ingestion.polyindex.subject_matcher_embeddings import fetch_embeddings_parallel
from src.ingestion.progress import STATUS_DONE, STATUS_PROGRESS, STATUS_STARTED, make_event
from src.models.polyindex_index import PolyindexIndexDocument
from src.models.settings import Settings
from src.persistence.subject_matcher_sqlite import (
    embedded_canonical_ids_for_model,
    set_subject_embedding,
)

_STAGE = "subject_embedding_backfill"
_BATCH_SIZE = 64
ProgressReporter = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class EmbeddingBackfillStatus:
    model: str
    total_subjects: int
    embedded_count: int
    missing_count: int


def embedding_backfill_status(
    polyindex_dir: Path,
    settings: Settings,
) -> EmbeddingBackfillStatus:
    document = PolyindexIndexDocument.load_file(polyindex_dir / "INDEX.json")
    model = settings.matcher_embedding_model
    embedded = embedded_canonical_ids_for_model(settings.sqlite_path, model)
    total = len(document.subjects)
    missing = sum(1 for canonical_id in document.subjects if canonical_id not in embedded)
    return EmbeddingBackfillStatus(
        model=model,
        total_subjects=total,
        embedded_count=total - missing,
        missing_count=missing,
    )


def list_missing_subject_embeddings(
    document: PolyindexIndexDocument,
    embedded: set[str],
) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for canonical_id in sorted(document.subjects):
        if canonical_id in embedded:
            continue
        entry = document.subjects[canonical_id]
        missing.append((canonical_id, entry.canonical_label))
    return missing


def run_subject_embedding_backfill(
    polyindex_dir: Path,
    settings: Settings,
    *,
    request_id: str,
    progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    document = PolyindexIndexDocument.load_file(polyindex_dir / "INDEX.json")
    model = settings.matcher_embedding_model
    embedded = embedded_canonical_ids_for_model(settings.sqlite_path, model)
    missing = list_missing_subject_embeddings(document, embedded)
    total = len(missing)
    if total == 0:
        return {
            "model": model,
            "generated": 0,
            "total_subjects": len(document.subjects),
            "embedded_count": len(document.subjects),
        }

    client = build_openai_client(settings)
    generated = 0
    if progress is not None:
        progress(
            make_event(
                "subject_embeddings",
                STATUS_STARTED,
                model=model,
                total=total,
                message=f"Generazione {total} embedding mancanti",
            )
        )

    for offset in range(0, total, _BATCH_SIZE):
        batch = missing[offset : offset + _BATCH_SIZE]
        labels = [label for _canonical_id, label in batch]
        vectors = fetch_embeddings_parallel(
            client,
            model,
            labels,
            request_id=request_id,
            max_parallel=settings.max_parallel_request,
        )
        if len(vectors) != len(batch):
            raise RuntimeError("embedding batch size mismatch")
        for (canonical_id, label), vector in zip(batch, vectors):
            set_subject_embedding(
                settings.sqlite_path,
                canonical_id,
                label,
                vector,
                model,
            )
        generated += len(batch)
        if progress is not None:
            progress(
                make_event(
                    "subject_embeddings",
                    STATUS_PROGRESS,
                    counts_as_step=True,
                    done=generated,
                    total=total,
                    model=model,
                    message=f"Embedding {generated}/{total}",
                )
            )

    Log(
        INFO_LOG_LEVEL,
        "subject embedding backfill completed",
        {
            "request_id": request_id,
            "model": model,
            "generated": generated,
            "total_subjects": len(document.subjects),
        },
    )
    if progress is not None:
        progress(
            make_event(
                "subject_embeddings",
                STATUS_DONE,
                generated=generated,
                total=total,
                model=model,
                message=f"Completati {generated} embedding",
            )
        )
    return {
        "model": model,
        "generated": generated,
        "total_subjects": len(document.subjects),
        "embedded_count": len(document.subjects),
    }

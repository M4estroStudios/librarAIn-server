"""Sync bozze ingest tra PC: metadati JSON (git) + pack/unpack ZIP (PDF inclusi).

Flusso tipico laptop → workstation:
  make drafts-pack     # crea data/sync/bundles/librarain-drafts.zip
  # copia lo zip (AirDrop / USB / scp)
  make drafts-unpack   # importa JSON + PDF nel DB e in input/drafts/
  make run-server      # all'avvio re-importa anche i JSON da data/sync/

I JSON in data/sync/ possono anche essere committati in git (senza PDF).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.api.appendix_types import list_appendix_types, upsert_appendix_type
from src.api.pdf_upload_storage import (
    draft_pdf_path_for_sha,
    find_draft_pdf_by_sha256,
    save_draft_pdf,
)
from src.core.hashing import validate_source_sha256
from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.persistence.pipeline_runs import _sqlite_connection

SYNC_VERSION = 1


def sync_root(data_root: Path | str) -> Path:
    path = Path(data_root) / "sync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_drafts_dir(data_root: Path | str) -> Path:
    path = sync_root(data_root) / "drafts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_bundles_dir(data_root: Path | str) -> Path:
    path = sync_root(data_root) / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def appendix_types_sync_path(data_root: Path | str) -> Path:
    return sync_root(data_root) / "appendix_types.json"


def draft_sync_json_path(data_root: Path | str, source_sha256: str) -> Path:
    digest = validate_source_sha256(source_sha256)
    return sync_drafts_dir(data_root) / f"{digest}.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_ingest_notes_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_notes (
            source_sha256 TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_draft INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(ingest_notes)").fetchall()}
    if "is_draft" not in cols:
        conn.execute(
            "ALTER TABLE ingest_notes ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0"
        )


def export_appendix_types(sqlite_path: str, data_root: Path | str) -> Path:
    items = list_appendix_types(sqlite_path)
    path = appendix_types_sync_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SYNC_VERSION,
        "updated_at": _utc_now_iso(),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def export_draft_record(
    sqlite_path: str,
    data_root: Path | str,
    source_sha256: str,
) -> Path | None:
    digest = validate_source_sha256(source_sha256)
    db_path = Path(sqlite_path)
    if not db_path.is_file():
        return None
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        row = conn.execute(
            """
            SELECT state_json, updated_at, is_draft
            FROM ingest_notes WHERE source_sha256 = ?
            """,
            (digest,),
        ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(str(row[0] or "{}"))
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    has_pdf = find_draft_pdf_by_sha256(Path(data_root), digest) is not None
    payload = {
        "version": SYNC_VERSION,
        "source_sha256": digest,
        "updated_at": str(row[1] or _utc_now_iso()),
        "is_draft": bool(int(row[2] or 0)),
        "has_pdf": has_pdf,
        "file_name": str(state.get("file_name") or "").strip(),
        "titolo": str(state.get("titolo") or "").strip(),
        "state": state,
    }
    path = draft_sync_json_path(data_root, digest)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def remove_exported_draft(data_root: Path | str, source_sha256: str) -> bool:
    path = draft_sync_json_path(data_root, source_sha256)
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def export_all_active_drafts(sqlite_path: str, data_root: Path | str) -> dict[str, Any]:
    db_path = Path(sqlite_path)
    exported: list[str] = []
    if db_path.is_file():
        with _sqlite_connection(str(db_path)) as conn:
            _ensure_ingest_notes_table(conn)
            rows = conn.execute(
                """
                SELECT source_sha256 FROM ingest_notes
                WHERE is_draft = 1
                ORDER BY updated_at DESC
                """
            ).fetchall()
        for row in rows:
            digest = str(row[0])
            if export_draft_record(sqlite_path, data_root, digest):
                exported.append(digest)
    types_path = export_appendix_types(sqlite_path, data_root)
    return {
        "ok": True,
        "drafts": exported,
        "count": len(exported),
        "appendix_types_path": str(types_path),
    }


def _import_one_draft_payload(
    sqlite_path: str,
    data_root: Path,
    payload: dict[str, Any],
    *,
    pdf_source: Path | None = None,
) -> str | None:
    digest = validate_source_sha256(str(payload.get("source_sha256") or ""))
    incoming_updated = _parse_iso(str(payload.get("updated_at") or ""))
    state = payload.get("state")
    if not isinstance(state, dict):
        state = {}
    is_draft = 1 if payload.get("is_draft", True) else 0
    updated_at = str(payload.get("updated_at") or _utc_now_iso())

    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _sqlite_connection(str(db_path)) as conn:
        _ensure_ingest_notes_table(conn)
        existing = conn.execute(
            "SELECT updated_at FROM ingest_notes WHERE source_sha256 = ?",
            (digest,),
        ).fetchone()
        if existing is not None:
            local_updated = _parse_iso(str(existing[0] or ""))
            if local_updated >= incoming_updated:
                # Locale più recente o uguale: non sovrascrivere lo state.
                if pdf_source and pdf_source.is_file() and find_draft_pdf_by_sha256(data_root, digest) is None:
                    save_draft_pdf(data_root, digest, pdf_source)
                return None
        conn.execute(
            """
            INSERT INTO ingest_notes (source_sha256, state_json, updated_at, is_draft)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_sha256) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at,
                is_draft = excluded.is_draft
            """,
            (
                digest,
                json.dumps(state, ensure_ascii=False),
                updated_at,
                is_draft,
            ),
        )
    if pdf_source and pdf_source.is_file():
        save_draft_pdf(data_root, digest, pdf_source)
    export_draft_record(sqlite_path, data_root, digest)
    return digest


def import_appendix_types_file(sqlite_path: str, path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return 0
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            upsert_appendix_type(sqlite_path, name)
            count += 1
        except ValueError:
            continue
    return count


def import_sync_dir(sqlite_path: str, data_root: Path | str) -> dict[str, Any]:
    root = Path(data_root)
    drafts_dir = sync_drafts_dir(root)
    imported: list[str] = []
    skipped = 0
    for path in sorted(drafts_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            Log(WARNING_LOG_LEVEL, "draft sync json unreadable", {"path": str(path), "error": str(exc)})
            continue
        if not isinstance(payload, dict):
            continue
        digest = str(payload.get("source_sha256") or path.stem).strip().lower()
        pdf = find_draft_pdf_by_sha256(root, digest)
        result = _import_one_draft_payload(
            sqlite_path,
            root,
            payload,
            pdf_source=pdf,
        )
        if result:
            imported.append(result)
        else:
            skipped += 1
    types_count = import_appendix_types_file(sqlite_path, appendix_types_sync_path(root))
    export_appendix_types(sqlite_path, root)
    Log(
        INFO_LOG_LEVEL,
        "draft sync import complete",
        {"imported": len(imported), "skipped": skipped, "appendix_types": types_count},
    )
    return {
        "ok": True,
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "appendix_types": types_count,
    }


def pack_drafts_bundle(
    sqlite_path: str,
    data_root: Path | str,
    *,
    dest: Path | None = None,
) -> Path:
    """Esporta bozze attive + PDF + tipologies in uno ZIP portabile."""
    root = Path(data_root)
    export_all_active_drafts(sqlite_path, root)
    out = dest or (sync_bundles_dir(root) / "librarain-drafts.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    drafts_dir = sync_drafts_dir(root)
    draft_files = sorted(drafts_dir.glob("*.json"))
    manifest = {
        "version": SYNC_VERSION,
        "created_at": _utc_now_iso(),
        "drafts": [p.stem for p in draft_files],
    }
    # Riscrivi zip atomicamente via temp.
    tmp = out.with_suffix(out.suffix + ".partial")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        types_path = appendix_types_sync_path(root)
        if types_path.is_file():
            zf.write(types_path, arcname="appendix_types.json")
        for json_path in draft_files:
            zf.write(json_path, arcname=f"drafts/{json_path.name}")
            digest = json_path.stem.lower()
            pdf = find_draft_pdf_by_sha256(root, digest)
            if pdf is not None:
                zf.write(pdf, arcname=f"pdfs/{digest}.pdf")
    tmp.replace(out)
    Log(
        INFO_LOG_LEVEL,
        "draft sync pack created",
        {"path": str(out), "drafts": len(draft_files), "bytes": out.stat().st_size},
    )
    return out


def unpack_drafts_bundle(
    sqlite_path: str,
    data_root: Path | str,
    bundle_path: Path | str,
) -> dict[str, Any]:
    """Importa ZIP creato da pack_drafts_bundle."""
    root = Path(data_root)
    bundle = Path(bundle_path)
    if not bundle.is_file():
        raise FileNotFoundError(f"bundle non trovato: {bundle}")
    tmp_dir = root / "tmp" / "draft_unpack"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    try:
        with zipfile.ZipFile(bundle, "r") as zf:
            zf.extractall(tmp_dir)
        types_file = tmp_dir / "appendix_types.json"
        types_count = import_appendix_types_file(sqlite_path, types_file)
        drafts_dir = tmp_dir / "drafts"
        pdfs_dir = tmp_dir / "pdfs"
        for json_path in sorted(drafts_dir.glob("*.json")) if drafts_dir.is_dir() else []:
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            digest = str(payload.get("source_sha256") or json_path.stem).strip().lower()
            pdf_candidate = pdfs_dir / f"{digest}.pdf"
            result = _import_one_draft_payload(
                sqlite_path,
                root,
                payload,
                pdf_source=pdf_candidate if pdf_candidate.is_file() else None,
            )
            if result:
                imported.append(result)
        export_appendix_types(sqlite_path, root)
        return {
            "ok": True,
            "imported": imported,
            "imported_count": len(imported),
            "appendix_types": types_count,
            "bundle": str(bundle),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def default_bundle_path(data_root: Path | str) -> Path:
    return sync_bundles_dir(data_root) / "librarain-drafts.zip"


def sync_on_draft_saved(
    sqlite_path: str,
    data_root: Path | str,
    source_sha256: str,
) -> None:
    try:
        export_draft_record(sqlite_path, data_root, source_sha256)
        export_appendix_types(sqlite_path, data_root)
    except Exception as exc:  # noqa: BLE001 — sync non deve far fallire il save
        Log(WARNING_LOG_LEVEL, "draft sync export failed", {"error": str(exc)})


def sync_on_draft_deleted(data_root: Path | str, source_sha256: str) -> None:
    try:
        remove_exported_draft(data_root, source_sha256)
    except Exception as exc:  # noqa: BLE001
        Log(WARNING_LOG_LEVEL, "draft sync delete export failed", {"error": str(exc)})


def bootstrap_draft_sync(sqlite_path: str, data_root: Path | str) -> dict[str, Any]:
    """All'avvio server: importa JSON sync (e PDF già presenti in input/drafts)."""
    try:
        result = import_sync_dir(sqlite_path, data_root)
        export_all_active_drafts(sqlite_path, data_root)
        return result
    except Exception as exc:  # noqa: BLE001
        Log(WARNING_LOG_LEVEL, "draft sync bootstrap failed", {"error": str(exc)})
        return {"ok": False, "error": str(exc)}

"""Operator-facing "Proposta export E-TALY" flow (slice F-007).

This module wires the deterministic export building blocks (``src.export.*``) into an
operator flow served by the stdlib :mod:`src.api.ingest_http_server`:

* ``GET  /api/etaly/export/list``    — list generated POH with their mapping status.
* ``POST /api/etaly/export/propose`` — run the ``etaly_metadata`` (+ optional
  ``timeline_fill``) LLM proposal for one slug. **Proposal only** (decision D-06):
  nothing is written to any E-TALY path.
* ``POST /api/etaly/export/confirm`` — persist the slug -> ``poh_id`` mapping through
  :meth:`EtalyRegistry.confirm` (assigning a fresh id for a *new* poh) and store the
  human-approved metadata under ``data/etaly/proposals/<slug>.json``.
* ``POST /api/etaly/export/build``   — for every *confirmed* slug: re-derive a
  :class:`PostprocessResult`, run :func:`to_etaly_article`, :func:`lint_article`, the
  :func:`assert_exportable` gate, and finally :func:`build_bundle` into a temp ``.zip``
  that is streamed back as a download. Pending/unconfirmed slugs are excluded
  (decision D-08); an unresolved ``poh:`` cross-link (:class:`ExportBlockedError`) or a
  lint failure (:class:`LintGateError`) blocks the build with a readable report
  (decision D-07 — nothing is ever written into E-TALY, the bundle is only downloaded).

The HTTP layer is intentionally thin: all decision logic lives in pure functions that
are unit-tested without a live server or LLM (the LLM call and the article/postprocess
loading are injectable so tests can mock them).
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.export.bundle import BundleItem, BundleResult, build_bundle
from src.export.etaly_adapter import (
    ApprovedMetadata,
    EtalyArticle,
    ExportBlockedError,
    TimelineEntryInput,
    normalize_year,
    to_etaly_article,
)
from src.export.lint import (
    LintGateError,
    LintReport,
    assert_exportable,
    format_report,
    lint_article,
)
from src.export.prompts import load_etaly_metadata_prompt, load_timeline_fill_prompt
from src.export.registry import POH_TYPES, EtalyRegistry, parse_poh_id
from src.ingestion.polyindex.index_json import PolyindexIndexDocument
from src.models.settings import Settings
from src.search.postprocess import PostprocessResult, postprocess_markdown

_MAX_TIMELINE_ENTRIES = 5
_PROPOSAL_MAX_BODY = 2 * 1024 * 1024
_METADATA_KEYS = ("tipo", "name", "timeline", "geo_hint")

# Type of the injectable LLM callable so the OpenAI client stays mockable in tests.
LlmCaller = Callable[..., str]

SendJson = Callable[[BaseHTTPRequestHandler, int, Any], None]
SendBytes = Callable[[BaseHTTPRequestHandler, int, bytes, str], None]
ReadBody = Callable[[BaseHTTPRequestHandler, int], bytes]


# --- Path / storage helpers --------------------------------------------------
def _safe_slug(slug: str) -> str:
    """Filesystem-safe token for a librarAIn slug (mirrors article_catalog)."""
    return re.sub(r"[^\w.\-]", "_", slug.strip())


def _articles_dir(data_root: Path) -> Path:
    return data_root / "research" / "articles"


def article_markdown_path(data_root: Path, slug: str) -> Path:
    return _articles_dir(data_root) / f"{_safe_slug(slug)}.md"


def proposals_dir(data_root: Path) -> Path:
    return data_root / "etaly" / "proposals"


def proposal_store_path(data_root: Path, slug: str) -> Path:
    return proposals_dir(data_root) / f"{_safe_slug(slug)}.json"


def load_article_markdown(data_root: Path, slug: str) -> str:
    path = article_markdown_path(data_root, slug)
    if not path.is_file():
        raise FileNotFoundError(f"article markdown not found for slug: {slug}")
    return path.read_text(encoding="utf-8")


def has_metadata_proposal(data_root: Path, slug: str) -> bool:
    return proposal_store_path(data_root, slug).is_file()


def load_stored_proposal(data_root: Path, slug: str) -> dict[str, Any] | None:
    path = proposal_store_path(data_root, slug)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        Log(WARNING_LOG_LEVEL, "etaly proposal store unreadable", {"slug": slug})
        return None
    return raw if isinstance(raw, dict) else None


def store_proposal(data_root: Path, slug: str, payload: dict[str, Any]) -> Path:
    path = proposal_store_path(data_root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# --- Catalog / polyindex loading ---------------------------------------------
def _load_catalog(data_root: Path) -> dict[str, Any]:
    path = data_root / "research" / "catalog.json"
    if not path.is_file():
        return {"articles": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"articles": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("articles"), dict):
        return {"articles": {}}
    return raw


def _load_index_document(data_root: Path) -> PolyindexIndexDocument:
    return PolyindexIndexDocument.load_file(data_root / "polyindex" / "INDEX.json")


@dataclass(frozen=True)
class SubjectContext:
    canonical_label: str
    aliases: list[str]
    time_range: str | None


def _subject_context(document: PolyindexIndexDocument, slug: str) -> SubjectContext:
    entry = document.subjects.get(slug)
    if entry is None:
        return SubjectContext(canonical_label=slug, aliases=[], time_range=None)
    return SubjectContext(
        canonical_label=entry.canonical_label,
        aliases=list(entry.aliases),
        time_range=entry.time_range,
    )


def _reicat_for_slug(data_root: Path, document: PolyindexIndexDocument, slug: str) -> dict[str, Any]:
    """Best-effort REICAT for a subject: the first source book's manifest reicat."""
    entry = document.subjects.get(slug)
    if entry is None:
        return {}
    for sha, book in entry.books.items():
        if not book.aligned_pages:
            continue
        manifest_path = data_root / "output" / sha / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("reicat"), dict):
            return manifest["reicat"]
    return {}


# --- Mapping list ------------------------------------------------------------
def _mapping_from_entry(entry: Any) -> dict[str, Any]:
    poh_id = getattr(entry, "poh_id", None)
    status = getattr(entry, "status", "pending")
    # A resolved status without a poh_id is treated as pending for safety (D-08).
    if status == "resolved" and not poh_id:
        status = "pending"
    return {
        "status": status,
        "poh_id": poh_id,
        "poh_type": getattr(entry, "poh_type", None),
        "score": getattr(entry, "score", None),
    }


def compute_mapping(registry: EtalyRegistry, slug: str, context: SubjectContext) -> dict[str, Any]:
    """Resolve/auto-match a slug and return its mapping descriptor.

    A confirmed (resolved) entry is reported as-is; an existing pending entry is kept
    untouched; a slug never seen before is auto-matched once (which may resolve it via a
    wikidata_qid exact hit, otherwise records a pending candidate — decision D-08).
    """
    resolved = registry.resolve(slug)
    if resolved is not None:
        return _mapping_from_entry(resolved)
    existing = registry.get(slug)
    if existing is not None:
        return _mapping_from_entry(existing)
    entry = registry.auto_match(
        slug,
        context.canonical_label,
        aliases=context.aliases,
    )
    return _mapping_from_entry(entry)


def build_export_list(data_root: Path, registry: EtalyRegistry) -> list[dict[str, Any]]:
    """List every generated article with its mapping status and proposal flag."""
    catalog = _load_catalog(data_root)
    articles = catalog.get("articles", {})
    document = _load_index_document(data_root)

    items: list[dict[str, Any]] = []
    for slug, meta in articles.items():
        if not article_markdown_path(data_root, slug).is_file():
            continue
        if isinstance(meta, dict) and meta.get("no_material"):
            continue
        context = _subject_context(document, slug)
        title = context.canonical_label
        if isinstance(meta, dict) and meta.get("title"):
            title = str(meta["title"])
        items.append(
            {
                "slug": slug,
                "title": title,
                "mapping": compute_mapping(registry, slug, context),
                "has_metadata_proposal": has_metadata_proposal(data_root, slug),
            }
        )
    items.sort(key=lambda item: str(item["title"]).casefold())
    return items


# --- Defensive JSON parsing of LLM proposals ---------------------------------
def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = re.sub(r"^```(?:json|markdown|md)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _normalize_timeline_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for element in raw:
        if not isinstance(element, dict):
            continue
        anno = element.get("anno")
        evento = element.get("evento")
        if anno is None or not isinstance(evento, str) or not evento.strip():
            continue
        item: dict[str, Any] = {"anno": anno, "evento": evento.strip()}
        if element.get("needs_review"):
            item["needs_review"] = True
        items.append(item)
    return items


def parse_metadata_proposal(raw_text: str) -> dict[str, Any]:
    """Parse the ``etaly_metadata`` LLM output into a normalized proposal dict.

    Raises :class:`ValueError` when the payload is not a JSON object carrying a valid
    ``tipo`` (one of ``p``/``o``/``m``); other fields are coerced defensively.
    """
    try:
        parsed = json.loads(_strip_fences(raw_text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata proposal is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("metadata proposal must be a JSON object")

    tipo = str(parsed.get("tipo") or "").strip().lower()
    if tipo not in POH_TYPES:
        raise ValueError(f"invalid tipo in proposal: {parsed.get('tipo')!r}")

    name = parsed.get("name")
    name_str = name.strip() if isinstance(name, str) else ""

    geo_raw = parsed.get("geo_hint")
    geo_hint: dict[str, Any] = {"lat": None, "lon": None, "note": None}
    if isinstance(geo_raw, dict):
        geo_hint["lat"] = _coerce_float(geo_raw.get("lat"))
        geo_hint["lon"] = _coerce_float(geo_raw.get("lon"))
        note = geo_raw.get("note")
        geo_hint["note"] = note.strip() if isinstance(note, str) and note.strip() else None

    return {
        "tipo": tipo,
        "name": name_str,
        "timeline": _normalize_timeline_items(parsed.get("timeline")),
        "geo_hint": geo_hint,
    }


def parse_timeline_fill(raw_text: str) -> list[dict[str, Any]]:
    """Parse the ``timeline_fill`` LLM output (a JSON array). Never raises."""
    try:
        parsed = json.loads(_strip_fences(raw_text))
    except json.JSONDecodeError:
        return []
    return _normalize_timeline_items(parsed)


def _anno_to_year(value: Any) -> int | None:
    """Normalize an ``anno`` (int or ``"YYYY[ a.C.]"`` string) to a signed year key."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        year, _is_bce = normalize_year(value)
        return year
    return None


def merge_timeline(
    existing: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge two timeline lists, de-duplicating by signed year, capped at 5 entries."""
    merged: list[dict[str, Any]] = []
    seen_years: set[int] = set()
    for item in [*existing, *extra]:
        year = _anno_to_year(item.get("anno"))
        if year is None or year in seen_years:
            continue
        seen_years.add(year)
        merged.append(item)
        if len(merged) >= _MAX_TIMELINE_ENTRIES:
            break
    return merged


def count_usable_timeline(items: list[dict[str, Any]]) -> int:
    return sum(1 for item in items if _anno_to_year(item.get("anno")) is not None)


# --- LLM proposal orchestration ----------------------------------------------
def _default_llm(
    settings: Settings,
    *,
    system_prompt: str,
    user_message: str,
    stage: str,
    request_id: str,
) -> str:
    from src.core.openai_client import build_openai_client  # noqa: PLC0415
    from src.core.openai_client_sync import chat_completion_with_retry_sync  # noqa: PLC0415
    from src.search.article_llm import research_model  # noqa: PLC0415

    client = build_openai_client(settings)
    return chat_completion_with_retry_sync(
        client,
        model=research_model(settings),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=settings.research_temperature,
        max_tokens=2048,
        request_id=request_id,
        stage=stage,
        reasoning_effort=settings.reasoning_effort_research,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_research,
    )


def run_metadata_proposal(
    slug: str,
    *,
    data_root: Path,
    settings: Settings,
    request_id: str = "",
    llm: LlmCaller = _default_llm,
    article_loader: Callable[[Path, str], str] = load_article_markdown,
) -> dict[str, Any]:
    """Run the metadata proposal for one slug. This never writes any output (D-06)."""
    markdown = article_loader(data_root, slug)
    document = _load_index_document(data_root)
    context = _subject_context(document, slug)
    reicat = _reicat_for_slug(data_root, document, slug)

    metadata_payload = json.dumps(
        {
            "article_markdown": markdown,
            "reicat": reicat,
            "time_range": context.time_range,
        },
        ensure_ascii=False,
    )
    raw = llm(
        settings,
        system_prompt=load_etaly_metadata_prompt(),
        user_message=metadata_payload,
        stage="etaly_metadata",
        request_id=request_id,
    )
    proposal = parse_metadata_proposal(raw)
    if not proposal["name"]:
        proposal["name"] = context.canonical_label

    if count_usable_timeline(proposal["timeline"]) < _MAX_TIMELINE_ENTRIES:
        fill_payload = json.dumps(
            {
                "article_markdown": markdown,
                "existing_events": proposal["timeline"],
            },
            ensure_ascii=False,
        )
        fill_raw = llm(
            settings,
            system_prompt=load_timeline_fill_prompt(),
            user_message=fill_payload,
            stage="etaly_timeline_fill",
            request_id=request_id,
        )
        proposal["timeline"] = merge_timeline(proposal["timeline"], parse_timeline_fill(fill_raw))

    proposal["slug"] = slug
    proposal["time_range"] = context.time_range
    Log(
        INFO_LOG_LEVEL,
        "etaly metadata proposal built",
        {"slug": slug, "tipo": proposal["tipo"], "timeline": len(proposal["timeline"])},
    )
    return proposal


# --- Confirm -----------------------------------------------------------------
class ConfirmValidationError(ValueError):
    """Raised when a confirm request body is malformed."""


@dataclass
class ConfirmRequest:
    slug: str
    poh_type: str
    name: str
    poh_id: str | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)
    geo: dict[str, Any] = field(default_factory=dict)
    wiki_title: str | None = None
    wiki_url: str | None = None
    wikidata_qid: str | None = None
    time_range: str | None = None


def parse_confirm_request(payload: Any) -> ConfirmRequest:
    if not isinstance(payload, dict):
        raise ConfirmValidationError("request body must be a JSON object")
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ConfirmValidationError("slug is required")
    poh_type = str(payload.get("poh_type") or "").strip().lower()
    if poh_type not in POH_TYPES:
        raise ConfirmValidationError(f"poh_type must be one of {POH_TYPES}")

    poh_id = payload.get("poh_id")
    poh_id_str = str(poh_id).strip() if isinstance(poh_id, str) and poh_id.strip() else None
    if poh_id_str is not None:
        parsed = parse_poh_id(poh_id_str)
        if parsed is None:
            raise ConfirmValidationError(f"invalid poh_id: {poh_id_str!r}")
        if parsed[0] != poh_type:
            raise ConfirmValidationError("poh_id type does not match poh_type")

    name = str(payload.get("name") or "").strip()

    timeline_raw = payload.get("timeline")
    timeline = timeline_raw if isinstance(timeline_raw, list) else []

    geo_raw = payload.get("geo")
    geo = geo_raw if isinstance(geo_raw, dict) else {}

    def _opt_str(key: str) -> str | None:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return ConfirmRequest(
        slug=slug,
        poh_type=poh_type,
        name=name,
        poh_id=poh_id_str,
        timeline=timeline,
        geo=geo,
        wiki_title=_opt_str("wiki_title"),
        wiki_url=_opt_str("wiki_url"),
        wikidata_qid=_opt_str("wikidata_qid"),
        time_range=_opt_str("time_range"),
    )


def _normalize_confirm_timeline(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        year = _anno_to_year(item.get("anno"))
        evento = item.get("evento")
        if year is None or not isinstance(evento, str) or not evento.strip():
            continue
        normalized.append({"anno": year, "evento": evento.strip()})
    return normalized


def confirm_mapping(
    request: ConfirmRequest,
    *,
    data_root: Path,
    registry: EtalyRegistry,
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """Confirm the slug -> poh_id mapping and persist the approved metadata.

    Assigns a fresh id via ``registry.next_id`` when the operator creates a NEW poh
    (no ``poh_id`` supplied).
    """
    poh_id = request.poh_id or registry.next_id(request.poh_type)
    entry = registry.confirm(request.slug, poh_id, confirmed_by=confirmed_by)

    geo = request.geo
    stored: dict[str, Any] = {
        "slug": request.slug,
        "poh_id": poh_id,
        "poh_type": request.poh_type,
        "name": request.name or entry.name or poh_id,
        "timeline": _normalize_confirm_timeline(request.timeline),
        "geo": {
            "lat": _coerce_float(geo.get("lat")),
            "lon": _coerce_float(geo.get("lon")),
            "region": geo.get("region") if isinstance(geo.get("region"), str) else None,
            "category": geo.get("category") if isinstance(geo.get("category"), str) else None,
            "poi_id": geo.get("poi_id"),
        },
        "wiki_title": request.wiki_title,
        "wiki_url": request.wiki_url,
        "wikidata_qid": request.wikidata_qid,
        "time_range": request.time_range,
        "confirmed_at": entry.confirmed_at,
        "confirmed_by": confirmed_by,
    }
    store_proposal(data_root, request.slug, stored)
    Log(
        INFO_LOG_LEVEL,
        "etaly export mapping confirmed",
        {"slug": request.slug, "poh_id": poh_id, "poh_type": request.poh_type},
    )
    return {
        "ok": True,
        "slug": request.slug,
        "poh_id": poh_id,
        "poh_type": request.poh_type,
        "status": "resolved",
    }


# --- Build -------------------------------------------------------------------
def build_approved_metadata(stored: dict[str, Any], entry: Any) -> ApprovedMetadata:
    geo = stored.get("geo") if isinstance(stored.get("geo"), dict) else {}
    timeline_items = stored.get("timeline") if isinstance(stored.get("timeline"), list) else []
    timeline = [
        TimelineEntryInput(anno=item["anno"], evento=item["evento"])
        for item in timeline_items
        if isinstance(item, dict) and isinstance(item.get("anno"), int) and item.get("evento")
    ]
    poh_id = stored.get("poh_id") or getattr(entry, "poh_id", None)
    poh_type = stored.get("poh_type") or getattr(entry, "poh_type", None)
    return ApprovedMetadata(
        poh_id=str(poh_id),
        poh_type=poh_type,
        name=stored.get("name") or None,
        wiki_title=stored.get("wiki_title"),
        wiki_url=stored.get("wiki_url"),
        wikidata_qid=stored.get("wikidata_qid"),
        poi_id=geo.get("poi_id"),
        lat=_coerce_float(geo.get("lat")),
        lon=_coerce_float(geo.get("lon")),
        region=geo.get("region"),
        category=geo.get("category"),
        timeline=timeline or None,
    )


def derive_postprocess_result(
    markdown: str, data_root: Path, request_id: str = ""
) -> PostprocessResult:
    """Re-derive a :class:`PostprocessResult` from a stored article's Markdown.

    Reuses :func:`postprocess_markdown` (the markdown -> PostprocessResult entrypoint)
    so citations / poh references / timeline rows are recovered deterministically,
    without re-running any LLM.
    """
    document = _load_index_document(data_root)
    return postprocess_markdown(
        markdown,
        data_root=data_root,
        index_document=document,
        request_id=request_id,
    )


@dataclass
class BundleBuildOutcome:
    zip_path: Path
    included: list[str]
    excluded: list[dict[str, str]]
    result: BundleResult
    reports: list[LintReport]


def build_export_bundle(
    slugs: list[str],
    *,
    data_root: Path,
    registry: EtalyRegistry,
    output_zip: Path | None = None,
    request_id: str = "",
    article_loader: Callable[[Path, str], str] = load_article_markdown,
    postprocess_deriver: Callable[[str, Path, str], PostprocessResult] = derive_postprocess_result,
) -> BundleBuildOutcome:
    """Assemble the bundle for the confirmed slugs (raises on gate failure).

    Pending/unconfirmed slugs are excluded (D-08). :class:`ExportBlockedError` (unresolved
    ``poh:`` link) and :class:`LintGateError` (lint failure) propagate to the caller so the
    HTTP layer can return a readable 4xx and never writes into E-TALY (D-07).
    """
    included: list[str] = []
    excluded: list[dict[str, str]] = []
    items: list[BundleItem] = []
    reports: list[LintReport] = []

    for slug in slugs:
        entry = registry.resolve(slug)
        if entry is None or not entry.poh_id:
            excluded.append({"slug": slug, "reason": "pending"})
            continue
        stored = load_stored_proposal(data_root, slug)
        if stored is None:
            excluded.append({"slug": slug, "reason": "no_metadata"})
            continue
        markdown = article_loader(data_root, slug)
        postprocess_result = postprocess_deriver(markdown, data_root, request_id)
        approved = build_approved_metadata(stored, entry)
        etaly_article: EtalyArticle = to_etaly_article(
            markdown, postprocess_result, approved, registry
        )
        report = lint_article(etaly_article, available_pages=set(etaly_article.cited_pages))
        reports.append(report)
        items.append(
            BundleItem(
                article=etaly_article,
                poh_type=approved.poh_type,
                name=approved.name,
                time_range=stored.get("time_range"),
            )
        )
        included.append(slug)

    if not items:
        raise ValueError("no confirmed slug available to export")

    try:
        assert_exportable(reports)
    except LintGateError as exc:
        # Surface the human-readable per-POH report alongside the machine failures.
        exc.report_text = format_report(reports)  # type: ignore[attr-defined]
        raise

    if output_zip is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="etaly-export-"))
        output_zip = tmp_dir / "etaly_export.zip"
    result = build_bundle(items, output_zip, registry=registry)
    Log(
        INFO_LOG_LEVEL,
        "etaly export bundle ready for download",
        {"included": len(included), "excluded": len(excluded), "zip": str(output_zip)},
    )
    return BundleBuildOutcome(
        zip_path=output_zip,
        included=included,
        excluded=excluded,
        result=result,
        reports=reports,
    )


# --- HTTP routing ------------------------------------------------------------
def _read_json_payload(handler: BaseHTTPRequestHandler, read_body: ReadBody) -> Any:
    body = read_body(handler, _PROPOSAL_MAX_BODY)
    return json.loads(body.decode("utf-8"))


def _send_zip_download(
    handler: BaseHTTPRequestHandler, content: bytes, filename: str
) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    try:
        handler.wfile.write(content)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def build_etaly_export_routes(
    *,
    data_root: Path,
    web_dir: Path,
    settings: Settings,
    send_json: SendJson,
    send_bytes: SendBytes,
    read_json_body: ReadBody,
    registry: EtalyRegistry | None = None,
    llm: LlmCaller = _default_llm,
) -> tuple[
    Callable[[BaseHTTPRequestHandler, str, dict[str, list[str]]], bool],
    Callable[[BaseHTTPRequestHandler, str], bool],
]:
    """Build the ``try_get`` / ``try_post`` closures for the E-TALY export endpoints."""
    shared_registry = registry

    def _registry() -> EtalyRegistry:
        nonlocal shared_registry
        if shared_registry is None:
            shared_registry = EtalyRegistry()
        return shared_registry

    def try_get(handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> bool:
        if path in ("/etaly-export", "/etaly-export.html", "/etaly_export.html"):
            page = web_dir / "etaly_export.html"
            if not page.is_file():
                send_json(handler, 500, {"ok": False, "error": "web/etaly_export.html missing"})
                return True
            send_bytes(handler, 200, page.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/api/etaly/export/list":
            try:
                items = build_export_list(data_root, _registry())
            except Exception as exc:  # noqa: BLE001 - report as 500, keep server alive
                Log(ERROR_LOG_LEVEL, "etaly export list failed", {"error": str(exc)})
                send_json(handler, 500, {"ok": False, "error": str(exc)})
                return True
            send_json(handler, 200, {"ok": True, "items": items, "count": len(items)})
            return True

        return False

    def _handle_propose(handler: BaseHTTPRequestHandler) -> None:
        try:
            payload = _read_json_payload(handler, read_json_body)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return
        slug = str(payload.get("slug") or "").strip() if isinstance(payload, dict) else ""
        if not slug:
            send_json(handler, 400, {"ok": False, "error": "slug is required"})
            return
        try:
            proposal = run_metadata_proposal(
                slug,
                data_root=data_root,
                settings=settings,
                request_id=slug,
                llm=llm,
            )
        except FileNotFoundError as exc:
            send_json(handler, 404, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            send_json(handler, 502, {"ok": False, "error": f"invalid LLM proposal: {exc}"})
            return
        except Exception as exc:  # noqa: BLE001
            Log(ERROR_LOG_LEVEL, "etaly propose failed", {"slug": slug, "error": str(exc)})
            send_json(handler, 500, {"ok": False, "error": str(exc)})
            return
        send_json(handler, 200, {"ok": True, "slug": slug, "proposal": proposal})

    def _handle_confirm(handler: BaseHTTPRequestHandler) -> None:
        try:
            payload = _read_json_payload(handler, read_json_body)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return
        try:
            request = parse_confirm_request(payload)
        except ConfirmValidationError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return
        try:
            result = confirm_mapping(
                request,
                data_root=data_root,
                registry=_registry(),
                confirmed_by="operator",
            )
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return
        send_json(handler, 200, result)

    def _handle_build(handler: BaseHTTPRequestHandler) -> None:
        try:
            payload = _read_json_payload(handler, read_json_body)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            send_json(handler, 400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return
        slugs_raw = payload.get("slugs") if isinstance(payload, dict) else None
        if not isinstance(slugs_raw, list) or not all(isinstance(s, str) for s in slugs_raw):
            send_json(handler, 400, {"ok": False, "error": "slugs must be a list of strings"})
            return
        slugs = [s.strip() for s in slugs_raw if s.strip()]
        if not slugs:
            send_json(handler, 400, {"ok": False, "error": "slugs must be a non-empty list"})
            return
        try:
            outcome = build_export_bundle(
                slugs,
                data_root=data_root,
                registry=_registry(),
                request_id="etaly-export",
            )
        except ExportBlockedError as exc:
            send_json(
                handler,
                422,
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "export_blocked",
                    "unresolved_slugs": exc.unresolved_slugs,
                },
            )
            return
        except LintGateError as exc:
            send_json(
                handler,
                422,
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "lint_failed",
                    "failures": exc.failures,
                    "report": getattr(exc, "report_text", format_report([])),
                },
            )
            return
        except ValueError as exc:
            send_json(handler, 400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            Log(ERROR_LOG_LEVEL, "etaly build failed", {"error": str(exc)})
            send_json(handler, 500, {"ok": False, "error": str(exc)})
            return

        content = outcome.zip_path.read_bytes()
        _send_zip_download(handler, content, "etaly_export.zip")
        Log(
            INFO_LOG_LEVEL,
            "etaly export downloaded",
            {"included": outcome.included, "excluded": outcome.excluded},
        )

    def try_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
        routes = {
            "/api/etaly/export/propose": _handle_propose,
            "/api/etaly/export/confirm": _handle_confirm,
            "/api/etaly/export/build": _handle_build,
        }
        handler_fn = routes.get(path)
        if handler_fn is None:
            return False
        handler_fn(handler)
        return True

    return try_get, try_post

from __future__ import annotations

import csv
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rapidfuzz import fuzz

from src.core.log import INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.ingestion.polyindex.index_md_parser import normalize_label

SCHEMA_VERSION = "1.0"
RegistrySchemaVersion = Literal["1.0"]

PohType = Literal["p", "o", "m"]
Source = Literal["auto", "manual"]
Status = Literal["resolved", "pending"]

POH_TYPES: tuple[PohType, ...] = ("p", "o", "m")

_MIN_ID_WIDTH = 4
_POH_ID_PATTERN = re.compile(r"^poh_([pom])(\d+)$")
_FRONTMATTER_KEY_PATTERN = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")

DEFAULT_FUZZY_THRESHOLD = 0.82


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_registry_path() -> Path:
    """Persistence file for the slug -> poh_id mapping (under the repo data dir)."""
    return _repo_root() / "data" / "etaly" / "registry.json"


def default_etaly_assets_path() -> Path:
    """Default location of the sibling E-TALY assets tree."""
    return _repo_root().parent / "E-TALY" / "e_taly" / "assets"


def parse_poh_id(poh_id: str) -> tuple[PohType, int] | None:
    match = _POH_ID_PATTERN.match(poh_id.strip())
    if match is None:
        return None
    poh_type: PohType = match.group(1)  # type: ignore[assignment]
    return poh_type, int(match.group(2))


def format_poh_id(poh_type: PohType, number: int) -> str:
    return f"poh_{poh_type}{number:0{_MIN_ID_WIDTH}d}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _similarity(normalized_a: str, normalized_b: str) -> float:
    if not normalized_a or not normalized_b:
        return 0.0
    return fuzz.token_sort_ratio(normalized_a, normalized_b) / 100.0


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slug: str
    poh_id: str | None = None
    poh_type: PohType | None = None
    name: str = ""
    wikidata_qid: str | None = None
    source: Source = "auto"
    status: Status = "pending"
    score: float | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None


class MatchCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    poh_id: str | None = None
    poh_type: PohType | None = None
    name: str = ""
    wikidata_qid: str | None = None
    score: float | None = None
    source: Source = "auto"


class RegistryDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: RegistrySchemaVersion = SCHEMA_VERSION
    entries: dict[str, RegistryEntry] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=lambda: {t: 0 for t in POH_TYPES})

    @classmethod
    def empty(cls) -> RegistryDocument:
        return cls(schema_version=SCHEMA_VERSION, entries={}, counters={t: 0 for t in POH_TYPES})

    @classmethod
    def load_file(cls, path: Path) -> RegistryDocument:
        if not path.is_file():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            Log(WARNING_LOG_LEVEL, "etaly registry unreadable; starting empty", {"path": str(path)})
            return cls.empty()
        if not isinstance(raw, dict):
            return cls.empty()
        try:
            document = cls.model_validate(raw)
        except ValidationError:
            Log(WARNING_LOG_LEVEL, "etaly registry invalid; starting empty", {"path": str(path)})
            return cls.empty()
        document.ensure_counters()
        return document

    def ensure_counters(self) -> None:
        for poh_type in POH_TYPES:
            self.counters.setdefault(poh_type, 0)

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def write_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.to_json_bytes()
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            tmp_path.write_bytes(content)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.is_file():
                tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class AssetCandidate:
    poh_id: str
    poh_type: PohType
    name: str
    normalized_name: str


@dataclass(frozen=True)
class EtalyAssetIndex:
    qid_to_poh: dict[str, str]
    name_to_poh: dict[str, str]
    poh_to_name: dict[str, str]
    candidates: list[AssetCandidate]
    max_number: dict[str, int]


def _parse_frontmatter(md_path: Path) -> dict[str, str] | None:
    """Minimal YAML-frontmatter reader: flat ``key: value`` pairs between the leading fences."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.lstrip().startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FRONTMATTER_KEY_PATTERN.match(line)
        if match is None:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result.setdefault(key, value)
    return result


def scan_etaly_assets(assets_path: Path) -> EtalyAssetIndex:
    """Build lookup indexes from the E-TALY catalog CSVs and article frontmatter.

    ``max_number`` reflects the highest ``NNNN`` per type found in the CSVs, which is the
    authoritative id-assignment catalog. Markdown ids of other types (e.g. ``e``/``s``) are
    ignored so that resolved mappings only ever point at a supported ``p``/``o``/``m`` id.
    """
    csv_dir = assets_path / "timeline" / "data" / "csv"
    md_dir = assets_path / "timeline" / "data" / "text" / "ITA"

    qid_to_poh: dict[str, str] = {}
    name_to_poh: dict[str, str] = {}
    poh_to_name: dict[str, str] = {}
    candidates: list[AssetCandidate] = []
    max_number: dict[str, int] = {t: 0 for t in POH_TYPES}
    seen_candidates: set[tuple[str, str]] = set()

    def _add_candidate(poh_id: str, poh_type: PohType, name: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        normalized = normalize_label(cleaned)
        if not normalized:
            return
        poh_to_name.setdefault(poh_id, cleaned)
        name_to_poh.setdefault(normalized, poh_id)
        key = (poh_id, normalized)
        if key in seen_candidates:
            return
        seen_candidates.add(key)
        candidates.append(AssetCandidate(poh_id, poh_type, cleaned, normalized))

    for poh_type in POH_TYPES:
        csv_path = csv_dir / f"poh_{poh_type}.csv"
        if not csv_path.is_file():
            continue
        try:
            with csv_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    poh_id = (row.get("id_code") or "").strip()
                    parsed = parse_poh_id(poh_id)
                    if parsed is None or parsed[0] != poh_type:
                        continue
                    max_number[poh_type] = max(max_number[poh_type], parsed[1])
                    _add_candidate(poh_id, poh_type, row.get("name") or "")
        except (OSError, csv.Error) as exc:
            Log(WARNING_LOG_LEVEL, "etaly csv unreadable", {"path": str(csv_path), "error": str(exc)})

    if md_dir.is_dir():
        for md_path in sorted(md_dir.glob("*.md")):
            frontmatter = _parse_frontmatter(md_path)
            if not frontmatter:
                continue
            poh_id = (frontmatter.get("id") or md_path.stem).strip()
            parsed = parse_poh_id(poh_id)
            if parsed is None:
                continue
            poh_type = parsed[0]
            qid = (frontmatter.get("wikidata_qid") or "").strip()
            if qid:
                qid_to_poh.setdefault(qid, poh_id)
            for name_key in ("name", "wiki_title"):
                _add_candidate(poh_id, poh_type, frontmatter.get(name_key) or "")

    return EtalyAssetIndex(
        qid_to_poh=qid_to_poh,
        name_to_poh=name_to_poh,
        poh_to_name=poh_to_name,
        candidates=candidates,
        max_number=max_number,
    )


class EtalyRegistry:
    """Maps librarAIn slugs to E-TALY ``poh_{p|o|m}NNNN`` ids with JSON persistence.

    The registry is the single source of truth for id assignment: per-type counters are
    persisted so an assigned number is never reused, even if a proposal is later discarded
    or the process restarts.
    """

    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        assets_path: str | Path | None = None,
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    ) -> None:
        self.registry_path = Path(registry_path) if registry_path else default_registry_path()
        self.assets_path = Path(assets_path) if assets_path else default_etaly_assets_path()
        self.fuzzy_threshold = float(fuzzy_threshold)
        self._lock = threading.RLock()
        self._asset_index: EtalyAssetIndex | None = None
        self.document = RegistryDocument.empty()
        self.load()

    def load(self) -> RegistryDocument:
        with self._lock:
            self.document = RegistryDocument.load_file(self.registry_path)
            self.document.ensure_counters()
        return self.document

    def save(self) -> None:
        with self._lock:
            self.document.write_atomic(self.registry_path)

    def asset_index(self, *, refresh: bool = False) -> EtalyAssetIndex:
        with self._lock:
            if self._asset_index is None or refresh:
                self._asset_index = scan_etaly_assets(self.assets_path)
            return self._asset_index

    def get(self, slug: str) -> RegistryEntry | None:
        with self._lock:
            entry = self.document.entries.get(slug)
            return entry.model_copy(deep=True) if entry is not None else None

    def resolve(self, slug: str) -> RegistryEntry | None:
        with self._lock:
            entry = self.document.entries.get(slug)
            if entry is not None and entry.status == "resolved":
                return entry.model_copy(deep=True)
            return None

    def next_id(self, poh_type: str) -> str:
        if poh_type not in POH_TYPES:
            raise ValueError(f"unsupported poh_type: {poh_type!r} (expected one of {POH_TYPES})")
        typed: PohType = poh_type  # type: ignore[assignment]
        with self._lock:
            asset_max = self.asset_index().max_number.get(typed, 0)
            registry_max = self._registry_max_number(typed)
            stored = self.document.counters.get(typed, 0)
            next_number = max(stored, asset_max, registry_max) + 1
            self.document.counters[typed] = next_number
            self.save()
            return format_poh_id(typed, next_number)

    def propose(self, slug: str, candidate: MatchCandidate) -> RegistryEntry:
        with self._lock:
            poh_type = candidate.poh_type
            if poh_type is None and candidate.poh_id:
                parsed = parse_poh_id(candidate.poh_id)
                poh_type = parsed[0] if parsed is not None else None
            entry = RegistryEntry(
                slug=slug,
                poh_id=candidate.poh_id,
                poh_type=poh_type,
                name=candidate.name,
                wikidata_qid=candidate.wikidata_qid,
                source=candidate.source,
                status="pending",
                score=candidate.score,
            )
            self.document.entries[slug] = entry
            self.save()
        Log(
            INFO_LOG_LEVEL,
            "etaly registry proposal recorded",
            {"slug": slug, "poh_id": entry.poh_id, "score": entry.score},
        )
        return entry.model_copy(deep=True)

    def confirm(self, slug: str, poh_id: str, confirmed_by: str | None = None) -> RegistryEntry:
        parsed = parse_poh_id(poh_id)
        if parsed is None:
            raise ValueError(f"invalid poh_id: {poh_id!r}")
        poh_type = parsed[0]
        with self._lock:
            existing = self.document.entries.get(slug)
            entry = RegistryEntry(
                slug=slug,
                poh_id=poh_id,
                poh_type=poh_type,
                name=existing.name if existing is not None else "",
                wikidata_qid=existing.wikidata_qid if existing is not None else None,
                source="manual",
                status="resolved",
                score=existing.score if existing is not None else None,
                confirmed_by=confirmed_by,
                confirmed_at=_utcnow_iso(),
            )
            self.document.entries[slug] = entry
            self.save()
        Log(
            INFO_LOG_LEVEL,
            "etaly registry entry confirmed",
            {"slug": slug, "poh_id": poh_id, "confirmed_by": confirmed_by},
        )
        return entry.model_copy(deep=True)

    def auto_match(
        self,
        slug: str,
        canonical_label: str,
        *,
        aliases: list[str] | None = None,
        wikidata_qid: str | None = None,
        refresh: bool = False,
    ) -> RegistryEntry:
        index = self.asset_index(refresh=refresh)
        qid = (wikidata_qid or "").strip()

        with self._lock:
            if qid and qid in index.qid_to_poh:
                poh_id = index.qid_to_poh[qid]
                parsed = parse_poh_id(poh_id)
                entry = RegistryEntry(
                    slug=slug,
                    poh_id=poh_id,
                    poh_type=parsed[0] if parsed is not None else None,
                    name=index.poh_to_name.get(poh_id, canonical_label),
                    wikidata_qid=qid,
                    source="auto",
                    status="resolved",
                    score=1.0,
                )
                self.document.entries[slug] = entry
                self.save()
                Log(
                    INFO_LOG_LEVEL,
                    "etaly registry auto-match via wikidata_qid",
                    {"slug": slug, "poh_id": poh_id, "wikidata_qid": qid},
                )
                return entry.model_copy(deep=True)

            best = self._best_fuzzy_candidate(canonical_label, aliases, index)
            if best is not None and best[1] >= self.fuzzy_threshold:
                candidate, score = best
                entry = RegistryEntry(
                    slug=slug,
                    poh_id=candidate.poh_id,
                    poh_type=candidate.poh_type,
                    name=candidate.name,
                    wikidata_qid=qid or None,
                    source="auto",
                    status="pending",
                    score=score,
                )
            else:
                entry = RegistryEntry(
                    slug=slug,
                    poh_id=None,
                    poh_type=None,
                    name=canonical_label,
                    wikidata_qid=qid or None,
                    source="auto",
                    status="pending",
                    score=best[1] if best is not None else None,
                )
            self.document.entries[slug] = entry
            self.save()
        Log(
            INFO_LOG_LEVEL,
            "etaly registry auto-match",
            {"slug": slug, "poh_id": entry.poh_id, "status": entry.status, "score": entry.score},
        )
        return entry.model_copy(deep=True)

    def _registry_max_number(self, poh_type: PohType) -> int:
        best = 0
        for entry in self.document.entries.values():
            if entry.poh_id is None:
                continue
            parsed = parse_poh_id(entry.poh_id)
            if parsed is None or parsed[0] != poh_type:
                continue
            best = max(best, parsed[1])
        return best

    def _best_fuzzy_candidate(
        self,
        canonical_label: str,
        aliases: list[str] | None,
        index: EtalyAssetIndex,
    ) -> tuple[AssetCandidate, float] | None:
        labels = [canonical_label, *(aliases or [])]
        normalized_labels = [normalize_label(label) for label in labels if label and label.strip()]
        normalized_labels = [norm for norm in normalized_labels if norm]
        if not normalized_labels or not index.candidates:
            return None
        best_candidate: AssetCandidate | None = None
        best_score = -1.0
        for candidate in index.candidates:
            for normalized in normalized_labels:
                score = _similarity(normalized, candidate.normalized_name)
                if score > best_score:
                    best_score = score
                    best_candidate = candidate
        if best_candidate is None:
            return None
        return best_candidate, best_score

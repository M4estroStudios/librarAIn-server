from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _empty_time_index() -> dict[str, Any]:
    return {"schema_version": "1.0", "years": {}, "dates": {}}


def _load_time_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_time_index()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_time_index()
    if not isinstance(raw, dict):
        return _empty_time_index()
    years = raw.get("years")
    dates = raw.get("dates")
    return {
        "schema_version": raw.get("schema_version", "1.0"),
        "years": years if isinstance(years, dict) else {},
        "dates": dates if isinstance(dates, dict) else {},
    }


@dataclass
class PohTimeRangeIndex:
    _by_poh_id: dict[str, str]
    _year_labels: list[str]

    @classmethod
    def build(cls, time_index_path: Path) -> PohTimeRangeIndex:
        time_index = _load_time_index(time_index_path)
        years = time_index.get("years", {})
        if not isinstance(years, dict):
            years = {}
        by_poh_id: dict[str, str] = {}
        year_labels: list[str] = []
        for year_label, entry in years.items():
            year_text = str(year_label)
            year_labels.append(year_text)
            if not isinstance(entry, dict):
                continue
            subjects = entry.get("subjects", [])
            if isinstance(subjects, list):
                for poh_id in subjects:
                    if isinstance(poh_id, str) and poh_id not in by_poh_id:
                        by_poh_id[poh_id] = year_text
            pages = entry.get("pages", {})
            if isinstance(pages, dict):
                for book_pages in pages.values():
                    if not isinstance(book_pages, dict):
                        continue
                    for poh_id in book_pages:
                        if isinstance(poh_id, str) and poh_id not in by_poh_id:
                            by_poh_id[poh_id] = year_text
        return cls(_by_poh_id=by_poh_id, _year_labels=year_labels)

    def lookup(self, poh_id: str, label: str) -> str | None:
        hit = self._by_poh_id.get(poh_id)
        if hit is not None:
            return hit
        label_lower = label.casefold()
        for year_label in self._year_labels:
            year_lower = year_label.casefold()
            if year_lower in label_lower or label_lower in year_lower:
                return year_label
        return None


@dataclass(frozen=True)
class _PohTimeRangeCacheEntry:
    mtime_ns: int
    index: PohTimeRangeIndex


_time_range_cache: dict[Path, _PohTimeRangeCacheEntry] = {}


def _time_index_mtime_ns(path: Path) -> int:
    if not path.is_file():
        return 0
    return path.stat().st_mtime_ns


def get_poh_time_range_index(polyindex_dir: Path) -> PohTimeRangeIndex:
    path = (polyindex_dir / "TIME_INDEX.json").resolve()
    mtime_ns = _time_index_mtime_ns(path)
    cached = _time_range_cache.get(path)
    if cached is not None and cached.mtime_ns == mtime_ns:
        return cached.index
    index = PohTimeRangeIndex.build(path)
    _time_range_cache[path] = _PohTimeRangeCacheEntry(mtime_ns=mtime_ns, index=index)
    return index


def clear_poh_time_range_cache() -> None:
    _time_range_cache.clear()


def lookup_poh_time_range(polyindex_dir: Path, poh_id: str, label: str) -> str | None:
    return get_poh_time_range_index(polyindex_dir).lookup(poh_id, label)

from __future__ import annotations

import json
from pathlib import Path

from src.search.time_lookup import load_time_index


def lookup_poh_time_range(polyindex_dir: Path, poh_id: str, label: str) -> str | None:
    time_index = load_time_index(polyindex_dir / "TIME_INDEX.json")
    years = time_index.get("years", {})
    if not isinstance(years, dict):
        return None
    label_lower = label.casefold()
    for year_label, entry in years.items():
        if not isinstance(entry, dict):
            continue
        subjects = entry.get("subjects", [])
        if isinstance(subjects, list) and poh_id in subjects:
            return str(year_label)
        pages = entry.get("pages", {})
        if isinstance(pages, dict):
            for book_pages in pages.values():
                if isinstance(book_pages, dict) and poh_id in book_pages:
                    return str(year_label)
    for year_label, entry in years.items():
        if str(year_label).casefold() in label_lower or label_lower in str(year_label).casefold():
            return str(year_label)
    return None

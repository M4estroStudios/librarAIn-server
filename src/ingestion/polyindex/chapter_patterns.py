from __future__ import annotations

import re
from typing import NamedTuple


class ChapterLineMatch(NamedTuple):
    label: str
    original_page: int


CAPITOLO_PATTERN = re.compile(
    r"^Capitolo\s+([IVXLCDM]+|\d+)\s*(?:[.\s\u00B7·…—\-]+)*\s*(\d+)\s*$",
    re.IGNORECASE,
)

CAP_PATTERN = re.compile(
    r"^Cap\.\s*(\d+)\s*(?:[—\-]\s*)?(.*?)\s+(\d+)\s*$",
    re.IGNORECASE,
)

TITLE_PAGE_PATTERN = re.compile(
    r"^(?P<label>[A-Za-zÀ-ÿ].+?)\s+(\d+)\s*$",
)


def try_match_chapter_line(line: str) -> ChapterLineMatch | None:
    stripped = line.strip()
    if not stripped:
        return None

    match = CAPITOLO_PATTERN.match(stripped)
    if match:
        numeral = match.group(1)
        return ChapterLineMatch(
            label=f"Capitolo {numeral}",
            original_page=int(match.group(2)),
        )

    match = CAP_PATTERN.match(stripped)
    if match:
        number = match.group(1)
        title = match.group(2).strip()
        page = int(match.group(3))
        if title:
            label = f"Cap. {number} — {title}"
        else:
            label = f"Cap. {number}"
        return ChapterLineMatch(label=label, original_page=page)

    if re.match(r"^Cap(?:itolo|\.)", stripped, re.IGNORECASE):
        return None

    match = TITLE_PAGE_PATTERN.match(stripped)
    if match:
        return ChapterLineMatch(
            label=match.group("label").strip(),
            original_page=int(match.group(2)),
        )

    return None

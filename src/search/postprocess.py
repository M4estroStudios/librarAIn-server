from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.models.polyindex_index import PolyindexIndexDocument

_SOURCE_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\((source:[^)]+)\)",
    re.IGNORECASE,
)
_POH_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\((poh:[^)]+)\)",
    re.IGNORECASE,
)
_SOURCE_URL_PATTERN = re.compile(
    r"^source:(?P<sha>[a-f0-9]+):aligned:(?P<page>\d+)$",
    re.IGNORECASE,
)
_CRONOLOGIA_HEADER = "## Cronologia"
_ANNOTAZIONI_HEADER = "## Annotazioni"
_TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|\s*$")
_UNVERIFIABLE = "*[[fonte non verificabile]]*"
_PERIOD_YEAR_PATTERN = re.compile(r"(\d{3,4})")
_INLINE_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class CitationRecord:
    source_sha256: str
    aligned_page: int
    label: str


@dataclass(frozen=True)
class PohReferenceRecord:
    poh_id: str
    label: str
    linked_from_count: int


@dataclass(frozen=True)
class TimelineRowRecord:
    period: str
    event: str
    source_links: list[str]


@dataclass
class PostprocessResult:
    markdown: str
    citations: list[CitationRecord] = field(default_factory=list)
    pohs_referenced: list[PohReferenceRecord] = field(default_factory=list)
    timeline_rows: list[TimelineRowRecord] = field(default_factory=list)
    invalid_source_links: int = 0
    invalid_poh_links: int = 0


@dataclass(frozen=True)
class _ManifestIndex:
    pages_by_book: dict[str, set[int]]


def _load_manifest_index(data_root: Path) -> _ManifestIndex:
    pages_by_book: dict[str, set[int]] = {}
    output_dir = data_root / "output"
    if not output_dir.is_dir():
        return _ManifestIndex(pages_by_book=pages_by_book)
    for book_dir in output_dir.iterdir():
        if not book_dir.is_dir():
            continue
        manifest_path = book_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        sha = str(raw.get("source_sha256") or book_dir.name)
        aligned: set[int] = set()
        pages = raw.get("pages")
        if isinstance(pages, list):
            for entry in pages:
                if isinstance(entry, dict) and isinstance(entry.get("aligned"), int):
                    aligned.add(entry["aligned"])
        pages_by_book[sha] = aligned
    return _ManifestIndex(pages_by_book=pages_by_book)


def _is_valid_source(url: str, manifest: _ManifestIndex) -> tuple[bool, str | None, int | None]:
    match = _SOURCE_URL_PATTERN.match(url.strip())
    if match is None:
        return False, None, None
    sha = match.group("sha").lower()
    page = int(match.group("page"))
    known = manifest.pages_by_book.get(sha)
    if known is None or page not in known:
        return False, sha, page
    return True, sha, page


def _is_valid_poh(url: str, known_ids: set[str]) -> tuple[bool, str]:
    poh_id = url.strip().removeprefix("poh:").removeprefix("POH:")
    if poh_id.startswith("unknown-"):
        return True, poh_id
    if poh_id in known_ids:
        return True, poh_id
    return False, poh_id


def _replace_source_links(
    markdown: str,
    manifest: _ManifestIndex,
    *,
    request_id: str,
) -> tuple[str, list[CitationRecord], int]:
    citations: dict[tuple[str, int], CitationRecord] = {}
    invalid = 0

    def replacer(match: re.Match[str]) -> str:
        nonlocal invalid
        label = match.group(1)
        url = match.group(2)
        valid, sha, page = _is_valid_source(url, manifest)
        if not valid or sha is None or page is None:
            invalid += 1
            Log(
                WARNING_LOG_LEVEL,
                "research postprocess invalid source link removed",
                {"request_id": request_id, "url": url},
            )
            return _UNVERIFIABLE
        key = (sha, page)
        if key not in citations:
            citations[key] = CitationRecord(
                source_sha256=sha,
                aligned_page=page,
                label=label.strip() or f"p.{page}",
            )
        return match.group(0)

    cleaned = _SOURCE_LINK_PATTERN.sub(replacer, markdown)
    return cleaned, sorted(
        citations.values(),
        key=lambda item: (item.source_sha256, item.aligned_page),
    ), invalid


def _replace_poh_links(
    markdown: str,
    known_ids: set[str],
    *,
    request_id: str,
) -> tuple[str, dict[str, PohReferenceRecord], int]:
    counts: dict[str, PohReferenceRecord] = {}
    invalid = 0

    def replacer(match: re.Match[str]) -> str:
        nonlocal invalid
        label = match.group(1)
        url = match.group(2)
        valid, poh_id = _is_valid_poh(url, known_ids)
        if not valid:
            invalid += 1
            Log(
                WARNING_LOG_LEVEL,
                "research postprocess invalid poh link removed",
                {"request_id": request_id, "url": url, "poh_id": poh_id},
            )
            return label
        full_id = f"poh:{poh_id}"
        existing = counts.get(full_id)
        if existing is None:
            counts[full_id] = PohReferenceRecord(
                poh_id=poh_id,
                label=label.strip() or poh_id,
                linked_from_count=1,
            )
        else:
            counts[full_id] = PohReferenceRecord(
                poh_id=existing.poh_id,
                label=existing.label,
                linked_from_count=existing.linked_from_count + 1,
            )
        return match.group(0)

    cleaned = _POH_LINK_PATTERN.sub(replacer, markdown)
    return cleaned, counts, invalid


def _period_sort_key(period: str) -> tuple[int, str]:
    text = period.strip().lower().replace("a.c.", "bc").replace("d.c.", "ad")
    years = [int(match) for match in _PERIOD_YEAR_PATTERN.findall(text)]
    if not years:
        return (999999, period.casefold())
    primary = years[0]
    if "bc" in text and "ad" not in text:
        primary = -primary
    return (primary, period.casefold())


def _split_table_row(line: str) -> list[str] | None:
    match = _TABLE_ROW_PATTERN.match(line.strip())
    if match is None:
        return None
    return [cell.strip() for cell in match.group(1).split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    if len(cells) != 3:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _extract_source_links(cell: str) -> list[str]:
    return [
        match.group(2)
        for match in _SOURCE_LINK_PATTERN.finditer(cell)
        if _SOURCE_URL_PATTERN.match(match.group(2).strip())
    ]


def _strip_annotazioni_section(markdown: str) -> str:
    idx = markdown.find(_ANNOTAZIONI_HEADER)
    if idx < 0:
        return markdown
    suffix = markdown[idx + len(_ANNOTAZIONI_HEADER) :]
    rest_start = len(suffix)
    for match in re.finditer(r"^## ", suffix, flags=re.MULTILINE):
        rest_start = match.start()
        break
    return markdown[:idx].rstrip() + suffix[rest_start:].lstrip("\n")


def _process_cronologia_section(
    markdown: str,
    *,
    request_id: str,
) -> tuple[str, list[TimelineRowRecord]]:
    idx = markdown.find(_CRONOLOGIA_HEADER)
    if idx < 0:
        return markdown, []

    prefix = markdown[:idx]
    suffix = markdown[idx + len(_CRONOLOGIA_HEADER) :]
    lines = suffix.splitlines()
    if not lines or lines[0].strip():
        body_lines = lines
        tail = ""
    else:
        body_lines = lines[1:]
        tail = ""

    table_lines: list[str] = []
    rest_lines: list[str] = []
    in_table = False
    for line in body_lines:
        stripped = line.strip()
        if not in_table and stripped.startswith("|"):
            in_table = True
        if in_table and stripped.startswith("|"):
            table_lines.append(line)
            continue
        if in_table and not stripped:
            in_table = False
            rest_lines.append(line)
            continue
        rest_lines.append(line)

    if len(table_lines) < 2:
        return markdown, []

    header = _split_table_row(table_lines[0])
    if header != ["Periodo", "Evento", "Fonti"]:
        Log(
            WARNING_LOG_LEVEL,
            "research postprocess cronologia header invalid",
            {"request_id": request_id, "header": header},
        )
        return markdown, []

    data_rows: list[list[str]] = []
    for line in table_lines[2:]:
        cells = _split_table_row(line)
        if cells is None or _is_separator_row(cells):
            continue
        if len(cells) != 3:
            continue
        data_rows.append(cells)

    sorted_rows = sorted(data_rows, key=lambda row: _period_sort_key(row[0]))
    if sorted_rows != data_rows:
        Log(
            INFO_LOG_LEVEL,
            "research postprocess cronologia rows reordered",
            {"request_id": request_id, "row_count": len(sorted_rows)},
        )

    timeline_rows: list[TimelineRowRecord] = []
    rebuilt = [
        _CRONOLOGIA_HEADER,
        "",
        "| Periodo | Evento | Fonti |",
        "|---------|--------|-------|",
    ]
    for period, event, sources in sorted_rows:
        rebuilt.append(f"| {period} | {event} | {sources} |")
        timeline_rows.append(
            TimelineRowRecord(
                period=period,
                event=event,
                source_links=_extract_source_links(sources),
            )
        )

    rebuilt_text = "\n".join(rebuilt)
    if rest_lines:
        rebuilt_text += "\n" + "\n".join(rest_lines)
    return prefix.rstrip() + "\n\n" + rebuilt_text + tail, timeline_rows


def postprocess_markdown(
    markdown: str,
    *,
    data_root: Path,
    index_document: PolyindexIndexDocument,
    request_id: str = "",
) -> PostprocessResult:
    manifest = _load_manifest_index(data_root)
    known_poh_ids = set(index_document.subjects.keys())

    with_sources, citations, invalid_sources = _replace_source_links(
        markdown,
        manifest,
        request_id=request_id,
    )
    with_poh, poh_map, invalid_poh = _replace_poh_links(
        with_sources,
        known_poh_ids,
        request_id=request_id,
    )
    without_annotazioni = _strip_annotazioni_section(with_poh)
    final_markdown, timeline_rows = _process_cronologia_section(
        without_annotazioni,
        request_id=request_id,
    )

    pohs_referenced = sorted(
        poh_map.values(),
        key=lambda item: (-item.linked_from_count, item.poh_id),
    )

    Log(
        INFO_LOG_LEVEL,
        "research postprocess completed",
        {
            "request_id": request_id,
            "citations": len(citations),
            "poh_links": len(pohs_referenced),
            "timeline_rows": len(timeline_rows),
            "invalid_source_links": invalid_sources,
            "invalid_poh_links": invalid_poh,
        },
    )

    return PostprocessResult(
        markdown=final_markdown.strip() + "\n",
        citations=citations,
        pohs_referenced=pohs_referenced,
        timeline_rows=timeline_rows,
        invalid_source_links=invalid_sources,
        invalid_poh_links=invalid_poh,
    )


def markdown_to_article_html(title: str, markdown: str, *, no_material: bool = False) -> str:
    body = _markdown_body_to_html(_strip_annotazioni_section(markdown))
    safe_title = escape(title)
    notice = ""
    if no_material:
        notice = '<p class="notice">Materiale insufficiente: nessuna fonte pertinente disponibile.</p>\n'
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title} — librarAIn</title>
<style>
:root {{
  color-scheme: dark;
  font-family: system-ui, sans-serif;
  line-height: 1.55;
  color: #d4d4d4;
  background: #1e1e1e;
}}
body {{ margin: 0 auto; max-width: 46rem; padding: 1.5rem 1rem 3rem; }}
a {{ color: #4ec9b0; word-break: break-word; }}
h1 {{ font-size: 1.6rem; margin: 0 0 1rem; color: #e8e8e8; }}
h2 {{ font-size: 1.1rem; margin: 1.5rem 0 0.6rem; color: #e8e8e8; }}
p, li {{ color: #c8c8c8; }}
.notice {{ color: #f0ad4e; font-size: 0.95rem; margin-bottom: 1rem; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.92rem; }}
th, td {{ border: 1px solid #444; padding: 0.45rem 0.55rem; vertical-align: top; }}
th {{ background: #2d2d2d; color: #e8e8e8; }}
.nav {{ margin-bottom: 1.2rem; font-size: 0.9rem; }}
ul {{ margin: 0.5rem 0 1rem; padding-left: 1.4rem; }}
</style>
</head>
<body>
<p class="nav"><a href="/ricerca.html">← Ricerca</a></p>
<h1>{safe_title}</h1>
{notice}{body}
</body>
</html>
"""


def _markdown_body_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    html_parts: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_table = False
    table_rows: list[list[str]] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(paragraph).strip()
        paragraph.clear()
        if text:
            html_parts.append(f"<p>{_inline_markdown(text)}</p>")

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        items = "".join(f"<li>{_inline_markdown(item)}</li>" for item in list_items)
        html_parts.append(f"<ul>{items}</ul>")
        list_items = []

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not table_rows:
            in_table = False
            return
        html_parts.append("<table>")
        for idx, row in enumerate(table_rows):
            tag = "th" if idx == 0 else "td"
            cells = "".join(f"<{tag}>{_inline_markdown(cell)}</{tag}>" for cell in row)
            html_parts.append(f"<tr>{cells}</tr>")
        html_parts.append("</table>")
        table_rows = []
        in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_list()
            flush_paragraph()
            cells = _split_table_row(stripped)
            if cells is None or _is_separator_row(cells):
                continue
            in_table = True
            table_rows.append(cells)
            continue
        if in_table:
            flush_table()
        if not stripped:
            flush_list()
            flush_paragraph()
            continue
        if stripped.startswith("## "):
            flush_list()
            flush_paragraph()
            html_parts.append(f"<h2>{escape(stripped[3:].strip())}</h2>")
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            list_items.append(stripped[2:].strip())
            continue
        flush_list()
        paragraph.append(stripped)

    if in_table:
        flush_table()
    flush_list()
    flush_paragraph()
    return "\n".join(html_parts)


def _inline_markdown(text: str) -> str:
    parts: list[str] = []
    last = 0
    for match in _INLINE_LINK_PATTERN.finditer(text):
        parts.append(escape(text[last : match.start()]))
        label = match.group(1)
        url = match.group(2)
        parts.append(
            f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
        )
        last = match.end()
    parts.append(escape(text[last:]))
    rendered = "".join(parts)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        r"<em>\1</em>",
        rendered,
    )
    return rendered

"""Deterministic adapter: finalized librarAIn article -> E-TALY article.

This module converts a research article produced by librarAIn (Markdown, Italian,
with ``poh:`` cross-links, ``source:`` citations and a ``## Cronologia`` GFM table)
into the format consumed by the E-TALY Flutter app (Markdown body preceded by a YAML
frontmatter block whose timeline is expressed as integer *year keys*).

The adapter is **fully deterministic**: it performs no network access and never calls
an LLM. The human-approved metadata (id, name, wiki fields, monument geo, and an
optional pre-fixed timeline) is passed in explicitly via :class:`ApprovedMetadata`.

BCE (``a.C.``) years
--------------------
E-TALY frontmatter timeline keys are integer years. There is no dedicated era flag,
so a ``NNNN a.C.`` period is represented as the **negative** integer ``-NNNN`` (e.g.
``509 a.C.`` -> key ``-509``). Because E-TALY support for negative year keys is not
guaranteed, every BCE entry also appends a ``warnings`` note so the reviewer can audit
it. The adapter never crashes on BCE input.

Hard gate (decision D-09)
-------------------------
If any ``poh:<slug>`` link does not resolve to a *resolved* registry entry, the whole
conversion is aborted with :class:`ExportBlockedError` listing every unresolved slug.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.export.registry import PohType

# --- Regexes -----------------------------------------------------------------
# ``[label](poh:<slug>)`` cross-links (slug may contain any char except ``)``).
_POH_LINK_RE = re.compile(r"\[([^\]]*)\]\(poh:([^)]+)\)", re.IGNORECASE)
# ``[label](source:<sha256>:aligned:<page>)`` inline citations, kept verbatim.
_SOURCE_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(source:([a-f0-9]+):aligned:(\d+)\)",
    re.IGNORECASE,
)
# ``[[File:...]]`` / ``[[Immagine:...]]`` media embeds (unsupported by E-TALY).
_FILE_EMBED_RE = re.compile(r"\[\[\s*(?:File|Immagine)\s*:[^\]]*\]\]", re.IGNORECASE)
# Raw HTML tags (unsupported by the E-TALY MDHandler).
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# Plain markdown links to the web: ``[x](http://y)`` -> degrade to ``x``.
_HTTP_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)", re.IGNORECASE)
# Wikilink used by E-TALY: ``[[poh_id|label]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
# Leading H1 (single ``#``) line.
_H1_RE = re.compile(r"^\s*#\s+(.*?)\s*$", re.MULTILINE)
# A GFM table data/separator row.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# First run of digits inside a period cell.
_YEAR_RE = re.compile(r"(\d{1,4})")
# ``a.C.`` / ``aC`` / ``a C`` era marker (Italian "avanti Cristo").
_BCE_RE = re.compile(r"\ba\.?\s*c\.?", re.IGNORECASE)

_CRONOLOGIA_HEADER = "## Cronologia"
_ANNOTAZIONI_HEADER = "## Annotazioni"
_FONTI_HEADER = "## Fonti"

_MAX_TIMELINE_ENTRIES = 5


class ExportBlockedError(RuntimeError):
    """Raised when an article cannot be exported because ``poh:`` slugs are unresolved.

    The offending slugs are available as :attr:`unresolved_slugs` and are also listed
    in the exception message (decision D-09, hard gate).
    """

    def __init__(self, unresolved_slugs: list[str]) -> None:
        self.unresolved_slugs = list(unresolved_slugs)
        joined = ", ".join(self.unresolved_slugs)
        super().__init__(f"export blocked: unresolved poh: slugs: {joined}")


class TimelineEntryInput(BaseModel):
    """A pre-approved timeline entry supplied by the reviewer."""

    model_config = ConfigDict(extra="ignore")

    anno: int
    evento: str


class ApprovedMetadata(BaseModel):
    """Human-approved values for the E-TALY frontmatter.

    ``timeline`` is optional: when present it overrides the article's ``## Cronologia``
    table. Monument geo fields (``poi_id``/``lat``/``lon``/``region``/``category``) are
    only emitted for ``poh_m`` entries and only when provided.
    """

    model_config = ConfigDict(extra="ignore")

    poh_id: str
    poh_type: PohType
    name: str | None = None

    wiki_title: str | None = None
    wiki_url: str | None = None
    wikidata_qid: str | None = None

    poi_id: int | str | None = None
    lat: float | None = None
    lon: float | None = None
    region: str | None = None
    category: str | None = None

    timeline: list[TimelineEntryInput] | None = None


@dataclass(frozen=True)
class _TimelineEntry:
    year: int
    evento: str
    is_bce: bool


@dataclass
class EtalyArticle:
    """Result of converting a librarAIn article to E-TALY format."""

    poh_id: str
    markdown: str
    warnings: list[str] = field(default_factory=list)
    cited_pages: set[tuple[str, int]] = field(default_factory=set)


# --- Small text helpers ------------------------------------------------------
def _to_plain_text(text: str) -> str:
    """Flatten E-TALY-unsupported inline syntax to human-readable plain text.

    Used for values (e.g. timeline events, the H1 title) that must live inside a YAML
    scalar and therefore cannot carry markdown links.
    """
    out = _WIKILINK_RE.sub(lambda m: m.group(2), text)
    out = _SOURCE_LINK_RE.sub(lambda m: m.group(1), out)
    out = _POH_LINK_RE.sub(lambda m: m.group(1), out)
    out = _HTTP_LINK_RE.sub(lambda m: m.group(1), out)
    out = out.replace("**", "")
    return re.sub(r"\s+", " ", out).strip()


def _yaml_scalar(value: str) -> str:
    """Serialize a string as a double-quoted YAML scalar (JSON-compatible escaping)."""
    return json.dumps(str(value), ensure_ascii=False)


# --- Transformation 1: poh: link rewrite ------------------------------------
def rewrite_poh_links(markdown: str, registry) -> tuple[str, list[str]]:
    """Rewrite ``[label](poh:<slug>)`` into ``[[<poh_id>|<label>]]``.

    Returns the rewritten markdown together with the sorted list of slugs that did not
    resolve to a *resolved* registry entry. The label text is preserved verbatim.
    """
    unresolved: list[str] = []
    seen_unresolved: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        label = match.group(1)
        slug = match.group(2).strip()
        entry = registry.resolve(slug)
        if entry is None or not getattr(entry, "poh_id", None):
            if slug not in seen_unresolved:
                seen_unresolved.add(slug)
                unresolved.append(slug)
            return match.group(0)
        return f"[[{entry.poh_id}|{label}]]"

    rewritten = _POH_LINK_RE.sub(_replace, markdown)
    return rewritten, sorted(unresolved)


# --- Transformation 2: source: citations ------------------------------------
def extract_cited_pages(markdown: str) -> set[tuple[str, int]]:
    """Collect the set of ``(source_sha256, aligned_page)`` referenced in the body."""
    pages: set[tuple[str, int]] = set()
    for match in _SOURCE_LINK_RE.finditer(markdown):
        pages.add((match.group(2).lower(), int(match.group(3))))
    return pages


# --- Transformation 3: Cronologia -> timeline -------------------------------
def normalize_year(period: str) -> tuple[int | None, bool]:
    """Normalize a ``Periodo`` cell to ``(year_key, is_bce)``.

    Accepts ``YYYY`` and ``YYYY d.C.`` as positive years and ``YYYY a.C.`` as a negative
    year key. Returns ``(None, False)`` when no year can be parsed (never raises).
    """
    text = period.strip()
    match = _YEAR_RE.search(text)
    if match is None:
        return None, False
    year = int(match.group(1))
    is_bce = _BCE_RE.search(text) is not None
    return (-year if is_bce else year), is_bce


def _parse_cronologia_rows(markdown: str) -> list[tuple[str, str]]:
    """Extract ``(period, event)`` pairs from the ``## Cronologia`` GFM table."""
    idx = markdown.find(_CRONOLOGIA_HEADER)
    if idx < 0:
        return []
    section = markdown[idx + len(_CRONOLOGIA_HEADER) :]
    next_header = re.search(r"^## ", section, flags=re.MULTILINE)
    if next_header is not None:
        section = section[: next_header.start()]

    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not _TABLE_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        period = cells[0]
        if period.lower() == "periodo":
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells if cell):
            continue
        rows.append((period, cells[1]))
    return rows


def build_timeline(
    markdown: str,
    approved: ApprovedMetadata,
    postprocess_result,
    warnings: list[str],
) -> list[_TimelineEntry]:
    """Resolve the timeline entries, preferring approved input over parsed data.

    Priority: ``approved.timeline`` -> ``## Cronologia`` table -> ``timeline_rows``.
    Events are flattened to plain text, sorted chronologically and capped at
    :data:`_MAX_TIMELINE_ENTRIES`.
    """
    raw_pairs: list[tuple[int, str, bool]] = []

    if approved.timeline:
        for item in approved.timeline:
            is_bce = item.anno < 0
            raw_pairs.append((item.anno, _to_plain_text(item.evento), is_bce))
    else:
        pairs = _parse_cronologia_rows(markdown)
        if not pairs and postprocess_result is not None:
            pairs = [(row.period, row.event) for row in postprocess_result.timeline_rows]
        for period, event in pairs:
            year, is_bce = normalize_year(period)
            if year is None:
                warnings.append(f"timeline row skipped (no year in period): {period!r}")
                continue
            raw_pairs.append((year, _to_plain_text(event), is_bce))

    entries: list[_TimelineEntry] = []
    seen_years: set[int] = set()
    for year, event, is_bce in sorted(raw_pairs, key=lambda item: item[0]):
        if year in seen_years:
            continue
        seen_years.add(year)
        entries.append(_TimelineEntry(year=year, evento=event, is_bce=is_bce))

    if len(entries) > _MAX_TIMELINE_ENTRIES:
        warnings.append(
            f"timeline truncated to {_MAX_TIMELINE_ENTRIES} of {len(entries)} entries"
        )
        entries = entries[:_MAX_TIMELINE_ENTRIES]

    for entry in entries:
        if entry.is_bce:
            warnings.append(
                f"BCE year {entry.year} emitted as negative key; "
                "verify E-TALY timeline support"
            )
    return entries


def remove_cronologia_section(markdown: str) -> str:
    """Remove the ``## Cronologia`` section from the body (decision D-10)."""
    idx = markdown.find(_CRONOLOGIA_HEADER)
    if idx < 0:
        return markdown
    after = markdown[idx + len(_CRONOLOGIA_HEADER) :]
    next_header = re.search(r"^## ", after, flags=re.MULTILINE)
    tail = after[next_header.start() :].lstrip("\n") if next_header is not None else ""
    prefix = markdown[:idx].rstrip()
    if tail:
        return prefix + "\n\n" + tail
    return prefix + "\n"


# --- Transformation 4: H1 removal -------------------------------------------
def strip_leading_h1(markdown: str) -> tuple[str, str | None]:
    """Remove the leading H1 and return ``(body, h1_plain_text)`` (decision D-15)."""
    match = _H1_RE.search(markdown)
    if match is None:
        return markdown, None
    h1_text = _to_plain_text(match.group(1))
    body = markdown[: match.start()] + markdown[match.end() :]
    return body.lstrip("\n"), h1_text


# --- Transformation 7: sanitize unsupported syntax --------------------------
def sanitize_body(markdown: str) -> tuple[str, list[str]]:
    """Remove/flag syntax the E-TALY MDHandler cannot render.

    Handles ``[[File:...]]``/``[[Immagine:...]]`` embeds, raw HTML tags, residual GFM
    tables and plain ``http(s)`` links. Preserves ``**bold**``, ``[[poh_id|label]]``,
    ``source:`` citations and ``##``/``###`` headings.
    """
    warnings: list[str] = []
    text = markdown

    if _FILE_EMBED_RE.search(text):
        for match in _FILE_EMBED_RE.finditer(text):
            warnings.append(f"removed unsupported media embed: {match.group(0)}")
        text = _FILE_EMBED_RE.sub("", text)

    if _HTML_TAG_RE.search(text):
        for match in _HTML_TAG_RE.finditer(text):
            warnings.append(f"removed raw HTML tag: {match.group(0)}")
        text = _HTML_TAG_RE.sub("", text)

    def _degrade_http(match: re.Match[str]) -> str:
        warnings.append(f"degraded web link to plain text: {match.group(0)}")
        return match.group(1)

    text = _HTTP_LINK_RE.sub(_degrade_http, text)

    cleaned_lines: list[str] = []
    table_block = 0
    for line in text.splitlines():
        if _TABLE_ROW_RE.match(line):
            table_block += 1
            continue
        if table_block:
            warnings.append(f"removed residual GFM table ({table_block} rows) from body")
            table_block = 0
        cleaned_lines.append(line)
    if table_block:
        warnings.append(f"removed residual GFM table ({table_block} rows) from body")

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
    return cleaned, warnings


# --- Transformation 8: frontmatter synthesis --------------------------------
def build_frontmatter(
    approved: ApprovedMetadata,
    name: str,
    timeline: list[_TimelineEntry],
) -> str:
    """Build a deterministic, valid YAML frontmatter block."""
    lines: list[str] = ["---", f"id: {approved.poh_id}", f"name: {_yaml_scalar(name)}"]

    if approved.wiki_title:
        lines.append(f"wiki_title: {_yaml_scalar(approved.wiki_title)}")
    if approved.wiki_url:
        lines.append(f"wiki_url: {_yaml_scalar(approved.wiki_url)}")
    if approved.wikidata_qid:
        lines.append(f"wikidata_qid: {_yaml_scalar(approved.wikidata_qid)}")

    if approved.poh_type == "m":
        if approved.poi_id is not None:
            if isinstance(approved.poi_id, int):
                lines.append(f"poi_id: {approved.poi_id}")
            else:
                lines.append(f"poi_id: {_yaml_scalar(approved.poi_id)}")
        if approved.lat is not None:
            lines.append(f"lat: {approved.lat}")
        if approved.lon is not None:
            lines.append(f"lon: {approved.lon}")
        if approved.region:
            lines.append(f"region: {_yaml_scalar(approved.region)}")
        if approved.category:
            lines.append(f"category: {_yaml_scalar(approved.category)}")

    for entry in timeline:
        lines.append(f"{entry.year}: {_yaml_scalar(entry.evento)}")

    lines.append("---")
    return "\n".join(lines)


# --- Top-level orchestration ------------------------------------------------
def to_etaly_article(
    article_markdown: str,
    postprocess_result,
    approved_metadata: ApprovedMetadata,
    registry,
) -> EtalyArticle:
    """Convert a finalized librarAIn article into an :class:`EtalyArticle`.

    Raises :class:`ExportBlockedError` if any ``poh:`` slug is unresolved (decision D-09).
    """
    warnings: list[str] = []

    cited_pages = extract_cited_pages(article_markdown)

    body, unresolved = rewrite_poh_links(article_markdown, registry)
    if unresolved:
        Log(
            WARNING_LOG_LEVEL,
            "etaly export blocked by unresolved poh slugs",
            {"poh_id": approved_metadata.poh_id, "unresolved": unresolved},
        )
        raise ExportBlockedError(unresolved)

    timeline = build_timeline(body, approved_metadata, postprocess_result, warnings)

    body = remove_cronologia_section(body)
    body, h1_text = strip_leading_h1(body)

    name = approved_metadata.name or h1_text or approved_metadata.poh_id
    if not approved_metadata.name and not h1_text:
        warnings.append("no name provided and no H1 found; defaulted to poh_id")

    body, sanitize_warnings = sanitize_body(body)
    warnings.extend(sanitize_warnings)

    frontmatter = build_frontmatter(approved_metadata, name, timeline)
    markdown = frontmatter + "\n\n" + body.strip() + "\n"

    Log(
        INFO_LOG_LEVEL,
        "etaly article built",
        {
            "poh_id": approved_metadata.poh_id,
            "timeline_entries": len(timeline),
            "cited_pages": len(cited_pages),
            "warnings": len(warnings),
        },
    )

    return EtalyArticle(
        poh_id=approved_metadata.poh_id,
        markdown=markdown,
        warnings=warnings,
        cited_pages=cited_pages,
    )

"""Pre-export **Lint "C"** (decision D-17): validate the FINAL E-TALY article.

This module is a *safety net* run over an already-produced :class:`EtalyArticle`
(the frontmatter + body Markdown) together with the set of source pages that were
actually rendered into the bundle. It is deliberately **independent** of the adapter's
own guards: even if an upstream transformation regressed, lint re-checks the invariants
that must hold before a bundle is written or downloaded.

Validation classes (each an ``error`` that blocks export):

* **(i) unresolved links** — any residual ``[label](poh:<slug>)`` link left in the body,
  or any ``[[target|label]]`` wikilink whose ``target`` is not a valid E-TALY id
  (``poh_[pom]\\d{4,}``).
* **(ii) frontmatter minimum** — the YAML frontmatter must parse and carry a non-empty
  ``id`` (matching the id pattern), a non-empty ``name`` and at least one integer *year*
  timeline key.
* **(iii) unsupported syntax** — ``[[File:...]]``/``[[Immagine:...]]`` embeds, raw HTML
  tags, GFM table separator rows (``|---|``) or non-source web links ``[x](http...)`` in
  the body (these should have been sanitized upstream; lint guarantees it).
* **(iv) dangling citations** — every ``[label](source:<sha>:aligned:<page>)`` in the body
  must reference a ``(sha, page)`` that is actually available in the bundle.

The hard gate is :func:`assert_exportable`, which raises :class:`LintGateError` (listing
the failing poh_ids and their error codes) when *any* report contains an error.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import yaml

from src.core.log import INFO_LOG_LEVEL, WARNING_LOG_LEVEL, Log
from src.export.etaly_adapter import EtalyArticle

# --- Issue codes / severities ------------------------------------------------
SEVERITY_ERROR = "error"

CODE_UNRESOLVED_LINK = "unresolved_link"  # class (i)
CODE_FRONTMATTER = "frontmatter_incomplete"  # class (ii)
CODE_UNSUPPORTED_SYNTAX = "unsupported_syntax"  # class (iii)
CODE_DANGLING_CITATION = "dangling_citation"  # class (iv)

# --- Regexes -----------------------------------------------------------------
# Valid E-TALY id, e.g. ``poh_p0001`` (>= 4 digits).
_ID_RE = re.compile(r"^poh_[pom]\d{4,}$")
# Residual ``[label](poh:<slug>)`` cross-links (should have been rewritten).
_POH_LINK_RE = re.compile(r"\[[^\]]*\]\(poh:[^)]+\)", re.IGNORECASE)
# ``[[target|label]]`` / ``[[target]]`` wikilinks; group 1 is the raw inner text.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# ``[label](source:<sha256>:aligned:<page>)`` inline citations.
_SOURCE_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(source:([a-f0-9]+):aligned:(\d+)\)",
    re.IGNORECASE,
)
# ``[[File:...]]`` / ``[[Immagine:...]]`` media embeds (unsupported by E-TALY).
_FILE_EMBED_RE = re.compile(r"\[\[\s*(?:File|Immagine)\s*:[^\]]*\]\]", re.IGNORECASE)
# Raw HTML tags (unsupported by the E-TALY MDHandler).
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# Non-source web link ``[x](http://y)``.
_HTTP_LINK_RE = re.compile(r"\[[^\]]+\]\(https?://[^)]+\)", re.IGNORECASE)
# A GFM table separator row such as ``|---|`` or ``|:--:|---|``.
_TABLE_SEP_RE = re.compile(r"^[ \t|:-]*\|[ \t|:-]*$")

_FRONTMATTER_FENCE = "---"


# --- Data model --------------------------------------------------------------
@dataclass(frozen=True)
class LintIssue:
    """A single, categorized lint finding for one article."""

    code: str
    severity: str
    message: str
    poh_id: str


@dataclass
class LintReport:
    """All lint findings for one article; ``ok`` is ``True`` when no error is present."""

    poh_id: str
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == SEVERITY_ERROR for issue in self.issues)

    @property
    def error_codes(self) -> list[str]:
        """Ordered, de-duplicated error codes on this report."""
        codes: list[str] = []
        for issue in self.issues:
            if issue.severity == SEVERITY_ERROR and issue.code not in codes:
                codes.append(issue.code)
        return codes


class LintGateError(RuntimeError):
    """Raised by :func:`assert_exportable` when at least one report has errors.

    The offending poh_ids and their error codes are available as :attr:`failures`
    (a ``{poh_id: [code, ...]}`` mapping) and are also listed in the message.
    """

    def __init__(self, failures: Mapping[str, list[str]]) -> None:
        self.failures = {poh_id: list(codes) for poh_id, codes in failures.items()}
        joined = "; ".join(
            f"{poh_id}: {', '.join(codes)}" for poh_id, codes in self.failures.items()
        )
        super().__init__(f"export blocked by lint: {joined}")


# --- Frontmatter / body helpers ---------------------------------------------
def _split_article(markdown: str) -> tuple[dict | None, str]:
    """Return ``(frontmatter_dict_or_None, body)`` for an E-TALY article.

    The frontmatter is the YAML block delimited by the leading ``---`` fences. When it is
    absent or unparseable, ``None`` is returned and the whole text is treated as the body.
    """
    if not markdown.lstrip().startswith(_FRONTMATTER_FENCE):
        return None, markdown
    parts = markdown.split(_FRONTMATTER_FENCE, 2)
    if len(parts) < 3:
        return None, markdown
    body = parts[2].lstrip("\n")
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None, body
    if not isinstance(data, dict):
        return None, body
    return data, body


def _count_year_keys(frontmatter: Mapping) -> int:
    """Count integer *year* keys (``bool`` excluded) in the frontmatter."""
    return sum(1 for key in frontmatter if isinstance(key, int) and not isinstance(key, bool))


# --- Individual validation passes -------------------------------------------
def _check_unresolved_links(body: str, poh_id: str, issues: list[LintIssue]) -> None:
    seen: set[str] = set()
    for match in _POH_LINK_RE.finditer(body):
        text = match.group(0)
        if text in seen:
            continue
        seen.add(text)
        issues.append(
            LintIssue(
                code=CODE_UNRESOLVED_LINK,
                severity=SEVERITY_ERROR,
                message=f"residual poh: link left in body: {text}",
                poh_id=poh_id,
            )
        )
    for match in _WIKILINK_RE.finditer(body):
        # Media embeds are reported by the unsupported-syntax pass instead.
        if _FILE_EMBED_RE.fullmatch(match.group(0)):
            continue
        target = match.group(1).split("|", 1)[0].strip()
        if _ID_RE.match(target):
            continue
        if target in seen:
            continue
        seen.add(target)
        issues.append(
            LintIssue(
                code=CODE_UNRESOLVED_LINK,
                severity=SEVERITY_ERROR,
                message=f"wikilink target is not a valid E-TALY id: [[{target}...]]",
                poh_id=poh_id,
            )
        )


def _check_frontmatter(frontmatter: dict | None, poh_id: str, issues: list[LintIssue]) -> None:
    if frontmatter is None:
        issues.append(
            LintIssue(
                code=CODE_FRONTMATTER,
                severity=SEVERITY_ERROR,
                message="missing or unparseable YAML frontmatter",
                poh_id=poh_id,
            )
        )
        return

    raw_id = str(frontmatter.get("id") or "").strip()
    if not _ID_RE.match(raw_id):
        issues.append(
            LintIssue(
                code=CODE_FRONTMATTER,
                severity=SEVERITY_ERROR,
                message=f"frontmatter id missing or invalid: {raw_id!r}",
                poh_id=poh_id,
            )
        )

    name = str(frontmatter.get("name") or "").strip()
    if not name:
        issues.append(
            LintIssue(
                code=CODE_FRONTMATTER,
                severity=SEVERITY_ERROR,
                message="frontmatter name is empty",
                poh_id=poh_id,
            )
        )

    if _count_year_keys(frontmatter) < 1:
        issues.append(
            LintIssue(
                code=CODE_FRONTMATTER,
                severity=SEVERITY_ERROR,
                message="frontmatter has no timeline year entry",
                poh_id=poh_id,
            )
        )


def _check_unsupported_syntax(body: str, poh_id: str, issues: list[LintIssue]) -> None:
    for match in _FILE_EMBED_RE.finditer(body):
        issues.append(
            LintIssue(
                code=CODE_UNSUPPORTED_SYNTAX,
                severity=SEVERITY_ERROR,
                message=f"unsupported media embed in body: {match.group(0)}",
                poh_id=poh_id,
            )
        )
    for match in _HTML_TAG_RE.finditer(body):
        issues.append(
            LintIssue(
                code=CODE_UNSUPPORTED_SYNTAX,
                severity=SEVERITY_ERROR,
                message=f"raw HTML tag in body: {match.group(0)}",
                poh_id=poh_id,
            )
        )
    for line in body.splitlines():
        stripped = line.strip()
        if "|" in stripped and "---" in stripped and _TABLE_SEP_RE.match(stripped):
            issues.append(
                LintIssue(
                    code=CODE_UNSUPPORTED_SYNTAX,
                    severity=SEVERITY_ERROR,
                    message=f"GFM table separator row in body: {stripped}",
                    poh_id=poh_id,
                )
            )
    for match in _HTTP_LINK_RE.finditer(body):
        issues.append(
            LintIssue(
                code=CODE_UNSUPPORTED_SYNTAX,
                severity=SEVERITY_ERROR,
                message=f"non-source web link in body: {match.group(0)}",
                poh_id=poh_id,
            )
        )


def _check_dangling_citations(
    body: str,
    poh_id: str,
    available_pages: set[tuple[str, int]],
    issues: list[LintIssue],
) -> None:
    normalized = {(sha.lower(), int(page)) for sha, page in available_pages}
    seen: set[tuple[str, int]] = set()
    for match in _SOURCE_LINK_RE.finditer(body):
        key = (match.group(2).lower(), int(match.group(3)))
        if key in seen:
            continue
        seen.add(key)
        if key not in normalized:
            issues.append(
                LintIssue(
                    code=CODE_DANGLING_CITATION,
                    severity=SEVERITY_ERROR,
                    message=(
                        f"citation not available in bundle: source:{key[0]}:aligned:{key[1]}"
                    ),
                    poh_id=poh_id,
                )
            )


# --- Public API --------------------------------------------------------------
def lint_article(
    etaly_article: EtalyArticle,
    *,
    available_pages: set[tuple[str, int]],
) -> LintReport:
    """Validate one final :class:`EtalyArticle` against the bundle's available pages."""
    poh_id = etaly_article.poh_id
    frontmatter, body = _split_article(etaly_article.markdown)

    issues: list[LintIssue] = []
    _check_unresolved_links(body, poh_id, issues)
    _check_frontmatter(frontmatter, poh_id, issues)
    _check_unsupported_syntax(body, poh_id, issues)
    _check_dangling_citations(body, poh_id, available_pages, issues)

    report = LintReport(poh_id=poh_id, issues=issues)
    Log(
        INFO_LOG_LEVEL if report.ok else WARNING_LOG_LEVEL,
        "etaly article linted",
        {"poh_id": poh_id, "ok": report.ok, "issues": len(issues), "codes": report.error_codes},
    )
    return report


def lint_bundle(reports_or_items: Iterable[LintReport]) -> dict[str, LintReport]:
    """Aggregate an iterable of :class:`LintReport` into a ``{poh_id: report}`` mapping."""
    return {report.poh_id: report for report in reports_or_items}


def _iter_reports(reports: Iterable[LintReport] | Mapping[str, LintReport]) -> list[LintReport]:
    if isinstance(reports, Mapping):
        return list(reports.values())
    return list(reports)


def assert_exportable(reports: Iterable[LintReport] | Mapping[str, LintReport]) -> None:
    """Hard gate: raise :class:`LintGateError` if any report has error-severity issues."""
    failures: dict[str, list[str]] = {}
    for report in _iter_reports(reports):
        if not report.ok:
            failures[report.poh_id] = report.error_codes
    if failures:
        Log(
            WARNING_LOG_LEVEL,
            "etaly export blocked by lint gate",
            {"failures": failures},
        )
        raise LintGateError(failures)


def format_report(reports: Iterable[LintReport] | Mapping[str, LintReport]) -> str:
    """Render a human-readable, per-POH summary with issue codes and messages."""
    lines: list[str] = []
    for report in _iter_reports(reports):
        status = "OK" if report.ok else "FAIL"
        lines.append(f"[{status}] {report.poh_id}")
        for issue in report.issues:
            lines.append(f"    - {issue.severity}/{issue.code}: {issue.message}")
    return "\n".join(lines)

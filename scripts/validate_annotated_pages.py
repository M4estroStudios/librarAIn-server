from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from src.ingestion.annotation_rules import annotations_by_original_page, normalize_label
from src.ingestion.pipeline.md_cache import strip_stage_md_marker

_TWO_CHRONO = re.compile(
    r"\)\s+[A-ZÀ-Ü]",
)
_PAGE_ONLY = re.compile(r"^\s*(?:p\.\s*)?\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*$")
_TWO_LEMMAS = re.compile(r"\S+\s{2,}\S+")
_ITALIC_LINE = re.compile(r"^(\*|_)(.+)\1\s*$")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


def _load_annotations(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("state"), dict):
        raw = raw["state"].get("annotations") or []
    elif isinstance(raw, dict):
        raw = raw.get("annotations") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _normalize_title(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").casefold()).strip()


def _has_aside(text: str) -> bool:
    return bool(re.search(r"(?m)^\{", text) and re.search(r"(?m)^\}", text))


def _headings(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def _allcaps_titles(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip("#").strip()
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", stripped)
        if len(stripped) >= 6 and letters and letters == letters.upper():
            titles.append(stripped)
    return titles


def _check_page(labels: set[str], text: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    headings = _headings(text)
    h1 = [title for level, title in headings if level == 1]
    h2 = [title for level, title in headings if level == 2]
    box_labels = {label for label in labels if label.startswith("box_curiosita")}
    if box_labels:
        aside_ok = _has_aside(text)
        checks.append(
            {"rule": "box_aside", "ok": aside_ok, "detail": "" if aside_ok else "nessun blocco { }"}
        )
        heading_clash = False
        detail = ""
        for _level, title in headings:
            norm = _normalize_title(title)
            if any(_normalize_title(candidate) == norm for candidate in _allcaps_titles(text)):
                heading_clash = True
                detail = f"heading «{title}» coincide con titolo ALL CAPS"
                break
        if not heading_clash:
            for _level, title in headings:
                if title.isupper() and len(title) >= 6:
                    heading_clash = True
                    detail = f"H{_level} «{title}»"
                    break
        checks.append({"rule": "box_not_heading", "ok": not heading_clash, "detail": detail})
    if "capitolo" in labels:
        ok = bool(h1)
        checks.append({"rule": "capitolo_h1", "ok": ok, "detail": "" if ok else "nessun heading #"})
    if "sezione" in labels:
        ok = bool(h2)
        checks.append({"rule": "sezione_h2", "ok": ok, "detail": "" if ok else "nessun heading ##"})
    if any(label.startswith("immagine") for label in labels):
        has_caption = bool(re.search(r"(?m)^>", text)) or any(
            _ITALIC_LINE.match(line.strip()) for line in text.splitlines() if line.strip()
        )
        checks.append(
            {
                "rule": "immagine_caption",
                "ok": has_caption,
                "detail": "" if has_caption else "nessuna didascalia > o italics",
            }
        )
    if "cronologia_2_colonne" in labels or (
        "senso_di_marcia_multicolonna" in labels and "cronologia_monocolonna" not in labels
    ):
        clash = any(_TWO_CHRONO.search(line) and "(" in line for line in text.splitlines())
        checks.append(
            {
                "rule": "cronologia_colonne",
                "ok": not clash,
                "detail": "due voci sulla stessa riga" if clash else "",
            }
        )
    if "indice_analitico_2_colonne" in labels:
        has_italic = any("*" in line or "_" in line for line in text.splitlines())
        row_across = any(_TWO_LEMMAS.search(line) and "," in line for line in text.splitlines())
        checks.append({"rule": "indice_italics", "ok": has_italic, "detail": "" if has_italic else "nessun italics"})
        checks.append(
            {
                "rule": "indice_not_row_read",
                "ok": not row_across,
                "detail": "possibile lettura riga-per-riga" if row_across else "",
            }
        )
    if "2_righe_di_pagine_cit" in labels:
        ok = any(_PAGE_ONLY.match(line) for line in text.splitlines())
        checks.append(
            {
                "rule": "pagine_continuazione",
                "ok": ok,
                "detail": "" if ok else "nessuna riga di soli numeri",
            }
        )
    if "toc_1_colonna" in labels or "titolo_toc" in labels:
        two_voices = any(re.search(r"\d+\s+\S+.+\s{2,}\d+\s+\S+", line) for line in text.splitlines())
        checks.append(
            {
                "rule": "toc_one_voice",
                "ok": not two_voices,
                "detail": "due voci sulla stessa riga" if two_voices else "",
            }
        )
    return checks


def _read_body(path: Path) -> str:
    return strip_stage_md_marker(path.read_text(encoding="utf-8"))


def _aligned_stem(aligned: int, pages_dir: Path) -> Path | None:
    matches = sorted(pages_dir.glob(f"p.{aligned:04d}.*.md"))
    return matches[0] if matches else None


def _stage_path(stage_dir: Path | None, aligned: int) -> Path | None:
    if stage_dir is None or not stage_dir.is_dir():
        return None
    matches = sorted(stage_dir.glob(f"p.{aligned:04d}.*.md"))
    return matches[0] if matches else None


def validate(
    *,
    sha: str,
    pages_dir: Path,
    annotations_path: Path,
    manifest_path: Path,
    only_original: list[int] | None,
    stage2_dir: Path | None,
    stage3_dir: Path | None,
    min_pass: float,
) -> dict[str, Any]:
    annotations = _load_annotations(annotations_path)
    by_page = annotations_by_original_page(annotations)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    orig_to_page = {
        int(item["original"]): item for item in manifest.get("pages") or [] if isinstance(item, dict)
    }
    pages_out: list[dict[str, Any]] = []
    wanted = set(only_original) if only_original else set(by_page)
    for original in sorted(wanted):
        elements = by_page.get(original) or []
        labels = {normalize_label(str(el.get("name") or "")) for el in elements}
        labels.discard("")
        mapping = orig_to_page.get(original)
        aligned = int(mapping["aligned"]) if mapping else None
        file_rel = str(mapping.get("file") or "") if mapping else ""
        md_path = pages_dir / Path(file_rel).name if file_rel else _aligned_stem(aligned or 0, pages_dir)
        if md_path is None or not md_path.is_file():
            pages_out.append(
                {
                    "original": original,
                    "aligned": aligned,
                    "file": file_rel,
                    "labels": sorted(labels),
                    "ok": False,
                    "checks": [{"rule": "file_exists", "ok": False, "detail": "MD assente"}],
                }
            )
            continue
        text = _read_body(md_path)
        checks = _check_page(labels, text)
        page_ok = all(item["ok"] for item in checks) if checks else True
        row: dict[str, Any] = {
            "original": original,
            "aligned": aligned,
            "file": str(md_path.relative_to(pages_dir.parent)) if pages_dir.parent in md_path.parents else str(md_path),
            "labels": sorted(labels),
            "ok": page_ok,
            "checks": checks,
        }
        if stage2_dir is not None and aligned is not None:
            s2 = _stage_path(stage2_dir, aligned)
            if s2 is not None:
                s2_ok = all(item["ok"] for item in _check_page(labels, _read_body(s2))) if labels else True
                row["stage2_ok"] = s2_ok
        if stage3_dir is not None and aligned is not None:
            s3 = _stage_path(stage3_dir, aligned)
            if s3 is not None:
                s3_ok = all(item["ok"] for item in _check_page(labels, _read_body(s3))) if labels else True
                row["stage3_ok"] = s3_ok
                if "stage2_ok" in row:
                    row["stage3_regressed"] = bool(row["stage2_ok"] and not s3_ok)
        pages_out.append(row)
    evaluated = len(pages_out)
    passed = sum(1 for row in pages_out if row["ok"])
    failed = evaluated - passed
    pass_rate = (passed / evaluated) if evaluated else 1.0
    return {
        "sha": sha,
        "pages_dir": str(pages_dir),
        "evaluated": evaluated,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "min_pass": min_pass,
        "pages": pages_out,
    }


def _parse_originals(raw: str) -> list[int]:
    pages: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        pages.append(int(part))
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate annotated ingest pages against label rules.")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--pages-dir", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--only-original", default="")
    parser.add_argument("--stage2-dir", type=Path)
    parser.add_argument("--stage3-dir", type=Path)
    parser.add_argument("--min-pass", type=float, default=0.9)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    sha = args.sha.strip().lower()
    pages_dir = args.pages_dir or args.data_root / "output" / sha / "pages"
    annotations = args.annotations or args.data_root / "sync" / "drafts" / f"{sha}.json"
    manifest = args.manifest or args.data_root / "output" / sha / "manifest.json"
    only = _parse_originals(args.only_original) if args.only_original else None
    report = validate(
        sha=sha,
        pages_dir=pages_dir,
        annotations_path=annotations,
        manifest_path=manifest,
        only_original=only,
        stage2_dir=args.stage2_dir,
        stage3_dir=args.stage3_dir,
        min_pass=args.min_pass,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(
        f"pass_rate={report['pass_rate']:.3f} evaluated={report['evaluated']} "
        f"passed={report['passed']} failed={report['failed']}"
    )
    for row in report["pages"]:
        if not row["ok"]:
            failed_rules = [c["rule"] for c in row["checks"] if not c["ok"]]
            print(f"  fail orig={row['original']} rules={failed_rules}")
    return 0 if report["pass_rate"] >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from src.api.page_guidance_suggest import normalize_annotations
from src.api.pdf_upload_storage import find_source_pdf_by_sha256
from src.core.config import load_settings
from src.core.openai_client import build_openai_client
from src.ingestion.annotation_rules import (
    annotations_by_original_page,
    compose_page_prompt_notes,
)
from src.ingestion.pipeline.md_cache import strip_stage_md_marker
from src.ingestion.pipeline.render import render_pdf_page_to_png
from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result
from src.ingestion.pipeline.stage2 import run_stage2_vision
from src.ingestion.pipeline.stage3 import run_stage3_editor
from src.ingestion.toc_index_refine import refine_index_md, refine_toc_md
from src.models.request import build_md_formatting_block


def _parse_pages(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _load_draft_state(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("state"), dict):
        return raw["state"]
    if isinstance(raw, dict):
        return raw
    return {}


def _find_pdf(data_root: Path, sha: str, explicit: Path | None) -> Path:
    if explicit is not None and explicit.is_file():
        return explicit
    found = find_source_pdf_by_sha256(data_root, sha)
    if found is not None:
        return found
    raise FileNotFoundError(
        f"PDF not found for {sha}. Re-upload the book or pass --pdf."
    )


def _stage1_for_pages(
    *,
    tmp_root: Path,
    slug: str,
    pages: list[tuple[int, int]],
    output_pages_dir: Path | None = None,
) -> Stage1Result:
    results: list[Stage1PageResult] = []
    missing: list[int] = []
    ocr_dir = tmp_root / "stage1OCR"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    for original, aligned in pages:
        txt = ocr_dir / f"p.{aligned:04d}.{slug}.txt"
        if not txt.is_file() or txt.stat().st_size == 0:
            seeded = False
            for candidate in (
                tmp_root / "stage2Vision" / f"p.{aligned:04d}.{slug}.md",
                *(
                    list(output_pages_dir.glob(f"p.{aligned:04d}.*.md"))
                    if output_pages_dir is not None and output_pages_dir.is_dir()
                    else []
                ),
            ):
                if candidate.is_file():
                    txt.write_text(
                        strip_stage_md_marker(candidate.read_text(encoding="utf-8")),
                        encoding="utf-8",
                    )
                    seeded = True
                    break
            if not seeded:
                missing.append(original)
                continue
        results.append(
            Stage1PageResult(
                aligned_page=aligned,
                original_page=original,
                txt_path=str(txt),
                char_count=txt.stat().st_size,
            )
        )
    return Stage1Result(pages=results, skipped_existing=0, missing=missing)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    sha = args.sha.strip().lower()
    data_root = Path(settings.data_root)
    tmp_root = data_root / "tmp" / sha
    output_root = data_root / "output" / sha
    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    slug = str(manifest.get("slug") or "book")
    orig_to_aligned = {
        int(item["original"]): int(item["aligned"])
        for item in manifest.get("pages") or []
        if isinstance(item, dict)
    }
    wanted = _parse_pages(args.original_pages)
    mapped: list[tuple[int, int]] = []
    for original in wanted:
        aligned = orig_to_aligned.get(original)
        if aligned is None:
            print(f"skip orig={original}: not in manifest", file=sys.stderr)
            continue
        mapped.append((original, aligned))
    if not mapped:
        raise SystemExit("no mapped pages to rerun")

    state = _load_draft_state(args.annotations or data_root / "sync" / "drafts" / f"{sha}.json")
    annotations = normalize_annotations(state.get("annotations") or [])
    by_original = annotations_by_original_page(annotations)
    prompt_notes = compose_page_prompt_notes(
        page_notes=str(state.get("page_notes") or ""),
        guidance=str(state.get("ai_page_guidance") or ""),
    )
    formatting = build_md_formatting_block()
    pdf = _find_pdf(data_root, sha, args.pdf)
    render_dir = tmp_root / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    for original, aligned in mapped:
        png = render_dir / f"p.{aligned:04d}.png"
        if not png.is_file():
            render_pdf_page_to_png(pdf, original - 1, png)

    stage1 = _stage1_for_pages(
        tmp_root=tmp_root,
        slug=slug,
        pages=mapped,
        output_pages_dir=output_root / "pages",
    )
    if stage1.missing:
        print(f"missing OCR for originals: {stage1.missing}", file=sys.stderr)
    if not stage1.pages:
        raise SystemExit("no Stage 1 pages available")

    client = build_openai_client(settings)
    if args.from_stage <= 2:
        stage2 = await run_stage2_vision(
            stage1,
            sha,
            settings,
            client,
            request_id="rerun-annotated",
            force_recompute=args.force_recompute,
            prompt_notes=prompt_notes,
            md_formatting=formatting,
            annotations_by_original=by_original,
        )
    else:
        from src.ingestion.pipeline.stage2 import Stage2PageResult, Stage2Result

        pages = []
        for item in stage1.pages:
            md_path = tmp_root / "stage2Vision" / f"p.{item.aligned_page:04d}.{slug}.md"
            pages.append(
                Stage2PageResult(
                    aligned_page=item.aligned_page,
                    original_page=item.original_page,
                    md_path=str(md_path),
                    char_count=md_path.stat().st_size if md_path.is_file() else 0,
                )
            )
        stage2 = Stage2Result(pages=pages, skipped_existing=0, missing=[])

    stage3 = await run_stage3_editor(
        stage2,
        sha,
        settings,
        client,
        request_id="rerun-annotated",
        force_recompute=args.force_recompute,
        prompt_notes=prompt_notes,
        md_formatting=formatting,
    )

    if args.write_output_pages:
        pages_dir = output_root / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        for page in stage3.pages:
            src = Path(page.md_path)
            dests = list(pages_dir.glob(f"p.{page.aligned_page:04d}.*.md"))
            body = strip_stage_md_marker(src.read_text(encoding="utf-8"))
            if dests:
                dests[0].write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
            else:
                dest = pages_dir / f"p.{page.aligned_page:04d}.{slug}.md"
                dest.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")

    if args.also_refine:
        cache = tmp_root / "stage4TocIndexRefine"
        toc = output_root / "TOC.md"
        index = output_root / "INDEX.md"
        if toc.is_file():
            await refine_toc_md(
                toc,
                client,
                settings,
                source_sha256=sha,
                request_id="rerun-annotated",
                cache_dir=cache,
                force_recompute=args.force_recompute,
                prompt_notes=str(state.get("notes") or "").strip() or None,
            )
        if index.is_file():
            await refine_index_md(
                index,
                client,
                settings,
                source_sha256=sha,
                request_id="rerun-annotated",
                cache_dir=cache,
                force_recompute=args.force_recompute,
                prompt_notes=str(state.get("index_notes") or "").strip() or None,
            )
    print(f"reran {len(stage3.pages)} pages from stage {args.from_stage}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run Stage 2+3 on a subset of original pages.")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--original-pages", required=True)
    parser.add_argument("--from-stage", type=int, default=2)
    parser.add_argument("--force-recompute", action="store_true")
    parser.add_argument("--write-output-pages", action="store_true")
    parser.add_argument("--also-refine", action="store_true")
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--pdf", type=Path)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

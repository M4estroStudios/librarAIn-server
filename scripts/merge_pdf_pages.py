#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-,]\s*(\d+)\s*$|^\s*(\d+)\s*$")


def _parse_range(text: str) -> tuple[int, int]:
    m = _RANGE_RE.match(text.strip())
    if not m:
        raise ValueError(
            "Formato range non valido. Usa: 10,50 oppure 10-50 oppure 10 (singola pagina)."
        )
    if m.group(3):
        p = int(m.group(3))
        return p, p
    start, end = int(m.group(1)), int(m.group(2))
    if start > end:
        raise ValueError(f"La pagina iniziale ({start}) è maggiore della finale ({end}).")
    return start, end


def _add_segment(writer: PdfWriter, pdf_path: Path, start: int, end: int) -> None:
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if start < 1 or end > total:
        raise ValueError(
            f"{pdf_path.name}: range {start}-{end} fuori dal PDF ({total} pagine, 1-based)."
        )
    for page_num in range(start, end + 1):
        writer.add_page(reader.pages[page_num - 1])


def _prompt_nonempty(prompt: str) -> str:
    while True:
        line = input(prompt).strip()
        if line:
            return line
        print("  (obbligatorio — ripeti)")


def _interactive() -> int:
    segments: list[tuple[Path, int, int]] = []
    print("Merge PDF per range di pagine (ordine = ordine di inserimento).")
    print("Paginazione 1-based, estremi inclusi. Path vuoto per terminare l'inserimento.\n")

    while True:
        path_raw = input("Path PDF (vuoto = fine inserimento): ").strip()
        if not path_raw:
            break
        pdf_path = Path(path_raw).expanduser()
        if not pdf_path.is_file():
            print(f"  File non trovato: {pdf_path}")
            continue
        range_raw = _prompt_nonempty("Pagine {da,a} (es. 10,50 o 10-50): ")
        try:
            start, end = _parse_range(range_raw)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        segments.append((pdf_path, start, end))
        print(f"  → aggiunto: {pdf_path.name} pagine {start}-{end}\n")

    if not segments:
        print("Nessun segmento inserito.")
        return 1

    out_raw = input("Path PDF di output [merged.pdf]: ").strip() or "merged.pdf"
    out_path = Path(out_raw).expanduser()

    writer = PdfWriter()
    for pdf_path, start, end in segments:
        _add_segment(writer, pdf_path, start, end)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        writer.write(fh)

    print(f"\nScritto: {out_path.resolve()} ({len(writer.pages)} pagine)")
    return 0


def _cli(argv: list[str]) -> int:
    if len(argv) < 4 or len(argv) % 2 != 0:
        print(
            "Uso CLI:\n"
            "  python scripts/merge_pdf_pages.py -o output.pdf libro1.pdf 10,50 libro2.pdf 1-20\n"
            "Senza argomenti: modalità interattiva."
        )
        return 1

    out_path: Path | None = None
    args = list(argv)
    if args[0] in ("-o", "--output"):
        out_path = Path(args[1]).expanduser()
        args = args[2:]
    if out_path is None:
        print("Specifica -o output.pdf")
        return 1
    if len(args) % 2 != 0:
        print("Ogni PDF deve essere seguito da un range.")
        return 1

    writer = PdfWriter()
    i = 0
    while i < len(args):
        pdf_path = Path(args[i]).expanduser()
        start, end = _parse_range(args[i + 1])
        i += 2
        if not pdf_path.is_file():
            print(f"File non trovato: {pdf_path}")
            return 1
        _add_segment(writer, pdf_path, start, end)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        writer.write(fh)
    print(f"Scritto: {out_path.resolve()} ({len(writer.pages)} pagine)")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return _cli(sys.argv[1:])
    return _interactive()


if __name__ == "__main__":
    raise SystemExit(main())

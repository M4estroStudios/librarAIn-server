from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from src.core.hashing import compute_file_sha256
from src.ingestion.ocr.render import render_aligned_pdf_pages, render_pdf_page_to_png


def _minimal_pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class PdfRenderTests(unittest.TestCase):
    def test_render_pdf_page_to_png_creates_expected_png_and_skips_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_path = root / "book.pdf"
            png_path = root / "page.png"
            pdf_path.write_bytes(_minimal_pdf_bytes(1))

            first = render_pdf_page_to_png(pdf_path, 0, png_path, dpi=72)
            first_png_mtime = png_path.stat().st_mtime_ns
            first_sidecar_mtime = (root / "page.png.json").stat().st_mtime_ns

            second = render_pdf_page_to_png(pdf_path, 0, png_path, dpi=72)

            self.assertEqual(first, png_path)
            self.assertEqual(second, png_path)
            self.assertTrue(png_path.is_file())
            self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(png_path.stat().st_mtime_ns, first_png_mtime)
            self.assertEqual((root / "page.png.json").stat().st_mtime_ns, first_sidecar_mtime)

    def test_render_aligned_pdf_pages_uses_digest_render_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_path = root / "aligned.pdf"
            target_dir = root / "data" / "tmp"
            pdf_path.write_bytes(_minimal_pdf_bytes(2))
            digest = compute_file_sha256(pdf_path)

            rendered = render_aligned_pdf_pages(pdf_path, target_dir, 72)
            expected_paths = [
                target_dir / digest / "render" / "p.0001.png",
                target_dir / digest / "render" / "p.0002.png",
            ]
            first_mtimes = [path.stat().st_mtime_ns for path in expected_paths]

            rendered_again = render_aligned_pdf_pages(pdf_path, target_dir, 72)

            self.assertEqual(rendered, [(1, expected_paths[0]), (2, expected_paths[1])])
            self.assertEqual(rendered_again, rendered)
            for path in expected_paths:
                self.assertTrue(path.is_file())
                self.assertTrue((path.parent / f"{path.name}.json").is_file())
            self.assertEqual([path.stat().st_mtime_ns for path in expected_paths], first_mtimes)


if __name__ == "__main__":
    unittest.main()

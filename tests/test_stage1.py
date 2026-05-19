from __future__ import annotations

import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from pypdf import PdfWriter

from src.ingestion.pipeline.stage1 import (
    Stage1Result,
    _slugify,
    run_stage1_ingest_step,
    run_stage1_ocr,
)
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pdf_alignment import build_aligned_pdf
from src.ingestion.request_validation import (
    init_books_schema,
    run_ingest_gate_phase,
    validate_and_enrich_request,
)
from src.models.request import (
    IngestInputErrorCode,
    IngestInputValidationException,
    PageRange,
    ReicatMetadata,
    UsefulPagesEnumeration,
)


def _pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _settings(data_root: str) -> object:
    s = MagicMock()
    s.data_root = data_root
    s.ocr_languages = ["it", "en"]
    s.max_parallel_request = 2
    return s


def _enumeration(original_pages: list[int], o_to_a: dict[int, int]) -> UsefulPagesEnumeration:
    a_to_o = {v: k for k, v in o_to_a.items()}
    max_orig = max(original_pages)
    return UsefulPagesEnumeration(
        source_sha256="deadbeef",
        original_page_count=max_orig,
        aligned_page_count=len(o_to_a),
        useful_original_pages=original_pages,
        original_page_to_aligned_page=o_to_a,
        aligned_page_to_original_page=a_to_o,
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=max(a_to_o.keys()), end=max(a_to_o.keys())),
    )


def _reicat(title: str) -> ReicatMetadata:
    return ReicatMetadata.model_validate({"titolo": title, "autore": ["Author One"]})


class FakeEngine:
    def __init__(self, page_texts: dict[int, str]) -> None:
        self._texts = page_texts
        self.calls: list[int] = []

    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        aligned = int(image_path.stem.split(".")[1])
        self.calls.append(aligned)
        return self._texts.get(aligned, f"text-{aligned}")


class FailEngine:
    def __init__(self, fail_on: set[int]) -> None:
        self._fail = fail_on

    def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
        aligned = int(image_path.stem.split(".")[1])
        if aligned in self._fail:
            raise RuntimeError(f"fail page {aligned}")
        return f"text-{aligned}"


class SlugifyTests(unittest.TestCase):
    def test_ascii_title(self) -> None:
        self.assertEqual(_slugify("Hello World"), "hello-world")

    def test_unicode_normalization(self) -> None:
        self.assertEqual(_slugify("Il Perché dell'Arte"), "il-perche-dell-arte")

    def test_max_length_enforced(self) -> None:
        slug = _slugify("A" * 50)
        self.assertLessEqual(len(slug), 32)
        self.assertRegex(slug, r"^[a-z0-9\-]+$")

    def test_no_trailing_dash_after_truncation(self) -> None:
        long_title = "word-" * 10
        slug = _slugify(long_title)
        self.assertFalse(slug.endswith("-"))
        self.assertLessEqual(len(slug), 32)

    def test_deterministic(self) -> None:
        self.assertEqual(_slugify("Foo Bar"), _slugify("Foo Bar"))

    def test_empty_like_title_returns_book(self) -> None:
        self.assertEqual(_slugify("---"), "book")


class Stage1OcrTests(unittest.TestCase):
    def test_produces_txt_files_and_correct_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(3))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2, 3], {1: 1, 2: 2, 3: 3})
            engine = FakeEngine({1: "page one", 2: "page two", 3: "page three"})

            result = run_stage1_ocr(pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Test Book"))

            self.assertIsInstance(result, Stage1Result)
            self.assertEqual(len(result.pages), 3)
            self.assertEqual(result.skipped_existing, 0)
            self.assertEqual(result.missing, [])
            self.assertIsNone(result.last_error)
            for pr in result.pages:
                txt = Path(pr.txt_path)
                self.assertTrue(txt.is_file())
                self.assertGreater(txt.stat().st_size, 0)
                self.assertEqual(pr.char_count, len(txt.read_text(encoding="utf-8")))

    def test_slug_appears_in_txt_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(1))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1], {1: 1})
            engine = FakeEngine({1: "content"})

            result = run_stage1_ocr(
                pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Très Spécial Livre")
            )

            self.assertEqual(len(result.pages), 1)
            filename = Path(result.pages[0].txt_path).name
            self.assertIn("tres-special-livre", filename)
            slug_part = Path(result.pages[0].txt_path).stem.split(".", 2)[2]
            self.assertLessEqual(len(slug_part), 32)
            self.assertRegex(slug_part, r"^[a-z0-9\-]+$")

    def test_idempotency_cache_skips_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(2))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2], {1: 1, 2: 2})
            engine1 = FakeEngine({1: "first", 2: "second"})

            result1 = run_stage1_ocr(pdf, "deadbeef", enum, settings, engine1, reicat=_reicat("Cache Book"))
            self.assertEqual(len(engine1.calls), 2)

            engine2 = FakeEngine({1: "first", 2: "second"})
            result2 = run_stage1_ocr(pdf, "deadbeef", enum, settings, engine2, reicat=_reicat("Cache Book"))

            self.assertEqual(result2.skipped_existing, 2)
            self.assertEqual(engine2.calls, [])
            self.assertEqual(len(result2.pages), 2)
            self.assertEqual(result2.missing, [])

    def test_force_recompute_bypasses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(1))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1], {1: 1})
            engine1 = FakeEngine({1: "original"})
            run_stage1_ocr(pdf, "deadbeef", enum, settings, engine1, reicat=_reicat("Recompute Book"))

            engine2 = FakeEngine({1: "updated"})
            result = run_stage1_ocr(
                pdf, "deadbeef", enum, settings, engine2, reicat=_reicat("Recompute Book"), force_recompute=True
            )

            self.assertEqual(result.skipped_existing, 0)
            self.assertEqual(engine2.calls, [1])
            self.assertEqual(result.pages[0].char_count, len("updated"))

    def test_missing_aligned_mapping_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(2))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2, 3], {1: 1, 2: 2})
            engine = FakeEngine({1: "a", 2: "b"})

            result = run_stage1_ocr(pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Missing Book"))

            self.assertIn(3, result.missing)
            self.assertEqual(len(result.pages), 2)
            self.assertEqual(result.skipped_existing, 0)

    def test_error_threshold_at_50_percent_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(4))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2, 3, 4], {1: 1, 2: 2, 3: 3, 4: 4})
            engine = FailEngine(fail_on={1, 2})

            with self.assertRaises(IngestInputValidationException) as ctx:
                run_stage1_ocr(pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Fail Book"))

            self.assertEqual(ctx.exception.detail.code, IngestInputErrorCode.OCR_STAGE_FAILED)
            self.assertIn("2/4", ctx.exception.detail.message)

    def test_below_threshold_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(4))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2, 3, 4], {1: 1, 2: 2, 3: 3, 4: 4})
            engine = FailEngine(fail_on={1})

            result = run_stage1_ocr(pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Partial Fail"))

            self.assertIsNotNone(result.last_error)
            self.assertEqual(len(result.pages), 3)

    def test_all_cached_no_threshold_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(2))
            settings = _settings(str(root / "data"))
            enum = _enumeration([1, 2], {1: 1, 2: 2})
            engine1 = FakeEngine({1: "x", 2: "y"})
            run_stage1_ocr(pdf, "deadbeef", enum, settings, engine1, reicat=_reicat("Cached All"))

            fail_engine = FailEngine(fail_on={1, 2})
            result = run_stage1_ocr(pdf, "deadbeef", enum, settings, fail_engine, reicat=_reicat("Cached All"))

            self.assertEqual(result.skipped_existing, 2)
            self.assertEqual(len(result.pages), 2)

    def test_parallel_respects_max_in_flight(self) -> None:
        class SlowEngine(FakeEngine):
            def __init__(self, page_texts: dict[int, str]) -> None:
                super().__init__(page_texts)
                self._lock = threading.Lock()
                self.in_flight = 0
                self.max_seen = 0

            def ocr_page(self, image_path: Path, *, lang: list[str]) -> str:
                with self._lock:
                    self.in_flight += 1
                    self.max_seen = max(self.max_seen, self.in_flight)
                try:
                    time.sleep(0.05)
                    return super().ocr_page(image_path, lang=lang)
                finally:
                    with self._lock:
                        self.in_flight -= 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "aligned.pdf"
            pdf.write_bytes(_pdf_bytes(4))
            settings = _settings(str(root / "data"))
            settings.max_parallel_request = 2
            enum = _enumeration([1, 2, 3, 4], {1: 1, 2: 2, 3: 3, 4: 4})
            engine = SlowEngine({i: f"t{i}" for i in range(1, 5)})
            run_stage1_ocr(pdf, "deadbeef", enum, settings, engine, reicat=_reicat("Parallel Book"))
            self.assertLessEqual(engine.max_seen, 2)
            self.assertGreater(engine.max_seen, 0)


class RunStage1IngestStepTests(unittest.TestCase):
    def test_run_stage1_ingest_step_resolves_pdf_and_writes_txt(self) -> None:
        pdf_body = _pdf_bytes(8)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            init_books_schema(str(base / "biblioteca.db"))
            raw_path = base / "book.pdf"
            raw_path.write_bytes(pdf_body)
            payload = {
                "schema_version": "1.0",
                "source_pdf_path": str(raw_path),
                "pages_to_remove": [1, 2],
                "toc_range": {"start": 3, "end": 4},
                "index_range": {"start": 5, "end": 8},
                "reicat": {"titolo": "Wire Test", "autore": ["Author"]},
            }
            enriched = validate_and_enrich_request(payload)
            phase = run_ingest_gate_phase(enriched, str(base / "biblioteca.db"))
            processed_dir = base / "processed"
            alignment = build_aligned_pdf(
                enriched,
                str(processed_dir),
                page_range_per_thread=10,
            )
            enum = build_useful_pages_enumeration(enriched, alignment)
            settings = MagicMock()
            settings.data_root = str(base / "app_data")
            settings.processed_pdf_input_dir = str(processed_dir)
            settings.page_range_per_thread = 10
            settings.ocr_languages = ["en"]
            settings.ocr_use_gpu = False
            engine = FakeEngine({i: f"line-{i}" for i in range(1, 10)})
            result = run_stage1_ingest_step(
                enriched, alignment, enum, settings, engine=engine
            )
            self.assertEqual(len(result.pages), len(enum.useful_original_pages))
            for row in result.pages:
                self.assertTrue(Path(row.txt_path).is_file())
                self.assertIn("line-", Path(row.txt_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

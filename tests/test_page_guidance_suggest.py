import json
import tempfile
import unittest
from pathlib import Path
from random import Random
from unittest.mock import MagicMock, patch

from PIL import Image

from src.api.page_guidance_http import (
    _ensure_ingest_notes_table,
    delete_ingest_draft,
    ensure_ingest_ai_page_guidance,
    list_ingest_drafts,
    load_ingest_notes_state,
    persist_ingest_notes_for_pdf,
    resolve_ingest_ui_state,
    save_ingest_draft,
    save_ingest_notes_state,
    try_handle_ingest_drafts_delete,
    try_handle_ingest_drafts_get,
    try_handle_ingest_notes_state_put,
)
from src.api.page_guidance_suggest import (
    choose_sample_pages,
    flatten_annotations_on_image,
    normalize_annotations,
)
from src.persistence.book_sqlite import init_books_schema, insert_book_minimal
from src.persistence.pipeline_runs import _sqlite_connection


class ChooseSamplePagesTests(unittest.TestCase):
    def test_excludes_annotated_and_caps_at_five(self) -> None:
        samples = choose_sample_pages(
            20,
            [1, 2, 3],
            sample_count=5,
            rng=Random(0),
        )
        self.assertEqual(len(samples), 5)
        self.assertTrue(all(page not in {1, 2, 3} for page in samples))
        self.assertEqual(samples, sorted(samples))

    def test_short_pdf_returns_all_remaining(self) -> None:
        samples = choose_sample_pages(4, [2], sample_count=5, rng=Random(1))
        self.assertEqual(samples, [1, 3, 4])

    def test_all_annotated_returns_empty(self) -> None:
        samples = choose_sample_pages(3, [1, 2, 3], sample_count=5, rng=Random(2))
        self.assertEqual(samples, [])


class NormalizeAnnotationsTests(unittest.TestCase):
    def test_keeps_valid_primitives(self) -> None:
        normalized = normalize_annotations(
            [
                {
                    "page": 2,
                    "elements": [
                        {"id": "a", "name": "titolo", "type": "bbox", "coords": [10, 20, 100, 80]},
                        {"name": "pin", "type": "point", "coords": [50, 60]},
                        {"name": "path", "type": "trail", "coords": [[1, 2], [3, 4]]},
                        {"name": "bad", "type": "circle", "coords": [1, 2]},
                    ],
                },
                {"page": 0, "elements": []},
            ]
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["page"], 2)
        self.assertEqual(len(normalized[0]["elements"]), 3)

    def test_preserves_description(self) -> None:
        normalized = normalize_annotations(
            [
                {
                    "page": 1,
                    "elements": [
                        {
                            "id": "b1",
                            "name": "colonna_dx",
                            "description": "ignora note a piè",
                            "type": "bbox",
                            "coords": [10, 20, 100, 80],
                        },
                    ],
                }
            ]
        )
        self.assertEqual(normalized[0]["elements"][0]["name"], "colonna_dx")
        self.assertEqual(normalized[0]["elements"][0]["description"], "ignora note a piè")


class FlattenAnnotationsTests(unittest.TestCase):
    def test_draws_without_error(self) -> None:
        image = Image.new("RGB", (200, 300), (240, 240, 240))
        out = flatten_annotations_on_image(
            image,
            [
                {"name": "box", "type": "bbox", "coords": [100, 100, 400, 500]},
                {"name": "dot", "type": "point", "coords": [500, 500]},
                {"name": "route", "type": "trail", "coords": [[100, 100], [800, 800]]},
            ],
        )
        self.assertEqual(out.size, (200, 300))
        self.assertEqual(out.mode, "RGB")


class EnsureIngestAiPageGuidanceTests(unittest.TestCase):
    def test_keeps_existing_guidance(self) -> None:
        payload = {"ai_page_guidance": "  already there  "}
        ensure_ingest_ai_page_guidance(
            Path("unused.pdf"),
            MagicMock(),
            payload,
            {"notes": "ignored"},
        )
        self.assertEqual(payload["ai_page_guidance"], "already there")

    def test_generates_when_missing(self) -> None:
        payload: dict = {}
        with patch(
            "src.api.page_guidance_http.suggest_page_guidance",
            return_value={"guidance": "generated tip"},
        ) as mock_suggest:
            ensure_ingest_ai_page_guidance(
                Path("book.pdf"),
                MagicMock(),
                payload,
                {
                    "notes": "general",
                    "index_notes": "index",
                    "page_notes": "pages",
                    "annotations_json": '[{"page":1,"elements":[]}]',
                },
            )
        self.assertEqual(payload["ai_page_guidance"], "generated tip")
        mock_suggest.assert_called_once()
        kwargs = mock_suggest.call_args.kwargs
        self.assertEqual(kwargs["notes"], "general")
        self.assertEqual(kwargs["index_notes"], "index")
        self.assertEqual(kwargs["page_notes"], "pages")
        self.assertEqual(kwargs["annotations"], [{"page": 1, "elements": []}])

    def test_rejects_invalid_annotations_json(self) -> None:
        with self.assertRaises(ValueError):
            ensure_ingest_ai_page_guidance(
                Path("book.pdf"),
                MagicMock(),
                {},
                {"annotations_json": "{bad"},
            )


class IngestNotesStatePersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        sha = "a" * 64
        state = {
            "notes": "toc tip",
            "index_notes": "index tip",
            "page_notes": "page tip",
            "ai_page_guidance": "guidance",
            "pages_to_remove": "1,2",
            "toc_range": "11-18",
            "index_range": "301-324",
            "biblio_range": "240-255",
            "reicat_pages": "1-3",
            "appendix_pages": "280,300-305",
            "appendix_sections_json": json.dumps(
                {
                    "sections": [
                        {"type": "Cronologia", "pages": "280"},
                        {"type": "Glossario", "pages": "280,300-305"},
                    ],
                    "splits": {"280": 0.55},
                },
                ensure_ascii=False,
            ),
            "titolo": "La Grande Guida",
            "autore": "Claudio Rendina",
            "editore": "Newton",
            "compute_plan": {
                "default_mode": "local",
                "steps": {
                    "stage3_editor": {
                        "compute_mode": "cloud",
                        "model": "gpt-4.1-mini",
                    }
                },
            },
            "annotations": [
                {
                    "page": 2,
                    "elements": [
                        {
                            "id": "bbox_1",
                            "type": "bbox",
                            "name": "bbox1",
                            "coords": [10, 20, 30, 40],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            save_ingest_notes_state(str(db), sha, state)
            loaded = load_ingest_notes_state(str(db), sha)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["notes"], "toc tip")
        self.assertEqual(loaded["index_notes"], "index tip")
        self.assertEqual(loaded["page_notes"], "page tip")
        self.assertEqual(loaded["ai_page_guidance"], "guidance")
        self.assertEqual(loaded["pages_to_remove"], "1,2")
        self.assertEqual(loaded["toc_range"], "11-18")
        self.assertEqual(loaded["index_range"], "301-324")
        self.assertEqual(loaded["biblio_range"], "240-255")
        self.assertEqual(loaded["reicat_pages"], "1-3")
        self.assertEqual(loaded["appendix_pages"], "280, 300-305")
        self.assertIn("Cronologia", loaded["appendix_sections_json"])
        self.assertIn("Glossario", loaded["appendix_sections_json"])
        self.assertIn('"280"', loaded["appendix_sections_json"])
        self.assertIn("0.55", loaded["appendix_sections_json"])
        self.assertEqual(loaded["titolo"], "La Grande Guida")
        self.assertEqual(loaded["autore"], "Claudio Rendina")
        self.assertEqual(loaded["editore"], "Newton")
        self.assertIn("stage3_editor", loaded["compute_plan"])
        self.assertEqual(loaded["annotations"][0]["page"], 2)
        self.assertEqual(loaded["annotations"][0]["elements"][0]["type"], "bbox")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            save_ingest_notes_state(str(db), sha, state)
            resolved = resolve_ingest_ui_state(str(db), sha)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["titolo"], "La Grande Guida")

    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            self.assertIsNone(load_ingest_notes_state(str(db), "b" * 64))
            self.assertIsNone(resolve_ingest_ui_state(str(db), "b" * 64))

    def test_persist_writes_alias_shas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "book.pdf"
            pdf.write_bytes(b"%PDF-1.4 alias-persist-test\n%%EOF\n")
            db = root / "biblioteca.db"
            alias = "c" * 64
            digest = persist_ingest_notes_for_pdf(
                str(db),
                pdf,
                {
                    "titolo": "Alias Book",
                    "autore": "Tester",
                    "toc_range": "2-3",
                    "index_range": "4-5",
                    "annotations_json": "[]",
                },
                {"ai_page_guidance": "tip"},
                alias_shas=[alias],
            )
            self.assertIsNotNone(digest)
            loaded_main = load_ingest_notes_state(str(db), str(digest))
            loaded_alias = load_ingest_notes_state(str(db), alias)
        self.assertIsNotNone(loaded_main)
        self.assertIsNotNone(loaded_alias)
        assert loaded_alias is not None
        self.assertEqual(loaded_alias["titolo"], "Alias Book")
        self.assertEqual(loaded_alias["ai_page_guidance"], "tip")


class IngestDraftsPersistenceTests(unittest.TestCase):
    def _is_draft(self, db: Path, sha: str) -> int | None:
        with _sqlite_connection(str(db)) as conn:
            _ensure_ingest_notes_table(conn)
            row = conn.execute(
                "SELECT is_draft FROM ingest_notes WHERE source_sha256 = ?",
                (sha,),
            ).fetchone()
        return None if row is None else int(row[0])

    def test_migration_adds_is_draft_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            with _sqlite_connection(str(db)) as conn:
                conn.execute(
                    """
                    CREATE TABLE ingest_notes (
                        source_sha256 TEXT PRIMARY KEY,
                        state_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO ingest_notes (source_sha256, state_json, updated_at) VALUES (?, ?, ?)",
                    ("a" * 64, '{"titolo":"Legacy"}', "2020-01-01T00:00:00+00:00"),
                )
            with _sqlite_connection(str(db)) as conn:
                _ensure_ingest_notes_table(conn)
                cols = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(ingest_notes)").fetchall()
                }
                self.assertIn("is_draft", cols)
                row = conn.execute(
                    "SELECT is_draft FROM ingest_notes WHERE source_sha256 = ?",
                    ("a" * 64,),
                ).fetchone()
            self.assertEqual(int(row[0]), 0)
            # Idempotent second call.
            with _sqlite_connection(str(db)) as conn:
                _ensure_ingest_notes_table(conn)

    def test_save_list_and_order(self) -> None:
        sha_old = "a" * 64
        sha_new = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            save_ingest_draft(
                str(db),
                sha_old,
                {"titolo": "Vecchio", "notes": "n1"},
                file_name="old.pdf",
            )
            with _sqlite_connection(str(db)) as conn:
                conn.execute(
                    "UPDATE ingest_notes SET updated_at = ? WHERE source_sha256 = ?",
                    ("2020-01-01T00:00:00+00:00", sha_old),
                )
            save_ingest_draft(
                str(db),
                sha_new,
                {"titolo": "Nuovo", "notes": "n2"},
                file_name="new.pdf",
            )
            save_ingest_notes_state(
                str(db),
                "c" * 64,
                {"titolo": "Non draft"},
                is_draft=False,
            )
            drafts = list_ingest_drafts(str(db))
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[0]["source_sha256"], sha_new)
        self.assertEqual(drafts[0]["title"], "Nuovo")
        self.assertEqual(drafts[0]["file_name"], "new.pdf")
        self.assertEqual(drafts[1]["source_sha256"], sha_old)
        self.assertEqual(drafts[1]["file_name"], "old.pdf")

    def test_persist_clears_draft_keeps_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "book.pdf"
            pdf.write_bytes(b"%PDF-1.4 draft-clear-test\n%%EOF\n")
            db = root / "biblioteca.db"
            from src.core.hashing import compute_file_sha256

            digest = compute_file_sha256(pdf)
            save_ingest_draft(
                str(db),
                digest,
                {"titolo": "Bozza", "notes": "keep-me", "file_name": "book.pdf"},
            )
            self.assertEqual(self._is_draft(db, digest), 1)
            returned = persist_ingest_notes_for_pdf(
                str(db),
                pdf,
                {
                    "titolo": "Bozza",
                    "notes": "keep-me",
                    "annotations_json": "[]",
                    "file_name": "book.pdf",
                },
                {"ai_page_guidance": "tip"},
            )
            self.assertEqual(returned, digest)
            self.assertEqual(self._is_draft(db, digest), 0)
            loaded = load_ingest_notes_state(str(db), digest)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["titolo"], "Bozza")
            self.assertEqual(loaded["notes"], "keep-me")
            self.assertEqual(list_ingest_drafts(str(db)), [])

    def test_delete_removes_draft_only_row(self) -> None:
        sha = "d" * 64
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            save_ingest_draft(str(db), sha, {"titolo": "Solo bozza", "notes": "x"})
            result = delete_ingest_draft(str(db), sha)
            self.assertTrue(result["removed"])
            self.assertFalse(result["cleared"])
            self.assertIsNone(load_ingest_notes_state(str(db), sha))
            self.assertEqual(list_ingest_drafts(str(db)), [])

    def test_delete_clears_flag_when_book_exists(self) -> None:
        sha = "e" * 64
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            init_books_schema(str(db))
            insert_book_minimal(
                str(db),
                sha,
                schema_version="1",
                title="In Biblioteca",
                authors_json="[]",
            )
            save_ingest_draft(
                str(db),
                sha,
                {"titolo": "In Biblioteca", "notes": "annotazioni utili"},
            )
            result = delete_ingest_draft(str(db), sha)
            self.assertFalse(result["removed"])
            self.assertTrue(result["cleared"])
            self.assertEqual(self._is_draft(db, sha), 0)
            loaded = load_ingest_notes_state(str(db), sha)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["notes"], "annotazioni utili")
            self.assertEqual(list_ingest_drafts(str(db)), [])

    def test_http_list_save_delete(self) -> None:
        sha = "f" * 64
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "biblioteca.db"
            settings = MagicMock()
            settings.sqlite_path = str(db)
            settings.data_root = tmp
            responses: list[tuple[int, dict]] = []

            def send_json(_handler: object, status: int, payload: dict) -> None:
                responses.append((status, payload))

            handled = try_handle_ingest_drafts_get(
                "/api/ingest/drafts",
                MagicMock(),
                settings=settings,
                send_json=send_json,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[-1][0], 200)
            self.assertEqual(responses[-1][1]["drafts"], [])

            body = json.dumps(
                {
                    "source_sha256": sha,
                    "state": {"titolo": "HTTP Draft", "notes": "via put"},
                    "file_name": "http.pdf",
                }
            ).encode("utf-8")
            handler = MagicMock()
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile.read.return_value = body

            def read_body(_handler: object, _max: int) -> bytes:
                return body

            handled = try_handle_ingest_notes_state_put(
                "/api/ingest/notes-state",
                handler,
                settings=settings,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[-1][0], 200)
            self.assertTrue(responses[-1][1]["is_draft"])
            self.assertEqual(responses[-1][1]["source_sha256"], sha)

            handled = try_handle_ingest_drafts_get(
                "/api/ingest/drafts",
                MagicMock(),
                settings=settings,
                send_json=send_json,
            )
            self.assertTrue(handled)
            drafts = responses[-1][1]["drafts"]
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["title"], "HTTP Draft")
            self.assertEqual(drafts[0]["file_name"], "http.pdf")
            self.assertFalse(drafts[0].get("has_pdf"))

            # Salva PDF bozza e verifica has_pdf + GET bytes.
            from src.api.pdf_upload_storage import save_draft_pdf, find_draft_pdf_by_sha256
            from src.api.page_guidance_http import try_handle_ingest_drafts_pdf_get
            from src.core.hashing import compute_file_sha256

            pdf_src = Path(tmp) / "sample.pdf"
            pdf_src.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
            digest = compute_file_sha256(pdf_src)
            # Re-key under the draft sha used above for simpler assertion path:
            save_draft_pdf(Path(tmp), sha, pdf_src)
            self.assertTrue(find_draft_pdf_by_sha256(Path(tmp), sha))
            handled = try_handle_ingest_drafts_get(
                "/api/ingest/drafts",
                MagicMock(),
                settings=settings,
                send_json=send_json,
            )
            self.assertTrue(handled)
            self.assertTrue(responses[-1][1]["drafts"][0]["has_pdf"])

            binary: list[tuple[int, bytes, str]] = []

            def send_bytes(_handler: object, status: int, content: bytes, content_type: str) -> None:
                binary.append((status, content, content_type))

            handled = try_handle_ingest_drafts_pdf_get(
                "/api/ingest/drafts/pdf",
                MagicMock(),
                {"source_sha256": [sha]},
                settings=settings,
                send_json=send_json,
                send_bytes=send_bytes,
            )
            self.assertTrue(handled)
            self.assertEqual(binary[-1][0], 200)
            self.assertEqual(binary[-1][2], "application/pdf")
            self.assertTrue(binary[-1][1].startswith(b"%PDF"))

            handled = try_handle_ingest_drafts_delete(
                "/api/ingest/drafts",
                MagicMock(),
                {"source_sha256": [sha]},
                settings=settings,
                send_json=send_json,
            )
            self.assertTrue(handled)
            self.assertTrue(responses[-1][1]["removed"])
            self.assertEqual(list_ingest_drafts(str(db), data_root=tmp), [])
            self.assertIsNone(find_draft_pdf_by_sha256(Path(tmp), sha))


class IngestedBooksReingestTests(unittest.TestCase):
    def test_list_and_prepare_reingest_keeps_notes(self) -> None:
        from src.api.page_guidance_http import (
            list_ingested_books_for_reingest,
            prepare_book_reingest_from_zero,
            try_handle_ingest_ingested_get,
            try_handle_ingest_reingest_prepare_post,
        )
        from src.api.pdf_upload_storage import save_draft_pdf

        sha = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "biblioteca.db"
            out_dir = root / "output" / sha
            out_dir.mkdir(parents=True)
            (out_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "source_sha256": sha,
                        "slug": "demo-book",
                        "reicat": {"titolo": "Demo Book"},
                        "pages": [{"aligned_page": 1}, {"aligned_page": 2}],
                    }
                ),
                encoding="utf-8",
            )
            tmp_dir = root / "tmp" / sha / "stage3Editor"
            tmp_dir.mkdir(parents=True)
            (tmp_dir / "0001.md").write_text("cached", encoding="utf-8")
            pdf_src = root / "book.pdf"
            pdf_src.write_bytes(b"%PDF-1.4\n%%EOF\n")
            save_draft_pdf(root, sha, pdf_src)
            save_ingest_notes_state(
                str(db),
                sha,
                {
                    "titolo": "Demo Book",
                    "file_name": "book.pdf",
                    "notes": "keep me",
                    "annotations": [
                        {
                            "page": 1,
                            "elements": [
                                {
                                    "id": "e1",
                                    "name": "col",
                                    "type": "bbox",
                                    "coords": [1, 2, 3, 4],
                                }
                            ],
                        }
                    ],
                },
                is_draft=False,
            )

            books = list_ingested_books_for_reingest(str(db), root)
            self.assertEqual(len(books), 1)
            self.assertEqual(books[0]["source_sha256"], sha)
            self.assertEqual(books[0]["title"], "Demo Book")
            self.assertEqual(books[0]["page_count"], 2)
            self.assertTrue(books[0]["has_output"])
            self.assertTrue(books[0]["has_pdf"])
            self.assertEqual(books[0]["annotation_pages"], 1)
            self.assertEqual(books[0]["annotation_elements"], 1)

            settings = MagicMock()
            settings.sqlite_path = str(db)
            settings.data_root = tmp
            responses: list[tuple[int, dict]] = []

            def send_json(_handler: object, status: int, payload: dict) -> None:
                responses.append((status, payload))

            handled = try_handle_ingest_ingested_get(
                "/api/ingest/ingested",
                MagicMock(),
                settings=settings,
                send_json=send_json,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[-1][0], 200)
            self.assertEqual(len(responses[-1][1]["books"]), 1)

            body = json.dumps({"source_sha256": sha}).encode("utf-8")

            def read_body(_handler: object, _max: int) -> bytes:
                return body

            handled = try_handle_ingest_reingest_prepare_post(
                "/api/ingest/ingested/prepare-reingest",
                MagicMock(),
                settings=settings,
                send_json=send_json,
                read_body=read_body,
            )
            self.assertTrue(handled)
            self.assertEqual(responses[-1][0], 200)
            self.assertTrue(responses[-1][1]["ok"])
            self.assertIn(f"output/{sha}", responses[-1][1]["removed"])
            self.assertIn(f"tmp/{sha}", responses[-1][1]["removed"])
            self.assertFalse((root / "output" / sha).exists())
            self.assertFalse((root / "tmp" / sha).exists())
            notes = load_ingest_notes_state(str(db), sha)
            self.assertIsNotNone(notes)
            assert notes is not None
            self.assertEqual(notes["notes"], "keep me")
            self.assertEqual(len(notes["annotations"]), 1)
            from src.persistence.pipeline_runs import _sqlite_connection

            with _sqlite_connection(str(db)) as conn:
                flag = conn.execute(
                    "SELECT is_draft FROM ingest_notes WHERE source_sha256 = ?",
                    (sha,),
                ).fetchone()[0]
            self.assertEqual(int(flag), 1)
            self.assertTrue(responses[-1][1].get("restored_as_draft"))
            result = prepare_book_reingest_from_zero(root, sha, sqlite_path=str(db))
            self.assertEqual(result["removed"], [])
            self.assertTrue(result["has_pdf"])
            self.assertTrue(result["restored_as_draft"])


if __name__ == "__main__":
    unittest.main()

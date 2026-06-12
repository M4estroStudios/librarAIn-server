from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.backup_data import (
    DATA_DIR_NAME,
    create_backup,
    restore_backup,
)


class TestBackupData(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_root = self.root / "data"
        self.backup_dir = self.root / "backup"
        self._seed_data_tree()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_data_tree(self) -> None:
        (self.data_root / "output" / "book-a").mkdir(parents=True)
        (self.data_root / "output" / "book-a" / "INDEX.md").write_text(
            "alpha", encoding="utf-8"
        )
        (self.data_root / "tmp" / "scratch").mkdir(parents=True)
        (self.data_root / "tmp" / "scratch" / "page.tmp").write_text(
            "volatile", encoding="utf-8"
        )
        (self.data_root / "db").mkdir(parents=True, exist_ok=True)
        (self.data_root / "db" / "biblioteca.db").write_bytes(b"\x00\x01")

    def test_backup_excludes_tmp_by_default(self) -> None:
        zip_path = create_backup(self.data_root, self.backup_dir, include_tmp=False)
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = archive.namelist()
        self.assertTrue(any(name.endswith("INDEX.md") for name in names))
        self.assertFalse(any("/tmp/" in name.replace("\\", "/") for name in names))

    def test_backup_includes_tmp_when_requested(self) -> None:
        zip_path = create_backup(self.data_root, self.backup_dir, include_tmp=True)
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = archive.namelist()
        self.assertTrue(any("tmp/scratch/page.tmp" in name.replace("\\", "/") for name in names))

    def test_backup_zip_contains_data_root_folder(self) -> None:
        zip_path = create_backup(self.data_root, self.backup_dir, include_tmp=False)
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = archive.namelist()
        self.assertTrue(all(name.startswith(f"{DATA_DIR_NAME}/") for name in names))

    def test_backup_counter_increments_for_same_stamp(self) -> None:
        first = create_backup(self.data_root, self.backup_dir, include_tmp=False)
        second = create_backup(self.data_root, self.backup_dir, include_tmp=False)
        self.assertTrue(first.name.endswith(".0.zip"))
        self.assertTrue(second.name.endswith(".1.zip"))
        self.assertEqual(first.name.rsplit(".", 2)[0], second.name.rsplit(".", 2)[0])

    def test_restore_replaces_data_directory(self) -> None:
        zip_path = create_backup(self.data_root, self.backup_dir, include_tmp=False)
        (self.data_root / "output" / "book-a" / "INDEX.md").write_text(
            "changed", encoding="utf-8"
        )
        (self.data_root / "extra.txt").write_text("gone", encoding="utf-8")

        restored_files = restore_backup(self.data_root, zip_path)

        self.assertGreater(restored_files, 0)
        self.assertEqual(
            (self.data_root / "output" / "book-a" / "INDEX.md").read_text(encoding="utf-8"),
            "alpha",
        )
        self.assertFalse((self.data_root / "extra.txt").exists())
        self.assertFalse((self.data_root / "tmp" / "scratch" / "page.tmp").exists())

    def test_restore_rejects_zip_without_data_folder(self) -> None:
        bad_zip = self.backup_dir / "bad.zip"
        bad_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bad_zip, "w") as archive:
            archive.writestr("other/file.txt", "x")
        with self.assertRaises(ValueError):
            restore_backup(self.data_root, bad_zip)


if __name__ == "__main__":
    unittest.main()

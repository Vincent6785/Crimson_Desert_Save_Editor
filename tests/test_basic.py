# SPDX-License-Identifier: MPL-2.0
"""Checks that need no real save file (saves are personal and are never committed)."""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cd_inventory  # noqa: E402
import cd_items  # noqa: E402
import cd_skills  # noqa: E402
import linux_only  # noqa: E402


def fake_header(version: int = 2) -> bytearray:
    header = bytearray(cd_items.HEADER_SIZE)
    header[:4] = b"SAVE"
    struct.pack_into("<H", header, 4, version)
    return header


class CryptoTest(unittest.TestCase):
    def test_round_trip(self):
        blob = bytearray(os.urandom(4096) + bytes(4096))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "save.save"
            path.write_bytes(cd_items.build_save(fake_header(), blob))
            header, back = cd_items.load_save(path)
        self.assertEqual(back, blob)
        self.assertEqual(header[:4], b"SAVE")

    def test_tampered_save_is_rejected(self):
        data = bytearray(cd_items.build_save(fake_header(), bytearray(b"x" * 1000)))
        data[-1] ^= 0xFF
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "save.save"
            path.write_bytes(data)
            with self.assertRaises(cd_items.SaveError):
                cd_items.load_save(path)

    def test_not_a_save(self):
        unknown_version = bytes(cd_items.build_save(fake_header(), bytearray(100)))
        unknown_version = unknown_version[:4] + b"\x09\x00" + unknown_version[6:]
        for content in (b"hello", b"SAVE\x02\x00", unknown_version):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "save.save"
                path.write_bytes(content)
                with self.assertRaises(cd_items.SaveError):
                    cd_items.load_save(path)


class FilesTest(unittest.TestCase):
    def test_find_saves_layouts(self):
        with tempfile.TemporaryDirectory() as d:
            save = Path(d) / "save"
            for rel in ("123/slot0", "123/slot1"):
                (save / rel).mkdir(parents=True)
                (save / rel / "save.save").write_bytes(b"")
            for root, expected in ((save, 2), (save / "123", 2), (save / "123" / "slot1", 1)):
                with mock.patch.dict(os.environ, {"CD_SAVE_DIR": str(root)}):
                    self.assertEqual(len(cd_items.find_saves()), expected, root)

    def test_backups_never_reuse_a_folder(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cd_items, "BACKUP_ROOT", Path(d)):
                save = Path(d) / "save" / "123" / "slot0" / "save.save"
                first = cd_items._backup_path(save)
                first.parent.mkdir(parents=True)
                second = cd_items._backup_path(save)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parts[-3:], ("123", "slot0", "save.save"))

    def test_items_db(self):
        db = cd_inventory.load_items_db()
        self.assertGreater(len(db), 5000)
        for key, info in db.items():
            self.assertIsInstance(key, int)
            self.assertIn("name", info)
        addable = [k for k, v in db.items() if not cd_inventory.blocked(v)
                   and cd_inventory.destination(k, db) is not None]
        self.assertGreater(len(addable), 5000)
        self.assertTrue(all(not cd_inventory.blocked(db[k]) for k in addable))

    def test_knowledge_names(self):
        self.assertGreater(len(cd_skills.load_knowledge_names()), 1000)


class WindowsTest(unittest.TestCase):
    def test_refused(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as cm:
                linux_only.refuse_windows()
        self.assertEqual(cm.exception.code, 1)

    def test_linux_allowed(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(os, "name", "posix"):
            linux_only.refuse_windows()


class GuiImportTest(unittest.TestCase):
    def test_import(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        import cd_items_gui  # noqa: F401


if __name__ == "__main__":
    unittest.main()

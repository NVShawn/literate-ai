"""Private journal interruption and foreign-change controls on real files."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import html_tree_journal as journal
from literate_ai.adapters.html_tree_journal import initial_html_tree_journal


@unittest.skipUnless(os.name == "posix", "journal requires POSIX descriptors")
class HtmlTreeJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "journal"

    def open(self):
        return initial_html_tree_journal(
            self.path,
            prepared_data={"test": "private intent"},
            require_current=lambda: None,
        )

    def test_equal_byte_replacement_refuses_and_preserves_foreign_inode(self):
        with self.assertRaisesRegex(ValueError, "journal-record-changed"):
            with self.open() as (append, _guard):
                path = self.path / "0000.json"
                original = path.read_bytes()
                foreign = self.root / "foreign"
                foreign.write_bytes(original)
                foreign.chmod(0o600)
                node = foreign.stat().st_ino
                os.replace(foreign, path)
                append("output-reserved", {})
        self.assertEqual(path.stat().st_ino, node)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.path / "0001.json").exists())

    def test_foreign_member_refuses_without_removing_it(self):
        with self.assertRaisesRegex(ValueError, "journal-membership-changed"):
            with self.open() as (append, _guard):
                foreign = self.path / "foreign"
                foreign.write_bytes(b"operator-owned evidence")
                append("output-reserved", {})
        self.assertEqual(foreign.read_bytes(), b"operator-owned evidence")
        self.assertFalse((self.path / "0001.json").exists())

    def test_saved_append_cannot_write_after_original_context_ends(self):
        with self.open() as (append, _guard):
            pass
        with self.assertRaisesRegex(ValueError, "journal-context-ended"):
            append("output-reserved", {})
        self.assertEqual(len(list(self.path.iterdir())), 1)

    def test_partial_record_write_is_retained_without_a_completion_record(self):
        real_write = os.write

        def interrupt_write(fd, content):
            real_write(fd, content[:17])
            raise InterruptedError("test-owned partial write")

        with self.assertRaises(InterruptedError):
            with self.open() as (append, _guard):
                original = (self.path / "0000.json").read_bytes()
                with patch.object(journal.os, "write", interrupt_write):
                    append("output-reserved", {})
        self.assertEqual((self.path / "0000.json").read_bytes(), original)
        partial = (self.path / "0001.json").read_bytes()
        self.assertEqual(len(partial), 17)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(partial)
        with self.assertRaises(FileExistsError):
            with self.open():
                self.fail("partial evidence must be preserved")
        self.assertEqual((self.path / "0001.json").read_bytes(), partial)

    def test_abrupt_process_exit_retains_prepared_record_and_refuses_reentry(self):
        script = """
import os, sys
from pathlib import Path
from literate_ai.adapters.html_tree_journal import initial_html_tree_journal
with initial_html_tree_journal(
    Path(sys.argv[1]), prepared_data={"crash": True}, require_current=lambda: None
):
    os._exit(17)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        path = self.path / "0000.json"
        raw = path.read_bytes()
        record = json.loads(raw)
        self.assertEqual(record["phase"], "prepared")
        self.assertIsNone(record["previous"])
        with self.assertRaises(FileExistsError):
            with self.open():
                self.fail("historical journal must not grant a new writer")
        self.assertEqual(path.read_bytes(), raw)

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError


class PinnedInputClosureTests(unittest.TestCase):
    def test_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "skill.json"
            authority.write_bytes(b"before")
            closure = PinnedInputClosure()
            closure.pin(authority, boundary=root, label="skill:portable")
            authority.write_bytes(b"after")

            with self.assertRaisesRegex(PinnedInputClosureError, "changed"):
                closure.require_unchanged()

    @unittest.skipIf(os.name == "nt", "symbolic-link creation needs Windows privilege")
    def test_symlink_capture_and_replacement_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            link = root / "authority.json"
            target.write_bytes(b"exact")
            link.symlink_to(target)
            closure = PinnedInputClosure()
            with self.assertRaisesRegex(PinnedInputClosureError, "symbolic link"):
                closure.pin(link, boundary=root, label="authority")

            link.unlink()
            link.write_bytes(b"exact")
            closure.pin(link, boundary=root, label="authority")
            link.unlink()
            link.symlink_to(target)
            with self.assertRaisesRegex(PinnedInputClosureError, "symbolic link"):
                closure.require_unchanged()


if __name__ == "__main__":
    unittest.main()

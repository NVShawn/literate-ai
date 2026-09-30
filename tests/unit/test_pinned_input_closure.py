from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError


class PinnedInputClosureTests(unittest.TestCase):
    def test_missing_authority_names_the_unavailable_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "workflows" / "retired" / "workflow.md"

            with self.assertRaises(PinnedInputClosureError) as raised:
                PinnedInputClosure().pin(
                    missing,
                    boundary=root,
                    label="retired-workflow",
                )

            self.assertEqual(raised.exception.code, "inputs.closure_unavailable")
            self.assertIn(str(missing), raised.exception.message)

    def test_exact_authority_is_portably_identified_and_can_be_forked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "component.json"
            second = root / "openspec" / "spec.md"
            second.parent.mkdir()
            first.write_bytes(b'{"component":true}\n')
            second.write_bytes(b"# Exact specification\n")
            closure = PinnedInputClosure()
            closure.pin(first, boundary=root, label="component:manifest")
            closure.pin(second, boundary=root, label="component:specification:spec.md")

            copied = closure.fork()

            self.assertEqual(copied.identity, closure.identity)
            self.assertEqual(copied.file_count, 2)
            self.assertEqual(
                copied.total_bytes, len(first.read_bytes()) + len(second.read_bytes())
            )
            copied.require_unchanged()
            self.assertNotIn(str(root), str(copied.to_dict()))

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

    def test_limits_and_expected_bytes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "workflow.json"
            authority.write_bytes(b"12345")
            closure = PinnedInputClosure(
                maximum_files=1,
                maximum_file_bytes=4,
                maximum_total_bytes=4,
            )
            with self.assertRaisesRegex(PinnedInputClosureError, "per-file limit"):
                closure.pin(authority, boundary=root, label="workflow")

            closure = PinnedInputClosure()
            with self.assertRaisesRegex(
                PinnedInputClosureError, "while it was captured"
            ):
                closure.pin(
                    authority,
                    boundary=root,
                    label="workflow",
                    expected_content=b"other",
                )


if __name__ == "__main__":
    unittest.main()

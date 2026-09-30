"""Real source commands retain candidate custody while producing test outputs."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import source_verification_workspace as workspaces
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.generation import _CommandSourceVerifier
from tests.unit.test_cli_generation import bind_tree
from tests.unit.test_standard_source_admission import generation


class SourceVerificationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "candidate"
        suite = self.root / "source" / "tests"
        suite.mkdir(parents=True)
        (suite / "manifest.json").write_text(json.dumps({"cases": [{}]}))
        self.authored = self.root / "source" / "app.py"
        self.authored.write_text("VALUE = 42\n")
        self.generated = bind_tree(generation(), self.root)

    def verify(self, *scripts):
        return _CommandSourceVerifier(
            self.root,
            self.generated.output.candidate.source_manifest_identity,
            tuple((sys.executable, "-c", script) for script in scripts),
        ).verify(self.generated)

    def test_outputs_and_bytecode_are_disposable_and_shared_by_test_commands(self):
        report = self.verify(
            "from pathlib import Path; import sys; sys.path.insert(0, 'source'); "
            "import app; assert app.VALUE == 42; "
            "Path('result.txt').write_text('passed')",
            "from pathlib import Path; "
            "assert Path('result.txt').read_text() == 'passed'",
        )
        self.assertEqual(len(report.test_results), 2)
        self.assertFalse((self.root / "result.txt").exists())
        self.assertFalse((self.root / "source" / "__pycache__").exists())
        self.assertEqual(self.authored.read_text(), "VALUE = 42\n")

    def test_changed_or_deleted_copy_source_is_refused_after_successful_command(self):
        for operation in ("write_text('VALUE = 0')", "unlink()"):
            with (
                self.subTest(operation=operation),
                self.assertRaises(CliFailure) as raised,
            ):
                self.verify(
                    f"from pathlib import Path; Path('source/app.py').{operation}"
                )
            self.assertEqual(raised.exception.code, "source_admission.source_changed")
            self.assertEqual(self.authored.read_text(), "VALUE = 42\n")

    def test_original_candidate_mutation_and_new_members_are_refused(self):
        for path in (self.authored, self.root / "extra.txt"):
            with self.subTest(path=path.name), self.assertRaises(CliFailure) as raised:
                self.verify(
                    "from pathlib import Path; "
                    f"Path({str(path)!r}).write_text('changed')"
                )
            self.assertEqual(raised.exception.code, "source_admission.source_changed")
            if path == self.authored:
                path.write_text("VALUE = 42\n")
            else:
                path.unlink()

    def test_drift_before_tests_refuses_without_executing(self):
        self.authored.write_text("VALUE = 0\n")
        marker = self.root.parent / "executed"
        with self.assertRaises(CliFailure) as raised:
            self.verify(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")
        self.assertFalse(marker.exists())

    def test_failed_command_retains_its_failure_code(self):
        with self.assertRaises(CliFailure) as raised:
            self.verify("raise SystemExit(7)")
        self.assertEqual(raised.exception.code, "source_admission.tests_failed")
        self.assertIn("exited 7", raised.exception.message)

    def test_readonly_source_copy_is_verified_and_cleaned(self):
        self.authored.chmod(0o444)
        with workspaces.source_verification_workspace(
            self.root, self.generated.output.candidate.tree_identity
        ) as workspace:
            copied = workspace.root
            self.assertEqual(
                (copied / "source/app.py").read_bytes(), self.authored.read_bytes()
            )
        self.assertFalse(copied.exists())
        self.assertEqual(self.authored.read_text(), "VALUE = 42\n")

    def test_hardlinked_source_is_refused(self):
        os.link(self.authored, self.root.parent / "alias")
        with self.assertRaises(CliFailure) as raised:
            self.verify("raise SystemExit(0)")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")

    def test_linked_source_is_refused(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.authored)
        except OSError as exc:
            self.skipTest(f"host cannot create symlink: {exc.errno}")
        with self.assertRaises(CliFailure) as raised:
            self.verify("raise SystemExit(0)")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")

    def test_executable_permission_and_empty_directory_are_preserved(self):
        self.authored.chmod(0o755)
        (self.root / "empty").mkdir()
        with workspaces.source_verification_workspace(
            self.root, self.generated.output.candidate.tree_identity
        ) as workspace:
            copied = workspace.root
            self.assertTrue((copied / "empty").is_dir())
            self.assertEqual(
                stat.S_IMODE((copied / "source/app.py").stat().st_mode),
                stat.S_IMODE(self.authored.stat().st_mode),
            )
        self.assertFalse(copied.exists())

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX executable mode")
    def test_copy_permission_change_is_refused(self):
        with self.assertRaises(CliFailure) as raised:
            self.verify("from pathlib import Path; Path('source/app.py').chmod(0o755)")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")

    def test_capture_limit_is_bounded_and_refuses_before_command(self):
        with mock.patch.object(workspaces, "_MAX_FILE_BYTES", 1):
            with self.assertRaises(CliFailure) as raised:
                self.verify("raise SystemExit(0)")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")

    def test_substituted_workspace_is_preserved_and_refused(self):
        moved = self.root.parent / "moved"
        replacement = None
        try:
            with self.assertRaises(workspaces.SourceVerificationWorkspaceError):
                with workspaces.source_verification_workspace(
                    self.root, self.generated.output.candidate.tree_identity
                ) as workspace:
                    replacement = workspace.root
                    replacement.rename(moved)
                    replacement.mkdir()
                    (replacement / "peer.txt").write_text("preserve")
            self.assertEqual((replacement / "peer.txt").read_text(), "preserve")
        finally:
            if replacement is not None:
                (replacement / "peer.txt").unlink(missing_ok=True)
                replacement.rmdir()


if __name__ == "__main__":
    unittest.main()

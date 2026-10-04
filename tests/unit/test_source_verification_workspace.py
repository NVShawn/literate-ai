"""Real source commands retain candidate custody while producing test outputs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.generation import _CommandSourceVerifier
from tests.support.fixtures_test_cli_generation import bind_tree
from tests.support.fixtures_test_standard_source_admission import generation


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

    def test_hardlinked_source_is_refused(self):
        os.link(self.authored, self.root.parent / "alias")
        with self.assertRaises(CliFailure) as raised:
            self.verify("raise SystemExit(0)")
        self.assertEqual(raised.exception.code, "source_admission.source_changed")


if __name__ == "__main__":
    unittest.main()

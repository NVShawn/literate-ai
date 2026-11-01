"""Real noisy adoption failures retain bounded evidence outside rollback custody."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from literate_ai.adapters.conversion_failure_evidence import retain_conversion_failure
from literate_ai.adapters.harness_inventory import HarnessBaselineError
from literate_ai.adapters.project_initialization import ProjectInitializationError
from tests.support.fixtures_test_project_cli import invoke


class ConversionFailureStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.root / "state"
        patch = mock.patch.dict(os.environ, {"LITAI_STATE_DIR": str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)
        self.error = HarnessBaselineError(
            "project.convert_legacy_gate_failed", "fixture failure", {"phases": []}
        )

    def retain(self):
        return retain_conversion_failure(
            self.project, self.error, recovery="rolled-back"
        )

    def test_concurrent_private_reports_are_unique_and_do_not_evict_at_quota(self):
        with mock.patch(
            "literate_ai.adapters.conversion_failure_evidence.MAX_FAILURE_REPORTS", 3
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                notices = list(executor.map(lambda _: self.retain(), range(8)))
        files = list((self.state / "adoption-failures").glob("*.json"))
        self.assertEqual(len(files), 3)
        self.assertEqual(sum("retained:" in notice for notice in notices), 3)
        self.assertTrue(all("retained:" in n or "store is full" in n for n in notices))
        before = {p.name: p.read_bytes() for p in files}
        with mock.patch(
            "literate_ai.adapters.conversion_failure_evidence.MAX_FAILURE_STORE_BYTES",
            1,
        ):
            self.assertIn("store is full", self.retain())
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})
        if os.name != "nt":
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files))

    def test_state_inside_rollback_target_is_refused_without_writes(self):
        with mock.patch.dict(
            os.environ, {"LITAI_STATE_DIR": str(self.project / "state")}
        ):
            self.assertIn("state overlaps project", self.retain())
        self.assertEqual(list(self.project.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX link and mode controls")
    def test_indirect_or_public_store_is_refused_without_touching_existing_files(self):
        self.state.mkdir()
        target = self.root / "outside"
        target.mkdir()
        directory = self.state / "adoption-failures"
        directory.symlink_to(target, target_is_directory=True)
        self.assertIn("unavailable", self.retain())
        self.assertEqual(list(target.iterdir()), [])
        directory.unlink()
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        self.assertIn("private directory permissions", self.retain())
        self.assertEqual(list(directory.iterdir()), [])

    @unittest.skipUnless(os.name != "nt", "POSIX Make fixture")
    def test_real_noisy_baseline_preserves_outcome_tails_and_private_report(self):
        self.exercise_baseline(rollback_fails=False)

    @unittest.skipUnless(os.name != "nt", "POSIX Make fixture")
    def test_real_baseline_with_refused_rollback_does_not_claim_restoration(self):
        self.exercise_baseline(rollback_fails=True)

    @unittest.skipUnless(os.name != "nt", "POSIX Make fixture")
    def test_rollback_os_error_preserves_original_diagnostics_and_incomplete_outcome(
        self,
    ):
        self.exercise_baseline(rollback_fails=True, rollback_os_error=True)

    def exercise_baseline(self, *, rollback_fails, rollback_os_error=False):
        (self.project / "harness.py").write_text(
            "import os, sys\n"
            "if sys.argv[1] == 'test':\n"
            "    print('warning\\n' * 1500, file=sys.stderr)\n"
            "    print('noise\\n' * 1500)\n"
            "    print('FAILED-ACTUAL-STDOUT token=' + os.environ['DIAG_API_TOKEN'])\n"
            "    print('ACTUAL-STDERR-TAIL', file=sys.stderr)\n"
            "    raise SystemExit(3)\n"
            "print('healthy phase')\n"
        )
        python = shlex.quote(sys.executable)
        (self.project / "Makefile").write_text(
            "".join(
                f"{p}:\n\t@{python} harness.py {p}\n"
                for p in ("build", "test", "package")
            )
        )
        original = {p.name: p.read_bytes() for p in self.project.iterdir()}
        recovery = (
            mock.patch(
                "literate_ai.adapters.project_initialization._rollback_converted_tree",
                side_effect=(
                    OSError(28, "private filesystem detail")
                    if rollback_os_error
                    else ProjectInitializationError(
                        "fixture.refused", "retained custody"
                    )
                ),
            )
            if rollback_fails
            else mock.patch.dict(os.environ, {})
        )
        with (
            recovery,
            mock.patch.dict(os.environ, {"DIAG_API_TOKEN": "private-fixture-token"}),
        ):
            status, result = invoke(
                "onboard",
                "adopt",
                str(self.project),
                "--apply",
                "--acknowledge",
                "--run-baseline",
                "--source-intelligence-provider",
                "none",
                "--baseline-diagnostic-chars",
                "4096",
            )
        self.assertEqual(status, 2, result)
        message = result["error"]["message"]
        expected = (
            "rollback also failed"
            if rollback_fails
            else "literate-ai conversion was rolled back"
        )
        self.assertTrue(message.startswith(expected), message)
        self.assertIn("FAILED-ACTUAL-STDOUT", message)
        self.assertIn("ACTUAL-STDERR-TAIL", message)
        self.assertNotIn("private-fixture-token", message)
        self.assertLessEqual(len(message), 4096 + 512)
        self.assertEqual(
            result["error"]["code"],
            "project.convert_rollback_failed"
            if rollback_fails
            else "project.convert_legacy_gate_failed",
        )
        files = list((self.state / "adoption-failures").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertIn(str(files[0]), message)
        content = files[0].read_bytes()
        self.assertNotIn(b"private-fixture-token", content)
        report = json.loads(content)
        self.assertEqual(
            report["recovery"], "incomplete" if rollback_fails else "rolled-back"
        )
        self.assertFalse(report["acceptance"])
        self.assertTrue(any(p.get("exit_code") == 2 for p in report["phases"]))
        if not rollback_fails:
            self.assertEqual(
                original, {p.name: p.read_bytes() for p in self.project.iterdir()}
            )
        else:
            self.assertTrue((self.project / "_legacy").exists())

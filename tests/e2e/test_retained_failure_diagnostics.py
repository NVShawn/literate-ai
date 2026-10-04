"""Retained failures retain bounded actionable evidence through the public CLI."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.harness_inventory import (
    HARNESS_DIAGNOSTIC_CHARS,
)
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _invoke,
    _selectors,
)


class RetainedFailureDiagnosticsTests(unittest.TestCase):
    def exercise(self, *, report_storage=False, host_errno=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            (project / "harness.py").write_text(
                "import os, sys\n"
                "if sys.argv[1] == 'test' and "
                "os.environ.get('LITAI_DIAG_FIXTURE_FAIL'):\n"
                "    print(('earlier error in unrelated test ' "
                "+ 'a' * 700 + '\\n') * 5)\n"
                "    print('x' * 16000)\n"
                + (
                    "    print('OSError: [Errno 28] "
                    "No space left on device (ENOSPC)')\n"
                    if report_storage
                    else ""
                )
                + "    print('z' * 18000)\n"
                "    print('ACTUAL-STDOUT-TAIL token=' "
                "+ os.environ['DIAG_API_TOKEN'])\n"
                "    sys.stderr.write('wrapper noise\\n' * 800 "
                "+ 'ACTUAL-STDERR-TAIL\\n')\n"
                "    raise SystemExit(2)\n"
                "print('Ran 2 tests\\nOK')\n"
            )
            (project / "Makefile").write_text(
                "".join(
                    f'{phase}:\n\t@"{sys.executable}" harness.py {phase}\n'
                    for phase in ("build", "test", "package")
                )
            )
            _adapter().initialize(
                project,
                flavor_selectors=_selectors(project),
                source_intelligence_provider="none",
                convert=True,
            )
            candidate = root / "candidate.json"
            with mock.patch.dict(
                os.environ,
                {
                    "LITAI_DIAG_FIXTURE_FAIL": "1",
                    "DIAG_API_TOKEN": "private-diagnostic-token",
                },
            ):
                if host_errno is not None:
                    with mock.patch(
                        "literate_ai.adapters.retained_harness_receipts.execute_retained_harness",
                        side_effect=OSError(host_errno, "private filesystem detail"),
                    ):
                        status, result = _invoke(
                            "project",
                            "test-receipt",
                            "run-retained",
                            str(candidate),
                            "--project",
                            str(project),
                            "--worker-id",
                            "local",
                        )
                else:
                    status, result = _invoke(
                        "project",
                        "test-receipt",
                        "run-retained",
                        str(candidate),
                        "--project",
                        str(project),
                        "--worker-id",
                        "local",
                    )
            self.assertNotEqual(status, 0)
            self.assertFalse(candidate.exists())
            self.assertFalse(candidate.with_name("candidate.evidence.json").exists())
            return result["error"]

    def test_reported_disk_failure_and_both_physical_tails_reach_cli(self):
        error = self.exercise(report_storage=True)
        self.assertEqual(error["code"], "project.convert_legacy_gate_failed")
        message = error["message"]
        self.assertIn("[reported storage failure]", message)
        self.assertIn("not a capacity measurement", message)
        self.assertIn("ENOSPC", message)
        self.assertIn("ACTUAL-STDOUT-TAIL", message)
        self.assertIn("ACTUAL-STDERR-TAIL", message)
        self.assertNotIn("private-diagnostic-token", message)
        self.assertLessEqual(len(message), HARNESS_DIAGNOSTIC_CHARS + 512)

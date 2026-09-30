"""Retained failures retain bounded actionable evidence through the public CLI."""

from __future__ import annotations

import errno
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.harness_inventory import (
    HARNESS_DIAGNOSTIC_CHARS,
    HarnessBaselineError,
    _command_output_excerpt,
    _gate_failure_message,
    _run_harness_command,
)
from tests.unit.test_retained_harness_receipts import (
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

    def test_generic_exit_two_preserves_tail_without_inventing_storage_failure(self):
        error = self.exercise()
        self.assertEqual(error["code"], "project.convert_legacy_gate_failed")
        self.assertNotIn("reported storage failure", error["message"])
        self.assertIn("ACTUAL-STDOUT-TAIL", error["message"])
        self.assertIn("ACTUAL-STDERR-TAIL", error["message"])

    def test_direct_enospc_has_a_specific_error_and_no_private_path(self):
        error = self.exercise(host_errno=errno.ENOSPC)
        self.assertEqual(error["code"], "project.host_storage_exhausted")
        self.assertIn("quotas and inodes", error["message"])
        self.assertNotIn("private filesystem detail", error["message"])

    def test_other_os_error_is_not_reported_as_storage_exhaustion(self):
        error = self.exercise(host_errno=errno.EACCES)
        self.assertEqual(error["code"], "retained_receipt.execution_failed")

    def test_full_stream_hint_finds_middle_failure_across_read_boundary(self):
        prefix = b"x" * (65536 - 3) + b"\n"
        for marker in (
            b"ENOSPC",
            b"EDQUOT",
            b"No space left on device",
            b"Disk quota exceeded",
        ):
            with self.subTest(marker=marker):
                raw = prefix + marker + b"\n" + b"y" * 9000 + b"\nACTUAL-TAIL"
                excerpt = _command_output_excerpt(
                    io.BytesIO(raw), size=len(raw), limit=512
                )
                self.assertIn("[reported storage failure]", excerpt)
                self.assertIn("ACTUAL-TAIL", excerpt)
                self.assertLessEqual(len(excerpt), 512)

    def test_large_generic_output_has_no_storage_hint(self):
        raw = b"unrelated error\n" * 1000 + b"ACTUAL-TAIL"
        excerpt = _command_output_excerpt(io.BytesIO(raw), size=len(raw), limit=512)
        self.assertNotIn("reported storage failure", excerpt)
        self.assertLess(excerpt.index("[output tail]"), excerpt.index("[output head]"))
        self.assertIn("ACTUAL-TAIL", excerpt)

    def test_long_command_does_not_consume_the_stream_diagnostic_budget(self):
        message = _gate_failure_message(
            "test",
            {
                "command": "long-command " + "x" * 10000,
                "exit_code": 2,
                "stdout_excerpt": "STDOUT-TAIL",
                "stderr_excerpt": "STDERR-TAIL",
            },
        )
        self.assertIn("STDOUT-TAIL", message)
        self.assertIn("STDERR-TAIL", message)
        self.assertLessEqual(len(message.splitlines()[0]), 511)

    def test_errno_name_split_before_identifier_suffix_is_not_a_match(self):
        raw = b"x" * (65536 - 7) + b"\nENOSPC_TEST_PASSED\n" + b"y" * 1000
        excerpt = _command_output_excerpt(io.BytesIO(raw), size=len(raw), limit=512)
        self.assertNotIn("reported storage failure", excerpt)

    def test_trimmed_overlap_does_not_invent_a_word_boundary(self):
        raw = b"a" * (65536 - 64) + b"ENOSPC\n" + b"b" * 1000
        excerpt = _command_output_excerpt(io.BytesIO(raw), size=len(raw), limit=512)
        self.assertNotIn("reported storage failure", excerpt)

    def test_direct_storage_error_while_starting_harness_is_classified(self):
        for number in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as temporary:
                with (
                    mock.patch(
                        "literate_ai.adapters.harness_inventory._ensure_disposable_git_identity"
                    ),
                    mock.patch(
                        "literate_ai.adapters.harness_inventory.subprocess.Popen",
                        side_effect=OSError(number, "private storage detail"),
                    ),
                    self.assertRaises(HarnessBaselineError) as raised,
                ):
                    _run_harness_command(
                        "test",
                        {"command": "fixture", "evidence": "fixture"},
                        legacy_root=Path(temporary),
                        timeout_seconds=10,
                    )
                self.assertEqual(
                    raised.exception.code, "project.host_storage_exhausted"
                )
                self.assertNotIn("private storage detail", raised.exception.message)

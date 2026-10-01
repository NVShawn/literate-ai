"""TEST child stdio refuses unsafe startup/input and keeps failures off stdout."""

import io
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS
from literate_ai.test_worker import main


class TestWorkerChildTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.cas = self.root / "cas"
        FileSystemCAS(self.cas)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.environment = {
            "LITAI_TEST_INPUT_IDENTITY": canonical_identity("fixture").uri,
            "LITAI_TEST_DEADLINE": (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat(),
        }

    def invoke(self, content=b"{}", factory=None, argv=None):
        self.stdout = io.BytesIO()
        self.stderr = io.StringIO()
        self.stdin = io.BytesIO(content)
        with (
            patch.dict(os.environ, self.environment),
            patch(
                "literate_ai.test_worker.sys.stdin", SimpleNamespace(buffer=self.stdin)
            ),
            patch(
                "literate_ai.test_worker.sys.stdout",
                SimpleNamespace(buffer=self.stdout),
            ),
            patch("literate_ai.test_worker.sys.stderr", self.stderr),
        ):
            return main(
                argv
                if argv is not None
                else ["--cas", str(self.cas), "--workspace", str(self.workspace)],
                runtime_factory=factory,
            )

    def test_unconfigured_child_does_not_read_input(self):
        self.assertEqual(self.invoke(b"untrusted"), 2)
        self.assertEqual(self.stdin.tell(), 0)
        self.assertEqual(self.stdout.getvalue(), b"")

    def test_oversized_input_stops_at_bound_before_runtime(self):
        factory = Mock()
        self.assertEqual(self.invoke(b"x" * (17 * 1024 * 1024), factory), 2)
        self.assertEqual(self.stdin.tell(), 16 * 1024 * 1024 + 1)
        factory.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), b"")

    def test_malformed_record_never_enters_runtime(self):
        factory = Mock()
        self.assertEqual(self.invoke(b"not-json", factory), 2)
        factory.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), b"")

    def test_private_errors_have_no_traceback_or_partial_result(self):
        with patch(
            "literate_ai.test_worker.execute_worker_test_from_cas",
            side_effect=RuntimeError("private-path secret-value"),
        ):
            self.assertEqual(self.invoke(factory=Mock()), 2)
        self.assertEqual(self.stdout.getvalue(), b"")
        self.assertEqual(
            self.stderr.getvalue(), "TEST child input or private runtime refused\n"
        )

    def test_relative_paths_and_expired_deadline_refuse_before_input(self):
        factory = Mock()
        self.assertEqual(
            self.invoke(
                factory=factory,
                argv=["--cas", "relative", "--workspace", str(self.workspace)],
            ),
            2,
        )
        self.assertEqual(self.stdin.tell(), 0)
        self.environment["LITAI_TEST_DEADLINE"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        self.assertEqual(self.invoke(factory=factory), 2)
        self.assertEqual(self.stdin.tell(), 0)
        factory.assert_not_called()

    def test_supervisor_paths_supply_defaults_and_reject_conflicting_flags(self):
        self.environment.update(
            LITAI_TEST_CAS=str(self.cas), LITAI_TEST_WORKSPACE=str(self.workspace)
        )
        with patch(
            "literate_ai.test_worker.execute_worker_test_from_cas", return_value=b"{}"
        ) as execute:
            self.assertEqual(self.invoke(factory=Mock(), argv=[]), 0)
            self.assertEqual(
                execute.call_args.kwargs["owned_workspace"], self.workspace
            )
        self.assertEqual(
            self.invoke(factory=Mock(), argv=["--workspace", str(self.root)]), 2
        )
        self.assertEqual(self.stdin.tell(), 0)

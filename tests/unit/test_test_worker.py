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

    def test_oversized_or_malformed_input_refused_before_runtime_without_traceback(
        self,
    ):
        cases = (
            ("oversized", b"x" * (17 * 1024 * 1024), 16 * 1024 * 1024 + 1),
            ("malformed", b"not-json", None),
        )
        for name, content, consumed in cases:
            with self.subTest(name):
                factory = Mock()
                self.assertEqual(self.invoke(content, factory), 2)
                if consumed is not None:
                    self.assertEqual(self.stdin.tell(), consumed)
                factory.assert_not_called()
                self.assertEqual(self.stdout.getvalue(), b"")
                self.assertNotIn("Traceback", self.stderr.getvalue())
        with patch(
            "literate_ai.test_worker.execute_worker_test_from_cas",
            side_effect=RuntimeError("private-path secret-value"),
        ):
            self.assertEqual(self.invoke(factory=Mock()), 2)
        self.assertEqual(self.stdout.getvalue(), b"")
        self.assertEqual(
            self.stderr.getvalue(), "TEST child input or private runtime refused\n"
        )

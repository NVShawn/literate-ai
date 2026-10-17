"""FINALIZE child boundary refuses unconfigured startup and hides partial results."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_finalize_result_record as fixture_module
from literate_ai import finalize_worker
from literate_ai.storage import FileSystemCAS


class FinalizeWorkerTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.cas = FileSystemCAS(root / "cas")
        self.workspace = root / "job"
        self.workspace.mkdir()
        self.environment = {
            "LITAI_FINALIZE_CAS": str(self.cas.root),
            "LITAI_FINALIZE_WORKSPACE": str(self.workspace),
            "LITAI_FINALIZE_INPUT_IDENTITY": f.identity.uri,
            "LITAI_FINALIZE_DEADLINE": f.fixture.deadline.expires_at.isoformat(),
        }
        self.loader = Mock(return_value={})
        self.guard = Mock()
        # This suite isolates stdin/config/stdout. Domain execution is covered by
        # the real command-chain test; these result references are synthetic.
        self.execute = self.enterContext(
            patch(
                "literate_ai.finalize_worker.execute_worker_finalize_from_cas",
                return_value=f.result.to_bytes(),
            )
        )

    def run_main(self, *, raw=None, argv=None, **changes):
        output = io.BytesIO()
        errors = io.StringIO()
        args = dict(
            runtime_factory=Mock(),
            proof_loader=self.loader,
            verify_package=Mock(),
            admission_guard=self.guard,
            require_execution_authority=Mock(),
        )
        args.update(changes)
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                finalize_worker.sys,
                "stdin",
                SimpleNamespace(
                    buffer=io.BytesIO(self.fixture.raw if raw is None else raw)
                ),
            ),
            patch.object(finalize_worker.sys, "stdout", SimpleNamespace(buffer=output)),
            patch.object(finalize_worker.sys, "stderr", errors),
        ):
            status = finalize_worker.main([] if argv is None else argv, **args)
        return status, output.getvalue(), errors.getvalue()

    def test_exact_input_is_admitted_before_private_proof_lookup(self):
        status, output, errors = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(output, self.fixture.result.to_bytes())
        self.assertEqual(errors, "")
        self.assertEqual(self.loader.call_args.args[0], self.fixture.value)
        self.assertEqual(
            self.execute.call_args.kwargs["input_record"], self.fixture.raw
        )
        self.assertEqual(
            self.execute.call_args.kwargs["workspace_root"], self.workspace
        )

    def test_each_missing_private_dependency_refuses_without_loading(self):
        for field in (
            "runtime_factory",
            "proof_loader",
            "verify_package",
            "admission_guard",
            "require_execution_authority",
        ):
            with self.subTest(field=field):
                status, output, _ = self.run_main(**{field: None})
                self.assertEqual((status, output), (2, b""))
        self.loader.assert_not_called()
        self.execute.assert_not_called()

    def test_bad_input_never_reaches_proof_loader(self):
        self.assertEqual(self.run_main(raw=b"invalid")[:2], (2, b""))
        self.loader.assert_not_called()
        self.execute.assert_not_called()

    def test_supervisor_workspace_cannot_be_overridden(self):
        self.assertEqual(
            self.run_main(argv=["--workspace", str(self.cas.root)])[:2], (2, b"")
        )
        self.loader.assert_not_called()

    def test_private_exception_is_sanitized_and_stdout_is_empty(self):
        self.loader.side_effect = RuntimeError("private credential path")
        status, output, errors = self.run_main()
        self.assertEqual((status, output), (2, b""))
        self.assertNotIn("credential", errors)
        self.assertEqual(errors, "FINALIZE child input or private runtime refused\n")

    def test_private_diagnostics_do_not_pollute_wire_output(self):
        def execute(**kwargs):
            print("private diagnostic")
            return self.fixture.result.to_bytes()

        self.execute.side_effect = execute
        status, output, errors = self.run_main()
        self.assertEqual((status, output), (0, self.fixture.result.to_bytes()))
        self.assertEqual(errors, "private diagnostic\n")

    def test_invalid_result_is_not_written(self):
        self.execute.return_value = b"invalid"
        self.assertEqual(self.run_main()[:2], (2, b""))

    def test_revocation_after_execution_prevents_stdout(self):
        def execute(**kwargs):
            self.guard.side_effect = PermissionError("revoked")
            return self.fixture.result.to_bytes()

        self.execute.side_effect = execute
        self.assertEqual(self.run_main()[:2], (2, b""))

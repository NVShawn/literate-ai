"""Execute real retained exports against withheld, exact verifier expectations."""

import hashlib
import json
import os
import shutil
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import retained_project_acceptance as acceptance
from literate_ai.adapters.builders import BuildError
from literate_ai.adapters.retained_project_inputs import (
    RetainedProjectInputError,
    discover_retained_project_inputs,
    verify_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase as Case,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy as Policy,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile
from tests.critical import test_retained_project_execution as fixtures


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX compiler"
)
class RetainedProjectAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(fixtures.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = fixtures.RetainedProjectExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.source
        self.outputs = self.fixture.base / "exports"
        self.outputs.mkdir()
        shutil.copyfile(self.fixture.fixture_binary, self.outputs / "app.bin")
        (self.outputs / "app.bin").chmod(0o755)
        (self.outputs / "docs.txt").write_bytes(b"manual")
        self.capture_outputs()
        self.case = Case(
            "oracle-command",
            "accept",
            "app.bin",
            ("check", "{output_root}/docs.txt"),
            b"",
            0,
            b"",
            b"",
            10,
        )
        self.policy = Policy(self.fixture.plan.component_locks, (self.case,))
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.fixture.base),
            "LC_ALL": "C",
        }
        self.write_policy()

    def capture_outputs(self):
        self.manifest = discover_retained_project_inputs(
            self.outputs,
            {"app.bin": "retained-output", "docs.txt": "retained-output"},
            self.fixture.manifest.limits,
        )

    def write_policy(self, content=None):
        content = (
            content
            if content is not None
            else canonical_json_bytes(self.policy.to_dict())
        )
        self.path = self.root / acceptance.ACCEPTANCE_POLICY_PATH
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_bytes(content)
        self.profile = replace(
            self.fixture.profile,
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        self.plan = replace(self.fixture.plan, profile=self.profile)

    def read(self):
        return acceptance.read_retained_acceptance_policy(
            self.root, self.profile, self.plan.component_locks, self.fixture.manifest
        )

    def require_current(self):
        self.assertEqual(self.read(), self.policy)
        verify_retained_project_inputs(self.outputs, self.manifest)

    def execute(self):
        return acceptance.verify_retained_acceptance_action(
            self.policy,
            self.plan,
            "accept",
            self.outputs,
            self.manifest,
            environment=self.environment,
            require_current=self.require_current,
        )

    def test_real_compiled_export_passes_exact_cases_and_binds_observations(self):
        self.assertEqual(self.read(), self.policy)
        (observed,) = self.execute()
        self.assertEqual(observed.plan_identity, self.plan.identity)
        self.assertEqual(observed.output_manifest_identity, self.manifest.identity)
        self.assertEqual(observed.policy_identity, self.policy.identity)
        self.assertEqual(observed.case_identity, self.case.identity)
        self.assertEqual(observed.exit_code, 0)
        self.assertIsInstance(observed.identity, ContentIdentity)
        with self.assertRaisesRegex(ValueError, "result-identity-mismatch"):
            replace(observed, result=replace(observed.result, stdout=b"substituted"))
        terminated = replace(
            observed, exit_code=-15, result=replace(observed.result, returncode=-15)
        )
        self.assertNotEqual(terminated.identity, observed.identity)

    def test_expected_nonzero_exit_and_stderr_are_real_negative_cases(self):
        self.case = replace(
            self.case, argv=("fail",), expected_exit=9, expected_stderr=b"failed"
        )
        self.policy = replace(self.policy, cases=(self.case,))
        self.write_policy()
        (observed,) = self.execute()
        self.assertEqual(observed.result.returncode, 9)
        self.assertEqual(observed.result.stderr, b"failed")

    def test_stdin_and_binary_stdout_are_compared_exactly(self):
        self.case = replace(
            self.case,
            argv=("echo",),
            stdin=b"binary\x00input",
            expected_stdout=b"binary\x00input",
        )
        self.policy = replace(self.policy, cases=(self.case,))
        self.write_policy()
        self.execute()

    def test_zero_exit_with_wrong_output_or_status_is_not_acceptance(self):
        for changes in (
            {"expected_exit": 3},
            {"expected_stdout": b"expected"},
            {"expected_stderr": b"expected"},
        ):
            with self.subTest(changes=changes):
                self.policy = replace(
                    self.policy, cases=(replace(self.case, **changes),)
                )
                self.write_policy()
                with self.assertRaisesRegex(
                    acceptance.RetainedProjectAcceptanceError, "result-mismatch"
                ) as error:
                    self.execute()
                (observed,) = error.exception.observations
                self.assertEqual(observed.result.returncode, 0)
                self.assertEqual(observed.result.stdout, b"")

    def test_policy_raw_byte_drift_duplicate_keys_and_redirection_refuse(self):
        original = self.path.read_bytes()
        self.path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.read()
        self.write_policy(b'{"schema": "x", "schema": "y"}')
        with self.assertRaisesRegex(ValueError, "document-invalid"):
            self.read()
        self.write_policy()
        moved = self.root / "other-policy.json"
        self.path.rename(moved)
        self.path.symlink_to(moved)
        with self.assertRaises(ValueError):
            self.read()

    def test_policy_cannot_enter_materialized_inputs(self):
        manifest = discover_retained_project_inputs(
            self.root, {"verification": "verifier-policy"}, self.fixture.manifest.limits
        )
        with self.assertRaisesRegex(ValueError, "must-be-withheld"):
            acceptance.read_retained_acceptance_policy(
                self.root, self.profile, self.plan.component_locks, manifest
            )

    def test_stale_locks_missing_cases_and_nonbuild_executables_refuse(self):
        with self.assertRaisesRegex(ValueError, "locks-changed"):
            self.policy.require_profile(
                self.profile, (canonical_identity("different"),)
            )
        for case in (
            replace(self.case, case_id="extra"),
            replace(self.case, action_id="other"),
            replace(self.case, executable="docs.txt"),
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                replace(self.policy, cases=(case,)).require_profile(
                    self.profile, self.plan.component_locks
                )

    def test_nonexecutable_or_mutated_exports_refuse(self):
        (self.outputs / "app.bin").chmod(0o644)
        self.capture_outputs()
        with self.assertRaisesRegex(ValueError, "sealed-executable"):
            self.execute()
        (self.outputs / "app.bin").chmod(0o755)
        self.capture_outputs()
        self.case = replace(self.case, argv=("changed", "{output_root}/docs.txt"))
        self.policy = replace(self.policy, cases=(self.case,))
        self.write_policy()
        with self.assertRaises(ValueError):
            self.execute()

    def test_native_dependency_drift_is_rejected_after_actual_command(self):
        before = canonical_identity("before")
        after = canonical_identity("after")
        with patch.object(
            acceptance,
            "retained_export_dependency_identity",
            side_effect=(before, after),
        ):
            with self.assertRaisesRegex(ValueError, "native-dependencies-changed"):
                self.execute()

    def test_actual_program_output_budget_is_enforced(self):
        self.case = replace(self.case, argv=("spam", "unused"))
        self.policy = replace(self.policy, cases=(self.case,))
        self.write_policy()
        with patch.object(acceptance, "MAX_ACCEPTANCE_STREAM_BYTES", 1024):
            with self.assertRaisesRegex(BuildError, "byte budget"):
                self.execute()

    def test_legacy_profile_wire_identity_is_preserved_and_explicit_null_refuses(self):
        legacy = self.fixture.profile.to_dict()
        self.assertNotIn("acceptance_policy_identity", legacy)
        self.assertEqual(RetainedProjectProfile.from_dict(legacy), self.fixture.profile)
        current = self.profile.to_dict()
        self.assertEqual(RetainedProjectProfile.from_dict(current), self.profile)
        self.assertNotEqual(self.profile.identity, self.fixture.profile.identity)
        current["acceptance_policy_identity"] = None
        with self.assertRaises(ValueError):
            RetainedProjectProfile.from_dict(current)

    def test_case_wire_bounds_and_duplicate_inventory_refuse(self):
        self.assertEqual(Policy.from_dict(self.policy.to_dict()), self.policy)
        for changes in (
            {"timeout_seconds": True},
            {"expected_exit": -1},
            {"executable": "../app"},
            {"stdin": b"x" * 65537},
            {"argv": ("bad\x00argument",)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.case, **changes)
        with self.assertRaises(ValueError):
            replace(self.policy, cases=(self.case, self.case))
        wire = self.case.to_dict()
        wire["expected_stdout"] = "not base64!"
        with self.assertRaises(ValueError):
            Case.from_dict(wire)

    def test_workspace_rejects_nonexecutable_declared_export_during_capture(self):
        self.fixture.plan = self.plan
        self.fixture.current = self.plan
        workspace = self.fixture.workspace()
        with self.assertRaisesRegex(
            RetainedProjectInputError, "lacks native execute permission"
        ):
            workspace.execute_next()
        self.assertFalse(
            (workspace.inputs / acceptance.ACCEPTANCE_POLICY_PATH).exists()
        )
        self.assertEqual(workspace.acceptance_observations, ())

    def test_workspace_runs_independent_cases_after_build_tests_and_docs(self):
        actions = list(self.fixture.actions)
        actions[0] = replace(actions[0], argv=("export", "{output_root}/app.bin"))
        report = json.dumps(
            {
                "schema": "literate-ai/retained-project-test-report@1",
                "action": "tests",
                "cases": [
                    {"id": "original-suite", "outcome": "passed", "reason": None}
                ],
            }
        )
        actions[1] = replace(
            actions[1], argv=("report", "{output_root}/test-result.json", report)
        )
        self.fixture.profile = replace(self.fixture.profile, actions=tuple(actions))
        self.case = replace(
            self.case,
            argv=("echo",),
            stdin=b"native product",
            expected_stdout=b"native product",
        )
        self.policy = replace(self.policy, cases=(self.case,))
        self.write_policy()
        self.fixture.plan = self.plan
        self.fixture.current = self.plan
        workspace = self.fixture.workspace()
        for _ in range(4):
            workspace.execute_next()
        (observed,) = workspace.acceptance_observations
        self.assertEqual(observed.case_identity, self.case.identity)
        self.assertEqual(len(workspace.test_reports), 1)
        self.assertFalse(
            (workspace.inputs / acceptance.ACCEPTANCE_POLICY_PATH).exists()
        )

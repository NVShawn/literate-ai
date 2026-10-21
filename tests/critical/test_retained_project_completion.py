"""A complete compiled run is joined; partial and mixed runs cannot substitute."""

import hashlib
import json
import os
import shutil
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase,
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from tests.critical import test_retained_project_execution as fixtures
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(fixtures.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = fixtures.RetainedProjectExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _, self.locks = retained_project_sbom_fixture()
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
        policy = RetainedProjectAcceptancePolicy(
            tuple(lock.identity for lock in self.locks),
            (
                RetainedProjectAcceptanceCase(
                    "oracle-command",
                    "accept",
                    "app.bin",
                    ("echo",),
                    b"native\x00product",
                    0,
                    b"native\x00product",
                    b"",
                    10,
                ),
            ),
        )
        content = canonical_json_bytes(policy.to_dict())
        path = self.fixture.source / ACCEPTANCE_POLICY_PATH
        path.parent.mkdir()
        path.write_bytes(content)
        profile = replace(
            self.fixture.profile,
            actions=tuple(actions),
            toolchain_identity=self.fixture.manifest.role_identity(
                {"sdk", "toolchain"}
            ),
            dependency_identity=self.fixture.manifest.role_identity({"dependency"}),
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        self.fixture.plan = replace(
            self.fixture.plan, profile=profile, component_locks=policy.component_locks
        )
        self.fixture.current = self.fixture.plan
        self.graph = project_retained_project_managed_graph(
            self.fixture.plan, self.locks, version="1.0.0"
        )

    def workspace(self, **changes):
        return self.fixture.workspace(
            **{
                "managed_graph": self.graph,
                "component_locks": self.locks,
                **changes,
            }
        )

    def finish(self, workspace=None):
        workspace = workspace or self.workspace()
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        return workspace, workspace.completed_run()

    def test_complete_native_run_binds_all_gates_without_issuing_a_receipt(self):
        workspace, result = self.finish()
        self.assertEqual(len(result.observations), 4)
        self.assertEqual(len(result.test_reports), 1)
        self.assertEqual(
            result.acceptance_observations[0].result.stdout, b"native\x00product"
        )
        self.assertEqual(result.source_bom, result.resolved_bom.source)
        self.assertEqual(len(result.resolved_bom.output_manifest.members), 1)
        self.assertEqual(len(result.output_manifest.members), 3)
        self.assertEqual(result.identity, workspace.completed_run().identity)
        wire = result.to_dict()
        self.assertFalse(wire["lifecycle_receipt"])
        self.assertFalse(wire["sdk_admitted"])
        self.assertEqual(wire["release_authority"], "original-source")
        wire["command_observations"].clear()
        self.assertEqual(len(result.to_dict()["command_observations"]), 4)

    def test_completion_requires_every_stage_but_does_not_consume_missing_actions(self):
        workspace = self.workspace()
        for _ in self.fixture.plan.profile.actions:
            with self.assertRaisesRegex(ValueError, "run-incomplete"):
                workspace.completed_run()
            workspace.execute_next()
        self.assertIsInstance(workspace.completed_run().identity, ContentIdentity)

    def test_legacy_zero_exit_commands_cannot_replace_boms_or_independent_cases(self):
        workspace = self.workspace(managed_graph=None, component_locks=None)
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        with self.assertRaisesRegex(ValueError, "boms-and-independent-acceptance"):
            workspace.completed_run()

    def test_failed_actual_command_cannot_complete(self):
        actions = list(self.fixture.plan.profile.actions)
        actions[0] = replace(actions[0], argv=("fail",))
        self.fixture.plan = replace(
            self.fixture.plan,
            profile=replace(self.fixture.plan.profile, actions=tuple(actions)),
        )
        self.fixture.current = self.fixture.plan
        self.graph = project_retained_project_managed_graph(
            self.fixture.plan, self.locks, version="1.0.0"
        )
        workspace = self.workspace()
        with self.assertRaisesRegex(ValueError, "command-failed"):
            workspace.execute_next()
        with self.assertRaisesRegex(ValueError, "run-incomplete"):
            workspace.completed_run()

    def test_current_source_policy_plan_and_export_custody_remain_required(self):
        workspace, result = self.finish()
        self.fixture.current = replace(
            self.fixture.plan, project_id="changed authority"
        )
        with self.assertRaisesRegex(ValueError, "plan-changed"):
            workspace.completed_run()
        self.fixture.current = self.fixture.plan
        source = self.fixture.source / "src/main.txt"
        original_source = source.read_bytes()
        source.write_bytes(b"changed source")
        with self.assertRaises(ValueError):
            workspace.completed_run()
        source.write_bytes(original_source)
        path = self.fixture.source / ACCEPTANCE_POLICY_PATH
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaises(ValueError):
            workspace.completed_run()
        path.write_bytes(original)
        (workspace.outputs / "docs.txt").write_bytes(b"changed manual")
        with self.assertRaises(ValueError):
            workspace.completed_run()
        self.assertEqual(len(result.output_manifest.members), 3)

    def test_missing_or_mixed_command_test_and_acceptance_observations_refuse(self):
        _, result = self.finish()
        changes = (
            {"observations": result.observations[:-1]},
            {"observations": result.observations[::-1]},
            {
                "observations": (
                    replace(
                        result.observations[0],
                        plan_identity=canonical_identity("foreign"),
                    ),
                    *result.observations[1:],
                )
            },
            {"test_reports": ()},
            {
                "test_reports": (
                    replace(
                        result.test_reports[0],
                        observation_identity=canonical_identity("foreign"),
                    ),
                )
            },
            {"acceptance_observations": ()},
            {
                "acceptance_observations": (
                    replace(
                        result.acceptance_observations[0],
                        case_identity=canonical_identity("foreign"),
                    ),
                )
            },
        )
        for values in changes:
            with self.subTest(values=tuple(values)), self.assertRaises(ValueError):
                replace(result, **values)

    def test_native_export_dependency_drift_refuses_at_completion(self):
        workspace, _ = self.finish()
        with patch.object(
            execution,
            "retained_export_dependency_identity",
            return_value=canonical_identity("changed native dependency"),
        ):
            with self.assertRaisesRegex(ValueError, "export-dependencies-changed"):
                workspace.completed_run()

    def test_missing_selected_package_member_refuses_despite_successful_actions(self):
        self.fixture.plan = replace(
            self.fixture.plan,
            profile=replace(
                self.fixture.plan.profile,
                package_inventory=("app.bin", "docs.txt/missing"),
            ),
        )
        self.fixture.current = self.fixture.plan
        self.graph = project_retained_project_managed_graph(
            self.fixture.plan, self.locks, version="1.0.0"
        )
        workspace = self.workspace()
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        with self.assertRaisesRegex(ValueError, "package-inventory-missing"):
            workspace.completed_run()

    def test_nested_export_custody_preserves_canonical_producer_roots(self):
        profile = self.fixture.plan.profile
        actions = list(profile.actions)
        actions[0] = replace(
            actions[0],
            argv=("export-nested", "{output_root}/bin/app.bin"),
            outputs=("bin/app.bin",),
        )
        path = self.fixture.source / ACCEPTANCE_POLICY_PATH
        policy = RetainedProjectAcceptancePolicy.from_dict(
            json.loads(path.read_bytes())
        )
        policy = replace(
            policy, cases=(replace(policy.cases[0], executable="bin/app.bin"),)
        )
        content = canonical_json_bytes(policy.to_dict())
        path.write_bytes(content)
        profile = replace(
            profile,
            actions=tuple(actions),
            package_inventory=("bin", "docs.txt"),
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        self.fixture.plan = replace(self.fixture.plan, profile=profile)
        self.fixture.current = self.fixture.plan
        self.graph = project_retained_project_managed_graph(
            self.fixture.plan, self.locks, version="1.0.0"
        )
        _, result = self.finish()
        self.assertIn("bin/app.bin", result.output_manifest.roots)
        self.assertNotIn("bin", result.output_manifest.directories)

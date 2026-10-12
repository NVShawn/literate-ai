"""FINALIZE stage sequencing refuses authority/custody drift without local fallback."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.action_finalize_stages import execute_finalize_stages
from literate_ai.contracts import canonical_identity


class FinalizeStageTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        value = f.value
        # Stage sequencing is isolated here; real proof/materialization lives in
        # the command-chain fixture. Runtime IO is stubbed explicitly.
        self.package = f.package
        builds = tuple(
            SimpleNamespace(
                plan=plan,
                inputs=SimpleNamespace(
                    contract=canonical_identity(plan.component_revision.uri)
                ),
                candidate=SimpleNamespace(tree_identity=canonical_identity("source")),
                source_custody_identity=canonical_identity("source-custody"),
            )
            for plan in value.project_plan.components
        )
        self.source = SimpleNamespace(
            identity=canonical_identity("source-custody"), generated_test_suite=object()
        )
        registry = Mock()
        registry.evidence.return_value = self.source
        self.prepared = FinalizeRuntimeInputs(
            value, self.package, Mock(root=Path("/fixture-package")), registry, builds
        )
        root = self.prepared.root_build
        self.custody = SimpleNamespace(
            root=self.prepared.package_tree.root,
            root_plan=root.plan,
            generated_test_suite=self.source.generated_test_suite,
        )
        self.calls = []
        self.ports = SimpleNamespace(
            source_trees=registry,
            contracts={
                build.plan.component_revision.uri: build.inputs.contract
                for build in builds
            },
            project_package_custody=Mock(return_value=self.custody),
        )
        for name in (
            "test_root_integration",
            "execute_packaged_project",
            "accept_project_independently",
        ):

            def operation(*args, name=name):
                self.calls.append((name, args))
                return canonical_identity(name)

            setattr(self.ports, name, Mock(side_effect=operation))
        self.guard = Mock()
        self.authority = Mock()

    def run_stages(self):
        return execute_finalize_stages(
            self.prepared,
            ports=self.ports,
            deadline=self.fixture.deadline,
            admission_guard=self.guard,
            require_execution_authority=self.authority,
        )

    def test_stages_bind_prior_results_and_exact_root_evidence(self):
        evidence = self.run_stages()
        self.assertEqual(
            [name for name, _ in self.calls],
            [
                "test_root_integration",
                "execute_packaged_project",
                "accept_project_independently",
            ],
        )
        self.assertEqual(
            self.calls[-1][1][-2:],
            (
                evidence.root_generated_integration_test_identity,
                evidence.packaged_execution_identity,
            ),
        )
        self.assertEqual(evidence.package_result, self.package)
        self.assertEqual(self.authority.call_count, 6)

    def test_missing_authority_refuses_before_any_stage(self):
        self.authority.side_effect = ActionWireError("fixture.denied", "denied")
        with self.assertRaises(ActionWireError):
            self.run_stages()
        self.assertEqual(self.calls, [])

    def test_changed_package_custody_refuses_before_any_stage(self):
        self.custody.root = Path("/foreign-package")
        with self.assertRaises(ActionWireError) as error:
            self.run_stages()
        self.assertEqual(error.exception.code, "action_finalize.runtime_mismatch")
        self.assertEqual(self.calls, [])

    def test_revocation_after_test_prevents_execution_and_acceptance(self):
        def authority(name, prepared):
            if self.calls:
                raise ActionWireError("fixture.revoked", "revoked")

        self.authority.side_effect = authority
        with self.assertRaises(ActionWireError):
            self.run_stages()
        self.assertEqual(len(self.calls), 1)

    def test_invalid_stage_result_cannot_reach_acceptance(self):
        self.ports.execute_packaged_project.side_effect = lambda *args: None
        with self.assertRaises(ActionWireError) as error:
            self.run_stages()
        self.assertEqual(error.exception.code, "action_finalize.stage_invalid")
        self.ports.accept_project_independently.assert_not_called()

    def test_different_private_command_contract_refuses_before_test(self):
        revision = self.prepared.root_build.plan.component_revision.uri
        self.ports.contracts[revision] = canonical_identity("foreign-contract")
        with self.assertRaises(ActionWireError) as error:
            self.run_stages()
        self.assertEqual(error.exception.code, "action_finalize.runtime_mismatch")
        self.assertEqual(self.calls, [])

    def test_test_failure_stops_execution_and_acceptance(self):
        self.ports.test_root_integration.side_effect = RuntimeError("test failed")
        with self.assertRaisesRegex(RuntimeError, "test failed"):
            self.run_stages()
        self.ports.execute_packaged_project.assert_not_called()
        self.ports.accept_project_independently.assert_not_called()

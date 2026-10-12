"""Standard project finalization delegates once and rejects foreign root evidence."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import tests.support.fixtures_test_standard_project_lifecycle as fixture_module
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleError,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


class StandardProjectFinalizerTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.StandardProjectLifecycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.ports = fixture_module.LifecyclePorts(f.execution, f.names)
        self.finalize = Mock(side_effect=self.evidence)
        self.service = fixture_module._service(
            self.ports, project_finalizer=SimpleNamespace(finalize=self.finalize)
        )
        for name in (
            "test_root_integration",
            "execute_packaged_project",
            "accept_project_independently",
        ):
            setattr(
                self.ports, name, Mock(side_effect=AssertionError("local fallback"))
            )

    def evidence(self, lock, project, graph, plan, package):
        return StandardRootIntegrationEvidence(
            lock.identity,
            self.fixture.execution.identity,
            project.identity,
            graph,
            next(
                link
                for link in graph.link_plans
                if link.identity == plan.link_plan_identity
            ),
            plan,
            package,
            canonical_identity("remote-root-tests"),
            canonical_identity("remote-execution"),
            canonical_identity("remote-acceptance"),
        )

    def execute(self):
        f = self.fixture
        return self.service.execute(
            f.execution,
            component_lock=f.lock,
            invalidation=fixture_module._decision(
                f.execution, f.names, "money", tuple(f.names.values())
            ),
            prepared_nodes=f.nodes,
            max_parallelism=2,
        )

    def test_exact_remote_evidence_reaches_project_receipt_without_local_stages(self):
        result = self.execute()
        self.assertTrue(result.successful)
        self.finalize.assert_called_once()
        self.assertEqual(
            result.root_integration, self.evidence(*self.finalize.call_args.args)
        )
        self.assertEqual(
            result.aggregate_receipt.root_integration_evidence_identity,
            result.root_integration.identity,
        )

    def test_untyped_result_cannot_admit_project(self):
        self.finalize.side_effect = None
        self.finalize.return_value = object()
        with self.assertRaises(StandardProjectLifecycleError) as error:
            self.execute()
        self.assertEqual(
            error.exception.code, "standard_lifecycle.finalization_invalid"
        )
        self.assertNotIn(("admit", "project"), self.ports.events)

    def test_other_execution_plan_cannot_admit_project(self):
        self.finalize.side_effect = lambda *args: replace(
            self.evidence(*args), execution_plan_identity=canonical_identity("foreign")
        )
        with self.assertRaises(StandardProjectLifecycleError) as error:
            self.execute()
        self.assertEqual(
            error.exception.code, "standard_lifecycle.finalization_mismatch"
        )
        self.assertNotIn(("admit", "project"), self.ports.events)

    def test_finalizer_failure_propagates_without_local_fallback(self):
        self.finalize.side_effect = RuntimeError("remote failure")
        with self.assertRaisesRegex(RuntimeError, "remote failure"):
            self.execute()
        self.assertNotIn(("admit", "project"), self.ports.events)

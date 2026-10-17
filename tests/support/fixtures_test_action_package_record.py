"""PACKAGE descriptors preserve exact plan intent without asserting LINK proof."""

import unittest
from datetime import UTC, datetime, timedelta

import tests.support.fixtures_test_standard_artifact_assembly as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_identity


class PackageWorkerInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = fixture_module.StandardArtifactAssemblyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        execution = fixture.fixture.execution
        root = fixture.result.root_integration
        package = next(
            node
            for node in plan_lifecycle_action_dag(execution, worker_ids=("package",))
            if node.kind is LifecycleActionKind.PACKAGE
        )
        self.value = PackageWorkerInput(
            execution,
            root.artifact_graph,
            root.package_plan,
            tuple(
                (action, canonical_identity(action))
                for action in package.predecessor_ids
            ),
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=1))

    def admit(self, value=None, raw=None):
        raw = (value or self.value).to_bytes() if raw is None else raw
        return PackageWorkerInput.admit(raw, record_identity(raw), self.deadline)

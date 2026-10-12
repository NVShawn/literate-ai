"""GENERATE dispatch binds its worker and exact handoff before child execution."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_generate_record as source_fixture
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate import execute_generate_action
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


def make_generate_request(value, deadline):
    worker = LifecycleActionWorker(
        "builder",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observation"),
    )
    action = next(
        node
        for node in plan_lifecycle_action_dag(
            value.execution_plan, worker_ids=(worker.worker_id,)
        )
        if node.kind is LifecycleActionKind.GENERATE
        and node.component_revision == value.plan.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            value.execution_plan.identity,
            value.plan.component_revision,
            LifecycleActionKind.GENERATE,
            value.generation_plan_identity,
        )
    )
    content = value.to_bytes()
    return (
        LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            (),
            deadline.identity,
            (record_identity(content),),
        ),
        {record_identity(content): content, record_identity(payload): payload},
    )


class GenerateActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.GenerateWorkerInputTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = self.fixture.value
        self.deadline = self.fixture.deadline
        self.request, self.records = make_generate_request(self.value, self.deadline)
        self.arguments = dict(
            request=self.request,
            deadline=self.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            launcher=LocalComponentToolBinding(sys.executable, ("-c", "pass")),
            cwd=self.fixture.cas.root.parent,
            admission_guard=lambda: None,
            environment=dict(os.environ),
        )

    def refuse_before_launch(self, **changes):
        process = Mock(side_effect=AssertionError("child launched"))
        with (
            patch(
                "literate_ai.adapters.action_generate.run_generate_worker_process",
                process,
            ),
            self.assertRaises(ActionWireError),
        ):
            execute_generate_action(**self.arguments | changes)
        process.assert_not_called()

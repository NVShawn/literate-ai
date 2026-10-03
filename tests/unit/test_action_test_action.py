"""TEST dispatch binds its worker and exact handoff before child execution."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test import admit_test_action, execute_test_action
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import (
    DependencyKind,
    canonical_identity,
    canonical_json_bytes,
)
from tests.unit import test_action_dag_planning as planning_fixture
from tests.unit import test_action_test_execution as source_fixture


def make_test_request(value, deadline):
    build = value.build_input
    worker = LifecycleActionWorker(
        "builder",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observation"),
    )
    action = next(
        node
        for node in plan_lifecycle_action_dag(
            build.execution_plan, worker_ids=(worker.worker_id,)
        )
        if node.kind is LifecycleActionKind.TEST
        and node.component_revision == build.candidate.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            build.execution_plan_identity,
            build.candidate.component_revision,
            LifecycleActionKind.TEST,
            build.generation_plan_identity,
        )
    )
    content = value.to_bytes()
    return (
        LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            (record_identity(content),),
            deadline.identity,
        ),
        {record_identity(content): content, record_identity(payload): payload},
    )


class TestActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionTestExecutionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = self.fixture.value
        self.request, self.records = make_test_request(
            self.value, self.fixture.deadline
        )
        self.arguments = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            launcher=LocalComponentToolBinding(sys.executable, ("-c", "pass")),
            cwd=self.fixture.fixture.root,
            environment=dict(os.environ),
        )

    def refuse_before_launch(self, **changes):
        process = Mock(side_effect=AssertionError("child launched"))
        with (
            patch("literate_ai.adapters.action_test.run_test_worker_process", process),
            self.assertRaises(ActionWireError),
        ):
            execute_test_action(**(self.arguments | changes))
        process.assert_not_called()

    def test_wrong_worker_phase_predecessor_and_deadline_refuse_before_launch(self):
        action = self.request.action
        for changes in (
            {"expected_worker_identity": canonical_identity("other-worker")},
            {
                "request": replace(
                    self.request, action=replace(action, kind=LifecycleActionKind.BUILD)
                )
            },
            {
                "request": replace(
                    self.request, action=replace(action, action_id="other")
                )
            },
            {
                "request": replace(
                    self.request, action=replace(action, predecessor_ids=("other",))
                )
            },
            {"request": replace(self.request, predecessor_result_identities=())},
            {
                "deadline": replace(
                    self.fixture.deadline,
                    expires_at=self.fixture.deadline.expires_at + timedelta(seconds=1),
                )
            },
        ):
            with self.subTest(changes=changes.keys()):
                self.refuse_before_launch(**changes)

    def test_missing_extra_corrupt_and_oversized_records_refuse_before_launch(self):
        input_identity = self.request.predecessor_result_identities[0]
        for records in (
            {},
            self.records | {canonical_identity("extra"): b"extra"},
            self.records | {input_identity: b"changed"},
            self.records | {input_identity: b"x" * (17 * 1024 * 1024)},
        ):
            with self.subTest(size=sum(map(len, records.values()))):
                self.refuse_before_launch(records=records)

    def test_validly_hashed_foreign_payload_cannot_launch(self):
        content = canonical_json_bytes(
            lifecycle_action_payload(
                canonical_identity("other-execution"),
                self.value.build_input.candidate.component_revision,
                LifecycleActionKind.TEST,
                self.value.build_input.generation_plan_identity,
            )
        )
        records = dict(self.records)
        del records[self.request.action.payload_identity]
        records[record_identity(content)] = content
        request = replace(
            self.request,
            action=replace(
                self.request.action, payload_identity=record_identity(content)
            ),
        )
        self.refuse_before_launch(request=request, records=records)

    def test_actual_child_cannot_return_input_as_successful_test_result(self):
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
        )
        with self.assertRaises(ActionWireError):
            execute_test_action(**(self.arguments | {"launcher": launcher}))

    def test_actual_child_result_is_readmitted_against_same_handoff(self):
        result = self.fixture.execute()
        (self.fixture.fixture.root / "return.json").write_bytes(result)
        launcher = LocalComponentToolBinding(
            sys.executable,
            (
                "-c",
                "from pathlib import Path; import sys; "
                "sys.stdout.buffer.write(Path('return.json').read_bytes())",
            ),
        )
        self.assertEqual(
            execute_test_action(**(self.arguments | {"launcher": launcher})), result
        )

    def test_canonical_validation_predecessors_cannot_be_erased(self):
        execution, nodes, _ = planning_fixture.ActionDagPlanningTests()._plan(
            DependencyKind.VALIDATION
        )
        revision = execution.root_revision
        action = nodes[lifecycle_action_id(revision, LifecycleActionKind.TEST)]
        self.assertGreater(len(action.predecessor_ids), 1)
        generation = next(
            item
            for item in execution.generation_plans
            if item.component_revision == revision
        )
        admitted = SimpleNamespace(
            build_input=SimpleNamespace(
                execution_plan=execution,
                execution_plan_identity=execution.identity,
                candidate=SimpleNamespace(component_revision=revision),
                generation_plan_identity=generation.identity,
            )
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                execution.identity,
                revision,
                LifecycleActionKind.TEST,
                generation.identity,
            )
        )
        input_identity = self.request.predecessor_result_identities[0]
        request = replace(
            self.request,
            action=action,
            worker=replace(self.request.worker, worker_id="alpha"),
        )
        records = {
            input_identity: self.records[input_identity],
            record_identity(payload): payload,
        }
        # Isolate action-metadata admission after the independently tested handoff
        # decoder; this does not claim cross-component TEST execution qualification.
        with patch(
            "literate_ai.adapters.action_test.TestWorkerInput.admit",
            return_value=admitted,
        ):
            arguments = dict(
                deadline=self.fixture.deadline,
                records=records,
                expected_worker_identity=request.worker.worker_identity,
            )
            self.assertIs(admit_test_action(request, **arguments), admitted)
            stripped = replace(
                action,
                predecessor_ids=(
                    lifecycle_action_id(revision, LifecycleActionKind.BUILD),
                ),
            )
            with self.assertRaises(ActionWireError):
                admit_test_action(replace(request, action=stripped), **arguments)


if __name__ == "__main__":
    unittest.main()

"""GENERATE dispatch binds its worker and exact handoff before child execution."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_generate_record as source_fixture
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate import (
    execute_generate_action,
)
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
from literate_ai.contracts import (
    canonical_identity,
    canonical_json_bytes,
)


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
            execute_generate_action(**(self.arguments | changes))
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
            {"request": replace(self.request, input_record_identities=())},
            {
                "deadline": replace(
                    self.deadline,
                    expires_at=self.deadline.expires_at + timedelta(seconds=1),
                )
            },
        ):
            with self.subTest(changes=changes.keys()):
                self.refuse_before_launch(**changes)

    def test_missing_extra_corrupt_and_oversized_records_refuse_before_launch(self):
        input_identity = self.request.input_record_identities[0]
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
                self.value.plan.component_revision,
                LifecycleActionKind.GENERATE,
                self.value.generation_plan_identity,
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
            execute_generate_action(**(self.arguments | {"launcher": launcher}))

    def test_production_generate_has_no_scheduling_predecessors(self):
        from literate_ai.adapters.action_generate import admit_generate_action

        self.assertEqual(self.request.action.predecessor_ids, ())
        self.assertEqual(
            admit_generate_action(
                self.request,
                self.deadline,
                self.records,
                expected_worker_identity=self.request.worker.worker_identity,
            ),
            self.value,
        )

    def test_child_result_binds_same_input_and_rejects_other_allocation(self):
        from tests.support.fixtures_test_action_generate_result import (
            GenerateWorkerResultTests,
        )

        fixture = GenerateWorkerResultTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        result = fixture.result.to_bytes()
        root = self.fixture.cas.root.parent
        (root / "return.json").write_bytes(result)
        launcher = LocalComponentToolBinding(
            sys.executable,
            (
                "-c",
                "import sys; from pathlib import Path; sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(Path('return.json').read_bytes())",
            ),
        )
        request, records = make_generate_request(fixture.value, self.deadline)
        args = self.arguments | dict(
            request=request, records=records, launcher=launcher
        )
        self.assertEqual(execute_generate_action(**args), result)
        foreign = replace(
            fixture.value, workspace_allocation_identity=canonical_identity("foreign")
        )
        request, records = make_generate_request(foreign, self.deadline)
        with self.assertRaises(ActionWireError):
            execute_generate_action(**(args | dict(request=request, records=records)))

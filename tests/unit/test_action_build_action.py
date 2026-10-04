"""BUILD dispatch must bind its worker and PLAN handoff before child execution."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from literate_ai.adapters.action_build import execute_build_action
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
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
from tests.support import fixtures_test_action_build_source as source_fixture
from tests.support.fixtures_test_action_build_record import build_worker_input


def build_request(value, deadline):
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
        if node.kind is LifecycleActionKind.BUILD
        and node.component_revision == value.candidate.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            value.execution_plan_identity,
            value.candidate.component_revision,
            LifecycleActionKind.BUILD,
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
            (record_identity(content),),
            deadline.identity,
        ),
        {record_identity(content): content, record_identity(payload): payload},
    )


class BuildActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionBuildSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = build_worker_input(self.fixture)
        self.request, self.records = build_request(self.value, self.fixture.deadline)
        self.arguments = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            launcher=LocalComponentToolBinding(sys.executable, ("-c", "pass")),
            cwd=self.fixture.root,
            environment=dict(os.environ),
        )

    def refuse_before_launch(self, **changes):
        process = Mock(side_effect=AssertionError("child launched"))
        with (
            patch(
                "literate_ai.adapters.action_build.run_build_worker_process", process
            ),
            self.assertRaises(ActionWireError),
        ):
            execute_build_action(**(self.arguments | changes))
        process.assert_not_called()

    def test_wrong_worker_phase_predecessor_and_deadline_refuse_before_launch(self):
        action = self.request.action
        for changes in (
            {"expected_worker_identity": canonical_identity("other-worker")},
            {
                "request": replace(
                    self.request, action=replace(action, kind=LifecycleActionKind.TEST)
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
                self.value.candidate.component_revision,
                LifecycleActionKind.BUILD,
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

    def test_actual_child_cannot_return_input_as_successful_build_result(self):
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
        )
        with self.assertRaises(ActionWireError):
            execute_build_action(**(self.arguments | {"launcher": launcher}))


if __name__ == "__main__":
    unittest.main()

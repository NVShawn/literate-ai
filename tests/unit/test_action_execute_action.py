"""EXECUTE dispatch binds its worker and exact handoff before child execution."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute import (
    admit_execute_action,
    execute_execution_action,
    execute_input_identity,
    execute_predecessor_records,
)
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
from tests.support import fixtures_test_action_dag_planning as planning_fixture
from tests.support import fixtures_test_action_execute_execution as source_fixture


def make_execute_request(value, deadline):
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
        if node.kind is LifecycleActionKind.EXECUTE
        and node.component_revision == build.candidate.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            build.execution_plan_identity,
            build.candidate.component_revision,
            LifecycleActionKind.EXECUTE,
            build.generation_plan_identity,
        )
    )
    identities, records = execute_predecessor_records(value, action)
    records[record_identity(payload)] = payload
    return (
        LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            identities,
            deadline.identity,
        ),
        records,
    )


class ExecuteActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionExecuteExecutionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = self.fixture.value
        self.request, self.records = make_execute_request(
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
            patch(
                "literate_ai.adapters.action_execute.run_execute_worker_process",
                process,
            ),
            self.assertRaises(ActionWireError),
        ):
            execute_execution_action(**(self.arguments | changes))
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
                LifecycleActionKind.EXECUTE,
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
            execute_execution_action(**(self.arguments | {"launcher": launcher}))

    def test_actual_child_result_is_readmitted_against_same_handoff(self):
        result = self.fixture.execute()
        (self.fixture.fixture.root / "return.json").write_bytes(result)
        launcher = LocalComponentToolBinding(
            sys.executable,
            (
                "-c",
                "from pathlib import Path; import sys; sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(Path('return.json').read_bytes())",
            ),
        )
        self.assertEqual(
            execute_execution_action(**(self.arguments | {"launcher": launcher})),
            result,
        )

    def test_canonical_runtime_predecessors_cannot_be_erased(self):
        execution, nodes, _ = planning_fixture.ActionDagPlanningTests()._plan(
            DependencyKind.RUNTIME
        )
        revision = execution.root_revision
        action = nodes[lifecycle_action_id(revision, LifecycleActionKind.EXECUTE)]
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
                LifecycleActionKind.EXECUTE,
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
        # decoder; this does not claim cross-component EXECUTE execution qualification.
        with patch(
            "literate_ai.adapters.action_execute.ExecuteWorkerInput.admit",
            return_value=admitted,
        ):
            arguments = dict(
                deadline=self.fixture.deadline,
                records=records,
                expected_worker_identity=request.worker.worker_identity,
            )
            # Metadata cannot admit an incomplete set of runtime predecessor records.
            with self.assertRaises(ActionWireError):
                admit_execute_action(request, **arguments)
            stripped = replace(
                action,
                predecessor_ids=(
                    lifecycle_action_id(revision, LifecycleActionKind.TEST),
                ),
            )
            with self.assertRaises(ActionWireError):
                admit_execute_action(replace(request, action=stripped), **arguments)


class RuntimeExecuteActionTests(unittest.TestCase):
    def setUp(self):
        from tests.support import fixtures_test_action_execute_providers as provider_fixture

        self.fixture = f = provider_fixture.ExecuteProviderTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.request, self.records = make_execute_request(f.value, f.fixture.deadline)

    def admit(self, request=None, records=None):
        return admit_execute_action(
            request or self.request,
            self.fixture.fixture.deadline,
            self.records if records is None else records,
            expected_worker_identity=self.request.worker.worker_identity,
        )

    def test_complete_runtime_predecessors_round_trip_through_wire(self):
        from literate_ai.adapters.action_dispatch_wire import (
            decode_action_request,
            encode_action_request,
        )

        raw = encode_action_request(
            self.request, self.fixture.fixture.deadline, self.records
        )
        request, _, records = decode_action_request(raw)
        self.assertGreater(len(request.predecessor_result_identities), 1)
        self.assertEqual(self.admit(request, records), self.fixture.value)

    def test_missing_reordered_and_duplicated_predecessors_refuse(self):
        identities = self.request.predecessor_result_identities
        for changed in (
            identities[:1],
            tuple(reversed(identities)),
            (identities[0],) * len(identities),
        ):
            with self.subTest(changed=changed), self.assertRaises(ActionWireError):
                self.admit(replace(self.request, predecessor_result_identities=changed))

    def test_substituted_receipt_with_valid_hash_refuses(self):
        receipt = self.fixture.value.accepted_providers[0]
        content = canonical_json_bytes(
            replace(
                receipt,
                acceptance_policy_identity=canonical_identity("substituted policy"),
            ).to_dict()
        )
        records = dict(self.records)
        del records[receipt.identity]
        replacement = record_identity(content)
        records[replacement] = content
        identities = tuple(
            replacement if item == receipt.identity else item
            for item in self.request.predecessor_result_identities
        )
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(self.request, predecessor_result_identities=identities), records
            )

    def test_erased_runtime_edge_cannot_weaken_wire_requirements(self):
        identity = execute_input_identity(self.request)
        action = replace(
            self.request.action,
            predecessor_ids=(
                lifecycle_action_id(
                    self.request.action.component_revision, LifecycleActionKind.TEST
                ),
            ),
        )
        request = replace(
            self.request, action=action, predecessor_result_identities=(identity,)
        )
        records = {
            key: self.records[key] for key in (identity, action.payload_identity)
        }
        with self.assertRaises(ActionWireError):
            self.admit(request, records)


if __name__ == "__main__":
    unittest.main()

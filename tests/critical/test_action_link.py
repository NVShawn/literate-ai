"""LINK dispatch cannot bypass production DAG admission or accepted proof."""

import unittest
from dataclasses import replace
from unittest.mock import Mock

import tests.support.fixtures_test_action_link_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_link import execute_link_action
from literate_ai.adapters.action_link_result import LinkWorkerResult
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
from literate_ai.storage import FileSystemCAS


class LinkActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.LinkWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = value = f.value
        build = value.build
        worker = LifecycleActionWorker(
            "link",
            canonical_identity("worker"),
            canonical_identity("catalog"),
            canonical_identity("observed"),
        )
        action = next(
            node
            for node in plan_lifecycle_action_dag(
                build.execution_plan, worker_ids=(worker.worker_id,)
            )
            if node.kind is LifecycleActionKind.LINK
            and node.component_revision == build.plan.component_revision
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                build.execution_plan_identity,
                action.component_revision,
                LifecycleActionKind.LINK,
                build.generation_plan_identity,
            )
        )
        self.raw = value.to_bytes()
        accepted = value.acceptance_result.to_bytes()
        self.request = LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            (record_identity(accepted),),
            f.deadline.identity,
            (record_identity(self.raw),),
        )
        self.records = {
            record_identity(raw): raw for raw in (self.raw, payload, accepted)
        }
        self.source = f.fixture.worker.cas
        self.cas = FileSystemCAS(self.source.root.parent / "link-action-cas")

    def execute(self, **changes):
        arguments = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            cas=self.cas,
            admission_guard=lambda: None,
            blob_source=self.source.get_bytes,
        )
        arguments.update(changes)
        return execute_link_action(**arguments)

    def test_actual_proof_reopening_returns_exact_result(self):
        raw = self.execute()
        result = LinkWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=self.raw,
            input_identity=record_identity(self.raw),
            deadline=self.fixture.deadline,
        )
        self.assertEqual(result.manifest, self.value.manifest)

    def test_wrong_worker_deadline_and_dag_refuse_before_transfer(self):
        action = self.request.action
        for changes in (
            {"expected_worker_identity": canonical_identity("other")},
            {
                "request": replace(
                    self.request, deadline_identity=canonical_identity("other")
                )
            },
            {
                "request": replace(
                    self.request, action=replace(action, predecessor_ids=("other",))
                )
            },
            {
                "request": replace(
                    self.request,
                    action=replace(action, kind=LifecycleActionKind.PACKAGE),
                )
            },
            {
                "request": replace(
                    self.request, action=replace(action, action_id="other")
                )
            },
        ):
            fetch = Mock(side_effect=AssertionError("transfer before admission"))
            with self.subTest(changes=changes), self.assertRaises(ActionWireError):
                self.execute(blob_source=fetch, **changes)
            fetch.assert_not_called()

    def test_predecessor_substitution_refuses(self):
        foreign = b"foreign"
        foreign_id = record_identity(foreign)
        request = replace(self.request, predecessor_result_identities=(foreign_id,))
        records = dict(self.records)
        records.pop(self.request.predecessor_result_identities[0])
        records[foreign_id] = foreign
        with self.assertRaises(ActionWireError):
            self.execute(request=request, records=records)

    def test_cancelled_action_does_not_transfer(self):
        fetch = Mock(side_effect=AssertionError("cancelled transfer"))
        with self.assertRaises(ActionWireError):
            self.execute(cancelled=lambda: True, blob_source=fetch)
        fetch.assert_not_called()

    def test_valid_descriptor_with_corrupt_proof_cannot_return_result(self):
        with self.assertRaises(ActionWireError):
            self.execute(blob_source=lambda _: b"corrupt")

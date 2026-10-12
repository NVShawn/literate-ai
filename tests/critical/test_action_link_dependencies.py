"""LINK closures bind production action IDs and reopen accepted artifact proof."""

import unittest
from unittest.mock import Mock

import tests.support.fixtures_test_action_link_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_link_dependencies import reopen_link_result_closure
from literate_ai.application.action_dag_planning import lifecycle_action_id
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS


class LinkDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.LinkWorkerResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = f.fixture.value
        self.result_raw = f.result.to_bytes()
        self.result_id = record_identity(self.result_raw)
        self.action = lifecycle_action_id(
            self.value.build.plan.component_revision, LifecycleActionKind.LINK
        )
        self.records = {f.identity: f.raw, self.result_id: self.result_raw}
        self.source = f.fixture.fixture.worker.cas
        self.cas = FileSystemCAS(self.source.root.parent / "link-closure")

    def reopen(self, **changes):
        arguments = dict(
            roots=((self.action, self.result_id),),
            execution_plan_identity=self.value.build.execution_plan_identity,
            records=self.records,
            cas=self.cas,
            deadline=self.fixture.fixture.deadline,
            admission_guard=lambda: None,
            blob_source=self.source.get_bytes,
        )
        arguments.update(changes)
        return reopen_link_result_closure(**arguments)

    def test_complete_leaf_proof_and_shared_reference_are_reopened_once(self):
        fetch = Mock(wraps=self.source.get_bytes)
        result = self.reopen(
            roots=((self.action, self.result_id), (self.action, self.result_id)),
            blob_source=fetch,
        )
        self.assertEqual(result, ((self.value, self.fixture.result),))
        self.assertTrue(fetch.called)
        self.assertEqual(
            len(fetch.call_args_list),
            len({call.args[0] for call in fetch.call_args_list}),
        )

    def test_foreign_action_or_execution_plan_refuses_before_blob_fetch(self):
        for changes in (
            {"roots": (("other-link", self.result_id),)},
            {"execution_plan_identity": canonical_identity("other-plan")},
        ):
            fetch = Mock(side_effect=AssertionError("unexpected fetch"))
            with self.assertRaises(ActionWireError):
                self.reopen(blob_source=fetch, **changes)
            fetch.assert_not_called()

    def test_missing_input_and_unrelated_records_refuse_before_fetch(self):
        for records in (
            {self.result_id: self.result_raw},
            self.records | {record_identity(b"extra"): b"extra"},
        ):
            fetch = Mock(side_effect=AssertionError("unexpected fetch"))
            with self.assertRaises(ActionWireError):
                self.reopen(records=records, blob_source=fetch)
            fetch.assert_not_called()

    def test_corrupt_accepted_proof_does_not_return_a_closure(self):
        with self.assertRaises(ActionWireError):
            self.reopen(blob_source=lambda _: b"corrupt")

    def test_leaf_has_no_dependency_records(self):
        self.assertEqual(self.reopen(roots=(), records={}), ())

    def test_transfer_cannot_replace_the_admitted_record_snapshot(self):
        def fetch(ref):
            self.records.clear()
            return self.source.get_bytes(ref)

        self.assertEqual(
            self.reopen(blob_source=fetch), ((self.value, self.fixture.result),)
        )

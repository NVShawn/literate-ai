"""Real AUTHORIZE receiver rejects changed custody and controller time."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_authorization import (
    authorization_action_inputs,
    execute_authorization_action,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.standard_authorization import StandardAuthorizationInputs
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.support import fixtures_test_action_source_index as source_fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)


class AuthorizationActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        _, _, candidate, self.intent = _python_copy_lifecycle(self.fixture.root)
        self.issued = datetime.now(UTC) - timedelta(seconds=1)
        index = canonical_json_bytes(
            {
                "schema": "literate-ai/disabled-source-index@1",
                "component_revision": self.intent.component_revision.uri,
                "source": self.intent.source_tree_identity.uri,
            }
        )
        content = authorization_action_inputs(
            self.fixture.execution_plan.identity,
            candidate.component_generation_plan_identity,
            self.intent,
            index,
            self.issued,
        )
        self.expected = StandardAuthorizationInputs(
            self.intent, record_identity(index), self.issued
        ).authorize()
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.fixture.execution_plan.identity,
                self.intent.component_revision,
                LifecycleActionKind.AUTHORIZE,
                candidate.component_generation_plan_identity,
            )
        )
        action = replace(
            self.fixture.request.action,
            action_id=lifecycle_action_id(
                self.intent.component_revision, LifecycleActionKind.AUTHORIZE
            ),
            component_revision=self.intent.component_revision,
            kind=LifecycleActionKind.AUTHORIZE,
            predecessor_ids=(
                lifecycle_action_id(
                    self.intent.component_revision, LifecycleActionKind.BUILD_INTENT
                ),
            ),
            payload_identity=record_identity(payload),
        )
        self.fixture.request = replace(
            self.fixture.request,
            action=action,
            predecessor_result_identities=(record_identity(content),),
        )
        self.fixture.records = {
            record_identity(payload): payload,
            record_identity(content): content,
        }

    def execute(self, request=None, records=None, **changes):
        return execute_authorization_action(
            request or self.fixture.request,
            changes.get("deadline", self.fixture.deadline),
            records or self.fixture.records,
            expected_worker_identity=changes.get(
                "worker", self.fixture.worker.identity
            ),
        )

    def changed(self, change):
        records = dict(self.fixture.records)
        old = self.fixture.request.predecessor_result_identities[0]
        document = json.loads(records.pop(old))
        change(document)
        content = canonical_json_bytes(document)
        records[record_identity(content)] = content
        return replace(
            self.fixture.request,
            predecessor_result_identities=(record_identity(content),),
        ), records

    def test_real_receiver_returns_exact_grant_without_host_execution(self):
        outcome = self.fixture.dispatch()
        self.assertIsNone(outcome.failure_code)
        self.assertEqual(
            self.fixture.results[outcome.result_identity],
            canonical_json_bytes(self.expected.to_dict()),
        )
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_time_index_and_payload_substitutions_are_refused(self):
        for change in (
            lambda d: d.update(
                issued_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            ),
            lambda d: d.update(
                issued_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat()
            ),
            lambda d: d.update(issued_at="2026-09-30T00:00:00"),
            lambda d: d["index_result"].update(
                source=canonical_identity("foreign").uri
            ),
            lambda d: d["index_result"].update(
                component_revision=canonical_identity("foreign").uri
            ),
            lambda d: d.update(
                generation_plan_identity=canonical_identity("foreign").uri
            ),
            lambda d: d.update(
                execution_plan_identity=canonical_identity("foreign").uri
            ),
            lambda d: d.update(profile="unrestricted"),
        ):
            request, records = self.changed(change)
            with self.assertRaises(ActionWireError):
                self.execute(request, records)
        request, records = self.changed(
            lambda d: d["index_result"].update(source=canonical_identity("foreign").uri)
        )
        self.fixture.request, self.fixture.records = request, records
        outcome = self.fixture.dispatch()
        self.assertEqual(outcome.failure_code, "action_authorization.invalid")
        self.assertIsNone(outcome.result_identity)

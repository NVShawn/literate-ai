"""Real one-shot PLAN execution with bounded, exact predecessor custody."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_plan import (
    PLAN_INPUTS_SCHEMA,
    execute_plan_action,
    plan_action_inputs,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit import test_action_source_index as source_fixture
from tests.unit.test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class PlanActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ports, _, candidate, self.intent = _python_copy_lifecycle(
            self.fixture.root
        )
        self.authorization = self.ports.authorize(self.intent, _identity("index"))
        self.expected = self.ports.finalize(self.intent, self.authorization)
        self.contract = self.ports.contracts[candidate.component_revision.uri]
        self.generation = candidate.component_generation_plan_identity
        content = plan_action_inputs(
            self.fixture.execution_plan.identity,
            self.generation,
            self.intent,
            self.authorization,
            self.contract,
            (),
            (),
            dependency_resolution="none",
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.fixture.execution_plan.identity,
                self.intent.component_revision,
                LifecycleActionKind.PLAN,
                self.generation,
            )
        )
        action = replace(
            self.fixture.request.action,
            action_id=lifecycle_action_id(
                self.intent.component_revision, LifecycleActionKind.PLAN
            ),
            component_revision=self.intent.component_revision,
            kind=LifecycleActionKind.PLAN,
            predecessor_ids=(
                lifecycle_action_id(
                    self.intent.component_revision, LifecycleActionKind.AUTHORIZE
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

    def execute(self, **changes):
        return execute_plan_action(
            changes.pop("request", self.fixture.request),
            changes.pop("deadline", self.fixture.deadline),
            changes.pop("records", self.fixture.records),
            expected_worker_identity=changes.pop(
                "expected_worker_identity", self.fixture.worker.identity
            ),
        )

    def changed_inputs(self, change):
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

    def test_real_command_receiver_returns_exact_plan_without_running_build(self):
        expected = canonical_json_bytes(self.expected.to_dict())
        outcome = self.fixture.dispatch()
        self.assertIsNone(outcome.failure_code)
        self.assertEqual(outcome.result_identity, record_identity(expected))
        self.assertEqual(self.fixture.results[outcome.result_identity], expected)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])
        self.assertEqual(self.execute(), expected)

    def test_current_grant_and_binding_substitutions_are_refused(self):
        def grant_change(key, value):
            return lambda doc: doc["authorization"]["grant"].update({key: value})

        changes = (
            lambda doc: doc.update(
                execution_plan_identity=canonical_identity("other").uri
            ),
            lambda doc: doc.update(
                generation_plan_identity=canonical_identity("other").uri
            ),
            lambda doc: doc.update(unknown=True),
            lambda doc: doc.update(dependency_resolution="npm"),
            grant_change("revoked", True),
            grant_change("classification_digest", canonical_identity("other").uri),
            grant_change("privileges", []),
            grant_change("request_digest", canonical_identity("other").uri),
            lambda doc: doc["authorization"]["grant"].update(
                issued_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
                expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            ),
            lambda doc: doc["authorization"]["grant"].update(
                issued_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                expires_at=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
            ),
        )
        for change in changes:
            with self.subTest(change=change):
                request, records = self.changed_inputs(change)
                with self.assertRaises(ActionWireError):
                    self.execute(request=request, records=records)

    def test_worker_deadline_phase_and_record_custody_are_enforced(self):
        extra = canonical_json_bytes({"extra": True})
        for changes in (
            {"expected_worker_identity": canonical_identity("other")},
            {
                "deadline": ActionDispatchDeadline(
                    datetime.now(UTC) + timedelta(seconds=30)
                )
            },
            {"records": {}},
            {"records": {**self.fixture.records, record_identity(extra): extra}},
            {
                "request": replace(
                    self.fixture.request,
                    action=replace(
                        self.fixture.request.action, kind=LifecycleActionKind.BUILD
                    ),
                )
            },
        ):
            with (
                self.subTest(change=tuple(changes)),
                self.assertRaises(ActionWireError),
            ):
                self.execute(**changes)
        with patch("literate_ai.adapters.action_plan.MAX_ACTION_RECORD_BYTES", 1):
            with self.assertRaises(ActionWireError):
                self.execute()

    def test_real_receiver_refuses_revoked_and_changed_plan_authority(self):
        for change in (
            lambda doc: doc["authorization"]["grant"].update(revoked=True),
            lambda doc: doc.update(
                generation_plan_identity=canonical_identity("other").uri
            ),
        ):
            with self.subTest(change=change):
                request, records = self.changed_inputs(change)
                with (
                    patch.object(self.fixture, "request", request),
                    patch.object(self.fixture, "records", records),
                ):
                    outcome = self.fixture.dispatch()
                self.assertEqual(outcome.failure_code, "action_plan.invalid")
                self.assertIsNone(outcome.result_identity)

    def test_duplicate_json_keys_and_changed_record_bytes_are_refused(self):
        original = self.fixture.request.predecessor_result_identities[0]
        for content, recompute in (
            (self.fixture.records[original] + b" ", False),
            (b'{"schema":"unexpected",' + self.fixture.records[original][1:], True),
        ):
            records = dict(self.fixture.records)
            records.pop(original)
            identity = record_identity(content) if recompute else original
            records[identity] = content
            request = replace(
                self.fixture.request, predecessor_result_identities=(identity,)
            )
            with self.subTest(recompute=recompute), self.assertRaises(ActionWireError):
                self.execute(request=request, records=records)

    def test_closed_public_schema_accepts_real_predecessor(self):
        from jsonschema import Draft202012Validator, ValidationError
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        from tests.unit.test_schema_catalog import SchemaCatalog

        resources = SchemaCatalog().resources
        registry = Registry().with_resources(
            (uri, Resource.from_contents(value, default_specification=DRAFT202012))
            for uri, value in resources.items()
        )
        validator = Draft202012Validator(
            {"$ref": PLAN_INPUTS_SCHEMA}, registry=registry
        )
        document = json.loads(
            self.fixture.records[self.fixture.request.predecessor_result_identities[0]]
        )
        validator.validate(document)
        with self.assertRaises(ValidationError):
            validator.validate({**document, "unknown": True})

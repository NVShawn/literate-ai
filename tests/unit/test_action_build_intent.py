"""Real BUILD_INTENT receiver with exact indexed-source/provider custody."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_build_intent import (
    INTENT_INPUTS_SCHEMA,
    build_intent_action_predecessors,
    execute_build_intent_action,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.standard_build_intent import StandardBuildIntentInputs
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit import test_action_source_index as source_fixture
from tests.unit.test_component_execution_planning import _diamond_lock, _models
from tests.unit.test_standard_local_command_adapter import _python_copy_lifecycle
from tests.unit.test_standard_post_source_evidence import _evidence


def provider_evidence(revision):
    old = _evidence()
    exports = tuple(
        replace(item, component_revision=revision) for item in old.build.exports
    )
    build = replace(
        old.build,
        component_revision=revision,
        exports=exports,
        resolved_sbom_export_identities=tuple(item.identity for item in exports),
    )
    tests = replace(
        old.generated_tests,
        component_revision=revision,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
    )
    execution = replace(
        old.execution,
        component_revision=revision,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        root_export_identity=exports[0].identity,
    )
    return replace(
        old,
        component_revision=revision,
        build=build,
        generated_tests=tests,
        execution=execution,
    )


class BuildIntentActionTests(unittest.TestCase):
    def setUp(self):
        self.configure(DependencyKind.BUILD)

    def configure(self, dependency_kind):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        lock = _diamond_lock(dependency_kind=dependency_kind)
        self.execution = plan_component_execution(lock, model_identities=_models(lock))
        generation = next(
            item
            for item in self.execution.generation_plans
            if item.component_revision == self.execution.root_revision
        )
        self.candidate = replace(
            self.fixture.candidate,
            component_revision=generation.component_revision,
            component_generation_plan_identity=generation.identity,
            generation_key_identity=generation.generation_key.identity,
        )
        ports, _, candidate, _ = _python_copy_lifecycle(self.fixture.root)
        contract = replace(
            ports.contracts[candidate.component_revision.uri],
            component_revision=self.candidate.component_revision,
        )
        action = next(
            node
            for node in plan_lifecycle_action_dag(
                self.execution, worker_ids=(self.fixture.worker.worker_id,)
            )
            if node.component_revision == self.candidate.component_revision
            and node.kind is LifecycleActionKind.BUILD_INTENT
        )
        self.acceptances = tuple(
            provider_evidence(plan.component_revision)
            for plan in self.execution.generation_plans
            if lifecycle_action_id(plan.component_revision, LifecycleActionKind.ACCEPT)
            in action.predecessor_ids
        )
        providers = tuple(
            sorted(
                (
                    export
                    for evidence in self.acceptances
                    for export in evidence.build.exports
                ),
                key=lambda item: item.identity.uri,
            )
        )
        self.inputs = StandardBuildIntentInputs(
            generation.identity, self.candidate, contract, providers, (), (), "none"
        )
        self.index_record = canonical_json_bytes(
            {
                "schema": "literate-ai/disabled-source-index@1",
                "component_revision": self.candidate.component_revision.uri,
                "source": self.candidate.tree_identity.uri,
            }
        )
        handoffs = build_intent_action_predecessors(
            self.execution, self.inputs, self.index_record, self.acceptances
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.execution.identity,
                self.candidate.component_revision,
                LifecycleActionKind.BUILD_INTENT,
                generation.identity,
            )
        )
        self.fixture.request = replace(
            self.fixture.request,
            action=action,
            predecessor_result_identities=tuple(
                record_identity(handoffs[key]) for key in action.predecessor_ids
            ),
        )
        self.fixture.records = {
            record_identity(value): value for value in (*handoffs.values(), payload)
        }
        self.index_position = action.predecessor_ids.index(
            lifecycle_action_id(
                self.candidate.component_revision, LifecycleActionKind.INDEX
            )
        )

    def execute(self, **changes):
        return execute_build_intent_action(
            changes.get("request", self.fixture.request),
            changes.get("deadline", self.fixture.deadline),
            changes.get("records", self.fixture.records),
            expected_worker_identity=changes.get(
                "expected_worker_identity", self.fixture.worker.identity
            ),
        )

    def changed_record(self, position, change):
        request = self.fixture.request
        identities = list(request.predecessor_result_identities)
        records = dict(self.fixture.records)
        value = json.loads(records.pop(identities[position]))
        replacement = change(value)
        content = canonical_json_bytes(value if replacement is None else replacement)
        identities[position] = record_identity(content)
        records[identities[position]] = content
        return replace(
            request, predecessor_result_identities=tuple(identities)
        ), records

    def test_real_receiver_uses_canonical_diamond_and_exact_accepted_exports(self):
        self.assertEqual(len(self.fixture.request.action.predecessor_ids), 3)
        expected = canonical_json_bytes(self.inputs.create().to_dict())
        self.assertEqual(self.execute(), expected)
        outcome = self.fixture.dispatch()
        self.assertIsNone(outcome.failure_code)
        self.assertEqual(self.fixture.results[outcome.result_identity], expected)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_interface_only_edges_do_not_require_provider_artifacts(self):
        self.configure(DependencyKind.GENERATION)
        self.assertEqual(len(self.fixture.request.action.predecessor_ids), 1)
        self.assertEqual(self.inputs.providers, ())
        outcome = self.fixture.dispatch()
        self.assertIsNone(outcome.failure_code)
        self.assertEqual(
            self.fixture.results[outcome.result_identity],
            canonical_json_bytes(self.inputs.create().to_dict()),
        )

    def test_real_receiver_refuses_changed_index_and_foreign_provider(self):
        provider_position = next(i for i in range(3) if i != self.index_position)
        for position, change in (
            (
                self.index_position,
                lambda doc: doc["index_result"].update(
                    source=canonical_identity("foreign").uri
                ),
            ),
            (
                provider_position,
                lambda doc: provider_evidence(canonical_identity("foreign")).to_dict(),
            ),
        ):
            with self.subTest(position=position):
                request, records = self.changed_record(position, change)
                with (
                    patch.object(self.fixture, "request", request),
                    patch.object(self.fixture, "records", records),
                ):
                    outcome = self.fixture.dispatch()
                self.assertEqual(outcome.failure_code, "action_intent.invalid")
                self.assertIsNone(outcome.result_identity)

    def test_missing_extra_and_reordered_predecessors_cannot_change_the_dag(self):
        request = self.fixture.request
        first = request.predecessor_result_identities[0]
        for changed, records in (
            (
                replace(
                    request,
                    predecessor_result_identities=request.predecessor_result_identities[
                        1:
                    ],
                ),
                {
                    key: value
                    for key, value in self.fixture.records.items()
                    if key != first
                },
            ),
            (
                replace(
                    request,
                    action=replace(
                        request.action,
                        predecessor_ids=request.action.predecessor_ids[1:],
                    ),
                    predecessor_result_identities=request.predecessor_result_identities[
                        1:
                    ],
                ),
                {
                    key: value
                    for key, value in self.fixture.records.items()
                    if key != first
                },
            ),
            (
                replace(
                    request,
                    predecessor_result_identities=tuple(
                        reversed(request.predecessor_result_identities)
                    ),
                ),
                self.fixture.records,
            ),
            (request, {**self.fixture.records, record_identity(b"extra"): b"extra"}),
        ):
            with (
                self.subTest(changed=changed.identity),
                self.assertRaises(ActionWireError),
            ):
                self.execute(request=changed, records=records)

    def test_changed_generation_contract_and_exports_are_refused(self):
        changes = (
            lambda doc: doc.update(unknown=True),
            lambda doc: doc["candidate"].update(
                generation_key_identity=canonical_identity("foreign").to_dict()
            ),
            lambda doc: doc["contract"].update(
                component_revision=canonical_identity("foreign").to_dict()
            ),
            lambda doc: doc["execution_plan"].update(
                component_lock_identity=canonical_identity("foreign").to_dict()
            ),
        )
        for change in changes:
            request, records = self.changed_record(self.index_position, change)
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                self.execute(request=request, records=records)
        position = next(i for i in range(3) if i != self.index_position)
        request, records = self.changed_record(
            position,
            lambda doc: doc["build"]["exports"][0].update(
                producer_identity=canonical_identity("foreign").to_dict()
            ),
        )
        with self.assertRaises(ActionWireError):
            self.execute(request=request, records=records)

    def test_deadline_worker_phase_bounds_and_changed_bytes_are_refused(self):
        first = self.fixture.request.predecessor_result_identities[0]
        for changes in (
            {
                "deadline": ActionDispatchDeadline(
                    datetime.now(UTC) + timedelta(seconds=15)
                )
            },
            {"expected_worker_identity": canonical_identity("other")},
            {
                "request": replace(
                    self.fixture.request,
                    action=replace(
                        self.fixture.request.action, kind=LifecycleActionKind.PLAN
                    ),
                )
            },
            {
                "records": {
                    **self.fixture.records,
                    first: self.fixture.records[first] + b" ",
                }
            },
        ):
            with (
                self.subTest(change=tuple(changes)),
                self.assertRaises(ActionWireError),
            ):
                self.execute(**changes)
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with self.assertRaises(ActionWireError):
            self.execute(
                deadline=expired,
                request=replace(
                    self.fixture.request, deadline_identity=expired.identity
                ),
            )
        for bound in ("MAX_ACTION_RECORD_BYTES", "MAX_ACTION_RECORDS"):
            with (
                patch("literate_ai.adapters.action_build_intent." + bound, 1),
                self.assertRaises(ActionWireError),
            ):
                self.execute()

    def test_encoder_refuses_unaccepted_or_packaging_inputs(self):
        for inputs, acceptances in (
            (self.inputs, self.acceptances[:-1]),
            (replace(self.inputs, providers=()), self.acceptances),
            (replace(self.inputs, packages=self.inputs.providers), self.acceptances),
            (self.inputs, (*self.acceptances, self.acceptances[0])),
        ):
            with self.subTest(inputs=inputs), self.assertRaises(ActionWireError):
                build_intent_action_predecessors(
                    self.execution, inputs, self.index_record, acceptances
                )

    def test_public_schema_and_duplicate_json_refusal(self):
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
            {"$ref": INTENT_INPUTS_SCHEMA}, registry=registry
        )
        identity = self.fixture.request.predecessor_result_identities[
            self.index_position
        ]
        content = self.fixture.records[identity]
        validator.validate(json.loads(content))
        with self.assertRaises(ValidationError):
            validator.validate({**json.loads(content), "unknown": True})
        duplicate = b'{"schema":"unknown",' + content[1:]
        records = dict(self.fixture.records)
        records.pop(identity)
        records[record_identity(duplicate)] = duplicate
        identities = list(self.fixture.request.predecessor_result_identities)
        identities[self.index_position] = record_identity(duplicate)
        with self.assertRaises(ActionWireError):
            self.execute(
                request=replace(
                    self.fixture.request,
                    predecessor_result_identities=tuple(identities),
                ),
                records=records,
            )

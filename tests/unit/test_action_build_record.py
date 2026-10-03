"""BUILD supervisor admission binds child bytes to exact source and plan authority."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
    validate_build_provider_receipts,
)
from literate_ai.adapters.action_build_source import materialize_build_source
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from tests.unit import test_action_build_source as source_fixture
from tests.unit.test_action_build_intent import provider_evidence
from tests.unit.test_component_node_generation_preparation import _fixture


def build_worker_input(fixture):
    _, execution = _fixture()
    return BuildWorkerInput(
        execution.identity,
        fixture.candidate.component_generation_plan_identity,
        fixture.candidate,
        fixture.plan,
        fixture.inputs,
        fixture.files,
        fixture.validation,
        fixture.custody.source_generation_identity,
        fixture.custody.identity,
        execution.generation_plans[0],
        execution,
    )


class ActionBuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionBuildSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.record = build_worker_input(self.fixture)

    def admit(self, content=None, **changes):
        content = self.record.to_bytes() if content is None else content
        arguments = dict(
            identity=record_identity(content),
            deadline=self.fixture.deadline,
            now=datetime.now(UTC),
        )
        arguments.update(changes)
        return BuildWorkerInput.admit(content, **arguments)

    def test_provider_receipts_require_exact_unique_canonical_exports(self):
        receipts = tuple(
            sorted(
                (provider_evidence(canonical_identity(name)) for name in ("a", "b")),
                key=lambda item: item.component_revision.uri,
            )
        )
        providers = tuple(
            sorted(
                (item for receipt in receipts for item in receipt.build.exports),
                key=lambda item: item.identity.uri,
            )
        )
        validate_build_provider_receipts(providers, receipts)
        for changed in ((), receipts[:1], receipts[::-1], receipts + receipts[:1]):
            with self.subTest(changed=changed), self.assertRaises(ActionWireError):
                validate_build_provider_receipts(providers, changed)
        with self.assertRaises(ActionWireError):
            validate_build_provider_receipts(providers[:-1], receipts)
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, accepted_providers=receipts).to_bytes())

    def test_provider_descriptors_cannot_be_added_without_accepted_receipts(self):
        document = json.loads(self.record.to_bytes())
        document["provider_builds"] = [
            {
                "schema": "literate-ai/provider-build-transfer@1",
                "receipt_identity": canonical_identity("foreign").uri,
                "artifact_archive": self.fixture.files[0].blob.to_dict(),
                "evidence_records": [self.fixture.files[0].blob.to_dict()],
            }
        ]
        with self.assertRaises(ActionWireError):
            self.admit(canonical_json_bytes(document))

    def test_admitted_record_reconstructs_exact_worker_source_custody(self):
        admitted = self.admit()
        self.assertEqual(admitted, self.record)
        with materialize_build_source(
            plan=admitted.plan,
            inputs=admitted.inputs,
            candidate=admitted.candidate,
            files=admitted.files,
            validation_inputs=admitted.source_validation,
            source_generation_identity=admitted.source_generation_identity,
            source_custody_identity=admitted.source_custody_identity,
            deadline=self.fixture.deadline,
            cas=self.fixture.cas,
            workspace_root=self.fixture.workspace,
            blob_source=self.fixture.controller_cas.get_bytes,
        ) as registry:
            self.assertEqual(
                registry.evidence(admitted.candidate.tree_identity),
                self.fixture.custody,
            )
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_changed_candidate_generation_contract_or_source_manifest_refuses(self):
        document = json.loads(self.record.to_bytes())
        for field, value in (
            ("generation_plan_identity", canonical_identity("foreign").uri),
            ("files", list(reversed(document["files"]))),
            ("files", document["files"][:-1]),
            ("dependency_resolution", "npm"),
        ):
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(canonical_json_bytes({**document, field: value}))
        candidate = replace(
            self.record.candidate, source_bundle_identity=canonical_identity("other")
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, candidate=candidate).to_bytes())

    def test_generation_plan_bytes_must_match_candidate_authority(self):
        changed = replace(
            self.record.generation_plan,
            component_graph_identity=canonical_identity("another-graph"),
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, generation_plan=changed).to_bytes())
        # Even a self-consistent replacement plan cannot replace candidate authority.
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.record,
                    generation_plan=changed,
                    generation_plan_identity=changed.identity,
                ).to_bytes()
            )
        candidate = replace(
            self.record.candidate,
            generation_key_identity=canonical_identity("wrong-key"),
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, candidate=candidate).to_bytes())
        document = json.loads(self.record.to_bytes())
        del document["generation_plan"]
        with self.assertRaises(ActionWireError):
            self.admit(canonical_json_bytes(document))

    def test_execution_plan_must_match_identity_and_generation_membership(self):
        changed = replace(
            self.record.execution_plan,
            planner_identity=canonical_identity("different-planner"),
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, execution_plan=changed).to_bytes())
        changed_generation = replace(
            self.record.generation_plan,
            generation_key=replace(
                self.record.generation_plan.generation_key,
                model_identity=canonical_identity("different-model"),
            ),
        )
        changed = replace(
            self.record.execution_plan,
            generation_plans=tuple(
                changed_generation if item == self.record.generation_plan else item
                for item in self.record.execution_plan.generation_plans
            ),
        )
        # A valid execution plan with its own matching hash still must contain
        # the original candidate's exact generation plan.
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.record,
                    execution_plan=changed,
                    execution_plan_identity=changed.identity,
                ).to_bytes()
            )
        document = json.loads(self.record.to_bytes())
        del document["execution_plan"]
        with self.assertRaises(ActionWireError):
            self.admit(canonical_json_bytes(document))

    def test_revoked_expired_or_substituted_plan_refuses(self):
        authorization = self.record.inputs.authorization
        revoked = replace(
            authorization, grant=replace(authorization.grant, revoked=True)
        )
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.record,
                    inputs=replace(self.record.inputs, authorization=revoked),
                ).to_bytes()
            )
        with self.assertRaises(ActionWireError):
            self.admit(now=authorization.grant.expires_at)
        changed = replace(
            self.record.inputs,
            authorization=replace(
                authorization,
                grant=replace(authorization.grant, authorization_id="another-grant"),
            ),
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.record, plan=changed.finalize()).to_bytes())

    def test_closed_bounded_json_and_exact_record_identity(self):
        content = self.record.to_bytes()
        document = json.loads(content)
        for value in (
            b"[]",
            b"not-json",
            b'{"schema":1,"schema":2}',
            b"x" * (17 * 1024 * 1024),
            canonical_json_bytes({**document, "launcher": "/incoming/tool"}),
            canonical_json_bytes({**document, "files": {}}),
        ):
            with self.subTest(size=len(value)), self.assertRaises(ActionWireError):
                self.admit(value)
        with self.assertRaises(ActionWireError):
            self.admit(identity=canonical_identity("wrong"))

    def test_changed_source_validation_still_requires_real_custody(self):
        altered = replace(
            self.record, source_custody_identity=canonical_identity("other")
        )
        admitted = self.admit(altered.to_bytes())
        # The enclosing dispatch must bind the record identity. Decoding does not
        # pretend to have verified the content of source blobs not yet fetched.
        with self.assertRaises(ActionWireError):
            with self.fixture.scope(
                source_custody_identity=admitted.source_custody_identity
            ):
                self.fail("substituted source custody was accepted")
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

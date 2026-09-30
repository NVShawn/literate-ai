"""Wire, schema, and tamper tests for Standard post-source lifecycle boundaries."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardProjectLifecycleError,
    StandardSourceCacheMembership,
)
from literate_ai.contracts import (
    SourceGenerationResumeCandidate,
    StandardBuildAuthorizationDocument,
    StandardComponentBuildIntentDocument,
    StandardComponentBuildPlanDocument,
    StandardSourceCacheMembershipDocument,
)
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_generation_scheduling import (
    _names,
    _prepared_execution,
)
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_standard_project_lifecycle import (
    LifecyclePorts,
    _identity,
    _prepared_nodes,
)


class StandardLifecycleWireContractTests(unittest.TestCase):
    def test_sdk_input_identities_are_optional_canonical_and_composite_bound(self):
        from dataclasses import replace

        from literate_ai.contracts import ArtifactMaterializationPlan

        sdk = _identity("sdk-input")
        intent = replace(self.intent, native_sdk_input_identities=(sdk,))
        self.assertNotIn("native_sdk_input_identities", self.intent.to_dict())
        self.assertNotIn(
            "native_sdk_input_identities", self.plan.materialization.to_dict()
        )
        self.assertNotEqual(intent.identity, self.intent.identity)
        self.schemas.validate(intent.to_dict()["schema"], intent.to_dict())
        self.assertEqual(
            StandardComponentBuildIntent.from_dict(intent.to_dict()), intent
        )
        materialization = replace(
            self.plan.materialization, native_sdk_input_identities=(sdk,)
        )
        self.schemas.validate(materialization.SCHEMA, materialization.to_dict())
        self.assertEqual(
            ArtifactMaterializationPlan.from_dict(materialization.to_dict()),
            materialization,
        )
        with self.assertRaises(ValueError):
            replace(self.plan, materialization=materialization)
        for invalid in (
            (sdk, sdk),
            ("sdk",),
            [sdk],
            tuple(
                sorted(
                    (sdk, _identity("another")), key=lambda item: item.uri, reverse=True
                )
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(
                    (TypeError, ValueError, StandardProjectLifecycleError)
                ):
                    replace(self.intent, native_sdk_input_identities=invalid)
                with self.assertRaises(
                    (TypeError, ValueError, StandardProjectLifecycleError)
                ):
                    replace(
                        self.intent.to_document(), native_sdk_input_identities=invalid
                    )
                with self.assertRaises(
                    (TypeError, ValueError, StandardProjectLifecycleError)
                ):
                    replace(
                        self.plan.materialization, native_sdk_input_identities=invalid
                    )
        wire = materialization.to_dict()
        wire["native_sdk_input_identities"] *= 2
        with self.assertRaises(ValueError):
            ArtifactMaterializationPlan.from_dict(wire)
        with self.assertRaises(AssertionError):
            self.schemas.validate(materialization.SCHEMA, wire)

    def setUp(self) -> None:
        lock = _diamond_lock()
        self.execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(self.execution, requests)
        self.names = _names(lock)
        self.ports = LifecyclePorts(self.execution, self.names)
        generation_plan = next(
            item
            for item in self.execution.generation_plans
            if self.names[item.component_revision.uri] == "money"
        )
        output = self.ports(nodes[generation_plan.component_revision.uri])
        source_candidate = output.candidate
        source = source_candidate.tree_identity
        self.intent = self.ports.create(
            self.execution, generation_plan, source_candidate, (), ()
        )
        index = self.ports.index(generation_plan.component_revision, source)
        self.authorization = self.ports.authorize(self.intent, index)
        self.plan = self.ports.finalize(self.intent, self.authorization)
        self.membership = StandardSourceCacheMembership(
            generation_plan.component_revision,
            generation_plan.generation_key.identity,
            SourceGenerationResumeCandidate(
                output,
                output.identity,
                nodes[
                    generation_plan.component_revision.uri
                ].request.request.budget.identity,
                nodes[
                    generation_plan.component_revision.uri
                ].request.request.complexity_decision_identity,
            ),
            _identity("accept-money"),
        )
        self.schemas = SchemaCatalog()

    def test_documents_round_trip_and_validate_against_public_schemas(self) -> None:
        cases = (
            (
                self.membership.to_document(),
                StandardSourceCacheMembershipDocument,
                StandardSourceCacheMembership,
            ),
            (
                self.intent.to_document(),
                StandardComponentBuildIntentDocument,
                StandardComponentBuildIntent,
            ),
            (
                self.authorization.to_document(),
                StandardBuildAuthorizationDocument,
                StandardBuildAuthorization,
            ),
            (
                self.plan.to_document(),
                StandardComponentBuildPlanDocument,
                StandardComponentBuildPlan,
            ),
        )
        for document, document_type, application_type in cases:
            with self.subTest(document=document_type.__name__):
                wire = document.to_dict()
                self.schemas.validate(document.SCHEMA, wire)
                decoded = document_type.from_dict(wire)
                self.assertEqual(decoded, document)
                self.assertEqual(decoded.identity, document.identity)
                application = application_type.from_dict(wire)
                self.assertEqual(application.to_dict(), wire)
                self.assertEqual(application.identity, document.identity)

    def test_unknown_fields_and_schema_versions_fail_closed(self) -> None:
        for document, document_type in (
            (
                self.membership.to_document(),
                StandardSourceCacheMembershipDocument,
            ),
            (self.intent.to_document(), StandardComponentBuildIntentDocument),
            (
                self.authorization.to_document(),
                StandardBuildAuthorizationDocument,
            ),
            (self.plan.to_document(), StandardComponentBuildPlanDocument),
        ):
            with self.subTest(document=document_type.__name__):
                wire = document.to_dict()
                with self.assertRaisesRegex(ValueError, "unknown fields"):
                    document_type.from_dict({**wire, "untrusted": True})
                with self.assertRaisesRegex(AssertionError, "unknown field"):
                    self.schemas.validate(document.SCHEMA, {**wire, "untrusted": True})
                with self.assertRaisesRegex(ValueError, "schema"):
                    document_type.from_dict({**wire, "schema": "unknown@99"})

    def test_intent_rejects_source_substitution(self) -> None:
        self.assertNotEqual(
            self.intent.source_tree_identity,
            self.intent.source_bundle_identity,
        )
        wire = copy.deepcopy(self.intent.to_dict())
        wire["build_request"]["source_bundle_digest"] = _identity(
            "substituted-source"
        ).uri

        with self.assertRaisesRegex(
            ValueError, "exact Component revision and source bundle"
        ):
            StandardComponentBuildIntentDocument.from_dict(wire)

    def test_membership_rejects_generation_key_substitution(self) -> None:
        wire = copy.deepcopy(self.membership.to_dict())
        wire["generation_key_identity"] = _identity("another-key").to_dict()

        with self.assertRaisesRegex(ValueError, "exact Component revision"):
            StandardSourceCacheMembershipDocument.from_dict(wire)

    def test_authorization_rejects_grant_for_another_request(self) -> None:
        wire = copy.deepcopy(self.authorization.to_dict())
        wire["grant"]["request_digest"] = _identity("substituted-request").uri

        with self.assertRaisesRegex(ValueError, "exact build request identity"):
            StandardBuildAuthorizationDocument.from_dict(wire)

    def test_plan_rejects_nested_identity_substitution(self) -> None:
        tampered_request = replace(
            self.plan.request,
            materialization_plan_identity=_identity("substituted-materialization"),
        )
        wire = self.plan.to_dict()
        wire["request"] = tampered_request.to_dict()

        with self.assertRaisesRegex(ValueError, "same exact authorities"):
            StandardComponentBuildPlanDocument.from_dict(wire)


if __name__ == "__main__":
    unittest.main()

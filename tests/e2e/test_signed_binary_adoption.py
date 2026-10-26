"""Signed test fixtures exercise adoption without inventing an old BUILD result."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.adapters.signed_binary_adoption import SignedReleaseBinaryAdopter
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectAction
from literate_ai.contracts.semantic_payload import (
    KNOWN_ORIGINAL_SOURCE_FIELDS,
    BinaryInputAdoptionAction,
    KnownOriginalBinaryFacts,
    NativePayloadQualificationInputs,
    PayloadAdmissionPolicy,
    SignedReleaseBinaryAdoptionRequest,
)
from tests.e2e import test_semantic_payload_readmission as fixture_module


class SignedBinaryAdoptionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.SemanticReadmissionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        source_action = next(
            action for action in fixture.plan.profile.actions if action.stage == "build"
        )
        adoption_action = BinaryInputAdoptionAction(
            source_action.action_id,
            RetainedProjectAction(
                "adopt",
                "acquire",
                source_action.tool,
                (),
                (),
                source_action.inventory,
                source_action.outputs,
                source_action.timeout_seconds,
            ),
        )
        unknown = tuple(
            sorted(
                (
                    "original-command",
                    "original-build-environment",
                    "original-compiler",
                    "original-toolchain",
                )
            )
        )
        fixture.policy = replace(
            fixture.policy,
            allowed_modes=("signed-release-binary-adoption",),
            allowed_unknown_original_inputs=unknown,
            binary_input_actions=(adoption_action,),
        )
        qualification = fixture.qualification.to_dict()
        qualification["profile_identity"] = fixture.plan.profile.identity.uri
        fixture.qualification = NativePayloadQualificationInputs.from_dict(
            qualification
        )
        self.facts = KnownOriginalBinaryFacts.from_dict(
            {
                "schema": "literate-ai/signed-binary-known-producer-facts@1",
                "known_source_inputs": {
                    field: fixture.inputs.to_dict()[field]
                    for field in KNOWN_ORIGINAL_SOURCE_FIELDS
                },
                "known_command": None,
                "known_environment": None,
                "known_tools": None,
                "unknown_original_inputs": list(unknown),
            }
        )
        release = fixture.put_json(
            {"schema": "fixture/signed-release-descriptor", "tag": "fixture-1.3.1"}
        )
        catalogue = fixture.put_json(
            {
                "schema": "fixture/signed-catalogue-descriptor",
                "identity": canonical_identity("fixture-native-catalogue").uri,
            }
        )
        fixture.request = SignedReleaseBinaryAdoptionRequest(
            canonical_identity("current-adoption-request"),
            fixture.plan,
            "adopt",
            "build",
            fixture.policy.identity,
            fixture.put_json(fixture.inputs.to_dict()),
            fixture.put_json(qualification),
            fixture.payload,
            release,
            catalogue,
            fixture.put_json(self.facts.to_dict()),
            unknown,
        )
        fixture.configure_graphs(
            prior_subject=release,
            common={
                "payload-descriptor": fixture.put_json(fixture.payload.to_dict()),
                "signed-release": release,
                "signed-catalogue": catalogue,
                "known-producer-facts": fixture.request.known_producer_facts,
            },
            prior_checks={
                name: b"fixture signed-release validation"
                for name in (
                    "release-signatures",
                    "catalogue-signature",
                    "package-checksum",
                    "known-producer-facts",
                )
            },
            current_extra_inputs={
                "current-build-inputs": fixture.request.current_build_inputs
            },
            current_extra_checks=(
                "catalogue-signature",
                "package-checksum",
                "known-source-unchanged",
            ),
        )
        self.adopter = SignedReleaseBinaryAdopter(
            fixture.authority, fixture.cas, fixture.admission, fixture.evidence
        )

    def test_policy_action_bytes_are_in_the_identity_and_roundtrip(self):
        policy = self.fixture.policy
        self.assertEqual(PayloadAdmissionPolicy.from_dict(policy.to_dict()), policy)
        original = policy.binary_input_actions[0]
        changed = replace(
            original, action=replace(original.action, argv=("different",))
        )
        self.assertNotEqual(
            replace(policy, binary_input_actions=(changed,)).identity, policy.identity
        )

    def test_adoption_preserves_unknowns_and_never_claims_a_build_key_hit(self):
        fixture = self.fixture
        result = self.adopter.adopt(fixture.request, fixture.grant)
        receipt = json.loads(fixture.cas.get_bytes(result.receipt))
        self.assertEqual(
            receipt["receipt_kind"], "qualified-signed-release-binary-input"
        )
        self.assertFalse(receipt["build_key_cache_hit"])
        self.assertFalse(receipt["source_build_equivalence"])
        self.assertFalse(receipt["compilation_repeated"])
        self.assertEqual(result.payload, fixture.payload)
        self.assertEqual(
            result.unknown_original_inputs, fixture.request.unknown_original_inputs
        )
        self.assertNotIn("prior_result", fixture.request.to_dict())
        self.assertEqual(fixture.cas.get_bytes(fixture.prior), fixture.old_bytes)

    def test_policy_must_explicitly_allow_unknown_original_compiler(self):
        fixture = self.fixture
        policy = replace(fixture.policy, allowed_unknown_original_inputs=())
        fixture.authority.policy.return_value = policy
        request = replace(fixture.request, admission_policy_identity=policy.identity)
        with self.assertRaisesRegex(ValueError, "unknown_original_provenance"):
            self.adopter.adopt(request, fixture.grant)
        self.assertEqual(fixture.spent, set())

    def test_known_source_options_or_compiler_change_refuses_despite_unknowns(self):
        fixture = self.fixture
        facts = self.facts.to_dict()
        facts["known_source_inputs"]["options"] = {"changed": True}
        request = replace(fixture.request, known_producer_facts=fixture.put_json(facts))
        with self.assertRaisesRegex(ValueError, "known_source_or_build_input_changed"):
            self.adopter.adopt(request, fixture.grant)
        facts = self.facts.to_dict()
        facts["known_tools"] = [
            {
                "name": "tool",
                "path": "sdk/tool",
                "identity": canonical_identity("different compiler").uri,
            }
        ]
        request = replace(fixture.request, known_producer_facts=fixture.put_json(facts))
        with self.assertRaisesRegex(ValueError, "known_tool_or_compiler_changed"):
            self.adopter.adopt(request, fixture.grant)
        self.assertEqual(fixture.spent, set())

    def test_missing_original_catalogue_signature_check_refuses(self):
        fixture = self.fixture
        plan = fixture.evidence.prior
        root = next(item for item in plan.requirements if item.envelope == plan.root)
        changed = replace(
            root,
            artifacts=tuple(
                item
                for item in root.artifacts
                if item[0] != "check/catalogue-signature"
            ),
        )
        self.adopter.evidence = replace(
            fixture.evidence,
            prior=replace(
                plan,
                requirements=tuple(
                    changed if item == root else item for item in plan.requirements
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "qualification_incomplete"):
            self.adopter.adopt(fixture.request, fixture.grant)
        self.assertEqual(fixture.spent, set())

    def test_graph_header_unknowns_cannot_be_relabelled_as_compiler_unknowns(self):
        facts = self.facts.to_dict()
        facts["unknown_original_inputs"] = ["missing-header-trace"]
        with self.assertRaisesRegex(ValueError, "header/graph"):
            KnownOriginalBinaryFacts.from_dict(facts)


if __name__ == "__main__":
    unittest.main()

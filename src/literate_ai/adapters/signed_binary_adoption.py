"""Policy-controlled previous signed release binary INPUT adoption.

This path deliberately requires no old canonical BuildWorkerResult. It does not
claim unchanged compilation, complete old compiler provenance, or source-build
equivalence. Known changed source/build facts and current graph gaps refuse it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from literate_ai.adapters.semantic_payload_readmission import (
    _REQUIRED_CHECKS,
    SemanticPayloadReadmitter,
    _reference,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.semantic_payload import (
    KNOWN_ORIGINAL_SOURCE_FIELDS,
    KnownOriginalBinaryFacts,
    NativePayloadQualificationInputs,
    SemanticBuildInputs,
    SignedReleaseBinaryAdoptionRequest,
)


@dataclass(frozen=True, slots=True)
class AdoptedSignedReleaseBinary:
    receipt: BlobRef
    payload: BlobRef
    current_request: ContentIdentity
    unknown_original_inputs: tuple[str, ...]


class SignedReleaseBinaryAdopter(SemanticPayloadReadmitter):
    """Freshly authenticate old signed assets and native current compatibility."""

    def adopt(self, request: SignedReleaseBinaryAdoptionRequest, grant):
        if not isinstance(request, SignedReleaseBinaryAdoptionRequest):
            raise TypeError(
                "signed binary input adoption requires its distinct typed request"
            )
        current = self._current(request)
        policy = self.authority.policy()
        if (
            policy.identity != request.admission_policy_identity
            or "signed-release-binary-adoption" not in policy.allowed_modes
        ):
            raise ValueError("binary_adoption.current_policy_forbids_adoption")
        binding = next(
            (
                item
                for item in policy.binary_input_actions
                if item.action.action_id == request.action_id
            ),
            None,
        )
        source_action = next(
            (
                item
                for item in current.profile.actions
                if item.action_id == request.source_action_id
            ),
            None,
        )
        if (
            binding is None
            or source_action is None
            or binding.source_action_id != request.source_action_id
            or tuple(sorted(binding.action.outputs))
            != tuple(sorted(source_action.outputs))
            or binding.action.tool not in {tool[0] for tool in current.profile.tools}
        ):
            raise ValueError(
                "binary_adoption.alternate_action_or_output_ownership_differs"
            )
        if policy.evidence_trust_policy_identity != self.evidence.policy.identity:
            raise ValueError("binary_adoption.trust_policy_differs")
        for plan, allowed in (
            (self.evidence.prior, policy.provenance_workflows),
            (self.evidence.current, policy.qualification_workflows),
        ):
            if any(
                item.expectation.workflow not in allowed for item in plan.requirements
            ):
                raise ValueError("binary_adoption.native_workflow_differs")
        if request.payload.size > policy.maximum_payload_bytes:
            raise ValueError("binary_adoption.payload_exceeds_policy_bound")
        inputs = SemanticBuildInputs(self._content(request.current_build_inputs))
        descriptor = inputs.to_dict()
        if (
            descriptor["payload_scope"] != "userland"
            or descriptor["target"] != current.profile.target
        ):
            raise ValueError("binary_adoption.native_target_or_kernel_rebuild_required")
        members = {item.path: item for item in self.authority.manifest.members}
        if any(
            members.get(item["path"]) is None or members[item["path"]].to_dict() != item
            for item in descriptor["members"]
        ):
            raise ValueError("binary_adoption.current_source_member_differs")
        facts = KnownOriginalBinaryFacts(
            self._content(request.known_producer_facts)
        ).to_dict()
        unknown = tuple(facts["unknown_original_inputs"])
        if unknown != request.unknown_original_inputs or not set(unknown) <= set(
            policy.allowed_unknown_original_inputs
        ):
            raise ValueError(
                "binary_adoption.unknown_original_provenance_not_authorized"
            )
        if facts["known_source_inputs"] != {
            field: descriptor[field] for field in KNOWN_ORIGINAL_SOURCE_FIELDS
        }:
            raise ValueError("binary_adoption.known_source_or_build_input_changed")
        for field, expected in (
            ("known_command", descriptor["command"]),
            ("known_environment", descriptor["environment"]),
        ):
            if facts[field] is not None and facts[field] != expected:
                raise ValueError("binary_adoption.known_command_or_environment_changed")
        tools = facts["known_tools"]
        if tools is not None and any(tool not in descriptor["tools"] for tool in tools):
            raise ValueError("binary_adoption.known_tool_or_compiler_changed")
        if not tools or not any(
            tool["identity"] == descriptor["compiler"] for tool in tools
        ):
            if "original-compiler" not in unknown:
                raise ValueError("binary_adoption.original_compiler_fact_missing")
        expected_tools = [
            tool
            for tool in descriptor["tools"]
            if tool["identity"] != descriptor["compiler"]
            or "original-compiler" not in unknown
        ]
        if "original-toolchain" not in unknown and tools != expected_tools:
            raise ValueError("binary_adoption.original_tool_closure_missing")
        qualification = NativePayloadQualificationInputs(
            self._content(request.qualification_inputs)
        ).to_dict()
        if (
            qualification["profile_identity"] != current.profile.identity.uri
            or qualification["test_policy_identity"]
            != current.profile.test_policy_identity.uri
            or qualification["target"] != current.profile.target
            or current.profile.acceptance_policy_identity is None
            or qualification["application_policy_identity"]
            != current.profile.acceptance_policy_identity.uri
        ):
            raise ValueError("binary_adoption.current_qualification_scope_differs")
        self._content(request.signed_release)
        self._content(request.signed_catalogue)
        self.cas.verify(request.payload)
        common = {
            "payload-descriptor": _reference(request.payload.to_dict()),
            "signed-release": request.signed_release,
            "signed-catalogue": request.signed_catalogue,
            "known-producer-facts": request.known_producer_facts,
        }
        evidence = self.evidence
        self._proof_shape(
            evidence.prior,
            subject=request.signed_release,
            target=None,
            checks={
                "release-signatures",
                "catalogue-signature",
                "package-checksum",
                "known-producer-facts",
            },
            inputs=common,
        )
        if evidence.current.project_authority != current.project_identity:
            raise ValueError("binary_adoption.qualification_selects_other_project")
        native_target = canonical_identity(
            {
                "target": current.profile.target,
                "worker_identity": current.profile.worker_identity.uri,
            }
        )
        leaves = self._proof_shape(
            evidence.current,
            subject=request.reference,
            target=native_target,
            checks=_REQUIRED_CHECKS
            | {"catalogue-signature", "package-checksum", "known-source-unchanged"},
            inputs={
                **common,
                "current-build-inputs": request.current_build_inputs,
                "qualification-inputs": request.qualification_inputs,
                "current-selection": request.reference,
            },
        )
        authorization = request.authorization_request(inputs)
        now = self.admission.clock()
        grant.require_valid(authorization, now=now)
        self.admission.verifier.require_build_valid(grant, authorization, now=now)
        if grant.privileges != authorization.requested_privileges:
            raise ValueError("binary_adoption.grant_privileges_differ")
        prior_checked, current_checked = (
            self._verify(evidence.prior),
            self._verify(evidence.current),
        )
        closure_object = next(
            (
                item
                for item in current_checked.objects
                if item.reference == leaves["check/producer-closure"]
            ),
            None,
        )
        if closure_object is None:
            raise ValueError("binary_adoption.current_producer_closure_unavailable")
        closure = json.loads(closure_object.content)
        expected = {
            "schema": "literate-ai/native-semantic-input-closure@1",
            "current_plan_identity": current.identity.uri,
            "manifest_identity": current.manifest_identity.uri,
            "profile_identity": current.profile.identity.uri,
            "semantic_inputs_identity": inputs.identity.uri,
            "unknown_edges": [],
        }
        if (
            closure != expected
            or canonical_json_bytes(closure) != closure_object.content
        ):
            raise ValueError("binary_adoption.current_graph_gap_or_closure_changed")
        self._current(request)
        if self.authority.policy() != policy:
            raise ValueError("binary_adoption.current_policy_changed")
        self.cas.verify(request.payload)
        prior_custody = self._retain_verified_graph(prior_checked, evidence.prior)
        current_custody = self._retain_verified_graph(current_checked, evidence.current)
        self.admission.admit(grant, authorization)
        record = {
            "schema": "literate-ai/signed-release-binary-input-adoption@1",
            "request": request.to_dict(),
            "authorization_identity": canonical_identity(grant.to_dict()).uri,
            "prior_verification_identity": prior_checked.identity.uri,
            "current_verification_identity": current_checked.identity.uri,
            "prior_evidence_custody": prior_custody,
            "current_evidence_custody": current_custody,
            "receipt_kind": "qualified-signed-release-binary-input",
            "compilation_repeated": False,
            "build_key_cache_hit": False,
            "source_build_equivalence": False,
            "unknown_original_inputs": list(unknown),
            "admitted_utc": self.admission.clock().isoformat(),
        }
        self._current(request)
        now = self.admission.clock()
        grant.require_valid(authorization, now=now)
        self.admission.verifier.require_build_valid(grant, authorization, now=now)
        if self.authority.policy() != policy:
            raise ValueError("binary_adoption.current_policy_changed")
        self.cas.verify(request.payload)
        receipt = self.cas.put_bytes(
            canonical_json_bytes(record), media_type="application/json"
        )
        return AdoptedSignedReleaseBinary(
            receipt, request.payload, request.request_identity, unknown
        )

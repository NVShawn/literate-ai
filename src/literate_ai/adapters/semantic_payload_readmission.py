"""Explicit payload re-admission under current retained authority and signed proof.

Old build records remain immutable provenance. This adapter never emits a new
BUILD result, executes a compiler, signs a package, or promotes a release receipt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.adapters.retained_project_planning import (
    plan_retained_project_with_graph,
)
from literate_ai.application.evidence_verification import verify_run_evidence_graph
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile
from literate_ai.contracts.semantic_payload import (
    NativePayloadQualificationInputs,
    PayloadAdmissionPolicy,
    SemanticBuildInputs,
    SemanticPayloadReadmissionRequest,
)
from literate_ai.contracts.standard_post_source_evidence import StandardBuildEvidence
from literate_ai.security.build_admission import SingleUseBuildAdmission
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.records import DerivationRun, PlatformRun
from literate_ai.storage import FileSystemCAS

_REQUIRED_CHECKS = frozenset(
    {
        "producer-closure",
        "path-contract",
        "base-abi",
        "runtime-abi",
        "package-constraints",
        "package-scripts",
        "native-install",
        "native-executable",
        "application",
    }
)


def _reference(value):
    content = canonical_json_bytes(value)
    identity = canonical_identity(value)
    return BlobRef(identity.digest, len(content), media_type="application/json")


class CurrentRetainedReadmissionAuthority:
    """Reopen the existing real retained planner, including ownership and bytes.

    The caller supplies reviewed project/binding/manifest owners, not a serialized
    `complete` flag or a Boolean admission callback. No native work is launched.
    """

    def __init__(self, project, binding, manifest, profile_path: Path):
        self.project, self.binding, self.manifest = project, binding, manifest
        self.profile_path = Path(profile_path)

    def policy(self):
        members = [
            member
            for member in self.manifest.members
            if member.role == "payload-admission-policy"
        ]
        if len(members) != 1:
            raise ValueError("semantic_payload.exact_current_policy_required")
        return PayloadAdmissionPolicy.from_dict(
            read_retained_project_document(self.project.root, members[0])
        )

    def plan(self):
        content = _read_regular_file(
            self.profile_path,
            maximum_bytes=16 * 1024 * 1024,
            code="semantic_payload.profile_invalid",
        )
        profile = RetainedProjectProfile.from_dict(json.loads(content))
        result = plan_retained_project_with_graph(
            self.project,
            self.binding,
            self.manifest,
            profile,
            worker_identity=profile.worker_identity,
            target=profile.target,
            profile_path=self.profile_path,
            profile_bytes=content,
        )
        if (
            not result.owned_components
            or not result.owned_inputs
            or result.ownership_catalog_identity is None
        ):
            raise ValueError("semantic_payload.current_registered_ownership_required")
        return result.plan


@dataclass(frozen=True, slots=True)
class SemanticPayloadEvidenceAuthority:
    prior: EvidenceVerificationPlan
    current: EvidenceVerificationPlan
    policy: object
    resolver: object
    locators: tuple
    current_state: object


@dataclass(frozen=True, slots=True)
class ReadmittedSemanticPayload:
    receipt: BlobRef
    payload: BlobRef
    current_request: ContentIdentity


class SemanticPayloadReadmitter:
    """Authenticate retained provenance and fresh native qualification separately."""

    def __init__(
        self,
        authority: CurrentRetainedReadmissionAuthority,
        cas: FileSystemCAS,
        admission: SingleUseBuildAdmission,
        evidence: SemanticPayloadEvidenceAuthority,
    ):
        if (
            not isinstance(authority, CurrentRetainedReadmissionAuthority)
            or not isinstance(cas, FileSystemCAS)
            or not isinstance(admission, SingleUseBuildAdmission)
            or not isinstance(evidence, SemanticPayloadEvidenceAuthority)
        ):
            raise TypeError(
                "semantic re-admission requires actual verifier-owned authority"
            )
        if not isinstance(evidence.prior, EvidenceVerificationPlan) or not isinstance(
            evidence.current, EvidenceVerificationPlan
        ):
            raise TypeError(
                "semantic re-admission requires independently pinned evidence plans"
            )
        self.authority, self.cas, self.admission, self.evidence = (
            authority,
            cas,
            admission,
            evidence,
        )

    def _current(self, request):
        plan = self.authority.plan()
        if plan != request.plan:
            raise ValueError("semantic_payload.current_plan_changed")
        return plan

    def _content(self, reference):
        if reference.size > 16 * 1024 * 1024:
            raise ValueError("semantic_payload.metadata_oversized")
        return self.cas.get_bytes(reference)

    def _proof_shape(self, plan, *, subject, target, checks, inputs):
        root = next(
            (item for item in plan.requirements if item.envelope == plan.root), None
        )
        if (
            root is None
            or root.expectation.predicate_type != PlatformRun.SCHEMA
            or root.expectation.subject != subject
            or target is not None
            and root.expectation.target != target
        ):
            raise ValueError("semantic_payload.native_proof_subject_or_target_differs")
        leaves = dict(root.artifacts)
        if not {"check/" + name for name in checks} <= set(leaves):
            raise ValueError("semantic_payload.native_qualification_incomplete")
        derivation = next(
            (
                item
                for item in plan.requirements
                if item.expectation.predicate_type == DerivationRun.SCHEMA
                and all(
                    dict(item.artifacts).get("input/" + name) == reference
                    for name, reference in inputs.items()
                )
            ),
            None,
        )
        if derivation is None:
            raise ValueError("semantic_payload.native_proof_inputs_differ")
        return leaves

    def _verify(self, plan):
        evidence = self.evidence
        references = {
            reference
            for requirement in plan.requirements
            for reference in (
                requirement.envelope,
                *(child.blob for child in requirement.children),
                *(reference for _, reference in requirement.artifacts),
            )
        }
        locators = tuple(
            locator for locator in evidence.locators if locator.subject in references
        )
        return verify_run_evidence_graph(
            plan.root,
            requirements=plan.requirements,
            resolver=evidence.resolver,
            locators=locators,
            policy=evidence.policy,
            current_state=evidence.current_state,
        )

    def _retain_verified_graph(self, checked, plan):
        for item in checked.objects:
            if (
                self.cas.put_bytes(item.content, media_type=item.reference.media_type)
                != item.reference
            ):
                raise ValueError("semantic_payload.verified_evidence_custody_differs")

        def retain(value):
            return self.cas.put_bytes(
                canonical_json_bytes(value), media_type="application/json"
            ).to_dict()

        return {
            "verification_identity": checked.identity.uri,
            "root": checked.root.to_dict(),
            "plan": retain(plan.to_dict()),
            "trust_policy": retain(self.evidence.policy.to_dict()),
            "initial_revocations": retain(checked.initial_state.revocations.to_dict()),
            "final_revocations": retain(checked.final_state.revocations.to_dict()),
            "objects": [item.reference.to_dict() for item in checked.objects],
        }

    def readmit(self, request: SemanticPayloadReadmissionRequest, grant):
        if not isinstance(request, SemanticPayloadReadmissionRequest):
            raise TypeError("semantic re-admission requires a typed current request")
        current = self._current(request)
        policy = self.authority.policy()
        if (
            policy.identity != request.admission_policy_identity
            or "semantic-key-reuse" not in policy.allowed_modes
        ):
            raise ValueError("semantic_payload.current_policy_forbids_reuse")
        if policy.evidence_trust_policy_identity != self.evidence.policy.identity:
            raise ValueError("semantic_payload.trust_policy_differs")
        for plan, allowed in (
            (self.evidence.prior, policy.provenance_workflows),
            (self.evidence.current, policy.qualification_workflows),
        ):
            if any(
                item.expectation.workflow not in allowed for item in plan.requirements
            ):
                raise ValueError("semantic_payload.native_workflow_differs")
        if request.payload.size > policy.maximum_payload_bytes:
            raise ValueError("semantic_payload.payload_exceeds_policy_bound")
        inputs = SemanticBuildInputs(self._content(request.semantic_inputs))
        descriptor = inputs.to_dict()
        action = next(
            (
                item
                for item in current.profile.actions
                if item.action_id == request.action_id
            ),
            None,
        )
        tool = (
            None
            if action is None
            else next(
                (item for item in current.profile.tools if item[0] == action.tool),
                None,
            )
        )
        if action is None or tool is None:
            raise ValueError("semantic_payload.action_or_tool_unknown")
        if descriptor["payload_scope"] != "userland":
            raise ValueError("semantic_payload.kernel_rebuild_required")
        if (
            descriptor["target"] != current.profile.target
            or descriptor["command"] != [tool[1], *action.argv]
            or descriptor["outputs"] != sorted(action.outputs)
        ):
            raise ValueError("semantic_payload.current_action_differs")
        members = {item.path: item for item in self.authority.manifest.members}
        if any(
            members.get(item["path"]) is None or members[item["path"]].to_dict() != item
            for item in descriptor["members"]
        ):
            raise ValueError("semantic_payload.current_member_differs")
        if not any(
            item["path"] == tool[1] and item["identity"] == tool[2].uri
            for item in descriptor["tools"]
        ):
            raise ValueError("semantic_payload.current_tool_differs")
        # Read and verify immutable prior products without treating an expired old
        # execution grant or old build result as a new execution authorization.
        prior_bytes = self._content(request.prior_result)
        value = json.loads(prior_bytes)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "input_identity",
                "evidence",
                "artifact_archive",
                "evidence_records",
            }
            or value["schema"] != "literate-ai/build-worker-result@1"
        ):
            raise ValueError("semantic_payload.canonical_prior_build_result_required")
        prior_result = BuildWorkerResult(
            ContentIdentity.parse_uri(value["input_identity"]),
            StandardBuildEvidence.from_dict(value["evidence"]),
            BlobRef.from_dict(value["artifact_archive"]),
            tuple(BlobRef.from_dict(item) for item in value["evidence_records"]),
        )
        prior_records = tuple(item.identity for item in prior_result.evidence_records)
        if (
            prior_result.to_bytes() != prior_bytes
            or prior_result.artifact_archive != request.payload
            or not prior_records
            or prior_records != tuple(sorted(set(prior_records)))
            or prior_result.evidence.identity.uri not in prior_records
        ):
            raise ValueError("semantic_payload.prior_result_payload_custody_differs")
        for reference in prior_result.evidence_records:
            self.cas.verify(reference)
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
            raise ValueError("semantic_payload.current_qualification_scope_differs")
        self.cas.verify(request.payload)
        native_target = canonical_identity(
            {
                "target": current.profile.target,
                "worker_identity": current.profile.worker_identity.uri,
            }
        )
        payload_descriptor = _reference(request.payload.to_dict())
        common = {
            "payload-descriptor": payload_descriptor,
            "prior-result": request.prior_result,
            "semantic-inputs": request.semantic_inputs,
        }
        evidence = self.evidence
        self._proof_shape(
            evidence.prior,
            subject=request.prior_result,
            target=None,
            checks={"provenance"},
            inputs=common,
        )
        if evidence.current.project_authority != current.project_identity:
            raise ValueError("semantic_payload.qualification_selects_other_project")
        leaves = self._proof_shape(
            evidence.current,
            subject=request.reference,
            target=native_target,
            checks=_REQUIRED_CHECKS,
            inputs={
                **common,
                "qualification-inputs": request.qualification_inputs,
                "current-selection": request.reference,
            },
        )
        authorization = request.authorization_request(inputs)
        now = self.admission.clock()
        grant.require_valid(authorization, now=now)
        self.admission.verifier.require_build_valid(grant, authorization, now=now)
        if grant.privileges != authorization.requested_privileges:
            raise ValueError("semantic_payload.grant_privileges_differ")
        prior_checked, current_checked = (
            self._verify(evidence.prior),
            self._verify(evidence.current),
        )
        # This is an authenticated producer check, not a caller's completeness
        # assertion. The trusted native workflow must emit actual closed traces.
        closure_reference = leaves["check/producer-closure"]
        closure_object = next(
            (
                item
                for item in current_checked.objects
                if item.reference == closure_reference
            ),
            None,
        )
        if closure_object is None:
            raise ValueError("semantic_payload.producer_closure_unavailable")
        closure = json.loads(closure_object.content)
        expected_closure = {
            "schema": "literate-ai/native-semantic-input-closure@1",
            "current_plan_identity": current.identity.uri,
            "manifest_identity": current.manifest_identity.uri,
            "profile_identity": current.profile.identity.uri,
            "semantic_inputs_identity": inputs.identity.uri,
            "unknown_edges": [],
        }
        if (
            canonical_json_bytes(closure) != closure_object.content
            or closure != expected_closure
        ):
            raise ValueError("semantic_payload.producer_closure_incomplete_or_changed")
        self._current(request)
        if self.authority.policy() != policy:
            raise ValueError("semantic_payload.current_policy_changed")
        self.cas.verify(request.payload)
        prior_custody = self._retain_verified_graph(prior_checked, evidence.prior)
        current_custody = self._retain_verified_graph(current_checked, evidence.current)
        # Spend a fresh issuer-authenticated grant only after both actual signed
        # graphs and current immutable bytes pass. It authorizes no compilation.
        self.admission.admit(grant, authorization)
        record = {
            "schema": "literate-ai/semantic-payload-readmission@1",
            "request": request.to_dict(),
            "authorization_identity": canonical_identity(grant.to_dict()).uri,
            "prior_verification_identity": prior_checked.identity.uri,
            "current_verification_identity": current_checked.identity.uri,
            "prior_evidence_custody": prior_custody,
            "current_evidence_custody": current_custody,
            "compilation_repeated": False,
            "build_result_rewritten": False,
            "receipt_kind": "payload-requalification",
            "admitted_utc": self.admission.clock().isoformat(),
        }
        self._current(request)
        now = self.admission.clock()
        grant.require_valid(authorization, now=now)
        self.admission.verifier.require_build_valid(grant, authorization, now=now)
        if self.authority.policy() != policy:
            raise ValueError("semantic_payload.current_policy_changed")
        self.cas.verify(request.payload)
        receipt = self.cas.put_bytes(
            canonical_json_bytes(record), media_type="application/json"
        )
        return ReadmittedSemanticPayload(
            receipt, request.payload, request.request_identity
        )

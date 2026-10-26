"""Cryptographic fixture qualification is not native product acceptance."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.semantic_payload_readmission import (
    CurrentRetainedReadmissionAuthority,
    SemanticPayloadEvidenceAuthority,
    SemanticPayloadReadmitter,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.retained_project import RetainedProjectMember
from literate_ai.contracts.semantic_payload import (
    NativePayloadQualificationInputs,
    PayloadAdmissionPolicy,
    SemanticBuildInputs,
    SemanticPayloadReadmissionRequest,
)
from literate_ai.security import BuildAuthorization, SecurityProfile
from literate_ai.security.build_admission import SingleUseBuildAdmission
from literate_ai.security.evidence import (
    DerivationRun,
    DsseError,
    Ed25519EvidenceSigner,
    EvidenceArtifact,
    EvidenceRunContext,
    EvidenceTrustError,
    PlatformRun,
)
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobIntegrityError
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture
from tests.support.fixtures_test_evidence_graph import _Graph
from tests.support.fixtures_test_standard_post_source_evidence import _evidence


class _Override:
    """Delegate to a real object while overriding named attributes."""

    def __init__(self, target, **overrides):
        self._target, self._overrides = target, overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._target, name)


def _without_profile_actions(current):
    return _Override(current, profile=_Override(current.profile, actions=()))


class SemanticReadmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cas = FileSystemCAS(temporary.name)
        plan, _ = retained_project_sbom_fixture()
        self.plan = replace(
            plan,
            profile=replace(
                plan.profile,
                acceptance_policy_identity=canonical_identity(
                    "fixture-application-policy"
                ),
            ),
        )
        self.member = RetainedProjectMember(
            "source.c", 1, 0o644, hashlib.sha256(b"c").hexdigest(), "implementation"
        )
        self.inputs = SemanticBuildInputs.from_dict(
            {
                "schema": "literate-ai/semantic-build-inputs@1",
                "payload_scope": "userland",
                "phase": "compile",
                "target": self.plan.profile.target,
                "compiler": self.plan.profile.tools[0][2].uri,
                "toolchain": self.plan.profile.toolchain_identity.uri,
                "sysroot": canonical_identity("headers").uri,
                "base_build_abi": canonical_identity("build-abi").uri,
                "path_contract": canonical_identity("exact-fixture-paths").uri,
                "command": ["sdk/tool"],
                "working_directory": "/fixture/exact-root",
                "environment": [],
                "members": [self.member.to_dict()],
                "member_owners": {"source.c": "fixture#compile"},
                "tools": [
                    {
                        "name": "tool",
                        "path": "sdk/tool",
                        "identity": self.plan.profile.tools[0][2].uri,
                    }
                ],
                "build_dependencies": [],
                "generated_inputs": [],
                "options": {},
                "flavor": None,
                "outputs": ["app"],
                "output_format": "fixture-binary",
            }
        )
        self.graph = _Graph()
        workflow = canonical_identity("workflow")
        self.policy = PayloadAdmissionPolicy(
            self.graph.policy.identity,
            (workflow,),
            (workflow,),
            ("semantic-key-reuse",),
            1024,
        )
        self.authority = CurrentRetainedReadmissionAuthority(
            None,
            None,
            SimpleNamespace(members=(self.member,)),
            "unused-fixture-profile",
        )
        self.addCleanup(patch.stopall)
        patch.object(self.authority, "plan", return_value=self.plan).start()
        patch.object(self.authority, "policy", return_value=self.policy).start()
        self.payload = self.cas.put_bytes(b"retained fixture payload")
        old_evidence = _evidence().build
        old_record = self.cas.put_bytes(canonical_json_bytes(old_evidence.to_dict()))
        self.prior = self.cas.put_bytes(
            BuildWorkerResult(
                canonical_identity("immutable-prior-input"),
                old_evidence,
                self.payload,
                (old_record,),
            ).to_bytes()
        )
        self.old_bytes = self.cas.get_bytes(self.prior)
        self.qualification = NativePayloadQualificationInputs.from_dict(
            {
                "schema": "literate-ai/native-payload-qualification-inputs@1",
                "profile_identity": self.plan.profile.identity.uri,
                "test_policy_identity": self.plan.profile.test_policy_identity.uri,
                "application_policy_identity": (
                    self.plan.profile.acceptance_policy_identity.uri
                ),
                "target": self.plan.profile.target,
                "base_version": "fixture-2",
                "base_artifact": self.payload.to_dict(),
                "base_runtime_abi": canonical_identity("compatible-runtime").uri,
                "runtime_dependencies": [],
                "package_scripts_identity": canonical_identity("scripts").uri,
            }
        )
        self.request = SemanticPayloadReadmissionRequest(
            canonical_identity("current-request"),
            self.plan,
            "build",
            self.policy.identity,
            self.put_json(self.inputs.to_dict()),
            self.put_json(self.qualification.to_dict()),
            self.payload,
            self.prior,
        )
        self.now = datetime.fromtimestamp(210, UTC)
        request = self.request.authorization_request(self.inputs)
        self.grant = BuildAuthorization(
            "fixture-grant",
            self.policy.identity.uri,
            canonical_identity(request.to_dict()).uri,
            self.plan.identity.uri,
            "fixture-issuer",
            "fixture admission test only",
            SecurityProfile.CONSTRAINED,
            request.requested_privileges,
            datetime.fromtimestamp(90, UTC),
            datetime.fromtimestamp(240, UTC),
        )
        self.spent = set()
        self.admission = SingleUseBuildAdmission(
            SimpleNamespace(require_build_valid=self.verify_grant),
            SimpleNamespace(consume=self.consume),
            clock=lambda: self.now,
        )
        self.configure_graphs()

    def put_json(self, value):
        return self.cas.put_bytes(
            canonical_json_bytes(value), media_type="application/json"
        )

    def verify_grant(self, grant, request, *, now):
        if grant != self.grant:
            raise ValueError("fixture issuer did not issue this grant")
        grant.require_valid(request, now=now)

    def consume(self, grant, request):
        if grant.authorization_id in self.spent:
            raise ValueError("fixture grant already consumed")
        self.spent.add(grant.authorization_id)

    def configure_graphs(
        self,
        *,
        prior_subject=None,
        common=None,
        prior_checks=None,
        current_extra_inputs=None,
        current_extra_checks=(),
    ):
        graph = self.graph
        workflow = canonical_identity("workflow")
        target = canonical_identity(
            {
                "target": self.plan.profile.target,
                "worker_identity": self.plan.profile.worker_identity.uri,
            }
        )
        prior_target = canonical_identity("original-different-worker")
        rule = replace(
            graph.policy.signers[0],
            targets=tuple(
                sorted(
                    {*graph.policy.signers[0].targets, target, prior_target},
                    key=lambda item: item.uri,
                )
            ),
        )
        graph.policy = replace(graph.policy, signers=(rule,))
        self.policy = replace(
            self.policy, evidence_trust_policy_identity=graph.policy.identity
        )
        self.authority.policy.return_value = self.policy
        self.request = replace(
            self.request, admission_policy_identity=self.policy.identity
        )
        request = self.request.authorization_request(self.inputs)
        self.grant = replace(
            self.grant,
            classification_digest=self.policy.identity.uri,
            request_digest=canonical_identity(request.to_dict()).uri,
            effective_revision_digest=self.plan.identity.uri,
            privileges=request.requested_privileges,
        )
        payload_descriptor = self.put_json(self.payload.to_dict())
        selection = self.put_json(self.request.to_dict())
        if common is None:
            common = {
                "payload-descriptor": payload_descriptor,
                "prior-result": self.prior,
                "semantic-inputs": self.request.semantic_inputs,
            }
        prior_subject = self.prior if prior_subject is None else prior_subject
        prior_checks = (
            {"provenance": b"fixture prior provenance"}
            if prior_checks is None
            else prior_checks
        )
        current_extra_inputs = (
            {} if current_extra_inputs is None else current_extra_inputs
        )
        for ref in (
            payload_descriptor,
            selection,
            prior_subject,
            self.request.qualification_inputs,
            *common.values(),
            *current_extra_inputs.values(),
        ):
            graph.contents[ref.identity] = self.cas.get_bytes(ref)
        for prefix, subject, checks, exact_inputs, native_target in (
            (
                "prior",
                prior_subject,
                prior_checks,
                common,
                prior_target,
            ),
            (
                "current",
                selection,
                {
                    name: b"fixture qualification"
                    for name in (
                        "producer-closure",
                        "path-contract",
                        "base-abi",
                        "runtime-abi",
                        "package-constraints",
                        "package-scripts",
                        "native-install",
                        "native-executable",
                        "application",
                        *current_extra_checks,
                    )
                },
                {
                    **common,
                    "qualification-inputs": self.request.qualification_inputs,
                    "current-selection": selection,
                    **current_extra_inputs,
                },
                target,
            ),
        ):
            if prefix == "current":
                checks["producer-closure"] = canonical_json_bytes(
                    {
                        "schema": "literate-ai/native-semantic-input-closure@1",
                        "current_plan_identity": self.plan.identity.uri,
                        "manifest_identity": self.plan.manifest_identity.uri,
                        "profile_identity": self.plan.profile.identity.uri,
                        "semantic_inputs_identity": self.inputs.identity.uri,
                        "unknown_edges": [],
                    }
                )
            derive, native = prefix + "-derive", prefix + "-native"
            for name in (derive, native):
                graph.contexts[name] = EvidenceRunContext(
                    name,
                    "https://example.test/repository",
                    "a" * 40,
                    workflow,
                    native_target,
                    100,
                    200,
                )
            journal = graph.blob(b"fixture journal")
            derivation = DerivationRun(
                graph.contexts[derive],
                subject,
                tuple(
                    EvidenceArtifact(name, ref)
                    for name, ref in sorted(exact_inputs.items())
                ),
                journal,
                "passed",
            )
            graph.add(
                derive,
                derivation,
                (),
                tuple(
                    sorted(
                        (
                            ("subject", subject),
                            ("journal", journal),
                            *(
                                ("input/" + name, ref)
                                for name, ref in exact_inputs.items()
                            ),
                        )
                    )
                ),
            )
            environment = graph.blob(
                b"fixture native environment; no product qualification"
            )
            artifacts = tuple(
                EvidenceArtifact(name, graph.blob(content))
                for name, content in sorted(checks.items())
            )
            platform = PlatformRun(
                graph.contexts[native],
                subject,
                graph.refs[derive],
                environment,
                artifacts,
                "passed",
            )
            graph.add(
                native,
                platform,
                (EvidenceArtifact("derivation", graph.refs[derive]),),
                tuple(
                    sorted(
                        (
                            ("subject", subject),
                            ("environment", environment),
                            *(("check/" + item.name, item.blob) for item in artifacts),
                        )
                    )
                ),
            )
        self.evidence = SemanticPayloadEvidenceAuthority(
            EvidenceVerificationPlan(
                canonical_identity("old-project"),
                graph.refs["prior-native"],
                tuple(
                    sorted(
                        (
                            graph.requirements[name]
                            for name in ("prior-derive", "prior-native")
                        ),
                        key=lambda item: item.envelope.identity,
                    )
                ),
            ),
            EvidenceVerificationPlan(
                self.plan.project_identity,
                graph.refs["current-native"],
                tuple(
                    sorted(
                        (
                            graph.requirements[name]
                            for name in ("current-derive", "current-native")
                        ),
                        key=lambda item: item.envelope.identity,
                    )
                ),
            ),
            graph.policy,
            graph.resolver,
            graph.locators(),
            graph.state,
        )
        self.readmitter = SemanticPayloadReadmitter(
            self.authority, self.cas, self.admission, self.evidence
        )

    def test_opaque_prior_result_is_not_canonical_build_provenance(self):
        prior = self.cas.put_bytes(canonical_json_bytes({"signed": "opaque fixture"}))
        with self.assertRaisesRegex(ValueError, "canonical_prior_build_result"):
            self.readmitter.readmit(
                replace(self.request, prior_result=prior), self.grant
            )
        self.assertFalse(self.spent)

    def test_prior_result_cannot_substitute_another_archive(self):
        value = json.loads(self.old_bytes)
        value["artifact_archive"] = self.cas.put_bytes(b"different bytes").to_dict()
        prior = self.put_json(value)
        with self.assertRaisesRegex(ValueError, "prior_result_payload_custody"):
            self.readmitter.readmit(
                replace(self.request, prior_result=prior), self.grant
            )
        self.assertFalse(self.spent)

    def test_policy_roundtrip_binds_every_owned_alternate_action(self):
        self.assertEqual(
            PayloadAdmissionPolicy.from_dict(self.policy.to_dict()), self.policy
        )

    def test_signed_fixture_requalification_preserves_payload_and_original_result(self):
        result = self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(result.payload, self.payload)
        self.assertEqual(self.cas.get_bytes(self.prior), self.old_bytes)
        receipt = json.loads(self.cas.get_bytes(result.receipt))
        self.assertEqual(receipt["receipt_kind"], "payload-requalification")
        self.assertFalse(receipt["build_result_rewritten"])
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.readmitter.readmit(self.request, self.grant)

    def refresh_current_evidence(self):
        self.evidence = replace(
            self.evidence,
            current=replace(
                self.evidence.current,
                root=self.graph.refs["current-native"],
                requirements=tuple(
                    sorted(
                        (
                            self.graph.requirements[name]
                            for name in ("current-derive", "current-native")
                        ),
                        key=lambda item: item.envelope.identity,
                    )
                ),
            ),
            locators=self.graph.locators(),
        )
        self.readmitter.evidence = self.evidence

    def test_untrusted_signature_cannot_turn_fixture_claims_into_qualification(self):
        self.graph.signer = Ed25519EvidenceSigner(bytes(reversed(range(32))))
        self.graph.resign("current-native", self.graph.records["current-native"])
        self.refresh_current_evidence()
        with self.assertRaises((EvidenceTrustError, DsseError)):
            self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(self.spent, set())

    def test_missing_native_application_check_refuses_before_grant_use(self):
        root = next(
            item
            for item in self.evidence.current.requirements
            if item.envelope == self.evidence.current.root
        )
        changed = replace(
            root,
            artifacts=tuple(
                item for item in root.artifacts if item[0] != "check/application"
            ),
        )
        current = replace(
            self.evidence.current,
            requirements=tuple(
                changed if item == root else item
                for item in self.evidence.current.requirements
            ),
        )
        self.readmitter.evidence = replace(self.evidence, current=current)
        with self.assertRaisesRegex(ValueError, "qualification_incomplete"):
            self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(self.spent, set())

    def test_signed_unknown_edges_still_refuse_reuse(self):
        record = self.graph.records["current-native"]
        closure = json.loads(
            self.graph.contents[
                next(
                    item.blob.identity
                    for item in record.checks
                    if item.name == "producer-closure"
                )
            ]
        )
        closure["unknown_edges"] = ["fixture missing generated input"]
        reference = self.graph.blob(canonical_json_bytes(closure))
        record = replace(
            record,
            checks=tuple(
                EvidenceArtifact(item.name, reference)
                if item.name == "producer-closure"
                else item
                for item in record.checks
            ),
        )
        requirement = self.graph.requirements["current-native"]
        self.graph.requirements["current-native"] = replace(
            requirement,
            artifacts=tuple(
                (name, reference) if name == "check/producer-closure" else (name, ref)
                for name, ref in requirement.artifacts
            ),
        )
        self.graph.resign("current-native", record)
        self.refresh_current_evidence()
        with self.assertRaisesRegex(ValueError, "closure_incomplete"):
            self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(self.spent, set())

    def test_current_compiler_or_header_changes_cannot_match_old_proof(self):
        value = self.inputs.to_dict()
        value["members"][0]["sha256"] = hashlib.sha256(b"changed header").hexdigest()
        value["members"][0]["size"] = len(b"changed header")
        changed = SemanticBuildInputs.from_dict(value)
        self.assertNotEqual(changed.identity, self.inputs.identity)
        request = replace(self.request, semantic_inputs=self.put_json(value))
        with self.assertRaisesRegex(ValueError, "current_member_differs"):
            self.readmitter.readmit(request, self.grant)
        value = self.inputs.to_dict()
        del value["compiler"]
        with self.assertRaises(ValueError):
            SemanticBuildInputs.from_dict(value)
        self.assertEqual(self.spent, set())

    def test_changed_current_authority_or_missing_policy_refuses_before_grant_use(self):
        self.authority.plan.return_value = replace(
            self.plan, manifest_identity=canonical_identity("changed")
        )
        with self.assertRaisesRegex(ValueError, "current_plan_changed"):
            self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(self.spent, set())

    def test_unknown_action_is_a_coded_refusal_not_stop_iteration(self):
        original = self.readmitter._current
        with (
            patch.object(
                self.readmitter,
                "_current",
                lambda request: _without_profile_actions(original(request)),
            ),
            self.assertRaises(ValueError) as refused,
        ):
            self.readmitter.readmit(self.request, self.grant)
        self.assertNotIsInstance(refused.exception, StopIteration)
        self.assertTrue(str(refused.exception).startswith("semantic_payload."))
        self.assertEqual(self.spent, set())

    def test_corrupt_payload_or_missing_native_checks_refuses(self):
        payload = self.cas.path_for(self.payload)
        payload.chmod(0o600)
        payload.write_bytes(b"corrupt fixture payload")
        with self.assertRaises(BlobIntegrityError):
            self.readmitter.readmit(self.request, self.grant)
        self.assertEqual(self.spent, set())


if __name__ == "__main__":
    unittest.main()

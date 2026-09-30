"""Fail-closed Standard generated-source admission and fanout contracts."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from literate_ai.application import (
    StandardSourceAdmissionError,
    StandardSourceAdmissionRequest,
    StandardSourceAdmissionService,
    StandardSourceVerification,
    require_source_admission_for_worker,
)
from literate_ai.contracts import (
    ContractValidationError,
    GeneratedSourceCandidate,
    InheritedSessionHandoffEvidence,
    InheritedSessionOutcome,
    InheritedSessionRequest,
    InheritedSessionResponse,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardSourceAdmissionMembership,
    StandardSourceAdmissionReceiptComposition,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
    canonical_identity,
)


def identity(label: str):
    return canonical_identity({"standard-source-admission-test": label})


def generation(
    *,
    node: str = "root",
    orchestration: str = "orchestration-request",
) -> SourceGenerationResumeCandidate:
    candidate = GeneratedSourceCandidate(
        identity(f"component-{node}"),
        identity(orchestration),
        identity(f"planned-coding-cli-request-{node}"),
        identity(f"plan-{node}"),
        identity(f"key-{node}"),
        identity(f"context-{node}"),
        identity(f"prompt-{node}"),
        identity(f"recipe-{node}"),
        identity(f"workspace-{node}"),
        identity(f"tree-{node}"),
        identity(f"bundle-{node}"),
        identity(f"manifest-{node}"),
        identity(f"source-bom-{node}"),
        identity(f"suite-{node}"),
    )
    provenance = SourceGenerationProvenance(
        candidate.source_generation_request_identity,
        candidate.planned_coding_cli_request_identity,
        identity("lock"),
        identity("application"),
        candidate.component_revision,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.context_manifest_identity,
        candidate.prompt_identity,
        candidate.recipe_identity,
        candidate.workspace_allocation_identity,
        identity("readiness"),
        (identity("route"),),
        (identity("coding-cli-transcript"),),
        candidate.identity,
    )
    output = SourceGenerationRunOutput(
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
    )
    return SourceGenerationResumeCandidate(
        output,
        output.identity,
        identity("budget"),
        identity("complexity"),
    )


def target_independent() -> StandardSourceSelectorSet:
    return StandardSourceSelectorSet(StandardSourceSelectorScope.TARGET_INDEPENDENT, ())


def target_specific(os_name: str) -> StandardSourceSelectorSet:
    return StandardSourceSelectorSet(
        StandardSourceSelectorScope.TARGET_SPECIFIC,
        (StandardSourceSelector("platform.os", os_name),),
    )


class Verifier:
    def __init__(self, generation_value, *, manifest=None, fail=False):
        self.generation = generation_value
        self.manifest = manifest
        self.fail = fail
        self.calls = 0

    def verify(self, generation_value):
        self.calls += 1
        if generation_value != self.generation:
            raise AssertionError("verifier received substituted generation")
        if self.fail:
            raise StandardSourceAdmissionError(
                "source_admission.tests_failed",
                "canonical generated-source tests failed",
            )
        return StandardSourceVerification(
            self.manifest or generation_value.output.candidate.source_manifest_identity,
            identity("test-plan"),
            (
                StandardSourceTestResult(
                    identity("oracle"),
                    identity("test-result"),
                    150,
                    150,
                    0,
                    0,
                ),
            ),
            identity("verifier"),
        )


class Publisher:
    def __init__(self):
        self.memberships = []

    def publish(self, membership):
        self.memberships.append(membership)
        return membership.identity


def request(
    generation_value: SourceGenerationResumeCandidate,
    *,
    selectors: StandardSourceSelectorSet | None = None,
) -> StandardSourceAdmissionRequest:
    candidate = generation_value.output.candidate
    return StandardSourceAdmissionRequest(
        generation_value,
        SourceDerivationCacheKey(
            candidate.recipe_identity,
            identity("execution-plan"),
            identity("coding-cli-tool"),
            SourceCacheModelBinding("claude", "inherited"),
            candidate.planned_coding_cli_request_identity,
        ),
        identity("flavors"),
        identity("skills"),
        identity("coding-cli-tool"),
        identity("coding-cli-transcript"),
        selectors or target_independent(),
        identity("framework-distribution"),
    )


def admitted(
    *,
    selectors: StandardSourceSelectorSet | None = None,
) -> StandardSourceAdmissionMembership:
    generated = generation()
    publisher = Publisher()
    return StandardSourceAdmissionService(
        Verifier(generated),
        publisher,
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
    ).admit(request(generated, selectors=selectors))


class StandardSourceAdmissionTests(unittest.TestCase):
    def test_inherited_handoff_allows_verifier_owned_asset_augmentation(self):
        generated = generation()
        candidate = generated.output.candidate
        handoff_request = InheritedSessionRequest(
            identity("inherited-provider"),
            identity("inherited-session"),
            candidate.source_generation_request_identity,
            candidate.component_generation_plan_identity,
            candidate.context_manifest_identity,
            candidate.prompt_identity,
            candidate.workspace_allocation_identity,
            generated.output.provenance.component_lock_identity,
            candidate.recipe_identity,
            identity("model-binding"),
            identity("output-contract"),
            identity("context-bundle"),
            "2026-08-15T00:00:00Z",
            "2026-08-15T00:15:00Z",
            "public-nonce",
        )
        handoff = InheritedSessionHandoffEvidence(
            handoff_request,
            InheritedSessionResponse(
                handoff_request.identity,
                handoff_request.provider_identity,
                handoff_request.session_identity,
                1,
                InheritedSessionOutcome.SUCCEEDED,
                identity("provider-output-before-locked-assets"),
                identity("source-payload"),
                identity("private-transcript-digest"),
                identity("provider-evidence-digest"),
            ),
            identity("authentication-key-id"),
        )
        provenance = replace(
            generated.output.provenance,
            model_stage_output_identities=(identity("stage-output-record"),),
            provider_evidence_identities=(handoff.identity,),
        )
        output = replace(
            generated.output,
            provenance=provenance,
            provenance_identity=provenance.identity,
        )
        generated = replace(
            generated,
            output=output,
            output_identity=output.identity,
        )
        admission_request = request(generated)
        admission_request = replace(
            admission_request,
            cache_key=replace(
                admission_request.cache_key,
                coding_cli_tool_binding_identity=handoff_request.provider_identity,
            ),
            coding_cli_tool_binding_identity=handoff_request.provider_identity,
            coding_cli_transcript_identity=handoff.identity,
            inherited_session_handoff=handoff,
        )
        verifier = Verifier(generated)
        membership = StandardSourceAdmissionService(
            verifier,
            Publisher(),
        ).admit(admission_request)

        self.assertEqual(verifier.calls, 1)
        self.assertEqual(
            membership.evidence.coding_cli_transcript_identity,
            handoff.identity,
        )

    def test_passing_generation_and_all_150_tests_admit_source(self):
        generated = generation()
        verifier = Verifier(generated)
        publisher = Publisher()
        membership = StandardSourceAdmissionService(
            verifier,
            publisher,
            clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        ).admit(request(generated))

        self.assertEqual(verifier.calls, 1)
        self.assertEqual(publisher.memberships, [membership])
        self.assertEqual(membership.evidence.test_results[0].passed_count, 150)
        self.assertEqual(
            StandardSourceAdmissionMembership.from_dict(membership.to_dict()),
            membership,
        )

    def test_twelve_planned_node_requests_share_one_orchestration(self):
        generated = tuple(generation(node=f"node-{index}") for index in range(12))
        memberships = []
        for item in generated:
            memberships.append(
                StandardSourceAdmissionService(Verifier(item), Publisher()).admit(
                    request(item)
                )
            )

        self.assertEqual(len(memberships), 12)
        self.assertEqual(
            {item.evidence.orchestration_request_identity for item in memberships},
            {identity("orchestration-request")},
        )
        self.assertEqual(
            len(
                {
                    item.evidence.planned_coding_cli_request_identity
                    for item in memberships
                }
            ),
            12,
        )

        swapped = replace(
            request(generated[0]),
            cache_key=request(generated[1]).cache_key,
        )
        verifier = Verifier(generated[0])
        with self.assertRaises(StandardSourceAdmissionError) as raised:
            StandardSourceAdmissionService(verifier, Publisher()).admit(swapped)
        self.assertEqual(raised.exception.code, "source_admission.cache_key_mismatch")
        self.assertEqual(verifier.calls, 0)

    def test_tampered_transcript_fails_before_verification(self):
        generated = generation()
        verifier = Verifier(generated)
        changed = replace(
            request(generated),
            coding_cli_transcript_identity=identity("tampered-transcript"),
        )
        with self.assertRaises(StandardSourceAdmissionError) as raised:
            StandardSourceAdmissionService(verifier, Publisher()).admit(changed)
        self.assertEqual(
            raised.exception.code,
            "source_admission.coding_cli_transcript_mismatch",
        )
        self.assertEqual(verifier.calls, 0)

    def test_failed_test_and_changed_source_manifest_refuse_admission(self):
        generated = generation()
        for verifier, code in (
            (Verifier(generated, fail=True), "source_admission.tests_failed"),
            (
                Verifier(generated, manifest=identity("changed-manifest")),
                "source_admission.source_manifest_mismatch",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(StandardSourceAdmissionError) as raised:
                    StandardSourceAdmissionService(verifier, Publisher()).admit(
                        request(generated)
                    )
                self.assertEqual(raised.exception.code, code)

    def test_timestamp_is_metadata_but_evidence_tamper_fails_closed(self):
        membership = admitted()
        changed_time = replace(membership.evidence, admitted_at="2026-08-15T00:00:00Z")
        self.assertEqual(changed_time.identity, membership.evidence.identity)
        wire = membership.to_dict()
        wire["evidence"]["framework_distribution_identity"] = identity(
            "other-framework"
        ).to_dict()
        with self.assertRaisesRegex(ContractValidationError, "evidence_identity"):
            StandardSourceAdmissionMembership.from_dict(wire)

    def test_worker_rejects_authority_framework_and_target_mismatches(self):
        membership = admitted(selectors=target_specific("linux"))
        evidence = membership.evidence
        common = {
            "expected_component_lock_identity": evidence.component_lock_identity,
            "expected_generation_plan_identity": evidence.generation_plan_identity,
            "expected_generation_key_identity": evidence.generation_key_identity,
            "expected_admission_orchestration_request_identity": (
                evidence.orchestration_request_identity
            ),
            "expected_flavor_set_identity": evidence.flavor_set_identity,
            "expected_skill_closure_identity": evidence.skill_closure_identity,
            "expected_framework_distribution_identity": (
                evidence.framework_distribution_identity
            ),
            "worker_selectors": target_specific("linux"),
        }
        self.assertIs(
            require_source_admission_for_worker(membership, **common),
            membership,
        )
        for field, code in (
            ("expected_component_lock_identity", "component_lock_mismatch"),
            ("expected_generation_plan_identity", "generation_plan_mismatch"),
            ("expected_generation_key_identity", "generation_key_mismatch"),
            (
                "expected_admission_orchestration_request_identity",
                "orchestration_request_mismatch",
            ),
            ("expected_flavor_set_identity", "flavor_set_mismatch"),
            ("expected_skill_closure_identity", "skill_closure_mismatch"),
            (
                "expected_framework_distribution_identity",
                "framework_distribution_mismatch",
            ),
        ):
            with self.subTest(field=field):
                changed = {**common, field: identity(f"other-{field}")}
                with self.assertRaises(StandardSourceAdmissionError) as raised:
                    require_source_admission_for_worker(membership, **changed)
                self.assertIn(code, raised.exception.code)
        with self.assertRaises(StandardSourceAdmissionError) as raised:
            require_source_admission_for_worker(
                membership,
                **{**common, "worker_selectors": target_specific("windows")},
            )
        self.assertEqual(
            raised.exception.code, "source_admission.target_selector_mismatch"
        )

    def test_target_independent_admission_fans_out_to_linux_and_windows(self):
        membership = admitted()
        evidence = membership.evidence
        for os_name in ("linux", "windows"):
            with self.subTest(os=os_name):
                self.assertIs(
                    require_source_admission_for_worker(
                        membership,
                        expected_component_lock_identity=(
                            evidence.component_lock_identity
                        ),
                        expected_generation_plan_identity=(
                            evidence.generation_plan_identity
                        ),
                        expected_generation_key_identity=(
                            evidence.generation_key_identity
                        ),
                        expected_admission_orchestration_request_identity=(
                            evidence.orchestration_request_identity
                        ),
                        expected_flavor_set_identity=evidence.flavor_set_identity,
                        expected_skill_closure_identity=(
                            evidence.skill_closure_identity
                        ),
                        expected_framework_distribution_identity=(
                            evidence.framework_distribution_identity
                        ),
                        worker_selectors=target_specific(os_name),
                    ),
                    membership,
                )

    def test_missing_coding_cli_provenance_and_downstream_receipt_fail_closed(self):
        generated = generation()
        provenance = generated.output.provenance
        with self.assertRaises(ContractValidationError):
            replace(
                provenance,
                route_decision_identities=(),
                model_stage_output_identities=(),
            )
        with self.assertRaises(ContractValidationError):
            StandardSourceAdmissionReceiptComposition(
                identity("execution-plan"),
                (admitted().identity,),
                (),
                identity("project-admission"),
            )

    def test_final_composition_contains_source_and_artifact_evidence(self):
        source = admitted()
        composition = StandardSourceAdmissionReceiptComposition(
            identity("execution-plan"),
            (source.identity,),
            (identity("object-binary-package-result"),),
            identity("project-admission"),
        )
        self.assertEqual(composition.source_admission_identities, (source.identity,))
        self.assertEqual(
            composition.downstream_lifecycle_result_identities,
            (identity("object-binary-package-result"),),
        )


if __name__ == "__main__":
    unittest.main()

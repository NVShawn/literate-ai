"""Verifier-owned Standard transition from generated to accepted source."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from literate_ai.contracts import ContentIdentity, SourceDerivationCacheKey
from literate_ai.contracts.executable_components import (
    SourceGenerationResumeCandidate,
)
from literate_ai.contracts.inherited_session import (
    InheritedSessionHandoffEvidence,
    InheritedSessionOutcome,
)
from literate_ai.contracts.standard_source_admission import (
    StandardSourceAdmissionEvidence,
    StandardSourceAdmissionMembership,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
)


class StandardSourceAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StandardSourceVerification:
    source_manifest_identity: ContentIdentity
    test_plan_identity: ContentIdentity
    test_results: tuple[StandardSourceTestResult, ...]
    verifier_identity: ContentIdentity

    def __post_init__(self) -> None:
        for name in (
            "source_manifest_identity",
            "test_plan_identity",
            "verifier_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError(f"{name} must be a ContentIdentity")
        if not self.test_results or any(
            not isinstance(item, StandardSourceTestResult) for item in self.test_results
        ):
            raise StandardSourceAdmissionError(
                "source_admission.test_evidence_missing",
                "source verifier must return at least one complete passing result",
            )


class StandardGeneratedSourceVerifier(Protocol):
    """Execute canonical tests/oracles without trusting generator assertions."""

    def verify(
        self, generation: SourceGenerationResumeCandidate
    ) -> StandardSourceVerification: ...


class StandardSourceAdmissionPublisher(Protocol):
    """Persist exact accepted-source membership after verification."""

    def publish(
        self, membership: StandardSourceAdmissionMembership
    ) -> ContentIdentity: ...


@dataclass(frozen=True, slots=True)
class StandardSourceAdmissionRequest:
    generation: SourceGenerationResumeCandidate
    cache_key: SourceDerivationCacheKey
    flavor_set_identity: ContentIdentity
    skill_closure_identity: ContentIdentity
    coding_cli_tool_binding_identity: ContentIdentity
    coding_cli_transcript_identity: ContentIdentity
    source_selectors: StandardSourceSelectorSet
    framework_distribution_identity: ContentIdentity
    inherited_session_handoff: InheritedSessionHandoffEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.generation, SourceGenerationResumeCandidate):
            raise TypeError("generation must be a SourceGenerationResumeCandidate")
        if not isinstance(self.cache_key, SourceDerivationCacheKey):
            raise TypeError("cache_key must be a SourceDerivationCacheKey")
        for name in (
            "flavor_set_identity",
            "skill_closure_identity",
            "coding_cli_tool_binding_identity",
            "coding_cli_transcript_identity",
            "framework_distribution_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError(f"{name} must be a ContentIdentity")
        if not isinstance(self.source_selectors, StandardSourceSelectorSet):
            raise TypeError("source_selectors must be a StandardSourceSelectorSet")
        if self.inherited_session_handoff is not None and not isinstance(
            self.inherited_session_handoff, InheritedSessionHandoffEvidence
        ):
            raise TypeError("inherited_session_handoff must be typed handoff evidence")


class StandardSourceAdmissionService:
    """Admit only exact source independently verified under current authority."""

    def __init__(
        self,
        verifier: StandardGeneratedSourceVerifier,
        publisher: StandardSourceAdmissionPublisher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("verifier must provide verify")
        if not callable(getattr(publisher, "publish", None)):
            raise TypeError("publisher must provide publish")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.verifier = verifier
        self.publisher = publisher
        self.clock = clock

    def admit(
        self, request: StandardSourceAdmissionRequest
    ) -> StandardSourceAdmissionMembership:
        if not isinstance(request, StandardSourceAdmissionRequest):
            raise TypeError("request must be a StandardSourceAdmissionRequest")
        generation = request.generation
        output = generation.output
        candidate = output.candidate
        provenance = output.provenance
        cache_key = request.cache_key
        if output.provenance_identity != provenance.identity:
            raise StandardSourceAdmissionError(
                "source_admission.provenance_identity_mismatch",
                "generation output does not bind its exact provenance",
            )
        if (
            output.candidate_identity != candidate.identity
            or provenance.candidate_identity != candidate.identity
        ):
            raise StandardSourceAdmissionError(
                "source_admission.candidate_identity_mismatch",
                "generation provenance does not bind the exact candidate",
            )
        if (
            provenance.generated_component_revision_identity
            != candidate.component_revision
            or provenance.source_generation_request_identity
            != candidate.source_generation_request_identity
            or provenance.planned_coding_cli_request_identity
            != candidate.planned_coding_cli_request_identity
            or provenance.component_generation_plan_identity
            != candidate.component_generation_plan_identity
            or provenance.generation_key_identity != candidate.generation_key_identity
            or provenance.recipe_identity != candidate.recipe_identity
        ):
            raise StandardSourceAdmissionError(
                "source_admission.generation_authority_mismatch",
                "candidate and generation provenance bind different authority",
            )
        if not provenance.model_stage_output_identities:
            raise StandardSourceAdmissionError(
                "source_admission.coding_cli_provenance_missing",
                "admission requires retained coding-CLI model output evidence",
            )
        handoff = request.inherited_session_handoff
        if handoff is not None:
            response = handoff.response
            handoff_request = handoff.request
            if response.outcome is not InheritedSessionOutcome.SUCCEEDED:
                raise StandardSourceAdmissionError(
                    "source_admission.inherited_session_not_successful",
                    "only a successful inherited-session handoff can be verified",
                )
            if (
                handoff_request.generation_request_identity
                != candidate.source_generation_request_identity
                or handoff_request.generation_plan_identity
                != candidate.component_generation_plan_identity
                or handoff_request.context_manifest_identity
                != candidate.context_manifest_identity
                or handoff_request.prompt_identity != candidate.prompt_identity
            ):
                raise StandardSourceAdmissionError(
                    "source_admission.inherited_session_binding_mismatch",
                    "inherited-session handoff binds another request, plan, context, "
                    "or prompt",
                )
            if (
                request.coding_cli_tool_binding_identity
                != handoff_request.provider_identity
                or request.coding_cli_transcript_identity != handoff.identity
                or handoff.identity not in provenance.provider_evidence_identities
            ):
                raise StandardSourceAdmissionError(
                    "source_admission.inherited_session_evidence_mismatch",
                    "provider and public handoff evidence must be retained by "
                    "generation provenance",
                )
        if (
            cache_key.recipe_identity != candidate.recipe_identity
            or cache_key.request_identity
            != candidate.planned_coding_cli_request_identity
            or cache_key.coding_cli_tool_binding_identity
            != request.coding_cli_tool_binding_identity
        ):
            raise StandardSourceAdmissionError(
                "source_admission.cache_key_mismatch",
                "admission request does not bind the exact generation recipe, "
                "planned coding-CLI request, and coding CLI",
            )
        if handoff is None and (
            request.coding_cli_transcript_identity
            not in provenance.model_stage_output_identities
        ):
            raise StandardSourceAdmissionError(
                "source_admission.coding_cli_transcript_mismatch",
                "coding-CLI transcript is not retained by generation provenance",
            )
        verification = self.verifier.verify(generation)
        if not isinstance(verification, StandardSourceVerification):
            raise StandardSourceAdmissionError(
                "source_admission.verifier_result_invalid",
                "source verifier returned an untyped result",
            )
        if verification.source_manifest_identity != candidate.source_manifest_identity:
            raise StandardSourceAdmissionError(
                "source_admission.source_manifest_mismatch",
                "verifier observed another source manifest",
            )
        evidence = StandardSourceAdmissionEvidence(
            component_revision_identity=candidate.component_revision,
            component_lock_identity=provenance.component_lock_identity,
            generation_plan_identity=candidate.component_generation_plan_identity,
            generation_key_identity=candidate.generation_key_identity,
            recipe_identity=candidate.recipe_identity,
            orchestration_request_identity=(
                candidate.source_generation_request_identity
            ),
            planned_coding_cli_request_identity=(
                candidate.planned_coding_cli_request_identity
            ),
            flavor_set_identity=request.flavor_set_identity,
            skill_closure_identity=request.skill_closure_identity,
            source_tree_identity=candidate.tree_identity,
            source_bundle_identity=candidate.source_bundle_identity,
            source_manifest_identity=candidate.source_manifest_identity,
            source_bom_identity=candidate.source_bom_identity,
            generated_test_suite_identity=candidate.generated_test_suite_identity,
            coding_cli_tool_binding_identity=request.coding_cli_tool_binding_identity,
            coding_cli_transcript_identity=request.coding_cli_transcript_identity,
            generation_provenance_identity=output.provenance_identity,
            test_plan_identity=verification.test_plan_identity,
            test_results=verification.test_results,
            source_selectors=request.source_selectors,
            framework_distribution_identity=request.framework_distribution_identity,
            verifier_identity=verification.verifier_identity,
            admitted_at=self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        membership = StandardSourceAdmissionMembership(generation, evidence)
        published = self.publisher.publish(membership)
        if published != membership.identity:
            raise StandardSourceAdmissionError(
                "source_admission.publication_identity_mismatch",
                "accepted-source publisher returned another membership identity",
            )
        return membership


def require_source_admission_for_worker(
    membership: StandardSourceAdmissionMembership,
    *,
    expected_component_lock_identity: ContentIdentity,
    expected_generation_plan_identity: ContentIdentity,
    expected_generation_key_identity: ContentIdentity,
    expected_flavor_set_identity: ContentIdentity,
    expected_skill_closure_identity: ContentIdentity,
    expected_framework_distribution_identity: ContentIdentity,
    worker_selectors: StandardSourceSelectorSet,
    expected_admission_orchestration_request_identity: ContentIdentity | None = None,
) -> StandardSourceAdmissionMembership:
    """Fail closed before worker allocation; this path has no generator port.

    The optional orchestration identity names the admission transaction, not the
    current continuation request.  Ordinary cross-session restore verifies that
    custody internally and rebinds the accepted tree to the new request separately.
    """

    if not isinstance(membership, StandardSourceAdmissionMembership):
        raise TypeError("membership must be a StandardSourceAdmissionMembership")
    evidence = membership.evidence
    expected = {
        "component_lock_identity": expected_component_lock_identity,
        "generation_plan_identity": expected_generation_plan_identity,
        "generation_key_identity": expected_generation_key_identity,
        "flavor_set_identity": expected_flavor_set_identity,
        "skill_closure_identity": expected_skill_closure_identity,
        "framework_distribution_identity": (expected_framework_distribution_identity),
    }
    if expected_admission_orchestration_request_identity is not None:
        expected["orchestration_request_identity"] = (
            expected_admission_orchestration_request_identity
        )
    for name, identity in expected.items():
        if not isinstance(identity, ContentIdentity):
            raise TypeError(f"{name} must be a ContentIdentity")
        if getattr(evidence, name) != identity:
            raise StandardSourceAdmissionError(
                f"source_admission.{name.removesuffix('_identity')}_mismatch",
                f"accepted source binds another {name.removesuffix('_identity')}",
            )
    if not isinstance(worker_selectors, StandardSourceSelectorSet):
        raise TypeError("worker_selectors must be a StandardSourceSelectorSet")
    if not evidence.source_selectors.compatible_with(worker_selectors):
        raise StandardSourceAdmissionError(
            "source_admission.target_selector_mismatch",
            "accepted target-specific source is incompatible with this worker",
        )
    # Round-trip the closed schema so mutated subclasses or stale in-memory values do
    # not cross the worker boundary.
    reparsed = StandardSourceAdmissionMembership.from_dict(membership.to_dict())
    if reparsed != membership:
        raise StandardSourceAdmissionError(
            "source_admission.membership_changed",
            "accepted-source membership changed during worker verification",
        )
    return membership


__all__ = [
    "StandardGeneratedSourceVerifier",
    "StandardSourceAdmissionError",
    "StandardSourceAdmissionPublisher",
    "StandardSourceAdmissionRequest",
    "StandardSourceAdmissionService",
    "StandardSourceVerification",
    "require_source_admission_for_worker",
]

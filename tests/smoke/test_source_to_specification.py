from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification import (
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    CoverageState,
    DraftArtifact,
    DraftScenario,
    DraftStatement,
    EvidenceReference,
    PromotionPolicy,
    ProviderValidation,
    ReviewAuthority,
    ReviewDisposition,
    ReviewStatementDecision,
    RunMode,
    SourceMutationError,
    SourceToSpecificationRequest,
    SourceToSpecificationWorkflow,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    SpecificationReviewDecision,
    promote,
)


def request(
    *,
    snapshot: str = "snapshot-1",
    content_digest: str = "sha256:source-1",
    mode: RunMode = RunMode.BOOTSTRAP,
    previous: str | None = None,
) -> SourceToSpecificationRequest:
    return SourceToSpecificationRequest(
        request_id="request-1",
        source_snapshot_id=snapshot,
        source_content_digest=content_digest,
        origin_attestation_id="origin-1",
        mode=mode,
        output_provider="openspec",
        skill_set_id="test-skills",
        routing_policy_id="route-1",
        redaction_policy_id="redact-1",
        egress_policy_id="egress-1",
        included_paths=("src",),
        facets=("behavior",),
        previous_specification_set_id=previous,
    )


def skill(*, content_digest: str = "sha256:skill-1") -> SpecAuthoringSkill:
    return SpecAuthoringSkill(
        skill_id="behavior",
        version="1.0.0",
        content_digest=content_digest,
        title="Behavior",
        capabilities=("source-to-specification.behavior",),
        facets=("behavior",),
        evidence_kinds=("symbols",),
    )


def skill_set(item: SpecAuthoringSkill) -> SpecAuthoringSkillSet:
    return SpecAuthoringSkillSet(
        skill_set_id="test-skills",
        version="1.0.0",
        skills=(item.ref,),
    )


def evidence(
    evidence_id: str = "evidence-a",
    path: str = "src/a.py",
    snapshot: str = "snapshot-1",
    digest: str = "sha256:evidence-a",
) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        source_snapshot_id=snapshot,
        content_digest=digest,
        path=path,
        symbol="run",
    )


def renderer(_request, observations, _uncertainty):
    statements = tuple(
        DraftStatement(
            statement_id=f"statement:{item.observation_id}",
            capability="behavior",
            requirement=item.statement,
            scenarios=(
                DraftScenario(
                    name="Observed behavior",
                    when="the entrypoint is called",
                    then=item.statement,
                ),
            ),
            observation_ids=(item.observation_id,),
        )
        for item in observations
        if item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
    )
    return statements, (
        DraftArtifact(path="specs/behavior/spec.md", content="# Behavior\n"),
    )


def validator(provider, _artifacts):
    return ProviderValidation(
        provider=provider,
        provider_version="1.0.0",
        valid=True,
    )


def run_result(
    *,
    source_request: SourceToSpecificationRequest | None = None,
    claim_kinds: tuple[ClaimKind, ...] = (ClaimKind.OBSERVED, ClaimKind.OBSERVED),
    source_root_guard: Path | None = None,
    mutate: Path | None = None,
):
    source_request = source_request or request()
    selected_skill = skill()
    references = (
        evidence(snapshot=source_request.source_snapshot_id),
        evidence(
            "evidence-b",
            "src/b.py",
            source_request.source_snapshot_id,
            "sha256:evidence-b",
        ),
    )
    surfaces = (
        BehaviorSurface("surface-a", "behavior", "Behavior A"),
        BehaviorSurface("surface-b", "behavior", "Behavior B"),
    )

    def execute(executing_skill, _request, _surfaces, supplied_evidence):
        if mutate is not None:
            mutate.write_text("changed\n")
        return tuple(
            BehaviorObservation(
                observation_id=f"observation-{index}",
                surface_ids=(surfaces[index].surface_id,),
                facet="behavior",
                claim_kind=claim_kind,
                statement=f"Behavior {index} occurs",
                evidence=()
                if claim_kind is ClaimKind.UNKNOWN
                else (supplied_evidence[index],),
                skill=executing_skill.ref,
                confidence=0.9,
            )
            for index, claim_kind in enumerate(claim_kinds)
        )

    return SourceToSpecificationWorkflow().run(
        request=source_request,
        skill_set=skill_set(selected_skill),
        skill_catalog={selected_skill.skill_id: selected_skill},
        surfaces=surfaces,
        evidence=references,
        execute_skill=execute,
        render_draft=renderer,
        validate_provider=validator,
        source_root_guard=source_root_guard,
    )


class WorkflowTests(unittest.TestCase):
    def test_workflow_produces_provider_valid_traceable_coverage(self):
        result = run_result()
        self.assertTrue(result.draft.validation.valid)
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(
            tuple(item.state for item in result.coverage.entries),
            (CoverageState.COVERED, CoverageState.COVERED),
        )
        self.assertEqual(result.run.draft_identity, result.draft.identity)
        self.assertFalse(result.uncertainty.items)

    def test_source_guard_detects_callback_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text("original\n")
            with self.assertRaises(SourceMutationError):
                run_result(source_root_guard=root, mutate=source)


class ReviewAndRefreshTests(unittest.TestCase):
    @staticmethod
    def review_for(result, *, authority=ReviewAuthority.HUMAN, resolve=()):
        return SpecificationReviewDecision(
            review_id="review-1",
            draft_id=result.draft.draft_id,
            base_specification_set_id=None,
            actor="reviewer",
            authority=authority,
            reason="Source-backed draft reviewed",
            statement_decisions=tuple(
                ReviewStatementDecision(
                    statement_id=item.statement_id,
                    disposition=ReviewDisposition.ACCEPT,
                    reason="Evidence supports the statement",
                )
                for item in result.draft.statements
            ),
            resolved_uncertainty_ids=resolve,
            resulting_artifacts=result.draft.artifacts,
            validation=result.draft.validation,
        )

    def test_complete_human_review_promotes_atomically(self):
        result = run_result()
        promoted = promote(
            result,
            self.review_for(result),
            PromotionPolicy("human-reviewed"),
            expected_base_specification_set_id=None,
        )
        self.assertEqual(promoted.statements, result.draft.statements)
        self.assertEqual(promoted.source_snapshot_id, result.request.source_snapshot_id)
        self.assertEqual(promoted.identity, promoted.specification_set_id)


if __name__ == "__main__":
    unittest.main()

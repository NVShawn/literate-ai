from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from literate_ai.source_to_specification import (
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    ComponentDefinitionDraft,
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
    SkillRef,
    SourceMutationError,
    SourceToSpecificationError,
    SourceToSpecificationRequest,
    SourceToSpecificationWorkflow,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    SpecificationReviewDecision,
    builtin_skill_set,
    canonical_digest,
    load_skill_catalog,
    load_skill_manifest,
    plan_refresh,
    promote,
    resolve_skill_set,
)
from literate_ai.source_to_specification.synthesis import (
    render_openspec_markdown,
    validate_openspec_draft,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_SKILLS = REPO_ROOT / "skills" / "source-to-specification"


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


class ContractTests(unittest.TestCase):
    def test_openspec_renderer_emits_one_unique_requirement_per_statement(self):
        statements = (
            DraftStatement(
                statement_id="statement:behavior-a",
                capability="behavior",
                requirement="Behavior A occurs.",
                scenarios=(
                    DraftScenario(
                        name="First behavior",
                        when="the first behavior is requested",
                        then="Behavior A occurs.",
                    ),
                    DraftScenario(
                        name="First behavior repeats",
                        when="the first behavior is requested again",
                        then="Behavior A occurs again.",
                    ),
                ),
                observation_ids=("observation-a",),
            ),
            DraftStatement(
                statement_id="statement:behavior-b",
                capability="behavior",
                requirement="Behavior B occurs.",
                scenarios=(
                    DraftScenario(
                        name="Second behavior",
                        when="the second behavior is requested",
                        then="Behavior B occurs.",
                    ),
                ),
                observation_ids=("observation-b",),
            ),
        )

        content = render_openspec_markdown(statements)
        validation = validate_openspec_draft(
            "openspec",
            (DraftArtifact(path="specs/derived/spec.md", content=content),),
        )

        self.assertEqual(content.count("### Requirement:"), 2)
        self.assertIn("behavior [statement:behavior-a]", content)
        self.assertIn("behavior [statement:behavior-b]", content)
        self.assertTrue(validation.valid, validation.diagnostics)

    def test_contracts_are_immutable_and_have_stable_semantic_identity(self):
        first = request()
        second = request()
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.identity, canonical_digest(first))
        with self.assertRaises(FrozenInstanceError):
            first.request_id = "changed"  # type: ignore[misc]

    def test_non_bootstrap_requires_previous_specification(self):
        with self.assertRaisesRegex(
            SourceToSpecificationError, "requires a previous specification set"
        ):
            request(mode=RunMode.REFRESH)

    def test_contract_rejects_path_traversal_and_invalid_mode(self):
        with self.assertRaises(SourceToSpecificationError):
            evidence(path="../outside.py")
        with self.assertRaisesRegex(SourceToSpecificationError, "unsupported run mode"):
            replace(request(), mode="invented")  # type: ignore[arg-type]


class SkillManifestTests(unittest.TestCase):
    def test_builtin_catalog_is_complete_pinned_and_ordered(self):
        catalog = load_skill_catalog(BUILTIN_SKILLS)
        selected = builtin_skill_set(catalog)
        resolved = resolve_skill_set(selected, catalog)
        self.assertEqual(
            tuple(item.skill_id for item in resolved),
            (
                "architecture",
                "api-surface",
                "behavior-state",
                "tests",
                "security",
                "operations",
            ),
        )
        self.assertTrue(
            all(item.content_digest.startswith("sha256:") for item in resolved)
        )

    def test_manifest_loader_rejects_unknown_fields(self):
        manifest = {
            "schema": "literate-ai/spec-authoring-skill@1",
            "skill_id": "test",
            "version": "1",
            "title": "Test",
            "capabilities": ["test"],
            "facets": ["test"],
            "evidence_kinds": ["source"],
            "unexpected": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skill.json"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(SourceToSpecificationError, "unknown"):
                load_skill_manifest(path)

    def test_manifest_loader_preserves_provider_specific_model_selection(self):
        manifest = {
            "schema": "literate-ai/spec-authoring-skill@1",
            "skill_id": "test",
            "version": "1.0.0",
            "title": "Test",
            "capabilities": ["test"],
            "facets": ["test"],
            "evidence_kinds": ["source"],
            "models": {
                "codex": "gpt-test",
                "claude": "claude-test",
                "opencode": "openai/gpt-test",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skill.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = load_skill_manifest(path)

        self.assertEqual(loaded.model_for("codex"), "gpt-test")
        self.assertEqual(loaded.model_for("claude"), "claude-test")
        self.assertEqual(loaded.model_for("opencode"), "openai/gpt-test")
        self.assertIsNone(loaded.model_for("cursor-agent"))

        with self.assertRaises(SourceToSpecificationError) as unsupported:
            replace(loaded, models={"unknown": "model"})
        self.assertEqual(
            unsupported.exception.code,
            "skill.model_provider_unsupported",
        )
        with self.assertRaises(SourceToSpecificationError) as reserved:
            replace(loaded, models={"codex": "cli-configured-default"})
        self.assertEqual(reserved.exception.code, "skill.model_selector_reserved")

    def test_skill_set_rejects_tampered_identity(self):
        selected_skill = skill()
        selected = SpecAuthoringSkillSet(
            skill_set_id="test-skills",
            version="1.0.0",
            skills=(SkillRef("behavior", "1.0.0", "sha256:tampered"),),
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "pinned reference"):
            resolve_skill_set(selected, {"behavior": selected_skill})


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

    def test_unknowns_remain_non_normative_and_block_coverage(self):
        result = run_result(claim_kinds=(ClaimKind.UNKNOWN, ClaimKind.OBSERVED))
        self.assertEqual(len(result.draft.statements), 1)
        self.assertEqual(result.coverage.entries[0].state, CoverageState.UNRESOLVED)
        self.assertEqual(len(result.uncertainty.items), 1)
        self.assertTrue(result.uncertainty.items[0].blocking)

    def test_workflow_rejects_evidence_from_another_snapshot(self):
        selected_skill = skill()
        with self.assertRaisesRegex(SourceToSpecificationError, "exact requested"):
            SourceToSpecificationWorkflow().run(
                request=request(),
                skill_set=skill_set(selected_skill),
                skill_catalog={"behavior": selected_skill},
                surfaces=(BehaviorSurface("surface", "behavior", "Behavior"),),
                evidence=(evidence(snapshot="other"),),
                execute_skill=lambda *_args: (),
                render_draft=renderer,
                validate_provider=validator,
            )

    def test_component_draft_must_match_exact_source_snapshot(self):
        selected_skill = skill()
        with self.assertRaisesRegex(SourceToSpecificationError, "component definition"):
            SourceToSpecificationWorkflow().run(
                request=request(),
                skill_set=skill_set(selected_skill),
                skill_catalog={"behavior": selected_skill},
                surfaces=(),
                evidence=(),
                execute_skill=lambda *_args: (),
                render_draft=renderer,
                validate_provider=validator,
                component_definition_draft=ComponentDefinitionDraft(
                    coordinate="local/component",
                    title="Component",
                    provided_capabilities=(),
                    source_snapshot_id="other",
                ),
            )

    def test_source_guard_detects_callback_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text("original\n")
            with self.assertRaises(SourceMutationError):
                run_result(source_root_guard=root, mutate=source)

    def test_source_guard_leaves_source_unchanged_in_normal_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text("original\n")
            run_result(source_root_guard=root)
            self.assertEqual(source.read_text(), "original\n")

    def test_source_cannot_supply_an_untrusted_execution_skill(self):
        untrusted = replace(skill(), trust_classification="source-supplied")
        with self.assertRaisesRegex(SourceToSpecificationError, "not authorized"):
            SourceToSpecificationWorkflow().run(
                request=request(),
                skill_set=skill_set(untrusted),
                skill_catalog={untrusted.skill_id: untrusted},
                surfaces=(BehaviorSurface("surface", "behavior", "Behavior"),),
                evidence=(evidence(),),
                execute_skill=lambda *_args: (),
                render_draft=renderer,
                validate_provider=validator,
            )


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

    def test_default_policy_rejects_automated_authority(self):
        result = run_result()
        with self.assertRaisesRegex(SourceToSpecificationError, "authority"):
            promote(
                result,
                self.review_for(result, authority=ReviewAuthority.POLICY),
                PromotionPolicy("human-reviewed"),
                expected_base_specification_set_id=None,
            )

    def test_unresolved_or_unsupported_source_behavior_blocks_promotion(self):
        result = run_result(claim_kinds=(ClaimKind.UNKNOWN, ClaimKind.OBSERVED))
        resolved = tuple(item.uncertainty_id for item in result.uncertainty.items)
        with self.assertRaisesRegex(SourceToSpecificationError, "coverage policy"):
            promote(
                result,
                self.review_for(result, resolve=resolved),
                PromotionPolicy("human-reviewed"),
                expected_base_specification_set_id=None,
            )

    def test_refresh_invalidates_only_observations_for_changed_paths(self):
        old_request = request(
            mode=RunMode.AUDIT,
            previous="specification-set-1",
        )
        result = run_result(source_request=old_request)
        new_request = request(
            snapshot="snapshot-2",
            content_digest="sha256:source-2",
            mode=RunMode.REFRESH,
            previous="specification-set-1",
        )
        selected_skill = skill()
        invalidation = plan_refresh(
            result,
            new_request,
            previous_skill_set=skill_set(selected_skill),
            new_skill_set=skill_set(selected_skill),
            current_evidence_digests={
                "evidence-a": "sha256:evidence-a",
                "evidence-b": "sha256:evidence-b",
            },
            changed_source_paths=("src/a.py",),
        )
        self.assertEqual(invalidation.invalidated_observation_ids, ("observation-0",))
        self.assertEqual(invalidation.retained_observation_ids, ("observation-1",))
        self.assertEqual(
            invalidation.invalidated_statement_ids,
            ("statement:observation-0",),
        )

    def test_skill_identity_change_invalidates_its_observations(self):
        result = run_result()
        old_skill = skill()
        new_skill = skill(content_digest="sha256:skill-2")
        invalidation = plan_refresh(
            result,
            request(),
            previous_skill_set=skill_set(old_skill),
            new_skill_set=skill_set(new_skill),
            current_evidence_digests={
                "evidence-a": "sha256:evidence-a",
                "evidence-b": "sha256:evidence-b",
            },
        )
        self.assertEqual(
            invalidation.invalidated_observation_ids,
            ("observation-0", "observation-1"),
        )


if __name__ == "__main__":
    unittest.main()

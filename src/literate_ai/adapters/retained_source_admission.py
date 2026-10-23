"""Bind live retained qualification to portable verifier-owned source admission."""

from __future__ import annotations

import hashlib

from literate_ai.application.standard_source_admission import (
    StandardSourceAdmissionError,
    StandardSourceAdmissionRequest,
    StandardSourceAdmissionService,
    StandardSourceVerification,
)
from literate_ai.contracts import (
    ComponentExecutionPlan,
    ContentIdentity,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
    canonical_identity,
)

from .retained_source import RetainedSourceInput


def completed_retained_source_admitter(
    retained: RetainedSourceInput,
    execution_plan: ComponentExecutionPlan,
    framework_distribution_identity: ContentIdentity,
    *,
    authored_assets=(),
):
    """Prepare admission for the same atomic entry as complete live acceptance."""

    def admit(publication, key, entry):
        generation = entry.standard_source_membership.generation
        candidate = generation.output.candidate
        provenance = generation.output.provenance
        retained.require_unchanged()
        if (
            provenance.retained_source_identity != retained.identity
            or provenance.component_lock_identity != retained.component_lock_identity
            or execution_plan.component_lock_identity
            != retained.component_lock_identity
        ):
            raise StandardSourceAdmissionError(
                "source_admission.retained_origin_mismatch",
                "live qualification does not bind the exact reviewed retained tree",
            )
        plan = next(
            item
            for item in execution_plan.generation_plans
            if item.component_revision == publication.component_revision
        )
        selected_assets = tuple(
            asset
            for asset in authored_assets
            if asset.component_revision == candidate.component_revision
        )
        expected = {
            path: (hashlib.sha256(content).hexdigest(), len(content))
            for path, content in retained.files
        }
        for asset in selected_assets:
            if asset.path in expected:
                raise StandardSourceAdmissionError(
                    "source_admission.retained_asset_collision",
                    "locked assets collide with reviewed retained source",
                )
            expected[asset.path] = (
                ContentIdentity.parse_uri(asset.blob.identity).digest,
                asset.blob.size,
            )
        actual = {
            item.path: (
                ContentIdentity.parse_uri(item.blob.identity).digest,
                item.blob.size,
            )
            for item in entry.source_files
        }
        if actual != expected or {a.identity for a in selected_assets} != set(
            plan.generation_key.asset_identities
        ):
            raise StandardSourceAdmissionError(
                "source_admission.retained_tree_mismatch",
                "admitted bytes differ from reviewed source and locked authored assets",
            )
        tests = publication.generated_test_evidence

        class CompletedVerifier:
            def verify(self, observed_generation):
                retained.require_unchanged()
                if observed_generation != generation:
                    raise StandardSourceAdmissionError(
                        "source_admission.retained_generation_mismatch",
                        "completed verifier facts bind another source generation",
                    )
                return StandardSourceVerification(
                    candidate.source_manifest_identity,
                    tests.generated_test_suite_identity,
                    (
                        StandardSourceTestResult(
                            tests.runner_identity,
                            tests.identity,
                            tests.selected_count,
                            tests.passed_count,
                            tests.executed_count - tests.passed_count,
                            tests.selected_count - tests.executed_count,
                        ),
                    ),
                    canonical_identity(
                        {
                            "schema": (
                                "literate-ai/completed-retained-source-verifier@1"
                            ),
                            "framework_distribution_identity": (
                                framework_distribution_identity.uri
                            ),
                            "review_identity": retained.identity.uri,
                            "acceptance_evidence_identity": (
                                publication.acceptance_evidence.identity.uri
                            ),
                        }
                    ),
                )

        membership = StandardSourceAdmissionService(CompletedVerifier()).prepare(
            StandardSourceAdmissionRequest(
                generation=generation,
                cache_key=key,
                flavor_set_identity=plan.generation_key.flavor_selection_identity,
                skill_closure_identity=canonical_identity(
                    {
                        "schema": "literate-ai/generation-skill-closure@1",
                        "skill_identities": [
                            i.uri for i in plan.generation_key.skill_identities
                        ],
                    }
                ),
                coding_cli_tool_binding_identity=key.coding_cli_tool_binding_identity,
                coding_cli_transcript_identity=None,
                retained_source_review_identity=retained.identity,
                source_selectors=StandardSourceSelectorSet(
                    StandardSourceSelectorScope.TARGET_SPECIFIC,
                    (StandardSourceSelector("target.profile", retained.target),),
                ),
                framework_distribution_identity=framework_distribution_identity,
            )
        )
        return membership.evidence

    return admit

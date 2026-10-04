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
)
from literate_ai.contracts import (
    GeneratedSourceCandidate,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardSourceAdmissionMembership,
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


class StandardSourceAdmissionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

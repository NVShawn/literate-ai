from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_standard_source_cache_roundtrip``."""






from dataclasses import replace








from literate_ai.contracts import (
    ContentIdentity,
    GeneratedSourceCandidate,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardSourceAdmissionCacheEntry,
    StandardSourceAdmissionEvidence,
    StandardSourceAdmissionMembership,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
)



from literate_ai.storage import FileSystemCAS

from tests.support.fixtures_test_source_cache import (
    _accepted_entry,
    _identity,
)



def _source_admission_entry(
    cas: FileSystemCAS,
    cache_key: SourceDerivationCacheKey | None = None,
) -> StandardSourceAdmissionCacheEntry:
    legacy = _accepted_entry(cas, suffix="source-admission")
    key = cache_key or replace(
        legacy.derivation.cache_key,
        source_semantics_identity=_identity("source-admission-semantics"),
    )
    manifest = cas.put_manifest(
        {"schema": "literate-ai/generated-source-manifest@1", "files": []}
    )
    candidate = GeneratedSourceCandidate(
        _identity("source-admission-component"),
        _identity("source-admission-orchestration-request"),
        key.request_identity,
        _identity("source-admission-plan"),
        _identity("source-admission-generation-key"),
        _identity("source-admission-context"),
        _identity("source-admission-prompt"),
        key.recipe_identity,
        _identity("source-admission-workspace"),
        legacy.derivation.source_tree_identity,
        _identity("source-admission-bundle"),
        ContentIdentity.parse_uri(manifest.identity),
        legacy.derivation.source_sbom_identity,
        legacy.derivation.generated_test_suite_identity,
    )
    provenance = SourceGenerationProvenance(
        candidate.source_generation_request_identity,
        candidate.planned_coding_cli_request_identity,
        legacy.derivation.component_lock_identity,
        _identity("source-admission-application"),
        candidate.component_revision,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.context_manifest_identity,
        candidate.prompt_identity,
        candidate.recipe_identity,
        candidate.workspace_allocation_identity,
        _identity("source-admission-readiness"),
        (_identity("source-admission-route"),),
        (_identity("source-admission-model-output"),),
        candidate.identity,
    )
    output = SourceGenerationRunOutput(
        candidate, candidate.identity, provenance, provenance.identity
    )
    generation = SourceGenerationResumeCandidate(
        output,
        output.identity,
        _identity("source-admission-budget"),
        _identity("source-admission-complexity"),
    )
    evidence = StandardSourceAdmissionEvidence(
        candidate.component_revision,
        provenance.component_lock_identity,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.recipe_identity,
        candidate.source_generation_request_identity,
        candidate.planned_coding_cli_request_identity,
        _identity("source-admission-flavors"),
        _identity("source-admission-skills"),
        candidate.tree_identity,
        candidate.source_bundle_identity,
        candidate.source_manifest_identity,
        candidate.source_bom_identity,
        candidate.generated_test_suite_identity,
        key.coding_cli_tool_binding_identity,
        _identity("source-admission-transcript"),
        provenance.identity,
        _identity("source-admission-test-plan"),
        (
            StandardSourceTestResult(
                _identity("source-admission-oracle"),
                _identity("source-admission-result"),
                150,
                150,
                0,
                0,
            ),
        ),
        StandardSourceSelectorSet(StandardSourceSelectorScope.TARGET_INDEPENDENT, ()),
        _identity("source-admission-framework"),
        _identity("source-admission-verifier"),
        "2026-08-14T00:00:00Z",
    )
    membership = StandardSourceAdmissionMembership(generation, evidence)
    return StandardSourceAdmissionCacheEntry(
        key,
        membership,
        legacy.source_files,
        manifest,
        legacy.source_sbom,
        legacy.generated_test_suite,
        cas.put_manifest(provenance.to_dict()),
        cas.put_manifest(evidence.semantic_dict()),
    )


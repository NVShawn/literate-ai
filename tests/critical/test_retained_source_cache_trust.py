"""Restored retained source keeps its review and never carries trusted acceptance."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    FilesystemStandardAcceptedSourcePublisher,
    FilesystemStandardSourceRestorer,
    SourceCacheMaterializer,
    SourceCacheResolver,
    restore_standard_source_cache_membership,
)
from literate_ai.application import (
    StandardAcceptedSourcePublication,
)
from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    GeneratedSourceCandidate,
    SourceCacheMode,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardAcceptedSourceCacheEntry,
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestCaseEvidence,
    StandardGeneratedTestExecutionEvidence,
)
from literate_ai.contracts.standard_lifecycle import (
    StandardSourceCacheMembershipDocument,
)
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_cache import (
    _accepted_entry,
    _configuration,
    _identity,
    _target,
)
from tests.support.fixtures_test_standard_post_source_evidence import _export


def _standard_entry(
    cas: FileSystemCAS, *, suffix: str = "standard", retained: bool = False
) -> StandardAcceptedSourceCacheEntry:
    legacy = _accepted_entry(cas, suffix=suffix)
    derivation = legacy.derivation
    key = derivation.cache_key
    review = _identity(f"{suffix}-retained-review") if retained else None
    if review is not None:
        key = replace(key, request_identity=review, source_semantics_identity=review)
        derivation = replace(derivation, cache_key=key)
    candidate = GeneratedSourceCandidate(
        component_revision=_identity(f"{suffix}-component-revision"),
        # This is the bounded framework request, not the coding-agent prompt
        # identity stored in SourceDerivationCacheKey.request_identity.
        source_generation_request_identity=_identity(f"{suffix}-source-request"),
        planned_coding_cli_request_identity=key.request_identity,
        component_generation_plan_identity=_identity(f"{suffix}-generation-plan"),
        generation_key_identity=_identity(f"{suffix}-generation-key"),
        context_manifest_identity=_identity(f"{suffix}-context"),
        prompt_identity=_identity(f"{suffix}-prompt"),
        recipe_identity=key.recipe_identity,
        workspace_allocation_identity=_identity(f"{suffix}-workspace"),
        tree_identity=derivation.source_tree_identity,
        source_bundle_identity=_identity(f"{suffix}-source-bundle"),
        source_manifest_identity=_identity(f"{suffix}-source-manifest"),
        source_bom_identity=derivation.source_sbom_identity,
        generated_test_suite_identity=derivation.generated_test_suite_identity,
    )
    provenance = SourceGenerationProvenance(
        candidate.source_generation_request_identity,
        candidate.planned_coding_cli_request_identity,
        derivation.component_lock_identity,
        _identity(f"{suffix}-application-root"),
        candidate.component_revision,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.context_manifest_identity,
        candidate.prompt_identity,
        candidate.recipe_identity,
        candidate.workspace_allocation_identity,
        _identity(f"{suffix}-readiness"),
        () if retained else (_identity(f"{suffix}-route"),),
        () if retained else (_identity(f"{suffix}-model-output"),),
        candidate.identity,
        retained_source_identity=review,
    )
    provenance_evidence = cas.put_manifest(provenance.to_dict())
    assert provenance_evidence.identity == provenance.identity.uri
    output = SourceGenerationRunOutput(
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
    )
    resume = SourceGenerationResumeCandidate(
        output,
        output.identity,
        _identity(f"{suffix}-complexity-budget"),
        _identity(f"{suffix}-complexity-decision"),
    )
    membership = StandardSourceCacheMembershipDocument(
        candidate.component_revision,
        candidate.generation_key_identity,
        resume,
        derivation.acceptance_identity,
    )
    updated_derivation = replace(derivation, provenance_identity=provenance.identity)
    values = {
        item.name: getattr(legacy, item.name)
        for item in fields(AcceptedSourceCacheEntry)
    }
    values.update(
        derivation=updated_derivation,
        provenance_evidence=provenance_evidence,
        standard_source_membership=membership,
    )
    return StandardAcceptedSourceCacheEntry(**values)


def _standard_publication(
    entry: StandardAcceptedSourceCacheEntry, cas: FileSystemCAS, *, suffix: str
) -> StandardAcceptedSourcePublication:
    """Build one complete accepted-publication object for the given cache entry."""

    membership = restore_standard_source_cache_membership(entry)
    assert membership is not None
    output = membership.generation.output
    component = membership.component_revision
    export = _export(component, output.candidate.tree_identity)
    build = StandardBuildEvidence(
        component,
        _identity(f"{suffix}-build-plan"),
        output.candidate.tree_identity,
        _identity(f"{suffix}-source-custody"),
        entry.derivation.source_sbom_binding,
        entry.derivation.resolved_sbom_binding,
        (export,),
        (export.identity,),
        _identity(f"{suffix}-build-observation"),
        _identity(f"{suffix}-artifact-custody"),
    )
    case = StandardGeneratedTestCaseEvidence(
        f"{suffix}-case",
        _identity(f"{suffix}-case-identity"),
        _identity(f"{suffix}-case-observation"),
    )
    generated_tests = StandardGeneratedTestExecutionEvidence(
        component,
        output.candidate.generated_test_suite_identity,
        build.identity,
        build.export_identities,
        _identity(f"{suffix}-test-runner"),
        _identity(f"{suffix}-test-custody"),
        (case.case_identity,),
        (case,),
        1,
        1,
        1,
    )
    execution = StandardExecutionEvidence(
        component,
        build.identity,
        build.export_identities,
        export.identity,
        _identity(f"{suffix}-execution-contract"),
        _identity(f"{suffix}-runtime"),
        build.artifact_custody_identity,
        _identity(f"{suffix}-execution-observation"),
        _identity(f"{suffix}-stdout"),
        _identity(f"{suffix}-stderr"),
        0,
    )
    acceptance = StandardComponentAcceptanceEvidence(
        component,
        output.identity,
        generated_tests.generated_test_suite_identity,
        build,
        generated_tests,
        execution,
        _identity(f"{suffix}-acceptance-policy"),
    )
    membership = replace(membership, acceptance_identity=acceptance.identity)
    return StandardAcceptedSourcePublication(
        component,
        build.build_plan_identity,
        output,
        (export,),
        _identity(f"{suffix}-index"),
        export.authorization_identity,
        membership,
        build,
        generated_tests,
        execution,
        acceptance,
    )


class RetainedSourceCacheTrustTests(unittest.TestCase):
    def test_retained_publication_roundtrip_keeps_review_and_untrusted_acceptance(self):
        from literate_ai.adapters.cache.standard import (
            StandardSourceCachePublicationError,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "cas")
            entry = _standard_entry(cas, retained=True)
            publication = _standard_publication(entry, cas, suffix="retained")
            source = root / "source"
            expected = {}
            for item in entry.source_files:
                path = source / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                expected[item.path] = cas.get_bytes(item.blob)
                path.write_bytes(expected[item.path])
            key = entry.derivation.cache_key
            cache = FileSystemSourceCache("standard", root / "cache")
            publisher = FilesystemStandardAcceptedSourcePublisher(
                cache=cache,
                caller_cas=cas,
                source_root=lambda _: source,
                source_custody=lambda _: SimpleNamespace(
                    managed_graph=entry.managed_sbom_graph,
                    source_bom_content=cas.get_bytes(entry.source_sbom),
                    generated_test_suite_content=cas.get_bytes(
                        entry.generated_test_suite
                    ),
                ),
                resolved_sbom_content=lambda _: cas.get_bytes(entry.resolved_sbom),
                cache_key=lambda _: key,
            )
            review = publication.source_output.provenance.retained_source_identity
            for changed in (
                replace(key, request_identity=_identity("model-request")),
                replace(key, source_semantics_identity=_identity("model-semantics")),
            ):
                publisher.cache_key = lambda _, changed=changed: changed
                with self.assertRaises(StandardSourceCachePublicationError):
                    publisher.publish_accepted(publication)
                self.assertEqual(cache.published_entries(), ())
            publisher.cache_key = lambda _: key
            publisher.publish_accepted(publication)
            shutil.rmtree(source)
            shutil.rmtree(cas.root)
            reader = FileSystemSourceCache("standard", root / "cache", writable=False)
            restorer = FilesystemStandardSourceRestorer(
                resolver=SourceCacheResolver(
                    _configuration(SourceCacheMode.READ_ONLY, _target("standard")),
                    {"standard": reader},
                ),
                materializer=SourceCacheMaterializer(),
            )
            restored = restorer.restore(
                key,
                root / "restored",
                component_lock_identity=entry.derivation.component_lock_identity,
            )
            self.assertIsNotNone(restored)
            self.assertFalse(restored.current_acceptance_trusted)
            provenance = restored.membership.generation.output.provenance
            self.assertEqual(provenance.retained_source_identity, review)
            self.assertEqual(provenance.model_stage_output_identities, ())
            self.assertEqual(provenance.route_decision_identities, ())
            self.assertEqual(provenance.provider_evidence_identities, ())
            self.assertEqual(
                {
                    path.relative_to(restored.source_root).as_posix(): path.read_bytes()
                    for path in restored.source_root.rglob("*")
                    if path.is_file()
                },
                expected,
            )
            self.assertEqual(
                reader.candidates(
                    replace(key, source_semantics_identity=_identity("other-review"))
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()

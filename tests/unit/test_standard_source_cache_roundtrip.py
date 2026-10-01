"""Accepted-cache round trip for complete Standard source-generation evidence."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    FilesystemStandardAcceptedSourcePublisher,
    FilesystemStandardSourceRestorer,
    SourceCacheError,
    SourceCacheMaterializer,
    SourceCacheResolver,
    restore_standard_source_cache_membership,
)
from literate_ai.adapters.cache.filesystem import _native_filesystem_path
from literate_ai.adapters.source_materialization import (
    capture_accepted_source_cache_archive,
)
from literate_ai.application import (
    StandardAcceptedSourcePublication,
    StandardProjectLifecycleError,
)
from literate_ai.contracts import (
    STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA,
    AcceptedSourceCacheEntry,
    ContentIdentity,
    ContractValidationError,
    GeneratedSourceCandidate,
    SourceCacheMode,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardAcceptedSourceCacheEntry,
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestCaseEvidence,
    StandardGeneratedTestExecutionEvidence,
    StandardSourceAdmissionCacheEntry,
    StandardSourceAdmissionEvidence,
    StandardSourceAdmissionMembership,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
)
from literate_ai.contracts.standard_lifecycle import (
    StandardSourceCacheMembershipDocument,
)
from literate_ai.remote_source_guard import extract_accepted_source_cache_archive
from literate_ai.storage import FileSystemCAS
from tests.unit.test_source_cache import (
    _accepted_entry,
    _cache_key,
    _configuration,
    _identity,
    _target,
)
from tests.unit.test_standard_post_source_evidence import _export
from tests.unit.test_wire_contract_versions import _v2_schemas


class _FabricatedAcceptedSourceCacheEntry(AcceptedSourceCacheEntry):
    pass


def _standard_entry(
    cas: FileSystemCAS, *, suffix: str = "standard"
) -> StandardAcceptedSourceCacheEntry:
    legacy = _accepted_entry(cas, suffix=suffix)
    derivation = legacy.derivation
    key = derivation.cache_key
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
        (_identity(f"{suffix}-route"),),
        (_identity(f"{suffix}-model-output"),),
        candidate.identity,
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


class StandardSourceCacheRoundTripTests(unittest.TestCase):
    def test_source_admission_contracts_match_public_schemas(self):
        from tests.unit.test_schema_catalog import SchemaCatalog

        with tempfile.TemporaryDirectory() as temporary:
            entry = _source_admission_entry(
                FileSystemCAS(Path(temporary).resolve() / "cas")
            )
        schemas = SchemaCatalog()
        for value in (entry.membership.evidence, entry.membership, entry):
            with self.subTest(schema=value.SCHEMA):
                schemas.validate(value.SCHEMA, value.to_dict())

    def test_source_admission_publishes_without_downstream_build_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "cas")
            cache = FileSystemSourceCache("runtime", root / "cache")
            entry = _source_admission_entry(cas)

            published = cache.publish(entry, caller_cas=cas)
            self.assertEqual(published, entry.identity)
            self.assertEqual(cache.published_entries(), (entry,))
            fresh_session_key = replace(
                entry.cache_key,
                request_identity=_identity("fresh-inherited-session-request"),
            )
            self.assertNotEqual(fresh_session_key, entry.cache_key)
            self.assertEqual(
                fresh_session_key.accepted_source_lookup_identity,
                entry.cache_key.accepted_source_lookup_identity,
            )
            self.assertEqual(cache.candidates(fresh_session_key), (entry,))
            for field, value in (
                ("recipe_identity", _identity("changed-recipe-or-lock")),
                ("execution_plan_identity", _identity("changed-plan-or-policy")),
                (
                    "coding_cli_tool_binding_identity",
                    _identity("changed-provider-tool"),
                ),
                (
                    "model_binding",
                    replace(
                        entry.cache_key.model_binding,
                        provider_id="changed-provider",
                    ),
                ),
            ):
                with self.subTest(field=field):
                    self.assertEqual(
                        cache.candidates(replace(fresh_session_key, **{field: value})),
                        (),
                    )
            restored = restore_standard_source_cache_membership(entry)
            self.assertEqual(restored, entry.membership)
            self.assertFalse(hasattr(entry, "build_evidence"))
            self.assertFalse(hasattr(entry, "resolved_sbom"))

    def test_twelve_published_keys_round_trip_through_portable_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            try:
                project = root / ("project with spaces " + ("long-" * 20))
                staging = root / "staging"
                project.mkdir()
                staging.mkdir()
                caller_cas = FileSystemCAS(root / "caller-cas")
                cache_root = project / "generated" / "accepted-source-cache"
                coordinator = FileSystemSourceCache("runtime", cache_root)
                entries = tuple(
                    _accepted_entry(
                        caller_cas,
                        suffix=f"node-{index}",
                        key=_cache_key(selector=f"model-node-{index}"),
                    )
                    for index in range(12)
                )
                for entry in entries:
                    coordinator.publish(entry, caller_cas=caller_cas)

                with patch.dict(os.environ, {"BUILD_DIR": str(project / "generated")}):
                    reference = capture_accepted_source_cache_archive(
                        project, directory=staging
                    )
                self.assertIsNotNone(reference)
                archive = staging / "accepted-source-cache.tar.gz"
                with tarfile.open(archive, "r:gz") as stream:
                    names = tuple(item.name for item in stream.getmembers())
                self.assertTrue(names)
                self.assertFalse(any("\\" in name for name in names))

                restored_root = root / "short build root" / "accepted-source-cache"
                restored_root.parent.mkdir()
                extract_accepted_source_cache_archive(archive, restored_root)
                restored = FileSystemSourceCache("runtime", restored_root)
                long_lookup = (
                    cache_root
                    / "keys"
                    / "sha256"
                    / entries[0].derivation.cache_key.identity.digest
                    / f"{entries[0].identity.digest}.json"
                )
                self.assertGreater(len(str(long_lookup)), 260)
                original_read_bytes = Path.read_bytes

                def reject_checkout_cache_lookup(path: Path) -> bytes:
                    if (
                        Path(_native_filesystem_path(path.absolute()))
                        .resolve()
                        .is_relative_to(
                            Path(_native_filesystem_path(cache_root)).resolve()
                        )
                    ):
                        raise AssertionError(
                            "restored lookup touched the long request-checkout cache"
                        )
                    return original_read_bytes(path)

                with patch.object(Path, "read_bytes", reject_checkout_cache_lookup):
                    candidates = tuple(
                        candidate
                        for entry in entries
                        for candidate in restored.candidates(entry.derivation.cache_key)
                    )
                self.assertEqual(
                    len(candidates),
                    12,
                )
                self.assertEqual(
                    len(tuple((restored_root / "keys" / "sha256").glob("*/*.json"))),
                    12,
                )
                self.assertEqual(
                    {candidate.identity for candidate in candidates},
                    {entry.identity for entry in entries},
                )
            finally:
                shutil.rmtree(Path(_native_filesystem_path(root)))

    def test_complete_publication_binds_every_durable_cache_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "cas")
            entry = _standard_entry(cas)
            membership = restore_standard_source_cache_membership(entry)
            assert membership is not None
            output = membership.generation.output
            component = membership.component_revision
            export = _export(component, output.candidate.tree_identity)
            build = StandardBuildEvidence(
                component,
                _identity("publication-build-plan"),
                output.candidate.tree_identity,
                _identity("publication-source-custody"),
                entry.derivation.source_sbom_binding,
                entry.derivation.resolved_sbom_binding,
                (export,),
                (export.identity,),
                _identity("publication-build-observation"),
                _identity("publication-artifact-custody"),
            )
            case = StandardGeneratedTestCaseEvidence(
                "publication-case",
                _identity("publication-case-identity"),
                _identity("publication-case-observation"),
            )
            generated_tests = StandardGeneratedTestExecutionEvidence(
                component,
                output.candidate.generated_test_suite_identity,
                build.identity,
                build.export_identities,
                _identity("publication-test-runner"),
                _identity("publication-test-custody"),
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
                _identity("publication-execution-contract"),
                _identity("publication-runtime"),
                build.artifact_custody_identity,
                _identity("publication-execution-observation"),
                _identity("publication-stdout"),
                _identity("publication-stderr"),
                0,
            )
            acceptance = StandardComponentAcceptanceEvidence(
                component,
                output.identity,
                generated_tests.generated_test_suite_identity,
                build,
                generated_tests,
                execution,
                _identity("publication-acceptance-policy"),
            )
            membership = replace(membership, acceptance_identity=acceptance.identity)

            publication = StandardAcceptedSourcePublication(
                component,
                build.build_plan_identity,
                output,
                (export,),
                _identity("publication-index"),
                export.authorization_identity,
                membership,
                build,
                generated_tests,
                execution,
                acceptance,
            )

            self.assertEqual(publication.membership, membership)
            self.assertEqual(publication.acceptance_evidence, acceptance)
            with self.assertRaisesRegex(
                StandardProjectLifecycleError, "complete accepted node"
            ):
                replace(
                    publication,
                    index_identity=_identity("changed-index"),
                    exports=(),
                )
            with self.assertRaisesRegex(
                StandardProjectLifecycleError, "complete accepted node"
            ):
                replace(
                    publication,
                    build_plan_identity=_identity("substituted-build-plan"),
                )

            source_root = root / "accepted-source"
            for source_file in entry.source_files:
                destination = source_root.joinpath(*Path(source_file.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(cas.path_for(source_file.blob).read_bytes())
            cache = FileSystemSourceCache("standard", root / "cache")
            publisher = FilesystemStandardAcceptedSourcePublisher(
                cache=cache,
                caller_cas=cas,
                source_root=lambda _identity: source_root,
                source_custody=lambda _identity: SimpleNamespace(
                    managed_graph=entry.managed_sbom_graph,
                    source_bom_content=cas.path_for(entry.source_sbom).read_bytes(),
                    generated_test_suite_content=cas.path_for(
                        entry.generated_test_suite
                    ).read_bytes(),
                ),
                resolved_sbom_content=lambda _publication: cas.path_for(
                    entry.resolved_sbom
                ).read_bytes(),
                cache_key=lambda _publication: entry.derivation.cache_key,
            )

            published = publisher.publish_accepted(publication)
            candidates = cache.candidates(entry.derivation.cache_key)

            self.assertEqual(published, membership.identity)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                tuple(item.path for item in candidates[0].source_files),
                tuple(sorted(item.path for item in candidates[0].source_files)),
            )
            self.assertEqual(
                restore_standard_source_cache_membership(candidates[0]), membership
            )

            # Re-open the durable store to prove the read does not depend on the
            # publishing process or any of its in-memory evidence objects.
            reader_store = FileSystemSourceCache(
                "standard", root / "cache", writable=False
            )
            resolver = SourceCacheResolver(
                _configuration(
                    SourceCacheMode.READ_ONLY,
                    _target("standard"),
                ),
                {"standard": reader_store},
            )
            restorer = FilesystemStandardSourceRestorer(
                resolver=resolver,
                materializer=SourceCacheMaterializer(),
            )
            (root / "restored-source").mkdir()
            restored = restorer.restore(
                entry.derivation.cache_key,
                root / "restored-source",
                component_lock_identity=entry.derivation.component_lock_identity,
            )

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.membership, membership)
            self.assertEqual(restored.entry_identity, candidates[0].identity)
            self.assertFalse(restored.current_acceptance_trusted)
            self.assertEqual(
                {
                    item.relative_to(restored.source_root).as_posix(): item.read_bytes()
                    for item in restored.source_root.rglob("*")
                    if item.is_file()
                },
                {
                    item.relative_to(source_root).as_posix(): item.read_bytes()
                    for item in source_root.rglob("*")
                    if item.is_file()
                },
            )
            bypass = restorer.restore(
                entry.derivation.cache_key,
                root / "forced-source",
                component_lock_identity=entry.derivation.component_lock_identity,
                force_regeneration=True,
            )
            self.assertIsNone(bypass)
            self.assertFalse((root / "forced-source").exists())

    def test_forced_regeneration_accepts_self_heal_a_plain_test_run(self) -> None:
        """Reproduce #137 end to end through the real Standard publisher.

        Two `--force-regeneration` accepts of the same Component land two
        accepted entries at the exact same source-cache key -- exactly the
        accumulation the issue reports across flaky retries. Without retiring
        the superseded membership, a subsequent plain (non-forced) resolve
        fails with `source-cache.ambiguous`. The publisher's
        `retire_prior_membership`, wired for forced-regeneration accepts,
        must self-heal that plain resolve without discarding any immutable
        accepted-source content.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "cas")
            cache = FileSystemSourceCache("standard", root / "cache")

            def _write_source(entry, suffix: str) -> Path:
                source_root = root / f"accepted-source-{suffix}"
                for source_file in entry.source_files:
                    destination = source_root.joinpath(*Path(source_file.path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(cas.path_for(source_file.blob).read_bytes())
                return source_root

            first_entry = _standard_entry(cas, suffix="attempt-one")
            second_entry = _standard_entry(cas, suffix="attempt-two")
            self.assertEqual(
                first_entry.derivation.cache_key, second_entry.derivation.cache_key
            )
            self.assertNotEqual(first_entry.identity, second_entry.identity)

            roots = {
                first_entry.derivation.source_tree_identity: _write_source(
                    first_entry, "attempt-one"
                ),
                second_entry.derivation.source_tree_identity: _write_source(
                    second_entry, "attempt-two"
                ),
            }
            by_entry = {
                first_entry.derivation.source_tree_identity: first_entry,
                second_entry.derivation.source_tree_identity: second_entry,
            }

            publisher = FilesystemStandardAcceptedSourcePublisher(
                cache=cache,
                caller_cas=cas,
                source_root=lambda identity: roots[identity],
                source_custody=lambda identity: SimpleNamespace(
                    managed_graph=by_entry[identity].managed_sbom_graph,
                    source_bom_content=cas.path_for(
                        by_entry[identity].source_sbom
                    ).read_bytes(),
                    generated_test_suite_content=cas.path_for(
                        by_entry[identity].generated_test_suite
                    ).read_bytes(),
                ),
                resolved_sbom_content=lambda publication: cas.path_for(
                    by_entry[
                        publication.source_output.candidate.tree_identity
                    ].resolved_sbom
                ).read_bytes(),
                cache_key=lambda publication: (
                    by_entry[
                        publication.source_output.candidate.tree_identity
                    ].derivation.cache_key
                ),
            )

            first_publication = _standard_publication(first_entry, cas, suffix="one")
            first_published = publisher.publish_accepted(first_publication)
            self.assertEqual(first_published, first_publication.membership.identity)

            # A retried `--force-regeneration` accepts a second, distinct entry at
            # the exact same key -- the accumulation reported in #137.
            second_publication = _standard_publication(second_entry, cas, suffix="two")
            second_published = publisher.publish_accepted(second_publication)
            self.assertEqual(second_published, second_publication.membership.identity)

            published = {
                restore_standard_source_cache_membership(entry).identity: entry.identity
                for entry in cache.published_entries()
            }
            self.assertEqual(len(published), 2)
            first_entry_identity = published[first_publication.membership.identity]
            second_entry_identity = published[second_publication.membership.identity]
            self.assertNotEqual(first_entry_identity, second_entry_identity)

            key = first_entry.derivation.cache_key
            resolver = SourceCacheResolver(
                _configuration(SourceCacheMode.READ_ONLY, _target("standard")),
                {"standard": FileSystemSourceCache("standard", root / "cache")},
            )
            with self.assertRaises(SourceCacheError) as captured:
                resolver.resolve(key)
            self.assertEqual(captured.exception.code, "source-cache.ambiguous")

            # The forced-regeneration accept path retires the superseded entry.
            publisher.retire_prior_membership(second_publication)

            healed_resolver = SourceCacheResolver(
                _configuration(SourceCacheMode.READ_ONLY, _target("standard")),
                {"standard": FileSystemSourceCache("standard", root / "cache")},
            )
            healed = healed_resolver.resolve(key)
            self.assertEqual(len(healed), 1)
            self.assertEqual(healed[0].identity, second_entry_identity)

            # The retired entry is no longer an exact-key candidate -- explicit
            # selection reports "not found" rather than resurrecting it.
            with self.assertRaises(SourceCacheError) as missing:
                healed_resolver.resolve(key, entry_identity=first_entry_identity)
            self.assertEqual(missing.exception.code, "source-cache.entry-not-found")

            # Retirement only prunes the membership index -- the immutable entry
            # manifest itself remains in the content-addressed store, untouched.
            self.assertTrue(
                (cache.entries / f"{first_entry_identity.digest}.json").is_file()
            )

    def test_standard_entry_round_trips_complete_typed_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cas = FileSystemCAS(Path(temporary) / "cas")
            entry = _standard_entry(cas)

            parsed = StandardAcceptedSourceCacheEntry.from_dict(entry.to_dict())
            restored = restore_standard_source_cache_membership(parsed)

            self.assertEqual(parsed, entry)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.to_document(), entry.standard_source_membership)
            self.assertEqual(
                restored.generation.output,
                entry.standard_source_membership.generation.output,
            )
            _v2_schemas().validate(
                STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA,
                entry.to_dict(),
            )

    def test_legacy_entry_is_readable_but_not_standard_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cas = FileSystemCAS(Path(temporary) / "cas")
            legacy = _accepted_entry(cas, suffix="legacy")

            parsed = AcceptedSourceCacheEntry.from_dict(legacy.to_dict())

            self.assertEqual(parsed, legacy)
            self.assertIsNone(restore_standard_source_cache_membership(parsed))

    def test_filesystem_dispatches_coexisting_entry_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "caller-cas")
            legacy = _accepted_entry(cas, suffix="legacy")
            standard = _standard_entry(cas)
            self.assertEqual(legacy.derivation.cache_key, standard.derivation.cache_key)
            cache = FileSystemSourceCache("fixture", root / "cache")

            cache.publish(legacy, caller_cas=cas)
            cache.publish(standard, caller_cas=cas)
            candidates = cache.candidates(legacy.derivation.cache_key)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                sum(
                    restore_standard_source_cache_membership(item) is not None
                    for item in candidates
                ),
                1,
            )
            self.assertEqual(
                {type(item) for item in candidates},
                {AcceptedSourceCacheEntry, StandardAcceptedSourceCacheEntry},
            )

    def test_standard_entry_rejects_membership_detached_from_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = _standard_entry(FileSystemCAS(Path(temporary) / "cas"))
            detached = replace(
                entry.standard_source_membership,
                acceptance_identity=_identity("another-acceptance"),
            )

            with self.assertRaisesRegex(
                ContractValidationError, "exact acceptance evidence"
            ):
                replace(entry, standard_source_membership=detached)

    def test_publisher_rejects_unversioned_entry_subclasses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cas = FileSystemCAS(root / "cas")
            legacy = _accepted_entry(cas, suffix="fabricated")
            values = {
                item.name: getattr(legacy, item.name)
                for item in fields(AcceptedSourceCacheEntry)
            }
            fabricated = _FabricatedAcceptedSourceCacheEntry(**values)
            cache = FileSystemSourceCache("fixture", root / "cache")

            with self.assertRaises(TypeError):
                cache.publish(fabricated, caller_cas=cas)

    def test_standard_reader_rejects_future_or_legacy_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = _standard_entry(FileSystemCAS(Path(temporary) / "cas"))
            future = entry.to_dict()
            future["schema"] = (
                "urn:literate-ai:schema:v3:standard-accepted-source-cache-entry"
            )
            legacy = dict(entry.to_dict())
            legacy["schema"] = "urn:literate-ai:schema:v2:accepted-source-cache-entry"

            with self.assertRaises(ContractValidationError):
                StandardAcceptedSourceCacheEntry.from_dict(future)
            with self.assertRaises(ContractValidationError):
                StandardAcceptedSourceCacheEntry.from_dict(legacy)


if __name__ == "__main__":
    unittest.main()

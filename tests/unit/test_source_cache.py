"""Immutable accepted-source cache contracts, resolution, and materialization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    SourceCacheError,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    AcceptedSourceCacheEntry,
    AcceptedSourceDerivation,
    CachedSourceFile,
    ComponentCoordinate,
    ComponentRevisionRef,
    ContentIdentity,
    ContractValidationError,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    DependencyKind,
    ManagedComponentKind,
    ProjectDefinition,
    ProjectSourceIntelligencePolicy,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheModelBinding,
    SourceCacheRootKind,
    SourceCacheTarget,
    SourceDerivationCacheKey,
    SourceIntelligenceArtifact,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceAttachment,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
    repository_dependency_identity,
)
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"label": label})


def _content_identity(value: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(value)


def _cache_key(*, selector: str = "model-a") -> SourceDerivationCacheKey:
    return SourceDerivationCacheKey(
        recipe_identity=_identity("recipe"),
        execution_plan_identity=_identity("execution-plan"),
        coding_cli_tool_binding_identity=_identity("coding-cli-tool-binding"),
        model_binding=SourceCacheModelBinding("test-provider", selector),
        request_identity=_identity("request"),
    )


def _evidence(cas: FileSystemCAS, kind: str, suffix: str):
    return cas.put_manifest(
        {
            "schema": f"urn:literate-ai:schema:v1:{kind}-evidence",
            "kind": kind,
            "suffix": suffix,
        }
    )


def _accepted_entry_with_attachment(
    cas: FileSystemCAS,
    *,
    suffix: str = "one",
    key: SourceDerivationCacheKey | None = None,
    managed_graph: CycloneDxManagedGraph | None = None,
    repository_resolutions: tuple[CycloneDxRepositorySourceResolution, ...] = (),
) -> tuple[AcceptedSourceCacheEntry, SourceIntelligenceAttachment]:
    selected_key = _cache_key() if key is None else key
    root_identity = _identity(f"component-{suffix}")
    root_ref = component_bom_ref(root_identity)
    if managed_graph is None:
        managed_graph = CycloneDxManagedGraph(
            root_ref,
            (
                CycloneDxManagedComponent(
                    root_ref,
                    ManagedComponentKind.ROOT,
                    root_identity,
                    "component://test/cache-fixture",
                    "1.0.0",
                    (),
                ),
            ),
            (),
            _identity(f"component-composition-{suffix}"),
        )
    source_sbom_bytes, source_sbom_binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=managed_graph,
    )
    resolved_sbom_bytes, resolved_sbom_binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.RESOLVED,
        managed_graph=managed_graph,
        source_bom=source_sbom_bytes,
        repository_resolutions=repository_resolutions,
    )
    files = {
        CYCLONEDX_SOURCE_SBOM_PATH: source_sbom_bytes,
        "source/main.py": f"print('cache-{suffix}')\n".encode(),
        "source/tests/test_main.py": (
            f"def test_value():\n    assert {suffix!r} == {suffix!r}\n".encode()
        ),
    }
    source_files = tuple(
        CachedSourceFile(
            path,
            cas.put_bytes(content, media_type="text/x-python"),
        )
        for path, content in sorted(files.items())
    )
    source_sbom = next(
        item.blob for item in source_files if item.path == CYCLONEDX_SOURCE_SBOM_PATH
    )
    resolved_sbom = cas.put_bytes(
        resolved_sbom_bytes,
        media_type="application/vnd.cyclonedx+json",
    )
    database_path = cas.root / f".fixture-intelligence-{suffix}.db"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("CREATE TABLE graph_fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO graph_fixture VALUES (?)", (suffix,))
    database = cas.put_file(
        database_path,
        media_type="application/vnd.sqlite3",
    )
    database_path.unlink()
    source_intelligence = SourceIntelligenceArtifact.create(
        provider_id="fixture-intelligence",
        provider_version="1.1.1",
        runtime_version="1.1.1",
        executable_identity=_identity("fixture-intelligence-executable").uri,
        source_tree_identity=generated_source_tree_identity(files),
        source_snapshot_identity=generated_source_snapshot_identity(files),
        capabilities=("call-graph", "declarations", "references"),
        provider_properties=("built-with-version=1.1.1", "extraction-version=1"),
        artifact_path="source-intelligence.sqlite",
        artifact_media_type="application/vnd.sqlite3",
        artifact_identity=database.identity,
        document_count=len(files),
        symbol_count=2,
        relationship_count=1,
        unresolved_relationship_count=0,
        warning_count=0,
    )
    generated_tests = _evidence(cas, "generated-test", suffix)
    build = _evidence(cas, "build", suffix)
    tests = _evidence(cas, "test", suffix)
    acceptance = _evidence(cas, "acceptance", suffix)
    provenance = _evidence(cas, "provenance", suffix)
    derivation = AcceptedSourceDerivation(
        cache_key=selected_key,
        component_lock_identity=managed_graph.resolved_graph_identity,
        source_tree_identity=_content_identity(
            source_intelligence.source_tree_identity
        ),
        managed_sbom_graph_identity=managed_graph.identity,
        source_sbom_identity=source_sbom_binding.bom_identity,
        resolved_sbom_identity=resolved_sbom_binding.bom_identity,
        source_sbom_binding=source_sbom_binding,
        resolved_sbom_binding=resolved_sbom_binding,
        generated_test_suite_identity=_content_identity(generated_tests.identity),
        build_evidence_identity=_content_identity(build.identity),
        test_evidence_identity=_content_identity(tests.identity),
        acceptance_identity=_content_identity(acceptance.identity),
        provenance_identity=_content_identity(provenance.identity),
    )
    entry = AcceptedSourceCacheEntry(
        derivation=derivation,
        source_files=source_files,
        managed_sbom_graph=managed_graph,
        source_sbom=source_sbom,
        resolved_sbom=resolved_sbom,
        generated_test_suite=generated_tests,
        build_evidence=build,
        test_evidence=tests,
        acceptance_evidence=acceptance,
        provenance_evidence=provenance,
        repository_resolutions=repository_resolutions,
    )
    attachment = SourceIntelligenceAttachment(
        source_tree_identity=derivation.source_tree_identity,
        intelligence=source_intelligence,
        artifact=database,
    )
    return entry, attachment


def _accepted_entry(
    cas: FileSystemCAS,
    *,
    suffix: str = "one",
    key: SourceDerivationCacheKey | None = None,
    managed_graph: CycloneDxManagedGraph | None = None,
    repository_resolutions: tuple[CycloneDxRepositorySourceResolution, ...] = (),
) -> AcceptedSourceCacheEntry:
    return _accepted_entry_with_attachment(
        cas,
        suffix=suffix,
        key=key,
        managed_graph=managed_graph,
        repository_resolutions=repository_resolutions,
    )[0]


def _target(
    target_id: str = "local",
    *,
    binding: str = "cache-root",
) -> SourceCacheTarget:
    return SourceCacheTarget(
        target_id=target_id,
        root_kind=SourceCacheRootKind.OPERATOR_BOUND,
        root_reference=binding,
    )


def _configuration(
    mode: SourceCacheMode,
    *targets: SourceCacheTarget,
    require_unique: bool = True,
) -> SourceCacheConfiguration:
    return SourceCacheConfiguration(
        mode=mode,
        targets=tuple(targets),
        write_target_id=(targets[0].target_id if mode.can_write else None),
        require_unique=require_unique,
    )


def _source_intelligence_policy() -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="none",
        command=None,
        minimum_version=None,
        artifact_path=None,
        stages=tuple(
            (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
        ),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


def _publish_legacy_v1_cache_root(
    root: Path,
    caller_cas: FileSystemCAS,
    entry: AcceptedSourceCacheEntry,
    attachment: SourceIntelligenceAttachment,
) -> None:
    """Write the exact frozen filesystem-v1 layout produced before detachment."""

    entries = root / "entries" / "sha256"
    keys = root / "keys" / "sha256"
    entries.mkdir(parents=True)
    keys.mkdir(parents=True)
    legacy_cas = FileSystemCAS(root / "cas")
    references = {
        *(source_file.blob for source_file in entry.source_files),
        entry.resolved_sbom,
        entry.generated_test_suite,
        entry.build_evidence,
        entry.test_evidence,
        entry.acceptance_evidence,
        entry.provenance_evidence,
        attachment.artifact,
    }
    for reference in references:
        self_stored = legacy_cas.put_file(
            caller_cas.path_for(reference), media_type=reference.media_type
        )
        if self_stored != reference:  # pragma: no cover - fixture invariant
            raise AssertionError("legacy fixture CAS changed an object")

    key_document = entry.derivation.cache_key.to_dict()
    key_document["schema"] = "urn:literate-ai:schema:v1:source-derivation-cache-key"
    model_binding = dict(key_document["model_binding"])
    model_binding["schema"] = "urn:literate-ai:schema:v1:source-cache-model-binding"
    key_document["model_binding"] = model_binding
    key_identity = canonical_identity(key_document)

    intelligence = attachment.intelligence
    properties = dict(item.split("=", 1) for item in intelligence.provider_properties)
    built_with = properties.get("built-with-version")
    extraction_text = properties.get("extraction-version")
    extraction = None if extraction_text is None else int(extraction_text)
    logical_material = {
        "schema": "urn:literate-ai:schema:v1:portable-codegraph-index-binding",
        "provider_id": intelligence.provider_id,
        "provider_version": intelligence.provider_version,
        "runtime_version": intelligence.runtime_version,
        "executable_identity": intelligence.executable_identity,
        "built_with_version": built_with,
        "extraction_version": extraction,
        "source_tree_identity": intelligence.source_tree_identity,
        "source_snapshot_identity": intelligence.source_snapshot_identity,
        "document_count": intelligence.document_count,
        "node_count": intelligence.symbol_count,
        "edge_count": intelligence.relationship_count,
    }
    logical_identity = canonical_identity(logical_material).uri
    index_binding = canonical_identity(
        {
            "schema": "urn:literate-ai:schema:v1:codegraph-database-binding",
            "logical_index_identity": logical_identity,
            "source_tree_identity": intelligence.source_tree_identity,
            "database_identity": attachment.artifact.identity,
        }
    ).uri
    index_material = {
        "schema": "urn:literate-ai:schema:v1:generated-source-index-binding",
        "provider_id": intelligence.provider_id,
        "provider_version": intelligence.provider_version,
        "runtime_version": intelligence.runtime_version,
        "executable_identity": intelligence.executable_identity,
        "source_tree_identity": intelligence.source_tree_identity,
        "source_snapshot_identity": intelligence.source_snapshot_identity,
        "logical_index_identity": logical_identity,
        "index_binding": index_binding,
        "index_path": ".codegraph/codegraph.db",
        "built_with_version": built_with,
        "extraction_version": extraction,
        "database_identity": attachment.artifact.identity,
        "document_count": intelligence.document_count,
        "node_count": intelligence.symbol_count,
        "edge_count": intelligence.relationship_count,
    }
    legacy_index = {
        **index_material,
        "index_identity": canonical_identity(index_material).uri,
    }
    derivation = entry.derivation.to_dict()
    derivation["schema"] = "urn:literate-ai:schema:v1:accepted-source-derivation"
    derivation["cache_key"] = key_document
    derivation["source_index_identity"] = ContentIdentity.parse_uri(
        legacy_index["index_identity"]
    ).to_dict()
    document = entry.to_dict()
    document["schema"] = "urn:literate-ai:schema:v1:accepted-source-cache-entry"
    document["derivation"] = derivation
    document["source_files"] = [
        {
            **source_file,
            "schema": "urn:literate-ai:schema:v1:cached-source-file",
        }
        for source_file in document["source_files"]
    ]
    document["source_index"] = legacy_index
    document["codegraph_database"] = attachment.artifact.to_dict()
    entry_bytes = canonical_json_bytes(document)
    entry_identity = canonical_identity(document)
    (entries / f"{entry_identity.digest}.json").write_bytes(entry_bytes)
    key_directory = keys / key_identity.digest
    key_directory.mkdir()
    (key_directory / f"{entry_identity.digest}.json").write_bytes(
        canonical_json_bytes(
            {
                "schema": "literate-ai/source-cache-membership@1",
                "cache_key_identity": key_identity.uri,
                "entry_identity": entry_identity.uri,
            }
        )
    )
    (root / "format.json").write_bytes(
        canonical_json_bytes(
            {
                "schema": "literate-ai/source-cache-layout@1",
                "format": "filesystem-v1",
            }
        )
    )


class _HealthCheckVerifier:
    def __init__(
        self,
        template: SourceIntelligenceArtifact,
        database: bytes,
        *,
        fail: bool = False,
    ) -> None:
        self.template = template
        self.database = database
        self.fail = fail
        self.calls: list[Path] = []

    def finalize(
        self,
        source_root: Path,
        files: dict[str, bytes],
    ) -> SourceIntelligenceArtifact:
        self.calls.append(source_root)
        if self.fail:
            raise RuntimeError("injected final-path intelligence failure")
        database = source_root / self.template.artifact_path
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(self.database)
        return self.template

    def verify(self, source_root, files, evidence):
        if not (source_root / evidence.artifact_path).is_file():
            raise ValueError("fixture source-intelligence artifact is missing")
        return evidence


class _ReindexVerifier:
    def __init__(self, template: SourceIntelligenceArtifact) -> None:
        self.template = template

    def finalize(
        self,
        source_root: Path,
        files: dict[str, bytes],
    ) -> SourceIntelligenceArtifact:
        database = source_root / self.template.artifact_path
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE graph_fixture (value TEXT NOT NULL)")
            connection.execute("INSERT INTO graph_fixture VALUES ('local-reindex')")
        replacement = database.read_bytes()
        return SourceIntelligenceArtifact.create(
            provider_id=self.template.provider_id,
            provider_version=self.template.provider_version,
            runtime_version="1.1.2",
            executable_identity=_identity("local-intelligence-executable").uri,
            source_tree_identity=generated_source_tree_identity(files),
            source_snapshot_identity=generated_source_snapshot_identity(files),
            capabilities=self.template.capabilities,
            provider_properties=(
                "built-with-version=1.1.2",
                "extraction-version=1",
            ),
            artifact_path=self.template.artifact_path,
            artifact_media_type=self.template.artifact_media_type,
            artifact_identity=f"sha256:{hashlib.sha256(replacement).hexdigest()}",
            document_count=self.template.document_count,
            symbol_count=self.template.symbol_count,
            relationship_count=self.template.relationship_count,
            unresolved_relationship_count=self.template.unresolved_relationship_count,
            warning_count=self.template.warning_count,
        )

    def verify(self, source_root, files, evidence):
        if evidence.source_tree_identity != generated_source_tree_identity(files):
            raise ValueError("fixture source-intelligence evidence names another tree")
        if not (source_root / evidence.artifact_path).is_file():
            raise ValueError("fixture source-intelligence artifact is missing")
        return evidence


class SourceCacheContractTests(unittest.TestCase):
    def test_every_cache_contract_matches_its_public_schema(self) -> None:
        from tests.support.fixtures_test_schema_catalog import SchemaCatalog

        with tempfile.TemporaryDirectory() as temporary:
            entry, attachment = _accepted_entry_with_attachment(
                FileSystemCAS(Path(temporary) / "cas")
            )
        target = _target()
        configuration = _configuration(SourceCacheMode.READ_WRITE, target)
        values = (
            entry.derivation.cache_key.model_binding,
            entry.derivation.cache_key,
            entry.derivation,
            target,
            configuration,
            *entry.source_files,
            attachment.intelligence,
            attachment,
            entry,
        )
        schemas = SchemaCatalog()

        for value in values:
            document = value.to_dict()
            schema = document["schema"]
            with self.subTest(schema=schema):
                schemas.validate(schema, document)

    def test_entry_source_tree_identity_matches_its_derivation(self) -> None:
        # litai cache publish crashed with an uncaught AttributeError reading
        # entry.source_tree_identity directly (issue #63); only
        # entry.derivation.source_tree_identity ever existed. Exercise the
        # real dataclass, not a Mock(spec=...), so this regresses honestly.
        with tempfile.TemporaryDirectory() as temporary:
            entry = _accepted_entry(FileSystemCAS(Path(temporary) / "cas"))
        self.assertEqual(
            entry.source_tree_identity, entry.derivation.source_tree_identity
        )

    def test_model_binding_is_honest_about_selector_and_optional_revision(self) -> None:
        selector_only = SourceCacheModelBinding("openai", "gpt-5.6")
        exact = SourceCacheModelBinding(
            "provider",
            "visible-alias",
            exact_revision_identity=_identity("provider-revision"),
        )

        self.assertIsNone(selector_only.exact_revision_identity)
        self.assertEqual(SourceCacheModelBinding.from_dict(exact.to_dict()), exact)
        self.assertNotEqual(selector_only.identity, exact.identity)

    def test_cache_entry_binds_both_boms_to_the_exact_component_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = _accepted_entry(FileSystemCAS(Path(temporary) / "cas"))
        wrong_composition = _identity("wrong-component-composition")
        with self.assertRaises(ContractValidationError):
            replace(
                entry.derivation,
                source_sbom_binding=replace(
                    entry.derivation.source_sbom_binding,
                    resolved_graph_identity=wrong_composition,
                ),
                resolved_sbom_binding=replace(
                    entry.derivation.resolved_sbom_binding,
                    resolved_graph_identity=wrong_composition,
                ),
            )
        with self.assertRaises(ContractValidationError):
            replace(
                entry.derivation,
                component_lock_identity=wrong_composition,
            )

    def test_cached_files_are_canonical_portable_tree_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blob = FileSystemCAS(Path(temporary) / "cas").put_bytes(b"source")
            for path in (
                "",
                "/main.py",
                "../main.py",
                "source/../main.py",
                "source//main.py",
                "source\\main.py",
            ):
                with (
                    self.subTest(path=path),
                    self.assertRaises(ContractValidationError),
                ):
                    CachedSourceFile(path, blob)

            for path in (
                "main.py",
                "source/main.py",
                ".literate/source-sbom.cdx.json",
            ):
                self.assertEqual(CachedSourceFile(path, blob).path, path)

    def test_configuration_modes_require_one_explicit_write_target(self) -> None:
        target = _target()
        with self.assertRaises(ContractValidationError):
            SourceCacheConfiguration(SourceCacheMode.READ_WRITE, (target,))
        with self.assertRaises(ContractValidationError):
            SourceCacheConfiguration(
                SourceCacheMode.READ_ONLY,
                (target,),
                write_target_id=target.target_id,
            )
        with self.assertRaises(ContractValidationError):
            SourceCacheConfiguration(
                SourceCacheMode.READ_WRITE,
                (target, target),
                write_target_id=target.target_id,
            )

    def test_project_definition_round_trips_optional_source_cache(self) -> None:
        from tests.support.fixtures_test_schema_catalog import SchemaCatalog

        source_cache = _configuration(SourceCacheMode.READ_ONLY, _target())
        definition = ProjectDefinition(
            project_id="cache-project",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
            source_cache=source_cache,
        )

        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)
        self.assertEqual(definition.to_dict()["source_cache"], source_cache.to_dict())
        SchemaCatalog().validate(definition.SCHEMA, definition.to_dict())


class FileSystemSourceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.caller_cas = FileSystemCAS(self.root / "caller-cas")
        self.entry, self.attachment = _accepted_entry_with_attachment(self.caller_cas)
        self.store = FileSystemSourceCache("local", self.root / "source-cache")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_miss_publish_hit_is_exact_and_idempotent(self) -> None:
        self.assertEqual(self.store.candidates(self.entry.derivation.cache_key), ())

        first = self.store.publish(self.entry, caller_cas=self.caller_cas)
        second = self.store.publish(self.entry, caller_cas=self.caller_cas)

        self.assertEqual(first, self.entry.identity)
        self.assertEqual(second, first)
        self.assertEqual(
            self.store.candidates(self.entry.derivation.cache_key),
            (self.entry,),
        )
        changed = _cache_key(selector="model-b")
        self.assertEqual(self.store.candidates(changed), ())

    def test_genuine_filesystem_v1_root_is_read_only_and_migrates_entries(
        self,
    ) -> None:
        legacy_root = self.root / "legacy-source-cache"
        _publish_legacy_v1_cache_root(
            legacy_root,
            self.caller_cas,
            self.entry,
            self.attachment,
        )
        target = SourceCacheTarget.from_dict(
            {
                "schema": "urn:literate-ai:schema:v1:source-cache-target",
                "target_id": "legacy",
                "format": "filesystem-v1",
                "root_kind": "operator-bound",
                "root_reference": "legacy-root",
            }
        )
        configuration = SourceCacheConfiguration(
            SourceCacheMode.READ_ONLY,
            (target,),
        )
        from tests.support.fixtures_test_schema_catalog import SchemaCatalog

        SchemaCatalog().validate(target.SCHEMA, target.to_dict())
        SchemaCatalog().validate(configuration.SCHEMA, configuration.to_dict())
        resolver = SourceCacheResolver.from_configuration(
            configuration,
            operator_roots={"legacy-root": legacy_root},
        )

        with self.assertRaisesRegex(SourceCacheError, "exact Component lock"):
            resolver.resolve(self.entry.derivation.cache_key)

        candidates = resolver.resolve(
            self.entry.derivation.cache_key,
            component_lock_identity=self.entry.derivation.component_lock_identity,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].entry, self.entry)
        self.assertEqual(candidates[0].target_ids, ("legacy",))
        self.assertEqual(
            candidates[0].store.intelligence_attachments(
                self.entry.derivation.source_tree_identity
            ),
            (),
        )
        self.assertEqual(target.to_dict()["format"], "filesystem-v1")
        round_tripped = SourceCacheTarget.from_dict(target.to_dict())
        repeated = SourceCacheResolver.from_configuration(
            SourceCacheConfiguration(SourceCacheMode.READ_ONLY, (round_tripped,)),
            operator_roots={"legacy-root": legacy_root},
        ).resolve(
            self.entry.derivation.cache_key,
            component_lock_identity=self.entry.derivation.component_lock_identity,
        )
        self.assertEqual(len(repeated), 1)
        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheResolver.from_configuration(
                SourceCacheConfiguration(
                    SourceCacheMode.READ_WRITE,
                    (target,),
                    write_target_id="legacy",
                ),
                operator_roots={"legacy-root": legacy_root},
            )
        self.assertEqual(captured.exception.code, "source-cache.legacy-write-disabled")

    def test_reindexing_one_source_never_creates_a_second_cache_candidate(
        self,
    ) -> None:
        replacement_index = SourceIntelligenceArtifact.create(
            provider_id=self.attachment.intelligence.provider_id,
            provider_version="1.2.0",
            runtime_version="1.2.0",
            executable_identity=_identity("replacement-intelligence").uri,
            source_tree_identity=self.attachment.intelligence.source_tree_identity,
            source_snapshot_identity=(
                self.attachment.intelligence.source_snapshot_identity
            ),
            capabilities=self.attachment.intelligence.capabilities,
            provider_properties=(
                "built-with-version=1.2.0",
                "extraction-version=2",
            ),
            artifact_path=self.attachment.intelligence.artifact_path,
            artifact_media_type=self.attachment.intelligence.artifact_media_type,
            artifact_identity=self.attachment.intelligence.artifact_identity,
            document_count=self.attachment.intelligence.document_count,
            symbol_count=self.attachment.intelligence.symbol_count,
            relationship_count=self.attachment.intelligence.relationship_count,
            unresolved_relationship_count=(
                self.attachment.intelligence.unresolved_relationship_count
            ),
            warning_count=self.attachment.intelligence.warning_count,
        )
        replacement = SourceIntelligenceAttachment(
            self.entry.derivation.source_tree_identity,
            replacement_index,
            self.attachment.artifact,
        )

        self.store.publish(
            self.entry,
            caller_cas=self.caller_cas,
            intelligence_attachments=(self.attachment,),
        )
        self.store.publish(
            self.entry,
            caller_cas=self.caller_cas,
            intelligence_attachments=(replacement,),
        )

        self.assertEqual(
            self.store.candidates(self.entry.derivation.cache_key), (self.entry,)
        )
        self.assertEqual(
            {
                item.identity
                for item in self.store.intelligence_attachments(
                    self.entry.derivation.source_tree_identity
                )
            },
            {self.attachment.identity, replacement.identity},
        )

    def test_repository_source_resolutions_survive_cache_replay(self) -> None:
        root_identity = _identity("repository-cache-root")
        root = ComponentRevisionRef(
            ComponentCoordinate("cache", "root"),
            "1.0.0",
            root_identity,
        )
        dependency = RepositorySourceDependency(
            "repository-cache-dependency",
            "https://example.invalid/cache-dependency.git",
            RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "release"),
            DependencyKind.BUILD,
        )
        managed_graph = CycloneDxManagedGraph.from_component_composition(
            root_ref=root,
            revision_refs=(root,),
            edges=(),
            composition_identity=_identity("repository-cache-composition"),
            repository_dependencies={root_identity.uri: (dependency,)},
        )
        resolution = CycloneDxRepositorySourceResolution(
            repository_dependency_identity(dependency),
            "9" * 40,
            _identity("repository-lock"),
            _identity("repository-snapshot"),
            _identity("repository-tree"),
            _identity("repository-resolver"),
            _identity("repository-index"),
            _identity("repository-admission"),
            _identity("repository-cache-record"),
        )
        entry = _accepted_entry(
            self.caller_cas,
            suffix="repository-resolution",
            managed_graph=managed_graph,
            repository_resolutions=(resolution,),
        )

        self.store.publish(entry, caller_cas=self.caller_cas)
        reloaded = self.store.candidates(entry.derivation.cache_key)

        self.assertEqual(reloaded, (entry,))
        self.assertEqual(reloaded[0].repository_resolutions, (resolution,))
        manifest = json.loads(
            (self.store.entries / f"{entry.identity.digest}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["repository_resolutions"],
            [resolution.to_dict()],
        )

    def test_caller_cas_is_verified_before_any_entry_becomes_reachable(self) -> None:
        source = self.entry.source_files[0].blob
        self.caller_cas.path_for(source).write_bytes(b"tampered")

        with self.assertRaises(SourceCacheError) as captured:
            self.store.publish(self.entry, caller_cas=self.caller_cas)

        self.assertEqual(captured.exception.code, "source-cache.object-invalid")
        self.assertEqual(self.store.candidates(self.entry.derivation.cache_key), ())

    def test_malformed_versioned_evidence_is_rejected(self) -> None:
        malformed = self.caller_cas.put_manifest(
            {"schema": "not-versioned", "kind": "generated-test"}
        )
        derivation = replace(
            self.entry.derivation,
            generated_test_suite_identity=_content_identity(malformed.identity),
        )
        malformed_entry = replace(
            self.entry,
            derivation=derivation,
            generated_test_suite=malformed,
        )

        with self.assertRaises(SourceCacheError) as captured:
            self.store.publish(malformed_entry, caller_cas=self.caller_cas)

        self.assertEqual(captured.exception.code, "source-cache.evidence-invalid")

    def test_full_sbom_bindings_are_recomputed_before_publication(self) -> None:
        forged_binding = replace(
            self.entry.derivation.resolved_sbom_binding,
            graph_identity=_identity("forged-resolved-graph"),
        )
        forged_derivation = replace(
            self.entry.derivation,
            resolved_sbom_binding=forged_binding,
        )
        forged_entry = replace(self.entry, derivation=forged_derivation)

        with self.assertRaises(SourceCacheError) as captured:
            self.store.publish(forged_entry, caller_cas=self.caller_cas)

        self.assertEqual(captured.exception.code, "source-cache.sbom-invalid")
        self.assertEqual(self.store.candidates(forged_derivation.cache_key), ())

    def test_membership_is_published_last_and_partial_publication_is_unreachable(
        self,
    ) -> None:
        injected = SourceCacheError("source-cache.injected", "injected crash")
        with (
            patch.object(
                self.store,
                "_publish_membership",
                side_effect=injected,
            ),
            self.assertRaises(SourceCacheError),
        ):
            self.store.publish(self.entry, caller_cas=self.caller_cas)

        manifest = self.store.entries / f"{self.entry.identity.digest}.json"
        self.assertTrue(manifest.is_file())
        self.assertEqual(self.store.candidates(self.entry.derivation.cache_key), ())

    def test_tampered_membership_fails_closed(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        membership = (
            self.store.keys
            / self.entry.derivation.cache_key.accepted_source_lookup_identity.digest
            / f"{self.entry.identity.digest}.json"
        )
        membership.write_bytes(
            canonical_json_bytes(
                {
                    "schema": "literate-ai/source-cache-membership@2",
                    "accepted_source_lookup_identity": _identity("another-key").uri,
                    "entry_identity": self.entry.identity.uri,
                }
            )
        )

        with self.assertRaises(SourceCacheError) as captured:
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.membership-invalid")

    def test_noncanonical_membership_fails_closed(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        membership = (
            self.store.keys
            / self.entry.derivation.cache_key.accepted_source_lookup_identity.digest
            / f"{self.entry.identity.digest}.json"
        )
        value = json.loads(membership.read_bytes())
        membership.write_text(json.dumps(value, indent=2), encoding="utf-8")

        with self.assertRaises(SourceCacheError) as captured:
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.membership-invalid")

    def test_tampered_entry_manifest_fails_closed(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        manifest = self.store.entries / f"{self.entry.identity.digest}.json"
        value = json.loads(manifest.read_bytes())
        value["derivation"]["source_tree_identity"] = _identity("tampered").to_dict()
        manifest.write_bytes(canonical_json_bytes(value))

        with self.assertRaises(SourceCacheError) as captured:
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(
            captured.exception.code,
            "source-cache.entry-identity-mismatch",
        )

    def test_tampered_source_object_fails_closed(self) -> None:
        reference = self.entry.source_files[0].blob
        isolated = FileSystemSourceCache("isolated", self.root / "isolated")
        isolated.publish(self.entry, caller_cas=self.caller_cas)
        isolated.cas.path_for(reference).write_bytes(b"tampered")

        with self.assertRaises(SourceCacheError) as captured:
            isolated.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.object-invalid")

    def test_tampered_intelligence_artifact_does_not_invalidate_source(self) -> None:
        self.store.publish(
            self.entry,
            caller_cas=self.caller_cas,
            intelligence_attachments=(self.attachment,),
        )
        assert self.store.cas is not None
        self.store.cas.path_for(self.attachment.artifact).write_bytes(b"tampered")

        self.assertEqual(
            self.store.candidates(self.entry.derivation.cache_key), (self.entry,)
        )
        with self.assertRaises(SourceCacheError) as captured:
            self.store.intelligence_attachments(
                self.entry.derivation.source_tree_identity
            )
        self.assertEqual(
            captured.exception.code,
            "source-cache.intelligence-object-invalid",
        )

    def test_symbolic_membership_is_rejected(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        membership = (
            self.store.keys
            / self.entry.derivation.cache_key.accepted_source_lookup_identity.digest
            / f"{self.entry.identity.digest}.json"
        )
        target = self.root / "membership-target"
        target.write_bytes(membership.read_bytes())
        membership.unlink()
        try:
            membership.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises(SourceCacheError) as captured:
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.membership-invalid")

    def test_symbolic_root_is_rejected(self) -> None:
        actual = self.root / "actual-cache"
        actual.mkdir()
        symbolic = self.root / "symbolic-cache"
        try:
            symbolic.symlink_to(actual, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises(SourceCacheError):
            FileSystemSourceCache("symbolic", symbolic)


class SourceCacheResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.caller_cas = FileSystemCAS(self.root / "caller-cas")
        self.key = _cache_key()
        self.entry = _accepted_entry(self.caller_cas, key=self.key)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _resolver(
        self,
        mode: SourceCacheMode,
        root: Path,
        *,
        require_unique: bool = True,
    ) -> SourceCacheResolver:
        target = _target()
        return SourceCacheResolver.from_configuration(
            _configuration(mode, target, require_unique=require_unique),
            operator_roots={"cache-root": root},
        )

    def test_all_modes_enforce_independent_read_and_write_permissions(self) -> None:
        off_root = self.root / "off"
        off = self._resolver(SourceCacheMode.OFF, off_root)
        self.assertEqual(off.resolve(self.key), ())
        with self.assertRaises(SourceCacheError):
            off.publish(self.entry, caller_cas=self.caller_cas)
        self.assertFalse(off_root.exists())

        read_root = self.root / "read-only"
        FileSystemSourceCache("local", read_root).publish(
            self.entry, caller_cas=self.caller_cas
        )
        read_only = self._resolver(SourceCacheMode.READ_ONLY, read_root)
        self.assertEqual(len(read_only.resolve(self.key)), 1)
        with self.assertRaises(SourceCacheError):
            read_only.publish(self.entry, caller_cas=self.caller_cas)

        write_only = self._resolver(
            SourceCacheMode.WRITE_ONLY, self.root / "write-only"
        )
        write_only.publish(self.entry, caller_cas=self.caller_cas)
        self.assertEqual(write_only.resolve(self.key), ())

        read_write = self._resolver(
            SourceCacheMode.READ_WRITE, self.root / "read-write"
        )
        read_write.publish(self.entry, caller_cas=self.caller_cas)
        self.assertEqual(len(read_write.resolve(self.key)), 1)

    def test_force_bypasses_reads_but_does_not_disable_publication(self) -> None:
        resolver = self._resolver(SourceCacheMode.READ_WRITE, self.root / "cache")
        resolver.publish(self.entry, caller_cas=self.caller_cas)

        self.assertEqual(resolver.resolve(self.key, force_regeneration=True), ())
        self.assertEqual(
            resolver.publish(self.entry, caller_cas=self.caller_cas),
            self.entry.identity,
        )

    def test_ambiguous_exact_results_require_default_or_explicit_selection(
        self,
    ) -> None:
        root = self.root / "cache"
        store = FileSystemSourceCache("local", root)
        another = _accepted_entry(self.caller_cas, suffix="two", key=self.key)
        store.publish(self.entry, caller_cas=self.caller_cas)
        store.publish(another, caller_cas=self.caller_cas)

        unique = self._resolver(SourceCacheMode.READ_ONLY, root)
        with self.assertRaises(SourceCacheError) as captured:
            unique.resolve(self.key)
        self.assertEqual(captured.exception.code, "source-cache.ambiguous")
        self.assertEqual(
            unique.resolve(self.key, entry_identity=another.identity)[0].entry,
            another,
        )
        with self.assertRaises(SourceCacheError) as missing:
            unique.resolve(self.key, entry_identity=_identity("missing-entry"))
        self.assertEqual(missing.exception.code, "source-cache.entry-not-found")

        set_valued = self._resolver(
            SourceCacheMode.READ_ONLY,
            root,
            require_unique=False,
        )
        self.assertEqual(
            {candidate.identity for candidate in set_valued.resolve(self.key)},
            {self.entry.identity, another.identity},
        )

    def test_repeated_forced_accepts_self_heal_after_retiring_stale_memberships(
        self,
    ) -> None:
        """Reproduce #137: repeated forced accepts must not permanently wedge reads.

        Two accepted entries land at the exact same key -- exactly what happens
        when ``--force-regeneration`` is retried across flaky attempts. A plain
        read is ambiguous until the caller explicitly retires the superseded
        membership, after which ordinary cache reuse self-heals. Retirement only
        prunes the exact-key membership index; the immutable entry manifests
        themselves are never deleted from the content-addressed store.
        """

        root = self.root / "cache"
        store = FileSystemSourceCache("local", root)
        first = self.entry
        second = _accepted_entry(self.caller_cas, suffix="two", key=self.key)
        third = _accepted_entry(self.caller_cas, suffix="three", key=self.key)
        store.publish(first, caller_cas=self.caller_cas)
        store.publish(second, caller_cas=self.caller_cas)

        writable = self._resolver(SourceCacheMode.READ_WRITE, root)
        with self.assertRaises(SourceCacheError) as captured:
            writable.resolve(self.key)
        self.assertEqual(captured.exception.code, "source-cache.ambiguous")

        # A second force-regeneration retry accepts yet another entry before the
        # caller ever retires the first -- the accumulation the issue describes.
        store.publish(third, caller_cas=self.caller_cas)
        writable.retire_other_memberships(self.key, keep=third.identity)

        healed = self._resolver(SourceCacheMode.READ_ONLY, root)
        candidates = healed.resolve(self.key)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].entry, third)

        # Retired entries are no longer exact-key candidates -- explicit selection
        # of a retired identity now reports the same "not found" outcome an
        # unknown identity would, rather than resurrecting a superseded accept.
        with self.assertRaises(SourceCacheError) as first_missing:
            healed.resolve(self.key, entry_identity=first.identity)
        self.assertEqual(first_missing.exception.code, "source-cache.entry-not-found")
        with self.assertRaises(SourceCacheError) as second_missing:
            healed.resolve(self.key, entry_identity=second.identity)
        self.assertEqual(second_missing.exception.code, "source-cache.entry-not-found")

        # Retirement only prunes the membership index -- the immutable entry
        # manifests remain in the content-addressed store, untouched.
        self.assertTrue((store.entries / f"{first.identity.digest}.json").is_file())
        self.assertTrue((store.entries / f"{second.identity.digest}.json").is_file())

        with self.assertRaises(SourceCacheError) as read_only_write:
            healed.retire_other_memberships(self.key, keep=third.identity)
        self.assertEqual(read_only_write.exception.code, "source-cache.write-disabled")

    def test_identical_entries_across_targets_form_one_set_candidate(self) -> None:
        first_root = self.root / "first"
        second_root = self.root / "second"
        FileSystemSourceCache("first", first_root).publish(
            self.entry, caller_cas=self.caller_cas
        )
        FileSystemSourceCache("second", second_root).publish(
            self.entry, caller_cas=self.caller_cas
        )
        targets = (
            _target("first", binding="first-binding"),
            _target("second", binding="second-binding"),
        )
        resolver = SourceCacheResolver.from_configuration(
            _configuration(SourceCacheMode.READ_ONLY, *targets),
            operator_roots={
                "first-binding": first_root,
                "second-binding": second_root,
            },
        )

        candidates = resolver.resolve(self.key)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].target_ids, ("first", "second"))

    def test_target_roots_must_not_overlap(self) -> None:
        targets = (
            _target("first", binding="first-binding"),
            _target("second", binding="second-binding"),
        )
        configuration = _configuration(SourceCacheMode.READ_ONLY, *targets)

        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheResolver.from_configuration(
                configuration,
                operator_roots={
                    "first-binding": self.root / "cache",
                    "second-binding": self.root / "cache" / "nested",
                },
            )

        self.assertEqual(captured.exception.code, "source-cache.target-overlap")

    def test_project_relative_cache_is_git_compatible_but_protects_git_metadata(
        self,
    ) -> None:
        project_root = self.root / "worktree"
        (project_root / ".git").mkdir(parents=True)
        (project_root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        definition = ProjectDefinition(
            project_id="worktree",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
        )
        project = LoadedProject(project_root, definition)
        target = SourceCacheTarget(
            "project",
            SourceCacheRootKind.PROJECT_RELATIVE,
            ".literate-ai/cache/source",
        )
        resolver = SourceCacheResolver.from_configuration(
            _configuration(SourceCacheMode.READ_WRITE, target),
            project=project,
        )

        resolver.publish(self.entry, caller_cas=self.caller_cas)

        self.assertEqual(
            (project_root / ".git" / "HEAD").read_text(),
            "ref: refs/heads/main\n",
        )
        self.assertEqual(len(resolver.resolve(self.key)), 1)

        git_target = SourceCacheTarget(
            "git",
            SourceCacheRootKind.PROJECT_RELATIVE,
            ".git/literate-ai-cache",
        )
        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheResolver.from_configuration(
                _configuration(SourceCacheMode.READ_WRITE, git_target),
                project=project,
            )
        self.assertEqual(captured.exception.code, "source-cache.authority-overlap")

    def test_project_relative_cache_cannot_overlap_authority_catalogs_or_reserved_index(
        self,
    ) -> None:
        project_root = self.root / "project"
        project_root.mkdir()
        definition = ProjectDefinition(
            project_id="project",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
            test_receipt="receipts/latest.json",
        )
        project = LoadedProject(project_root, definition)
        for target_path in (
            "components/cache",
            ".codegraph/cache",
            "receipts",
            "SKILL.md/cache",
        ):
            with self.subTest(target_path=target_path):
                target = SourceCacheTarget(
                    "project",
                    SourceCacheRootKind.PROJECT_RELATIVE,
                    target_path,
                )
                with self.assertRaises(SourceCacheError) as captured:
                    SourceCacheResolver.from_configuration(
                        _configuration(SourceCacheMode.READ_ONLY, target),
                        project=project,
                    )
                self.assertEqual(
                    captured.exception.code,
                    "source-cache.authority-overlap",
                )


class SourceCacheMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.caller_cas = FileSystemCAS(self.root / "caller-cas")
        self.entry, self.attachment = _accepted_entry_with_attachment(self.caller_cas)
        self.store = FileSystemSourceCache("local", self.root / "cache")
        self.store.publish(
            self.entry,
            caller_cas=self.caller_cas,
            intelligence_attachments=(self.attachment,),
        )
        self.candidate = SourceCacheResolver(
            _configuration(SourceCacheMode.READ_ONLY, _target()),
            {"local": self.store},
        ).resolve(self.entry.derivation.cache_key)[0]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_source_and_fresh_intelligence_are_admitted_after_final_path_check(
        self,
    ) -> None:
        destination = self.root / "materialized"
        verifier = _HealthCheckVerifier(
            self.attachment.intelligence,
            self.caller_cas.get_bytes(self.attachment.artifact),
        )

        materialized = SourceCacheMaterializer().materialize(
            self.candidate,
            destination,
            intelligence_verifier=verifier,
        )

        self.assertEqual(verifier.calls, [destination])
        self.assertEqual(materialized.entry_identity, self.entry.identity)
        self.assertEqual(materialized.intelligence, self.attachment.intelligence)
        for source_file in self.entry.source_files:
            self.assertEqual(
                destination.joinpath(*Path(source_file.path).parts).read_bytes(),
                self.store.cas.get_bytes(source_file.blob),
            )
        self.assertEqual(
            (destination / "source-intelligence.sqlite").read_bytes(),
            self.store.cas.get_bytes(self.attachment.artifact),
        )

    def test_materialization_can_skip_source_intelligence_entirely(self) -> None:
        destination = self.root / "source-only"

        materialized = SourceCacheMaterializer().materialize(
            self.candidate,
            destination,
        )

        self.assertIsNone(materialized.intelligence)
        self.assertFalse((destination / ".codegraph").exists())

    def test_final_path_verifier_failure_removes_only_new_destination(self) -> None:
        destination = self.root / "rejected"
        neighbor = self.root / "keep.txt"
        neighbor.write_text("keep", encoding="utf-8")

        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheMaterializer().materialize(
                self.candidate,
                destination,
                intelligence_verifier=_HealthCheckVerifier(
                    self.attachment.intelligence,
                    self.caller_cas.get_bytes(self.attachment.artifact),
                    fail=True,
                ),
            )

        self.assertEqual(captured.exception.code, "source-cache.final-index-invalid")
        self.assertFalse(destination.exists())
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.root.glob(".rejected.source-cache-*")), [])

    def test_final_path_reindex_returns_and_persists_local_index_evidence(self) -> None:
        destination = self.root / "reindexed"

        materialized = SourceCacheMaterializer().materialize(
            self.candidate,
            destination,
            intelligence_verifier=_ReindexVerifier(self.attachment.intelligence),
        )

        marker = json.loads(
            (destination / ".literate-source-intelligence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(marker, materialized.intelligence.to_dict())
        self.assertNotEqual(
            materialized.intelligence.artifact_identity,
            self.attachment.intelligence.artifact_identity,
        )
        self.assertEqual(materialized.intelligence.runtime_version, "1.1.2")

    def test_existing_or_protected_destination_is_never_modified(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        verifier = _HealthCheckVerifier(
            self.attachment.intelligence,
            self.caller_cas.get_bytes(self.attachment.artifact),
        )

        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheMaterializer().materialize(
                self.candidate,
                existing,
                intelligence_verifier=verifier,
            )
        self.assertEqual(captured.exception.code, "source-cache.destination-exists")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

        protected = self.root / "authority"
        with self.assertRaises(SourceCacheError) as authority:
            SourceCacheMaterializer(protected_paths=(protected,)).materialize(
                self.candidate,
                protected / "generated",
                intelligence_verifier=verifier,
            )
        self.assertEqual(authority.exception.code, "source-cache.authority-overlap")
        self.assertEqual(verifier.calls, [])

    def test_source_object_tamper_after_resolution_does_not_create_destination(
        self,
    ) -> None:
        self.store.cas.path_for(self.entry.source_files[0].blob).write_bytes(
            b"tampered"
        )
        destination = self.root / "never-created"

        with self.assertRaises(SourceCacheError) as captured:
            SourceCacheMaterializer().materialize(
                self.candidate,
                destination,
                intelligence_verifier=_HealthCheckVerifier(
                    self.attachment.intelligence,
                    self.caller_cas.get_bytes(self.attachment.artifact),
                ),
            )

        self.assertEqual(
            captured.exception.code, "source-cache.materialization-invalid"
        )
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

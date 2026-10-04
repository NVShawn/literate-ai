"""Shared test fixtures extracted from test_source_cache."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    AcceptedSourceCacheEntry,
    AcceptedSourceDerivation,
    CachedSourceFile,
    ContentIdentity,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    ManagedComponentKind,
    ProjectSourceIntelligencePolicy,
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
    component_bom_ref,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
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

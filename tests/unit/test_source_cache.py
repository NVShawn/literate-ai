"""Immutable accepted-source cache contracts, resolution, and materialization."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
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
    ContentIdentity,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    ManagedComponentKind,
    ProjectDefinition,
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
    canonical_json_bytes,
    component_bom_ref,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
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


class SourceCacheResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.caller_cas = FileSystemCAS(self.root / "caller-cas")
        self.key = _cache_key()
        self.entry = _accepted_entry(self.caller_cas, key=self.key)

    def tearDown(self) -> None:
        self.temporary.cleanup()

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


if __name__ == "__main__":
    unittest.main()

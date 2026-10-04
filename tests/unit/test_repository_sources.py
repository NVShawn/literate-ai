from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.generation_preparation import (
    GenerationPreparationError,
    load_flavor_contributions,
)
from literate_ai.adapters.source import (
    QuarantineRepositorySourceCache,
    RepositorySourceCachePolicyError,
)
from literate_ai.application import (
    RepositoryBuildApproval,
    RepositoryBuildVerification,
    RepositoryCheckout,
    RepositorySourceIndexBinding,
    RepositorySourceResolutionError,
    RepositorySourceResolver,
)
from literate_ai.contracts import (
    ComponentDefinition,
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    DependencyKind,
    HashAlgorithm,
    MergeOperator,
    ProjectSourceIntelligencePolicy,
    RepositoryBuildCommand,
    RepositoryBuildEnvironment,
    RepositoryBuildOutput,
    RepositoryBuildPlan,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceAdmission,
    RepositorySourceDependency,
    RepositorySourceLock,
    SourceIntelligenceArtifact,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    canonical_identity,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
from literate_ai.sources import GitFacts, QuarantineStore, SourceSnapshotter
from literate_ai.storage import FileSystemCAS, ReferenceIndex
from tests.support.fixtures_test_project_cli import (
    copy_generation_catalogs,
    copy_hello_component,
    invoke,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

COMMIT = "a" * 40


def identity(label: str):
    return canonical_identity({"fixture": label})


def dependency(
    selector: RepositoryRevisionSelector | None = None,
) -> RepositorySourceDependency:
    return RepositorySourceDependency(
        "portable-lib",
        "https://example.test/oss/portable-lib.git",
        selector or RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "main"),
        DependencyKind.BUILD,
    )


def component_wire(
    source_dependencies: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": ComponentDefinition.SCHEMA,
        "coordinate": {"namespace": "tests", "name": "repository-consumer"},
        "version": "1.0.0",
        "display_name": "Repository consumer",
        "description": "Contract fixture",
        "profiles": [],
        "sample": False,
        "provides": [],
        "requires": [],
        "specification_provider": "literate-markdown",
        "specification_roots": ["component.md"],
        "authoring_inputs": [],
        "workflow_definition": ContentReference(
            "workflow", "workflows/test.json", identity("workflow")
        ).to_dict(),
        "routing_policy": ContentReference(
            "routing-policy", "routing/test.json", identity("routing")
        ).to_dict(),
        "flavor_slots": [],
        "entrypoints": [],
        "acceptance_contracts": [],
    }
    if source_dependencies is not None:
        value["source_dependencies"] = source_dependencies
    return value


def source_intelligence_policy(
    provider_id: str,
    repository_mode: SourceIntelligenceMode,
) -> ProjectSourceIntelligencePolicy:
    no_provider = provider_id == "none"
    return ProjectSourceIntelligencePolicy(
        provider_id=provider_id,
        command=None if no_provider else "source-intelligence",
        minimum_version=None if no_provider else "1.0.0",
        artifact_path=None if no_provider else ".intelligence/evidence.bin",
        stages=tuple(
            (
                stage,
                (
                    SourceIntelligenceMode.OFF
                    if no_provider
                    else (
                        repository_mode
                        if stage is SourceIntelligenceStage.REPOSITORY_SOURCE_ADMISSION
                        else SourceIntelligenceMode.OFF
                    )
                ),
            )
            for stage in SourceIntelligenceStage
        ),
        artifact_publication=(
            SourceIntelligenceArtifactPublication.METADATA_ONLY
            if no_provider
            else SourceIntelligenceArtifactPublication.INCLUDE_ARTIFACT
        ),
    )


class RepositorySourceContractTests(unittest.TestCase):
    def test_project_rejects_a_component_without_spec_to_source_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            copy_generation_catalogs(target)
            component = copy_hello_component(target)
            manifest = component / "component.md"
            value = manifest.read_text(encoding="utf-8")
            start = value.index("authoring_inputs:")
            end = value.index("workflow_definition:")
            manifest.write_text(
                value[:start] + "authoring_inputs: []\n" + value[end:],
                encoding="utf-8",
            )
            status, envelope = invoke("project", "documentation-review", str(target))

        self.assertEqual(status, 2)
        self.assertEqual(envelope["error"]["code"], "generate.skill_required")

    def test_dependency_selector_lock_plan_and_admission_round_trip(self) -> None:
        selected = dependency()
        self.assertFalse(selected.revision_selector.immutable)
        lock = RepositorySourceLock(
            dependency=selected,
            resolved_commit=COMMIT,
            source_snapshot=identity("snapshot"),
            source_tree=identity("tree"),
            resolver=identity("resolver"),
        )
        plan = RepositoryBuildPlan(
            source_lock=lock.identity,
            effective_revision=identity("effective"),
            flavor_set=identity("flavors"),
            evidence=(identity("index"),),
            model_decision=identity("model-decision"),
            toolchains=(identity("cmake"),),
            commands=(
                RepositoryBuildCommand(
                    "configure",
                    ("cmake", "-S", ".", "-B", "build"),
                ),
                RepositoryBuildCommand(
                    "compile",
                    ("cmake", "--build", "build"),
                ),
            ),
            expected_outputs=("build/libportable.a", "build/portable.h"),
        )
        admission = RepositorySourceAdmission.create(
            dependency_id=selected.dependency_id,
            source_lock=lock.identity,
            source_snapshot=lock.source_snapshot,
            source_tree=lock.source_tree,
            effective_revision=plan.effective_revision,
            flavor_set=plan.flavor_set,
            index_binding=identity("index"),
            build_plan=plan.identity,
            classification=identity("classification"),
            authorization=identity("authorization"),
            build_result=identity("result"),
            expected_outputs=plan.expected_outputs,
            build_outputs=(
                RepositoryBuildOutput("build/libportable.a", identity("library")),
                RepositoryBuildOutput("build/portable.h", identity("header")),
            ),
        )

        self.assertEqual(
            RepositorySourceDependency.from_dict(selected.to_dict()), selected
        )
        self.assertEqual(RepositorySourceLock.from_dict(lock.to_dict()), lock)
        self.assertEqual(RepositoryBuildPlan.from_dict(plan.to_dict()), plan)
        self.assertEqual(
            RepositorySourceAdmission.from_dict(admission.to_dict()), admission
        )
        for mutated_outputs in (
            admission.to_dict()["build_outputs"][:-1],
            list(reversed(admission.to_dict()["build_outputs"])),
            [
                *admission.to_dict()["build_outputs"],
                admission.to_dict()["build_outputs"][0],
            ],
            [
                *admission.to_dict()["build_outputs"],
                RepositoryBuildOutput(
                    "build/unexpected", identity("unexpected")
                ).to_dict(),
            ],
        ):
            wire = admission.to_dict()
            wire["build_outputs"] = mutated_outputs
            with self.assertRaises(ContractValidationError):
                RepositorySourceAdmission.from_dict(wire)
        self.assertNotIn("effective_revision", lock.to_dict())
        self.assertNotIn("flavor_set", lock.to_dict())
        other_target_key = RepositorySourceAdmission.compute_cache_key(
            selected.dependency_id,
            plan.effective_revision,
            identity("other-flavors"),
            lock.identity,
        )
        self.assertNotEqual(admission.cache_key, other_target_key)
        schemas = SchemaCatalog()
        for schema_id, value in (
            (selected.SCHEMA, selected.to_dict()),
            (lock.SCHEMA, lock.to_dict()),
            (plan.SCHEMA, plan.to_dict()),
            (admission.SCHEMA, admission.to_dict()),
        ):
            schemas.validate(schema_id, value)

    def test_exact_commit_selector_cannot_resolve_to_another_commit(self) -> None:
        selected = dependency(
            RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, COMMIT)
        )
        self.assertTrue(selected.revision_selector.immutable)
        with self.assertRaisesRegex(ContractValidationError, "requested exact commit"):
            RepositorySourceLock(
                dependency=selected,
                resolved_commit="b" * 40,
                source_snapshot=identity("snapshot"),
                source_tree=identity("tree"),
                resolver=identity("resolver"),
            )

    def test_repository_locations_and_build_commands_fail_closed(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "credentials"):
            RepositorySourceDependency(
                "unsafe",
                "https://token@example.test/org/repo.git",
                RepositoryRevisionSelector(RepositoryRevisionKind.DEFAULT, None),
                DependencyKind.BUILD,
            )
        with self.assertRaisesRegex(ContractValidationError, "command shell"):
            RepositoryBuildCommand("build", ("sh", "-c", "make"))
        with self.assertRaisesRegex(ContractValidationError, "NUL"):
            RepositoryBuildCommand("build", ("cmake", "bad\x00argument"))
        with self.assertRaisesRegex(ContractValidationError, "NUL"):
            RepositoryBuildEnvironment("BUILD_MODE", "bad\x00value")
        with self.assertRaisesRegex(TypeError, "passed must be a boolean"):
            RepositoryBuildVerification(
                source_lock=identity("lock"),
                build_plan=identity("plan"),
                build_approval=identity("approval"),
                build_result=identity("result"),
                expected_outputs=("build/output",),
                build_outputs=(
                    RepositoryBuildOutput("build/output", identity("output")),
                ),
                passed="yes",  # type: ignore[arg-type]
            )

    def test_build_outputs_bind_exact_portable_paths(self) -> None:
        expected = ("build/app", "build/app.sha256")
        first = RepositoryBuildOutput(expected[0], identity("app"))
        second = RepositoryBuildOutput(expected[1], identity("checksum"))

        verification = RepositoryBuildVerification(
            source_lock=identity("lock"),
            build_plan=identity("plan"),
            build_approval=identity("approval"),
            build_result=identity("result"),
            expected_outputs=expected,
            build_outputs=(first, second),
            passed=True,
        )

        self.assertEqual(
            tuple(item.path for item in verification.build_outputs), expected
        )
        with self.assertRaisesRegex(ValueError, "exactly match expected paths"):
            RepositoryBuildVerification(
                source_lock=identity("lock"),
                build_plan=identity("plan"),
                build_approval=identity("approval"),
                build_result=identity("result"),
                expected_outputs=expected,
                build_outputs=(first,),
                passed=True,
            )
        with self.assertRaisesRegex(ValueError, "exactly match expected paths"):
            RepositoryBuildVerification(
                source_lock=identity("lock"),
                build_plan=identity("plan"),
                build_approval=identity("approval"),
                build_result=identity("result"),
                expected_outputs=expected,
                build_outputs=(
                    first,
                    second,
                    RepositoryBuildOutput("build/extra", identity("extra")),
                ),
                passed=True,
            )
        with self.assertRaisesRegex(ValueError, "exactly match expected paths"):
            RepositoryBuildVerification(
                source_lock=identity("lock"),
                build_plan=identity("plan"),
                build_approval=identity("approval"),
                build_result=identity("result"),
                expected_outputs=expected,
                build_outputs=(second, first),
                passed=True,
            )
        with self.assertRaisesRegex(ValueError, "paths must be unique"):
            RepositoryBuildVerification(
                source_lock=identity("lock"),
                build_plan=identity("plan"),
                build_approval=identity("approval"),
                build_result=identity("result"),
                expected_outputs=expected,
                build_outputs=(
                    first,
                    RepositoryBuildOutput(first.path, identity("other")),
                ),
                passed=True,
            )
        with self.assertRaisesRegex(ContractValidationError, "must be normalized"):
            RepositoryBuildOutput("../build/app", identity("app"))

    def test_existing_component_wire_and_identity_do_not_gain_empty_field(self) -> None:
        raw = component_wire()
        definition = ComponentDefinition.from_dict(raw)
        self.assertEqual(definition.source_dependencies, ())
        self.assertEqual(definition.to_dict(), raw)

    def test_component_and_flavor_dependencies_are_explicitly_typed(self) -> None:
        reference = ContentReference(
            "repository-source-dependency",
            "dependencies/portable-lib.json",
            identity("dependency-manifest"),
        )
        raw = component_wire([reference.to_dict()])
        parsed = ComponentDefinition.from_dict(raw)
        self.assertEqual(parsed.source_dependencies, (reference,))
        contribution = ContributionReference(
            "portable-lib",
            ContributionKind.SOURCE_DEPENDENCY,
            MergeOperator.KEYED_UNION,
            "portable-lib",
            reference,
        )
        self.assertEqual(contribution.kind, ContributionKind.SOURCE_DEPENDENCY)
        with self.assertRaisesRegex(ContractValidationError, "keyed-union"):
            ContributionReference(
                "portable-lib",
                ContributionKind.SOURCE_DEPENDENCY,
                MergeOperator.ADDITIVE_SET,
                "portable-lib",
                reference,
            )

    def test_flavor_dependency_key_must_equal_embedded_dependency_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = json.dumps(
                dependency().to_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            path = root / "dependency.json"
            path.write_bytes(payload)
            reference = ContentReference(
                "repository-source-dependency",
                path.name,
                ContentIdentity(
                    HashAlgorithm.SHA256,
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            contribution = ContributionReference(
                "different-key",
                ContributionKind.SOURCE_DEPENDENCY,
                MergeOperator.KEYED_UNION,
                "dependencies",
                reference,
            )
            flavor = SimpleNamespace(
                coordinate=SimpleNamespace(uri="flavor://test/mismatch@1"),
                contributions=(contribution,),
            )

            with self.assertRaisesRegex(GenerationPreparationError, "stable key"):
                load_flavor_contributions(root, flavor, boundary=root)


class _Acquirer:
    def __init__(self, root: Path, events: list[str]) -> None:
        self.root = root
        self.events = events

    @contextmanager
    def acquire(self, _dependency):
        self.events.append("acquire")
        try:
            yield RepositoryCheckout(self.root, COMMIT, identity("resolver"))
        finally:
            self.events.append("cleanup")


class _Capturer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def capture(self, checkout):
        self.events.append("capture")
        return SourceSnapshotter().snapshot_git_paths(
            checkout.source_root,
            ("build.txt",),
            GitFacts(COMMIT, None),
        )


class _Indexer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.index_identity = identity("index")

    def index(self, _checkout, capture):
        self.events.append("index")
        return RepositorySourceIndexBinding(
            source_snapshot=capture.snapshot.identity,
            source_tree=capture.snapshot.tree_identity,
            provider=identity("fixture-intelligence-provider"),
            index=self.index_identity,
        )


class _WrongIndexer(_Indexer):
    def index(self, _checkout, _capture):
        self.events.append("index")
        return RepositorySourceIndexBinding(
            source_snapshot=identity("another-snapshot"),
            source_tree=identity("another-tree"),
            provider=identity("fixture-intelligence-provider"),
            index=self.index_identity,
        )


class _Planner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def plan(self, lock, index_binding, effective_revision, flavor_set, toolchains):
        self.events.append("plan")
        return RepositoryBuildPlan(
            source_lock=lock.identity,
            effective_revision=effective_revision,
            flavor_set=flavor_set,
            evidence=(index_binding,),
            model_decision=identity("model"),
            toolchains=toolchains,
            commands=(RepositoryBuildCommand("build", ("cmake", "--build", "build")),),
            expected_outputs=("build/libportable.a",),
        )


class _Authorizer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def authorize(self, lock, _index, plan):
        self.events.append("authorize")
        return RepositoryBuildApproval(
            lock.identity,
            plan.identity,
            identity("classification"),
            identity("authorization"),
        )


class _Builder:
    def __init__(self, events: list[str], *, passed: bool = True) -> None:
        self.events = events
        self.passed = passed

    def verify(self, _checkout, _capture, lock, plan, approval):
        self.events.append("build")
        return RepositoryBuildVerification(
            source_lock=lock.identity,
            build_plan=plan.identity,
            build_approval=approval.identity,
            build_result=identity("build-result"),
            expected_outputs=plan.expected_outputs,
            build_outputs=(
                RepositoryBuildOutput("build/libportable.a", identity("library")),
            )
            if self.passed
            else (),
            passed=self.passed,
        )


class _Cache:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def admit(self, _checkout, _capture, _admission):
        self.events.append("cache")
        return identity("cache-record")


class _WrongApprovalBuilder(_Builder):
    def verify(self, _checkout, _capture, lock, plan, _approval):
        self.events.append("build")
        return RepositoryBuildVerification(
            source_lock=lock.identity,
            build_plan=plan.identity,
            build_approval=identity("another-approval"),
            build_result=identity("build-result"),
            expected_outputs=plan.expected_outputs,
            build_outputs=(
                RepositoryBuildOutput("build/libportable.a", identity("library")),
            ),
            passed=True,
        )


class _WrongOutputPathBuilder(_Builder):
    def verify(self, _checkout, _capture, lock, plan, approval):
        self.events.append("build")
        return RepositoryBuildVerification(
            source_lock=lock.identity,
            build_plan=plan.identity,
            build_approval=approval.identity,
            build_result=identity("build-result"),
            expected_outputs=("build/another-library.a",),
            build_outputs=(
                RepositoryBuildOutput("build/another-library.a", identity("library")),
            ),
            passed=True,
        )


class _InvalidCache(_Cache):
    def admit(self, _checkout, _capture, _admission):
        self.events.append("cache")
        return "not-content-identified"


class RepositorySourceResolverTests(unittest.TestCase):
    def _resolver(
        self,
        root: Path,
        events: list[str],
        *,
        passed: bool = True,
        builder=None,
        cache=None,
        indexer=None,
    ):
        return RepositorySourceResolver(
            acquirer=_Acquirer(root, events),
            capturer=_Capturer(events),
            indexer=indexer or _Indexer(events),
            planner=_Planner(events),
            authorizer=_Authorizer(events),
            builder=builder or _Builder(events, passed=passed),
            cache=cache or _Cache(events),
        )

    def test_cache_admission_occurs_only_after_index_and_authorized_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            result = self._resolver(root, events).resolve(
                dependency(),
                effective_revision=identity("effective"),
                flavor_set=identity("flavors"),
                toolchains=(identity("cmake"),),
            )

        self.assertEqual(result.lock.resolved_commit, COMMIT)
        self.assertEqual(
            events,
            [
                "acquire",
                "capture",
                "index",
                "capture",
                "plan",
                "authorize",
                "build",
                "capture",
                "cache",
                "cleanup",
            ],
        )

    def test_failed_build_never_reaches_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            with self.assertRaisesRegex(
                RepositorySourceResolutionError, "verified outputs"
            ):
                self._resolver(root, events, passed=False).resolve(
                    dependency(),
                    effective_revision=identity("effective"),
                    flavor_set=identity("flavors"),
                    toolchains=(identity("cmake"),),
                )
        self.assertNotIn("cache", events)
        self.assertEqual(events[-1], "cleanup")

    def test_index_binding_must_name_the_exact_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            with self.assertRaisesRegex(
                RepositorySourceResolutionError, "another source capture"
            ):
                self._resolver(
                    root,
                    events,
                    indexer=_WrongIndexer(events),
                ).resolve(
                    dependency(),
                    effective_revision=identity("effective"),
                    flavor_set=identity("flavors"),
                    toolchains=(identity("cmake"),),
                )
        self.assertNotIn("plan", events)

    def test_build_verification_must_bind_the_exact_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            with self.assertRaisesRegex(
                RepositorySourceResolutionError, "another lock, plan, or approval"
            ):
                self._resolver(
                    root,
                    events,
                    builder=_WrongApprovalBuilder(events),
                ).resolve(
                    dependency(),
                    effective_revision=identity("effective"),
                    flavor_set=identity("flavors"),
                    toolchains=(identity("cmake"),),
                )
        self.assertNotIn("cache", events)

    def test_build_verification_must_match_the_plan_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            with self.assertRaisesRegex(
                RepositorySourceResolutionError, "another expected output path set"
            ):
                self._resolver(
                    root,
                    events,
                    builder=_WrongOutputPathBuilder(events),
                ).resolve(
                    dependency(),
                    effective_revision=identity("effective"),
                    flavor_set=identity("flavors"),
                    toolchains=(identity("cmake"),),
                )
        self.assertNotIn("cache", events)

    def test_cache_must_return_a_content_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build.txt").write_text("portable source", encoding="utf-8")
            events: list[str] = []
            with self.assertRaisesRegex(
                RepositorySourceResolutionError, "ContentIdentity"
            ):
                self._resolver(
                    root,
                    events,
                    cache=_InvalidCache(events),
                ).resolve(
                    dependency(),
                    effective_revision=identity("effective"),
                    flavor_set=identity("flavors"),
                    toolchains=(identity("cmake"),),
                )
        self.assertEqual(events[-1], "cleanup")

    def test_repository_cache_composition_enforces_stage_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = FileSystemCAS(root / "cas")
            quarantine = QuarantineStore(root / "source-cache", cas)
            off = QuarantineRepositorySourceCache.from_project_policy(
                quarantine,
                cas,
                source_intelligence_policy("none", SourceIntelligenceMode.OFF),
            )
            self.assertIs(off.source_intelligence_mode, SourceIntelligenceMode.OFF)
            self.assertIsNone(off.source_intelligence_provider)

            preferred = QuarantineRepositorySourceCache.from_project_policy(
                quarantine,
                cas,
                source_intelligence_policy(
                    "alternative-provider", SourceIntelligenceMode.PREFERRED
                ),
            )
            self.assertIs(
                preferred.source_intelligence_mode,
                SourceIntelligenceMode.PREFERRED,
            )
            self.assertEqual(
                preferred.source_intelligence_unavailable_reason,
                "source-intelligence.provider-unsupported",
            )

            with self.assertRaises(RepositorySourceCachePolicyError) as caught:
                QuarantineRepositorySourceCache.from_project_policy(
                    quarantine,
                    cas,
                    source_intelligence_policy(
                        "alternative-provider", SourceIntelligenceMode.REQUIRED
                    ),
                )
            self.assertEqual(
                caught.exception.code,
                "source-intelligence.provider-unsupported",
            )

    def test_quarantine_cache_materializes_exact_source_and_target_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "build.txt").write_text("portable source", encoding="utf-8")
            capture = SourceSnapshotter().snapshot_git_paths(
                source, ("build.txt",), GitFacts(COMMIT, None)
            )
            lock = RepositorySourceLock(
                dependency=dependency(),
                resolved_commit=COMMIT,
                source_snapshot=capture.snapshot.identity,
                source_tree=capture.snapshot.tree_identity,
                resolver=identity("resolver"),
            )
            admission = RepositorySourceAdmission.create(
                dependency_id="portable-lib",
                source_lock=lock.identity,
                source_snapshot=lock.source_snapshot,
                source_tree=lock.source_tree,
                effective_revision=identity("effective"),
                flavor_set=identity("flavors"),
                index_binding=identity("index"),
                build_plan=identity("plan"),
                classification=identity("classification"),
                authorization=identity("authorization"),
                build_result=identity("result"),
                expected_outputs=("build/libportable.a",),
                build_outputs=(
                    RepositoryBuildOutput("build/libportable.a", identity("library")),
                ),
            )
            cas = FileSystemCAS(root / "cas")
            references = ReferenceIndex(root / "indexes", cas)
            quarantine = QuarantineStore(root / "source-cache", cas)

            class MaterializingProvider:
                def index(self, tree, files):
                    sidecar = tree / ".codegraph"
                    sidecar.mkdir()
                    database = sidecar / "codegraph.db"
                    database.write_bytes(b"graph")
                    return SourceIntelligenceArtifact.create(
                        provider_id="fixture-intelligence",
                        provider_version="test-fixture-v1",
                        runtime_version="1.1.1",
                        executable_identity="sha256:" + "d" * 64,
                        source_tree_identity=generated_source_tree_identity(files),
                        source_snapshot_identity=generated_source_snapshot_identity(
                            files
                        ),
                        capabilities=("call-graph", "declarations", "references"),
                        provider_properties=(
                            "built-with-version=1.1.1",
                            "extraction-version=1",
                        ),
                        artifact_path=".codegraph/codegraph.db",
                        artifact_media_type="application/vnd.sqlite3",
                        artifact_identity=(
                            f"sha256:{hashlib.sha256(b'graph').hexdigest()}"
                        ),
                        document_count=1,
                        symbol_count=1,
                        relationship_count=0,
                        unresolved_relationship_count=0,
                        warning_count=0,
                    )

                def verify(self, tree, files, evidence):
                    del tree, files
                    return evidence

            cache = QuarantineRepositorySourceCache(
                quarantine,
                cas,
                references,
                source_intelligence_provider=MaterializingProvider(),
                source_intelligence_mode=SourceIntelligenceMode.REQUIRED,
                source_intelligence_provider_id="fixture-intelligence",
                source_intelligence_artifact_path=".codegraph/codegraph.db",
            )
            record = cache.admit(
                RepositoryCheckout(source, COMMIT, identity("resolver")),
                capture,
                admission,
            )
            cache_reference = next(
                reference
                for reference in cas.iter_refs()
                if reference.identity == record.uri
            )
            cache_manifest = cas.get_manifest(cache_reference)
            repeated = cache.admit(
                RepositoryCheckout(source, COMMIT, identity("resolver")),
                capture,
                admission,
            )

            self.assertEqual(repeated, record)
            self.assertEqual(
                cache_manifest["materialized_source_intelligence"][
                    "source_tree_identity"
                ],
                generated_source_tree_identity({"build.txt": b"portable source"}),
            )
            self.assertEqual(
                cache_manifest["source_intelligence_status"],
                {
                    "schema": "literate-ai/source-intelligence-stage-status@1",
                    "stage": "repository-source-admission",
                    "mode": "required",
                    "state": "current",
                    "provider_id": "fixture-intelligence",
                },
            )
            self.assertTrue(
                (
                    root
                    / "source-cache"
                    / "quarantine"
                    / capture.snapshot.identity.digest
                    / "tree"
                    / ".codegraph"
                    / "codegraph.db"
                ).is_file()
            )
            self.assertIsNotNone(
                references.resolve("repository-source", admission.cache_key.digest)
            )
            off_cache = QuarantineRepositorySourceCache.from_project_policy(
                quarantine,
                cas,
                source_intelligence_policy("none", SourceIntelligenceMode.OFF),
                references,
            )
            off_record = off_cache.admit(
                RepositoryCheckout(source, COMMIT, identity("resolver")),
                capture,
                admission,
            )
            off_reference = next(
                reference
                for reference in cas.iter_refs()
                if reference.identity == off_record.uri
            )
            off_manifest = cas.get_manifest(off_reference)
            self.assertEqual(off_manifest["source_intelligence_status"]["state"], "off")
            self.assertFalse(
                (
                    root
                    / "source-cache"
                    / "quarantine"
                    / capture.snapshot.identity.digest
                    / "tree"
                    / ".codegraph"
                ).exists()
            )

            class InterruptedProvider:
                def index(self, tree, files):
                    del files
                    sidecar = tree / ".codegraph"
                    sidecar.mkdir()
                    (sidecar / "codegraph.db").write_bytes(b"partial")
                    raise RuntimeError("simulated provider interruption")

                def verify(self, tree, files, evidence):  # pragma: no cover
                    raise AssertionError((tree, files, evidence))

            required_cache = QuarantineRepositorySourceCache(
                quarantine,
                cas,
                references,
                source_intelligence_provider=InterruptedProvider(),
                source_intelligence_mode=SourceIntelligenceMode.REQUIRED,
                source_intelligence_provider_id="fixture-intelligence",
                source_intelligence_artifact_path=".codegraph/codegraph.db",
            )
            with self.assertRaisesRegex(RuntimeError, "provider interruption"):
                required_cache.admit(
                    RepositoryCheckout(source, COMMIT, identity("resolver")),
                    capture,
                    admission,
                )
            self.assertFalse(
                (
                    root
                    / "source-cache"
                    / "quarantine"
                    / capture.snapshot.identity.digest
                    / "tree"
                    / ".codegraph"
                ).exists()
            )
            preferred_cache = QuarantineRepositorySourceCache(
                quarantine,
                cas,
                references,
                source_intelligence_provider=InterruptedProvider(),
                source_intelligence_mode=SourceIntelligenceMode.PREFERRED,
                source_intelligence_provider_id="fixture-intelligence",
                source_intelligence_artifact_path=".codegraph/codegraph.db",
            )
            preferred_record = preferred_cache.admit(
                RepositoryCheckout(source, COMMIT, identity("resolver")),
                capture,
                admission,
            )
            preferred_reference = next(
                reference
                for reference in cas.iter_refs()
                if reference.identity == preferred_record.uri
            )
            preferred_manifest = cas.get_manifest(preferred_reference)
            self.assertIsNone(preferred_manifest["materialized_source_intelligence"])
            self.assertEqual(
                preferred_manifest["source_intelligence_status"],
                {
                    "schema": "literate-ai/source-intelligence-stage-status@1",
                    "stage": "repository-source-admission",
                    "mode": "preferred",
                    "state": "unavailable",
                    "provider_id": "fixture-intelligence",
                    "reason_code": ("source-intelligence.provider-execution-failed"),
                },
            )
            self.assertFalse(
                (
                    root
                    / "source-cache"
                    / "quarantine"
                    / capture.snapshot.identity.digest
                    / "tree"
                    / ".codegraph"
                ).exists()
            )
            self.assertEqual(
                cache_manifest["admission"]["build_outputs"],
                [
                    {
                        "path": "build/libportable.a",
                        "identity": identity("library").to_dict(),
                    }
                ],
            )
            self.assertEqual(
                references.resolve(
                    "repository-source", admission.cache_key.digest
                ).target.identity,
                preferred_record.uri,
            )


if __name__ == "__main__":
    unittest.main()

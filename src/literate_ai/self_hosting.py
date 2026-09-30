"""Deterministic exact-snapshot replication conformance fixture.

This module fills the generation lifecycle's provider port with an exact replay adapter.
It does not invoke a coding CLI or claim that a model authored a successor framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    discover_python_toolchain,
)
from literate_ai.adapters.component_lock_planning import FilesystemComponentLockPlanner
from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    PortableHostDependencyObserver,
    build_cyclonedx_bom,
    declare_optional_python_distributions,
    observe_installed_python_distributions,
    python_import_names,
)
from literate_ai.adapters.flavor_markdown import (
    FlavorMarkdownError,
    parse_flavor_markdown,
)
from literate_ai.adapters.intelligence import (
    SourceIntelligenceProviderSelection,
)
from literate_ai.adapters.lifecycle import (
    GeneratedTreeSecurityClassifier,
    LifecycleEventStoreAdapter,
    PipelineValidatorAdapter,
    PolicyBuildAuthorizer,
    PythonBuildAdapter,
    WorkspaceTreeAdapter,
)
from literate_ai.adapters.specifications import (
    LiterateMarkdownProvider,
    LoadedOpenSpec,
    OpenSpecError,
    OpenSpecProvider,
)
from literate_ai.application import (
    ComponentSourceEvidenceReadiness,
    GeneratedTestSuitePolicy,
    GenerationContextBinding,
    GenerationExecutionPlan,
    GenerationOrchestrator,
    GenerationRequest,
    GenerationStatus,
    IndependentAcceptancePolicy,
    compile_generation_execution_plan,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
    project_locked_generation_authority,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    Capability,
    ComponentAuthoring,
    ComponentDefinition,
    ComponentRevision,
    ContentIdentity,
    ContentReference,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    FlavorAxis,
    FlavorDefinition,
    FlavorRevision,
    HashAlgorithm,
    LockedComponentRevision,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    TargetConstraint,
    TargetProfile,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
)
from literate_ai.models import Locality, ModelEndpoint
from literate_ai.ports import NON_EXECUTING_EXACT_TREE_PROFILE
from literate_ai.projects import discover_project, project_boundary
from literate_ai.registry import FlavorDescriptor
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequestDeclaration,
    LiveBuildAuthorizationVerifier,
    RuleBasedSourceScanner,
    SecurityPolicy,
    baseline_python_rules,
)
from literate_ai.sources import SourceSnapshotter
from literate_ai.storage import AppendOnlyEventStore
from literate_ai.validation import PythonSyntaxValidator, ValidationPipeline
from literate_ai.workspace import WorkspaceTreeStore

SNAPSHOT_REPLICATION_REPORT_SCHEMA = "literate-ai/snapshot-replication@1"
SNAPSHOT_REPLICATION_SKILL_SCHEMA = "literate-ai/snapshot-replication-skill-set@1"
SNAPSHOT_REPLICATION_PROOF_SCHEMA = "literate-ai/snapshot-replication-proof@1"
FIXED_TIME = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
_OPERATIONAL_DIRECTORIES = frozenset({"__pycache__"})
_OPERATIONAL_SUFFIXES = frozenset({".pyc", ".pyo"})


class SelfHostError(RuntimeError):
    """The snapshot-replication proof cannot establish its invariant."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FrozenPackageTree:
    """Complete UTF-8 package tree after declared operational exclusions."""

    files: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, package_root: str | Path) -> FrozenPackageTree:
        configured = Path(package_root)
        if configured.is_symlink() or not configured.is_dir():
            raise SelfHostError(
                "self-host.source-unavailable",
                "snapshot source must be a regular package directory",
            )
        root = configured.resolve(strict=True)
        files: list[tuple[str, str]] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for name in sorted(directories):
                path = current_path / name
                if path.is_symlink():
                    raise SelfHostError(
                        "self-host.source-symlink",
                        f"snapshot source contains a directory symlink: {name}",
                    )
                if name not in _OPERATIONAL_DIRECTORIES:
                    kept.append(name)
            directories[:] = kept
            for name in sorted(filenames):
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    raise SelfHostError(
                        "self-host.source-symlink",
                        f"snapshot source contains a file symlink: {relative}",
                    )
                if path.suffix in _OPERATIONAL_SUFFIXES:
                    continue
                if not path.is_file():
                    raise SelfHostError(
                        "self-host.source-invalid",
                        f"snapshot source entry is not a regular file: {relative}",
                    )
                try:
                    content = path.read_bytes().decode("utf-8")
                except (OSError, UnicodeError) as exc:
                    raise SelfHostError(
                        "self-host.source-not-text",
                        f"snapshot package file is not UTF-8 text: {relative}",
                    ) from exc
                files.append((relative, content))
        required = {"__init__.py", "self_hosting.py", "py.typed"}
        missing = required - {path for path, _content in files}
        if missing:
            raise SelfHostError(
                "self-host.source-incomplete",
                "snapshot package lacks required files: " + ", ".join(sorted(missing)),
            )
        return cls(tuple(sorted(files)))

    @property
    def manifest(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "path": path,
                "size": len(content.encode("utf-8")),
                "digest": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
            }
            for path, content in self.files
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "policy": {
                    "excluded_directories": sorted(_OPERATIONAL_DIRECTORIES),
                    "excluded_suffixes": sorted(_OPERATIONAL_SUFFIXES),
                    "encoding": "utf-8",
                },
                "files": self.manifest,
            }
        )

    @property
    def generated_files(self) -> dict[str, str]:
        return {f"source/literate_ai/{path}": content for path, content in self.files}

    def generated_test_suite_content(
        self,
        *,
        recipe_identity: str,
        specification_references: tuple[str, ...],
    ) -> str:
        """Return the standard generated-suite artifact for exact replay."""

        references = list(specification_references)
        return canonical_json_bytes(
            {
                "schema": GENERATED_TEST_SUITE_SCHEMA,
                "recipe_identity": recipe_identity,
                "generation_mode": "major-rebuild",
                "cases": [
                    {
                        "case_id": "every-python-source-compiled",
                        "category": "example",
                        "specification_refs": references,
                        "arguments": ["compiled-source-coverage"],
                        "expected_result": True,
                    },
                    {
                        "case_id": "snapshot-path-boundary",
                        "category": "boundary",
                        "specification_refs": references,
                        "arguments": ["portable-path-boundary"],
                        "expected_result": True,
                    },
                    {
                        "case_id": "generated-package-matches-frozen-snapshot",
                        "category": "invariant",
                        "specification_refs": references,
                        "arguments": ["exact-tree-equality"],
                        "expected_result": self.identity.uri,
                    },
                ],
            }
        ).decode("utf-8")


@dataclass(frozen=True, slots=True)
class SnapshotReplicationResult:
    stable_outputs: Mapping[str, object]
    accepted_root: Path
    accepted_package_root: Path


@dataclass(frozen=True, slots=True)
class _SkillStage:
    stage_id: str
    produces_tree: bool
    instructions: str
    response_schema_name: str
    response_schema: Mapping[str, object]
    content_kind: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class _SkillSet:
    skill_set_id: str
    version: str
    stages: tuple[_SkillStage, ...]
    identity: ContentIdentity


class _ExactReadiness:
    def __init__(
        self,
        source_snapshot_id: str,
        specification_id: str,
        skill_set_id: str,
    ) -> None:
        self.source_snapshot_id = source_snapshot_id
        self.specification_id = specification_id
        self.skill_set_id = skill_set_id

    def inspect(
        self,
        authority: LockedGenerationAuthority,
        component_revision: ContentIdentity,
    ) -> ComponentSourceEvidenceReadiness:
        if not any(
            item.revision.identity == component_revision
            for item in authority.lock.nodes
        ):
            raise SelfHostError(
                "self-host.readiness-component-unknown",
                "readiness target is absent from locked authority",
            )
        return ComponentSourceEvidenceReadiness(
            component_revision=component_revision,
            source_snapshot_ids=(self.source_snapshot_id,),
            evidence_ids=(self.specification_id, self.skill_set_id),
            source_ready=True,
            evidence_ready=True,
        )


class ExactSnapshotReplayProvider:
    """Lifecycle test adapter whose only output is one exact frozen snapshot.

    It implements the model-provider port to exercise orchestration, but is
    deterministic fixture code rather than a model or coding-CLI source generator.
    """

    provider_id = "conformance-provider:exact-snapshot-replay@1"

    def __init__(
        self,
        tree: FrozenPackageTree,
        *,
        source_snapshot_id: str,
        specification_id: str,
        skill_set_id: str,
        generated_test_suite_content: str,
        source_sbom_content: str,
    ) -> None:
        self.tree = tree
        self.source_snapshot_id = source_snapshot_id
        self.specification_id = specification_id
        self.skill_set_id = skill_set_id
        self.generated_test_suite_content = generated_test_suite_content
        self.source_sbom_content = source_sbom_content
        self.calls: list[str] = []

    def complete_structured(
        self, request: Mapping[str, object]
    ) -> Mapping[str, object]:
        stage = request.get("stage_id")
        source_ids = request.get("source_snapshot_ids")
        evidence_ids = request.get("evidence_ids")
        if source_ids != [self.source_snapshot_id] or evidence_ids != [
            self.specification_id,
            self.skill_set_id,
        ]:
            raise SelfHostError(
                "self-host.model-evidence-mismatch",
                "snapshot replay did not receive the exact pinned evidence closure",
            )
        if stage == "plan":
            self.calls.append("plan")
            return {
                "source_snapshot_id": self.source_snapshot_id,
                "specification_id": self.specification_id,
                "skill_set_id": self.skill_set_id,
                "package_tree_id": self.tree.identity.uri,
                "file_manifest": list(self.tree.manifest),
            }
        if stage == "generate":
            prior = request.get("prior_stage_outputs")
            inventory = prior.get("plan") if isinstance(prior, Mapping) else None
            if (
                not isinstance(inventory, Mapping)
                or inventory.get("package_tree_id") != self.tree.identity.uri
            ):
                raise SelfHostError(
                    "self-host.model-stage-mismatch",
                    "generation stage lacks the exact inventory-stage result",
                )
            self.calls.append("generate")
            return {
                "files": {
                    **self.tree.generated_files,
                    GENERATED_TEST_SUITE_PATH: self.generated_test_suite_content,
                    CYCLONEDX_SOURCE_SBOM_PATH: self.source_sbom_content,
                },
                "source_snapshot_id": self.source_snapshot_id,
                "specification_id": self.specification_id,
                "skill_set_id": self.skill_set_id,
            }
        raise SelfHostError(
            "self-host.model-stage-unknown",
            f"unknown snapshot-replication stage: {stage!r}",
        )


class _ExactSnapshotGeneratedTreeTester:
    """Prove that every replayed Python source reached the exact build artifact."""

    runner_id = "tester:exact-snapshot-generated-tree@1"

    def __init__(self, tree: FrozenPackageTree, suite_content: str) -> None:
        self.tree = tree
        self.suite_content = suite_content

    def run(
        self,
        artifact,
        build,
        classification,
        dependency_resolution,
        test_suite,
    ):
        files = artifact["files"]
        compiled = build["compiled_files"]
        python_sources = sorted(path for path in files if path.endswith(".py"))
        suite_content = files.get(GENERATED_TEST_SUITE_PATH)
        checks = (
            len(compiled) == len(python_sources)
            and dependency_resolution["artifact_digest"] == build["artifact_digest"],
            suite_content == self.suite_content,
            test_suite.content_identity == artifact["generated_test_suite_identity"],
        )
        passed = all(checks)
        return {
            "passed": passed,
            "runner_id": self.runner_id,
            "execution_profile": NON_EXECUTING_EXACT_TREE_PROFILE,
            "profile_reason": (
                "deterministic snapshot replay proves exact compilation coverage; "
                "the isolated candidate probe executes after replication"
            ),
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": test_suite.content_identity,
            "total": 3,
            "passed_count": sum(checks),
            "failed_count": len(checks) - sum(checks),
            "skipped_count": 0,
            "case_results": [
                {
                    "case_id": "every-python-source-compiled",
                    "passed": checks[0],
                    "classification_digest": classification["classification_digest"],
                },
                {"case_id": "snapshot-path-boundary", "passed": checks[1]},
                {
                    "case_id": "generated-package-matches-frozen-snapshot",
                    "passed": checks[2],
                },
            ],
        }


class _ExactSnapshotAcceptanceRunner:
    """Non-executing lifecycle acceptance for the deterministic replay fixture."""

    runner_id = "acceptance:exact-snapshot-tree@1"

    def __init__(
        self, tree: FrozenPackageTree, suite_content: str, suite_identity: str
    ) -> None:
        self.tree = tree
        self.suite_content = suite_content
        self.acceptance_suite_identity = suite_identity

    def run(
        self,
        artifact,
        build,
        classification,
        generated_tests,
        dependency_resolution,
    ):
        files = artifact["files"]
        expected_files = {
            **self.tree.generated_files,
            GENERATED_TEST_SUITE_PATH: self.suite_content,
            CYCLONEDX_SOURCE_SBOM_PATH: artifact["files"][CYCLONEDX_SOURCE_SBOM_PATH],
        }
        passed = (
            files == expected_files
            and generated_tests["passed"] is True
            and generated_tests["test_suite_identity"] == self.acceptance_suite_identity
            and dependency_resolution["artifact_digest"] == build["artifact_digest"]
        )
        return {
            "passed": passed,
            "runner_id": self.runner_id,
            "execution_profile": NON_EXECUTING_EXACT_TREE_PROFILE,
            "profile_reason": (
                "deterministic replay accepts exact file equality; executable behavior "
                "is proven by the isolated two-replication candidate probes"
            ),
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": self.acceptance_suite_identity,
            "total": 1,
            "passed_count": 1 if passed else 0,
            "failed_count": 0 if passed else 1,
            "skipped_count": 0,
            "case_results": [
                {
                    "case_id": "generated-package-matches-frozen-snapshot",
                    "passed": passed,
                    "subject_tree_identity": self.tree.identity.uri,
                }
            ],
        }


def _content_identity(path: Path) -> ContentIdentity:
    content = path.read_bytes()
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


@dataclass(frozen=True, slots=True)
class _PythonDependencyAuthority:
    core: tuple[str, ...]
    optional_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    optional_imports: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def optional_mapping(self) -> dict[str, tuple[str, ...]]:
        return dict(self.optional_groups)

    def optional_import_mapping(self) -> dict[str, tuple[str, ...]]:
        return dict(self.optional_imports)


def _project_runtime_requirements(path: Path) -> _PythonDependencyAuthority:
    if path.is_symlink() or not path.is_file():
        raise SelfHostError(
            "self-host.project-manifest-unavailable",
            "self-host project dependency manifest is unavailable",
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document["project"]
        dependencies = project["dependencies"]
        optional_dependencies = project.get("optional-dependencies", {})
    except (KeyError, OSError, TypeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SelfHostError(
            "self-host.project-manifest-invalid",
            "self-host project dependency manifest is invalid",
        ) from exc
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or any(not isinstance(item, str) or not item.strip() for item in dependencies)
    ):
        raise SelfHostError(
            "self-host.project-dependencies-invalid",
            "self-host project must declare a non-empty Python dependency list",
        )
    if not isinstance(optional_dependencies, Mapping):
        raise SelfHostError(
            "self-host.project-optional-dependencies-invalid",
            "self-host project optional Python dependency groups are invalid",
        )
    optional_groups: list[tuple[str, tuple[str, ...]]] = []
    for group, values in sorted(optional_dependencies.items()):
        if (
            not isinstance(group, str)
            or not group.strip()
            or not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)
        ):
            raise SelfHostError(
                "self-host.project-optional-dependencies-invalid",
                "self-host project optional Python dependency groups are invalid",
            )
        optional_groups.append((group, tuple(values)))
    return _PythonDependencyAuthority(tuple(dependencies), tuple(optional_groups))


def _source_bom_runtime_requirements(path: Path) -> _PythonDependencyAuthority:
    if path.is_symlink() or not path.is_file():
        raise SelfHostError(
            "self-host.dependency-authority-unavailable",
            "self-host SOURCE BOM dependency authority is unavailable",
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        root_ref = document["metadata"]["component"]["bom-ref"]
        components = document["components"]
        dependencies = document["dependencies"]
    except (KeyError, OSError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelfHostError(
            "self-host.dependency-authority-invalid",
            "self-host SOURCE BOM dependency authority is invalid",
        ) from exc
    if (
        not isinstance(root_ref, str)
        or not isinstance(components, list)
        or not isinstance(dependencies, list)
    ):
        raise SelfHostError(
            "self-host.dependency-authority-invalid",
            "self-host SOURCE BOM dependency graph is invalid",
        )
    direct_refs: set[str] = set()
    for edge in dependencies:
        if not isinstance(edge, Mapping) or edge.get("ref") != root_ref:
            continue
        targets = edge.get("dependsOn")
        if not isinstance(targets, list) or any(
            not isinstance(item, str) for item in targets
        ):
            raise SelfHostError(
                "self-host.dependency-authority-invalid",
                "self-host SOURCE BOM root dependencies are invalid",
            )
        direct_refs.update(targets)
    requirements: set[str] = set()
    optional_groups: dict[str, set[str]] = {}
    optional_imports: dict[str, set[str]] = {}
    for component in components:
        if not isinstance(component, Mapping):
            raise SelfHostError(
                "self-host.dependency-authority-invalid",
                "self-host SOURCE BOM component is invalid",
            )
        if component.get("bom-ref") not in direct_refs:
            continue
        properties = component.get("properties")
        if not isinstance(properties, list):
            continue
        declared = {
            str(item["value"])
            for item in properties
            if isinstance(item, Mapping)
            and item.get("name") == "literate-ai:python-declared-requirement"
            and isinstance(item.get("value"), str)
            and str(item["value"]).strip()
        }
        optional_records = [
            str(item["value"])
            for item in properties
            if isinstance(item, Mapping)
            and item.get("name") == "literate-ai:python-optional-requirement"
            and isinstance(item.get("value"), str)
            and str(item["value"]).strip()
        ]
        import_names = {
            str(item["value"])
            for item in properties
            if isinstance(item, Mapping)
            and item.get("name") == "literate-ai:python-top-level-import"
            and isinstance(item.get("value"), str)
            and str(item["value"]).strip()
        }
        if not optional_records:
            if component.get("scope") == "optional":
                raise SelfHostError(
                    "self-host.dependency-authority-invalid",
                    "self-host optional Python dependency lacks group authority",
                )
            requirements.update(declared)
            continue
        if component.get("scope") != "optional":
            raise SelfHostError(
                "self-host.dependency-authority-invalid",
                "self-host optional Python dependency lacks optional scope",
            )
        for raw in optional_records:
            try:
                record = json.loads(raw)
                group = record["group"]
                requirement = record["requirement"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise SelfHostError(
                    "self-host.dependency-authority-invalid",
                    "self-host optional Python dependency record is invalid",
                ) from exc
            if (
                not isinstance(record, dict)
                or set(record) != {"group", "requirement"}
                or not isinstance(group, str)
                or not group.strip()
                or not isinstance(requirement, str)
                or not requirement.strip()
                or requirement not in declared
            ):
                raise SelfHostError(
                    "self-host.dependency-authority-invalid",
                    "self-host optional Python dependency record is invalid",
                )
            optional_groups.setdefault(group, set()).add(requirement)
        component_name = component.get("name")
        if not isinstance(component_name, str) or not component_name.strip():
            raise SelfHostError(
                "self-host.dependency-authority-invalid",
                "self-host optional Python dependency lacks a package name",
            )
        optional_imports.setdefault(component_name, set()).update(import_names)
    if not requirements:
        raise SelfHostError(
            "self-host.dependency-authority-invalid",
            "self-host SOURCE BOM lacks declared Python requirements",
        )
    return _PythonDependencyAuthority(
        tuple(sorted(requirements)),
        tuple(
            (group, tuple(sorted(values)))
            for group, values in sorted(optional_groups.items())
        ),
        tuple(
            (name, tuple(sorted(values)))
            for name, values in sorted(optional_imports.items())
        ),
    )


def _runtime_dependency_authority(
    package_root: Path,
) -> tuple[Path, _PythonDependencyAuthority]:
    project_file = package_root.parent.parent / "pyproject.toml"
    if project_file.exists() or project_file.is_symlink():
        return project_file, _project_runtime_requirements(project_file)
    source_bom = package_root.parent.joinpath(
        *CYCLONEDX_SOURCE_SBOM_PATH.removeprefix("source/").split("/")
    )
    return source_bom, _source_bom_runtime_requirements(source_bom)


def _require_pinned_files_unchanged(
    pinned: tuple[tuple[str, Path, ContentIdentity], ...],
) -> None:
    """Fail closed when any direct replication-authority file changes."""

    for label, path, expected in pinned:
        try:
            changed = (
                path.is_symlink()
                or not path.is_file()
                or _content_identity(path) != expected
            )
        except OSError as exc:
            raise SelfHostError(
                "self-host.input-changed",
                f"snapshot-replication {label} changed during the operation",
            ) from exc
        if changed:
            raise SelfHostError(
                "self-host.input-changed",
                f"snapshot-replication {label} changed during the operation",
            )


def _load_replication_proof(path: Path, skills_path: Path) -> ContentReference:
    if path.is_symlink() or not path.is_file():
        raise SelfHostError(
            "self-host.proof-unavailable",
            "snapshot-replication proof manifest is unavailable",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelfHostError(
            "self-host.proof-invalid",
            "snapshot-replication proof manifest is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "proof_id",
        "classification",
        "coding_cli_involved",
        "claim",
        "dependencies",
        "skill_set",
    }:
        raise SelfHostError(
            "self-host.proof-invalid",
            "snapshot-replication proof manifest fields are invalid",
        )
    if (
        raw.get("schema") != SNAPSHOT_REPLICATION_PROOF_SCHEMA
        or raw.get("classification") != "deterministic-offline-conformance-replay"
        or raw.get("coding_cli_involved") is not False
        or not isinstance(raw.get("proof_id"), str)
        or not raw["proof_id"]
        or not isinstance(raw.get("claim"), str)
        or not raw["claim"]
        or raw.get("dependencies") != ["literate-ai-framework-checkout"]
    ):
        raise SelfHostError(
            "self-host.proof-invalid",
            "snapshot-replication proof classification is invalid",
        )
    try:
        reference = ContentReference.from_dict(raw["skill_set"])
    except (TypeError, ValueError) as exc:
        raise SelfHostError(
            "self-host.proof-invalid",
            "snapshot-replication skill reference is invalid",
        ) from exc
    configured = path.parent / reference.uri
    if (
        reference.kind != "snapshot-replication-skill-set"
        or configured.is_symlink()
        or not configured.is_file()
        or configured.resolve(strict=True) != skills_path
        or _content_identity(skills_path) != reference.identity
    ):
        raise SelfHostError(
            "self-host.skills-unpinned",
            "snapshot-replication skill set does not match its proof pin",
        )
    return reference


def _load_skills(path: Path, expected_reference: ContentReference) -> _SkillSet:
    if path.is_symlink() or not path.is_file():
        raise SelfHostError(
            "self-host.skills-unavailable",
            "snapshot-replication skill set is unavailable",
        )
    identity = _content_identity(path)
    if expected_reference.identity != identity:
        raise SelfHostError(
            "self-host.skills-unpinned",
            "snapshot-replication skill set does not match the proof pin",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication skill set is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "skill_set_id",
        "version",
        "stages",
    }:
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication skill-set fields are invalid",
        )
    if raw.get("schema") != SNAPSHOT_REPLICATION_SKILL_SCHEMA:
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication skill-set schema is unsupported",
        )
    stages_value = raw.get("stages")
    if not isinstance(stages_value, list):
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication stages must be an array",
        )
    stages: list[_SkillStage] = []
    expected_stage_fields = {
        "stage_id",
        "produces_tree",
        "instructions",
        "response_schema_name",
        "response_schema",
        "content_kind",
        "max_output_tokens",
    }
    for value in stages_value:
        if not isinstance(value, dict) or set(value) != expected_stage_fields:
            raise SelfHostError(
                "self-host.skills-invalid",
                "snapshot-replication stage fields are invalid",
            )
        if not isinstance(value["response_schema"], dict):
            raise SelfHostError(
                "self-host.skills-invalid",
                "snapshot-replication response schema must be an object",
            )
        try:
            stages.append(_SkillStage(**value))
        except TypeError as exc:
            raise SelfHostError(
                "self-host.skills-invalid",
                "snapshot-replication stage values are invalid",
            ) from exc
    if [item.stage_id for item in stages] != ["plan", "generate"]:
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication skill set requires plan then generate stages",
        )
    if stages[0].produces_tree or not stages[1].produces_tree:
        raise SelfHostError(
            "self-host.skills-invalid",
            "only the final snapshot-replication stage may produce a tree",
        )
    skill_set_id = raw.get("skill_set_id")
    version = raw.get("version")
    if not isinstance(skill_set_id, str) or not isinstance(version, str):
        raise SelfHostError(
            "self-host.skills-invalid",
            "snapshot-replication skill identity must be text",
        )
    return _SkillSet(skill_set_id, version, tuple(stages), identity)


def _materialized_snapshot(tree: FrozenPackageTree) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(
        prefix="literate-snapshot-replication-"
    ) as temporary:
        root = Path(temporary)
        for relative, content in tree.files:
            target = root.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        capture = SourceSnapshotter().snapshot_local(root)
    return capture.snapshot.identity.uri, capture.snapshot.tree_identity.uri


def _load_component_authority(
    component_file: Path,
) -> tuple[ComponentAuthoring, LoadedOpenSpec]:
    if component_file.name != "component.md":
        raise SelfHostError(
            "self-host.component-authority-required",
            "snapshot replication requires strict component.md authority",
        )
    project = discover_project(component_file)
    if project is None:
        raise SelfHostError(
            "self-host.project-required",
            "snapshot replication Component must belong to a Literate AI project",
        )
    try:
        content = component_file.read_text(encoding="utf-8")
        authoring = parse_component_markdown(
            component_file, content, project_root=project.root
        )
        if authoring.specification_provider != "literate-markdown":
            raise SelfHostError(
                "self-host.specification-provider-invalid",
                "snapshot replication requires the literate-markdown provider",
            )
        loaded = LiterateMarkdownProvider().load(
            component_file.parent,
            authoring.specification_roots,
            id_prefix=(f"{authoring.coordinate.namespace}.{authoring.coordinate.name}"),
        )
        loaded.require_unchanged(component_file.parent)
    except SelfHostError:
        raise
    except (ComponentMarkdownError, OpenSpecError, OSError, UnicodeError) as exc:
        raise SelfHostError(
            "self-host.component-authority-invalid",
            "snapshot replication Component authority is invalid",
        ) from exc
    return authoring, loaded


def _locked_component_definition(
    authoring: ComponentAuthoring, revision: LockedComponentRevision
) -> ComponentDefinition:
    interfaces = {(item.kind, item.uri): item for item in revision.public_interfaces}
    return ComponentDefinition(
        coordinate=authoring.coordinate,
        version=authoring.version,
        display_name=authoring.display_name,
        description=authoring.description,
        profiles=authoring.profiles,
        sample=authoring.sample,
        provides=tuple(
            Capability(
                item.name,
                item.version,
                (
                    None
                    if item.interface is None
                    else interfaces[(item.interface.kind, item.interface.uri)].identity
                ),
            )
            for item in authoring.provides
        ),
        requires=authoring.requires,
        specification_provider=authoring.specification_provider,
        specification_roots=authoring.specification_roots,
        authoring_inputs=revision.authoring_inputs,
        workflow_definition=revision.workflow_definition,
        routing_policy=revision.routing_policy,
        flavor_slots=authoring.flavor_slots,
        entrypoints=authoring.entrypoints,
        acceptance_contracts=revision.acceptance_contracts,
        source_dependencies=(),
    )


def _runtime_recipe(
    definition: ComponentDefinition,
    specification,
    source_snapshot: ContentIdentity,
    skills: _SkillSet,
    platform_flavor: FlavorDescriptor,
):
    base = ComponentRevision(definition, specification, source_snapshot, (), ())
    target = TargetProfile(
        "snapshot-replication-python",
        "1.0.0",
        "offline-exact",
        skills.identity,
        (
            TargetConstraint(
                FlavorAxis.PLATFORM_OS,
                platform_flavor.axis_value,
            ),
            TargetConstraint(
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                "python",
            ),
        ),
    )
    return base, target


def _host_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise SelfHostError(
        "self-host.platform-unsupported", f"unsupported host platform: {sys.platform}"
    )


def _load_platform_flavor(path: Path) -> FlavorDescriptor:
    if path.is_symlink() or not path.is_file():
        raise SelfHostError(
            "self-host.platform-flavor-unavailable",
            "snapshot-replication platform Flavor is unavailable",
        )
    try:
        content = path.read_bytes()
        if path.name == "flavor.md":
            boundary = project_boundary(path.parent, legacy=path.parent)

            def read_reference(uri: str) -> bytes:
                configured = path.parent.joinpath(*PurePosixPath(uri).parts)
                resolved = configured.resolve(strict=True)
                if (
                    configured.is_symlink()
                    or not resolved.is_file()
                    or not resolved.is_relative_to(boundary.resolve(strict=True))
                ):
                    raise ValueError("unsafe Flavor reference")
                return resolved.read_bytes()

            definition = parse_flavor_markdown(content, source=path.as_posix()).resolve(
                read_reference
            )
        else:
            definition = FlavorDefinition.from_dict(json.loads(content))
    except (
        FlavorMarkdownError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise SelfHostError(
            "self-host.platform-flavor-invalid",
            "snapshot-replication platform Flavor is invalid",
        ) from exc
    host_os = _host_os()
    if (
        definition.primary_axis is not FlavorAxis.PLATFORM_OS
        or definition.supported_targets != (host_os,)
    ):
        raise SelfHostError(
            "self-host.platform-flavor-mismatch",
            "snapshot-replication platform Flavor does not match the current host",
        )
    loaded = OpenSpecProvider().load(
        path.parent,
        tuple(item.uri for item in definition.specification_fragments),
    )
    expected = tuple(item.identity for item in definition.specification_fragments)
    actual = tuple(item.identity for item in loaded.specification_set.artifacts)
    if expected != actual:
        raise SelfHostError(
            "self-host.platform-specification-mismatch",
            "snapshot-replication platform Flavor specification changed",
        )
    loaded.require_unchanged(path.parent)
    revision = FlavorRevision(
        definition,
        ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())
        if path.name == "flavor.md"
        else None,
        (),
    )
    return FlavorDescriptor(revision.identity, definition, host_os)


def _execution_plan(
    definition: ComponentDefinition,
    skills: _SkillSet,
    acceptance_policy: IndependentAcceptancePolicy,
    *,
    workflow_content: bytes,
    routing_content: bytes,
) -> GenerationExecutionPlan:
    endpoint = ModelEndpoint(
        endpoint_id="exact-snapshot-replay",
        provider="deterministic-conformance-fixture",
        model="exact-snapshot-replay@1",
        base_url="unix:///snapshot-replication",
        locality=Locality.LOCAL,
        capabilities=("structured-output", "source-generation"),
        context_tokens=4_000_000,
        model_revision=None,
    )
    plan = compile_generation_execution_plan(
        component=definition,
        workflow_content=workflow_content,
        routing_content=routing_content,
        endpoint=endpoint,
        generation_prompt=(
            "Replay the exact snapshot using conformance skill set "
            f"{skills.identity.uri}; no coding CLI is involved."
        ),
        independent_acceptance_policy=acceptance_policy,
    )
    if tuple(item.stage_id for item in plan.model_stages) != tuple(
        item.stage_id for item in skills.stages
    ):
        raise SelfHostError(
            "self-host.workflow-skill-mismatch",
            "pinned workflow stages do not match the snapshot-replication skill set",
        )
    bound_stages = []
    for stage, skill in zip(plan.model_stages, skills.stages, strict=True):
        if (
            stage.produces_tree != skill.produces_tree
            or stage.content_kind != skill.content_kind
        ):
            raise SelfHostError(
                "self-host.workflow-skill-mismatch",
                "pinned workflow and skill stage output contracts disagree",
            )
        bound_stages.append(
            replace(
                stage,
                instructions=stage.instructions + "\n\n" + skill.instructions,
                response_schema_name=skill.response_schema_name,
                response_schema=skill.response_schema,
                max_output_tokens=skill.max_output_tokens,
            )
        )
    return replace(plan, model_stages=tuple(bound_stages))


def run_snapshot_replication(
    *,
    source_root: str | Path,
    component_path: str | Path,
    skills_path: str | Path,
    proof_path: str | Path,
    platform_flavor_path: str | Path,
    runtime_root: str | Path,
    workspace_reference: str = "snapshot-replication/candidate",
    host_build_acknowledged: bool = False,
) -> SnapshotReplicationResult:
    """Replay, validate, classify, build, test, and accept one exact candidate copy."""

    configured_source = Path(source_root)
    if configured_source.is_symlink():
        raise SelfHostError(
            "self-host.source-symlink",
            "snapshot source cannot be a symbolic link",
        )
    package_root = configured_source.resolve(strict=True)
    if Path(__file__).resolve().parent != package_root:
        raise SelfHostError(
            "self-host.runtime-source-mismatch",
            "snapshot-replication runtime was not imported from the package "
            "it was asked to replicate",
        )
    dependency_authority, python_dependencies = _runtime_dependency_authority(
        package_root
    )
    configured_component = Path(component_path)
    configured_skills = Path(skills_path)
    configured_proof = Path(proof_path)
    configured_platform_flavor = Path(platform_flavor_path)
    if (
        configured_component.is_symlink()
        or configured_skills.is_symlink()
        or configured_proof.is_symlink()
        or configured_platform_flavor.is_symlink()
    ):
        raise SelfHostError(
            "self-host.recipe-symlink",
            "snapshot-replication Component, proof, skill, and platform Flavor "
            "inputs cannot be symbolic links",
        )
    component_file = configured_component.resolve(strict=True)
    sample_root = component_file.parent
    skills_file = configured_skills.resolve(strict=True)
    proof_file = configured_proof.resolve(strict=True)
    platform_flavor_file = configured_platform_flavor.resolve(strict=True)
    pinned_inputs = (
        (
            "runtime dependency authority",
            dependency_authority,
            _content_identity(dependency_authority),
        ),
        ("Component definition", component_file, _content_identity(component_file)),
        ("skill set", skills_file, _content_identity(skills_file)),
        ("proof manifest", proof_file, _content_identity(proof_file)),
        (
            "platform Flavor",
            platform_flavor_file,
            _content_identity(platform_flavor_file),
        ),
    )
    authoring, loaded_specification = _load_component_authority(component_file)
    proof_reference = _load_replication_proof(proof_file, skills_file)
    skills = _load_skills(skills_file, proof_reference)
    platform_flavor = _load_platform_flavor(platform_flavor_file)
    _require_pinned_files_unchanged(pinned_inputs)
    tree = FrozenPackageTree.capture(package_root)
    imported_python_names: set[str] = set()
    for relative, content in tree.files:
        if PurePosixPath(relative).suffix.casefold() == ".py":
            imported_python_names.update(python_import_names(content))
    source_snapshot_id, source_tree_id = _materialized_snapshot(tree)
    source_snapshot = ContentIdentity.parse_uri(source_snapshot_id)
    specification_id = loaded_specification.specification_set.identity.uri
    selectors = (
        f"+os:{platform_flavor.axis_value}",
        "+language:python",
    )
    lock_plan, authority_catalog = FilesystemComponentLockPlanner().plan_with_snapshot(
        sample_root,
        target_name="snapshot-replication-python",
        flavor_selectors=selectors,
    )
    lock_result = ComponentLockResolver().resolve(
        lock_plan,
        expected_input_evidence_identity=lock_plan.identity,
    )
    authority = project_locked_generation_authority(
        lock_result.lock,
        root_authoring=authority_catalog.root_authoring,
        authorings=lock_result.lock.authorings,
        flavor_catalog=authority_catalog.flavor_revisions,
        target_name="snapshot-replication-python",
        flavor_selectors=selectors,
    )
    effective = next(
        item.revision
        for item in authority.lock.nodes
        if item.revision.identity == authority.lock.root_revision
    )
    if effective.authoring_identity != authoring.identity or tuple(
        item.identity for item in effective.specifications
    ) != tuple(
        item.identity for item in loaded_specification.specification_set.artifacts
    ):
        raise SelfHostError(
            "self-host.component-authority-changed",
            "locked Component authority differs from the strict provider snapshot",
        )
    definition = _locked_component_definition(authoring, effective)
    base, target = _runtime_recipe(
        definition,
        loaded_specification.specification_set,
        source_snapshot,
        skills,
        platform_flavor,
    )
    managed_sbom_graph = CycloneDxManagedGraph.from_component_lock(authority.lock)
    source_python_dependencies = observe_installed_python_distributions(
        python_dependencies.core,
        root_ref=managed_sbom_graph.root_ref,
    )
    optional_python_dependencies = declare_optional_python_distributions(
        python_dependencies.optional_mapping(),
        imported_names=tuple(sorted(imported_python_names)),
        root_ref=managed_sbom_graph.root_ref,
        import_names_by_distribution=python_dependencies.optional_import_mapping(),
    )
    core_refs = {str(item["bom-ref"]) for item in source_python_dependencies.components}
    if any(
        str(item["bom-ref"]) in core_refs
        for item in optional_python_dependencies.components
    ):
        raise SelfHostError(
            "self-host.project-dependencies-invalid",
            "self-host Python dependency is both core and source-imported optional",
        )
    source_sbom_bytes, _source_sbom = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=managed_sbom_graph,
        additional_components=(
            *source_python_dependencies.components,
            *optional_python_dependencies.components,
        ),
        additional_edges=(
            *source_python_dependencies.edges,
            *optional_python_dependencies.edges,
        ),
    )
    source_sbom_content = source_sbom_bytes.decode("utf-8")
    suite_recipe_identity = canonical_identity(
        {
            "component_revision": base.identity.to_dict(),
            "effective_revision": effective.identity.to_dict(),
            "specification": specification_id,
            "skill_set": skills.identity.to_dict(),
            "snapshot": tree.identity.to_dict(),
        }
    ).uri
    suite_specification_references = tuple(
        item.uri for item in loaded_specification.specification_set.artifacts
    )
    generated_test_suite_policy = GeneratedTestSuitePolicy(
        suite_recipe_identity,
        suite_specification_references,
    )
    generated_test_suite_content = tree.generated_test_suite_content(
        recipe_identity=suite_recipe_identity,
        specification_references=suite_specification_references,
    )
    generated_test_suite_identity = (
        "sha256:"
        + hashlib.sha256(generated_test_suite_content.encode("utf-8")).hexdigest()
    )
    acceptance_policy = IndependentAcceptancePolicy.self_host_exact_tree(
        generated_test_suite_identity
    )
    locked_definition = replace(
        definition,
        workflow_definition=effective.workflow_definition,
        routing_policy=effective.routing_policy,
    )
    execution_plan = _execution_plan(
        locked_definition,
        skills,
        acceptance_policy,
        workflow_content=authority_catalog.component_content(
            effective.authoring_identity, effective.workflow_definition
        ),
        routing_content=authority_catalog.component_content(
            effective.authoring_identity, effective.routing_policy
        ),
    )
    replay_provider = ExactSnapshotReplayProvider(
        tree,
        source_snapshot_id=source_snapshot_id,
        specification_id=specification_id,
        skill_set_id=skills.identity.uri,
        generated_test_suite_content=generated_test_suite_content,
        source_sbom_content=source_sbom_content,
    )
    configured_runtime = Path(runtime_root)
    if configured_runtime.is_symlink():
        raise SelfHostError(
            "self-host.runtime-not-clean", "self-host runtime root cannot be a symlink"
        )
    root = configured_runtime.resolve()
    authority_roots = {
        project_boundary(sample_root, legacy=sample_root),
        project_boundary(package_root, legacy=package_root),
    }
    if any(
        root == authority or root.is_relative_to(authority)
        for authority in authority_roots
    ):
        raise SelfHostError(
            "self-host.runtime-inside-authority",
            "self-host runtime root must be outside generation-authority trees",
        )
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SelfHostError(
            "self-host.runtime-not-clean", "self-host runtime root must start empty"
        )
    root.mkdir(parents=True, exist_ok=True)
    if not host_build_acknowledged:
        raise SelfHostError(
            "self-host.host-build-acknowledgement-required",
            "snapshot replication compiles on the ambient host and requires "
            "explicit acknowledgement",
        )
    policy = SecurityPolicy(
        canonical_identity({"policy": "snapshot-replication@1"}).uri
    )
    # Both in-process and isolated replay stages must bind the interpreter that
    # launched the proof.  PATH discovery can select a different launcher for the
    # first stage while the isolated stage deliberately pins ``PYTHON``; launcher
    # identity is part of build authorization and dependency-resolution evidence.
    python_toolchain = discover_python_toolchain(pinned_command=sys.executable)
    workspace_store = WorkspaceTreeStore(root / "workspace")
    # The isolated self-host candidate intentionally contains only the package and
    # exact proof inputs, not ambient project authority. Bind its required adapter
    # explicitly rather than discovering a policy outside that frozen closure.
    workspace_source_intelligence = SourceIntelligenceProviderSelection(
        SourceIntelligenceStage.SOURCE_GENERATION,
        SourceIntelligenceMode.OFF,
        "none",
        None,
    )
    events = LifecycleEventStoreAdapter(AppendOnlyEventStore(root / "events"))
    request = GenerationRequest(
        locked_authority=authority,
        component_revision=effective.identity,
        generation_context=GenerationContextBinding(
            component_revision=effective.identity,
            component_generation_plan_identity=canonical_identity(
                {
                    "lock": authority.lock.identity.uri,
                    "component": effective.identity.uri,
                }
            ),
            generation_key_identity=canonical_identity(
                {
                    "component": effective.identity.uri,
                    "execution_plan": execution_plan.identity.uri,
                }
            ),
            context_manifest_identity=canonical_identity(
                {
                    "component": effective.identity.uri,
                    "prompt": (
                        "Replay the exact snapshot using conformance skill set "
                        f"{skills.identity.uri}; no coding CLI is involved."
                    ),
                }
            ),
            complexity_decision_identity=canonical_identity(
                {"component": effective.identity.uri, "bounded": True}
            ),
            prompt_identity=ContentIdentity.parse_uri(
                "sha256:"
                + hashlib.sha256(
                    (
                        "Replay the exact snapshot using conformance skill set "
                        f"{skills.identity.uri}; no coding CLI is involved."
                    ).encode()
                ).hexdigest()
            ),
            generation_recipe_identity=ContentIdentity.parse_uri(suite_recipe_identity),
            workspace_allocation_identity=canonical_identity(
                {
                    "component": effective.identity.uri,
                    "workspace": workspace_reference,
                }
            ),
            workspace_reference=workspace_reference,
            prompt=(
                "Replay the exact snapshot using conformance skill set "
                f"{skills.identity.uri}; no coding CLI is involved."
            ),
        ),
        execution_plan=execution_plan,
        build_request_declaration=BuildRequestDeclaration(
            effective_revision_digest=effective.identity.uri,
            builder_id="builder:python-bytecode@1",
            toolchain_digest=python_toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("python-bytecode",),
        ),
        workspace_reference=workspace_reference,
        generated_test_suite_policy=generated_test_suite_policy,
        managed_sbom_graph=managed_sbom_graph,
    )
    orchestrator = GenerationOrchestrator(
        readiness_provider=_ExactReadiness(
            source_snapshot_id, specification_id, skills.identity.uri
        ),
        model_provider=replay_provider,
        validator=PipelineValidatorAdapter(
            ValidationPipeline(
                (PythonSyntaxValidator(),), required_categories=("syntax",)
            )
        ),
        classifier=GeneratedTreeSecurityClassifier(
            policy,
            RuleBasedSourceScanner(
                "scanner:snapshot-replication-python@1", baseline_python_rules()
            ),
            "source-provider:exact-snapshot-replay@1",
            "literate-ai-snapshot-replication-local-root",
        ),
        build_authorizer=PolicyBuildAuthorizer(
            policy,
            "literate-ai-snapshot-replication",
            "compile an exact replayed snapshot on the acknowledged ambient host",
            yolo_acknowledged=host_build_acknowledged,
            clock=lambda: FIXED_TIME,
        ),
        builder=PythonBuildAdapter(
            artifact_store=root / "artifacts",
            toolchain=python_toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=LiveBuildAuthorizationVerifier(
                lambda: AuthorizationRevocationSet()
            ),
        ),
        dependency_resolver=CycloneDxLifecycleResolver(
            managed_graph=managed_sbom_graph,
            observer=PortableHostDependencyObserver(
                toolchain_commands=(python_toolchain.command,),
                python_requirements=python_dependencies.core,
            ),
            evidence_path=root / "evidence" / "resolved-sbom.cdx.json",
        ),
        generated_test_runner=_ExactSnapshotGeneratedTreeTester(
            tree, generated_test_suite_content
        ),
        acceptance_runner=_ExactSnapshotAcceptanceRunner(
            tree, generated_test_suite_content, generated_test_suite_identity
        ),
        workspace=WorkspaceTreeAdapter(
            workspace_store,
            source_intelligence=workspace_source_intelligence,
            source_intelligence_custody=None,
        ),
        event_store=events,
    )
    run = orchestrator.execute(request)
    if run.status is not GenerationStatus.COMPLETE or run.provenance is None:
        raise SelfHostError(
            "self-host.generation-incomplete",
            "snapshot-replication orchestrator did not complete",
        )
    if replay_provider.calls != ["plan", "generate"]:
        raise SelfHostError(
            "self-host.model-stages-incomplete",
            "snapshot replay adapter did not execute the exact proof stages",
        )
    expected_steps = (
        "validate",
        "classify",
        "authorize-build",
        "build",
        "resolve-dependencies",
        "test-generated",
        "verify-independent",
        "prepare-tree",
        "commit-tree",
    )
    if tuple(item.step_id for item in run.lifecycle_executions) != expected_steps:
        raise SelfHostError(
            "self-host.lifecycle-incomplete",
            "snapshot replication did not execute the complete concrete lifecycle",
        )
    accepted_root = workspace_store.resolve(workspace_reference)
    if accepted_root is None:
        raise SelfHostError(
            "self-host.accepted-tree-missing",
            "snapshot-replication workspace has no accepted tree",
        )
    accepted_package = accepted_root / "source" / "literate_ai"
    candidate_tree = FrozenPackageTree.capture(accepted_package)
    if candidate_tree.files != tree.files or candidate_tree.identity != tree.identity:
        expected_paths = {path for path, _content in tree.files}
        actual_paths = {path for path, _content in candidate_tree.files}
        raise SelfHostError(
            "self-host.candidate-incomplete",
            "replayed package differs from exact input; missing="
            f"{sorted(expected_paths - actual_paths)}, extra="
            f"{sorted(actual_paths - expected_paths)}",
        )
    validation = run.step("validate")
    classification = run.step("classify")
    authorization = run.step("authorize-build")
    build = run.step("build")
    dependency_resolution = run.step("resolve-dependencies")
    generated_tests = run.step("test-generated")
    acceptance = run.step("verify-independent")
    if any(
        item is None
        for item in (
            validation,
            classification,
            authorization,
            build,
            dependency_resolution,
            generated_tests,
            acceptance,
        )
    ):
        raise SelfHostError(
            "self-host.lifecycle-evidence-missing",
            "snapshot-replication lifecycle evidence is incomplete",
        )
    assert validation is not None
    assert classification is not None
    assert authorization is not None
    assert build is not None
    assert dependency_resolution is not None
    assert generated_tests is not None
    assert acceptance is not None
    compiled = build.output.get("compiled_files")
    python_file_count = sum(path.endswith(".py") for path, _content in tree.files)
    if not isinstance(compiled, list) or len(compiled) != python_file_count:
        raise SelfHostError(
            "self-host.build-incomplete",
            "snapshot-replication build did not compile every replayed Python module",
        )
    if len(events.stream(run.run_id)) != len(run.events):
        raise SelfHostError(
            "self-host.events-incomplete",
            "snapshot-replication event stream does not match orchestration events",
        )
    loaded_specification.require_unchanged(sample_root)
    _require_pinned_files_unchanged(pinned_inputs)
    current_platform_flavor = _load_platform_flavor(platform_flavor_file)
    if current_platform_flavor.revision_identity != platform_flavor.revision_identity:
        raise SelfHostError(
            "self-host.input-changed",
            "snapshot-replication platform Flavor closure changed during the operation",
        )
    current_tree = FrozenPackageTree.capture(package_root)
    if current_tree.files != tree.files or current_tree.identity != tree.identity:
        raise SelfHostError(
            "self-host.source-changed",
            "snapshot source changed during the replication operation",
        )
    stable = {
        "schema": SNAPSHOT_REPLICATION_REPORT_SCHEMA,
        "classification": "deterministic-offline-conformance-replay",
        "coding_cli_involved": False,
        "source_snapshot_id": source_snapshot_id,
        "source_tree_id": source_tree_id,
        "semantic_package_tree_id": tree.identity.uri,
        "specification_id": specification_id,
        "skill_set_id": skills.identity.uri,
        "platform_flavor_id": platform_flavor.revision_identity.uri,
        "component_revision_id": base.identity.uri,
        "effective_revision_id": run.provenance.root_revision_identity.uri,
        "accepted_tree_id": run.provenance.accepted_tree_identity.uri,
        "accepted_workspace_tree_digest": run.step("commit-tree").output["tree_digest"],
        "file_count": len(tree.files),
        "python_file_count": python_file_count,
        "model_stage_ids": [item.stage_id for item in run.stage_executions],
        "lifecycle_step_ids": list(expected_steps),
        "validation": {
            "passed": validation.output["passed"],
            "validator_ids": validation.output["validator_ids"],
        },
        "security_classification": {
            "profile": classification.output["profile"],
            "maximum_severity": classification.output["maximum_severity"],
            "finding_count": len(classification.output["finding_ids"]),
        },
        "authorization": {
            "authorization_id": authorization.output["authorization_id"],
            "profile": authorization.output["profile"],
        },
        "build": {
            "source_bundle_digest": build.output["source_bundle_digest"],
            "compiled_file_count": len(compiled),
        },
        "dependency_resolution": {
            "resolution_identity": dependency_resolution.output["resolution_identity"],
            "source_bom": dependency_resolution.output["source_bom"],
            "resolved_bom": dependency_resolution.output["resolved_bom"],
        },
        "generated_tests": {
            "passed": generated_tests.output["passed"],
            "test_suite_identity": generated_tests.output["test_suite_identity"],
            "test_count": generated_tests.output["total"],
        },
        "acceptance": {
            "passed": acceptance.output["passed"],
            "execution_profile": acceptance.output["execution_profile"],
            "suite_artifact_identity": acceptance.output["test_suite_identity"],
            "test_count": acceptance.output["total"],
        },
        "event_types": [item.event_type for item in run.events],
        "operational_nondeterminism_excluded": [
            "artifact_path",
            "event_timestamps",
            "runtime_root",
            "workspace_path",
        ],
    }
    canonical_json_bytes(stable)
    return SnapshotReplicationResult(stable, accepted_root, accepted_package)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--skills", required=True)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--platform-flavor", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument(
        "--workspace-reference", default="snapshot-replication/candidate"
    )
    parser.add_argument("--allow-host-build", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_snapshot_replication(
        source_root=args.source_root,
        component_path=args.component,
        skills_path=args.skills,
        proof_path=args.proof,
        platform_flavor_path=args.platform_flavor,
        runtime_root=args.runtime_root,
        workspace_reference=args.workspace_reference,
        host_build_acknowledged=args.allow_host_build,
    )
    print(canonical_json_bytes(result.stable_outputs).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExactSnapshotReplayProvider",
    "FrozenPackageTree",
    "SNAPSHOT_REPLICATION_PROOF_SCHEMA",
    "SNAPSHOT_REPLICATION_REPORT_SCHEMA",
    "SNAPSHOT_REPLICATION_SKILL_SCHEMA",
    "SelfHostError",
    "SnapshotReplicationResult",
    "run_snapshot_replication",
]

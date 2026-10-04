"""Concrete local-process ports for the Standard Component lifecycle.

These adapters deliberately know nothing about a sample or a programming language.
They materialize an accepted generated tree, invoke configured host commands, and
carry exact provider artifacts into consumer command environments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from literate_ai._filesystem import (
    name_or_magic_is_native_or_wasm,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters._processes import (
    ProcessTreeOwnership,
    create_process_tree_ownership,
    run_with_tree_kill,
    terminate_process_tree,
)
from literate_ai.adapters.builders import (
    JAVASCRIPT_SOURCE_SUFFIXES,
    BuildError,
    controlled_node_environment,
    run_bounded_process,
)
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.compiler_cache import compiler_cache_session
from literate_ai.adapters.dependencies import (
    CycloneDxBomError,
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    HostDependencyObservation,
    validate_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.python_install import (
    PIP_INSTALLER,
    install_python_wheels,
)
from literate_ai.adapters.dependencies.python_source import (
    prepare_python_source_authority,
)
from literate_ai.adapters.dependencies.python_target import observe_python_wheel_target
from literate_ai.adapters.lifecycle._service_http import open_service_request
from literate_ai.adapters.lifecycle.standard_npm import (
    StandardNpmDependencyEvidence,
    StandardNpmLifecycleError,
    StandardNpmSourceAuthority,
    StandardNpmTarget,
    dependency_bearing_package_manifests,
    load_npm_source_authority,
    normalized_npm_inventory,
    validate_npm_inventory,
)
from literate_ai.adapters.lifecycle.standard_python import (
    StandardPythonBuildBinding,
    StandardPythonDependencyObserver,
    StandardPythonTarget,
    retain_standard_python_dependencies,
)
from literate_ai.adapters.lifecycle.standard_python import (
    _read_manifest as _read_python_manifest,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    direct_service_process_argv,
)
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.adapters.shared_cache_config import BoundSharedCache
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
    realize_manifest,
)
from literate_ai.application.standard_authorization import StandardAuthorizationInputs
from literate_ai.application.standard_build_inputs import (
    StandardBuildInputError,
    validate_standard_build_authority,
    validate_standard_build_inputs,
)
from literate_ai.application.standard_build_intent import (
    StandardBuildIntentInputs,
)
from literate_ai.application.standard_plan_finalization import (
    StandardPlanFinalizationInputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardBuildOutput,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardNodeLifecycleResult,
    StandardProjectBuildPlan,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    LITAI_SMOKE_MODE_FLAG,
    NATIVE_CLI_ENTRYPOINT_KIND,
    PERSISTENT_SERVICE_ENTRYPOINT_KIND,
    PORTABLE_APPLICATION_ENTRYPOINT_KIND,
    WEB_APPLICATION_ENTRYPOINT_KIND,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardEntrypointExecutionEvidence,
    StandardEntrypointGeneratedTestEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestCaseEvidence,
    StandardGeneratedTestExecutionEvidence,
    generated_source_tree_identity,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import (
    ArtifactBuildGraph,
    ArtifactExport,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandRole,
    ComponentEntrypointCommandContract,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ExactLinkPlan,
    GeneratedSourceCandidate,
    LibraryConsumerBinding,
    PackageEntrypoint,
    PackageFileKind,
    PackageInput,
    PackageKind,
    PackagePlan,
    PackageResult,
    RuntimeRequirement,
    RuntimeRequirementKind,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.product_json import (
    product_json_bytes,
    product_json_identity,
    product_json_values_equal,
)
from literate_ai.contracts.standard_execution_inputs import (
    StandardExecutionAuthority,
    StandardExecutionInputScope,
    standard_execution_request,
    standard_execution_runtime_identity,
)
from literate_ai.diagnostics import (
    inherited_verbose_environment,
    log_operation,
    report_progress,
    trace_subprocess,
)
from literate_ai.evidence_ledger import EvidenceNode, attach_run
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    ValidatedGeneratedTestSuite,
)
from literate_ai.ports import BuildDependencyObservation
from literate_ai.security import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)


class LocalStandardLifecycleError(RuntimeError):
    pass


class GeneratedCandidateCommandError(LocalStandardLifecycleError):
    """A generated build or generated-test command rejected candidate bytes."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PersistentServiceAcceptanceError(LocalStandardLifecycleError):
    """Independent persistent-service acceptance failed with child diagnostics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        returncode: int | None,
        stdout: str,
        stderr: str,
        contract_identity: str | None = None,
    ) -> bytes:
        self.code = code
        self.message = message
        self.phase = phase
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.contract_identity = contract_identity
        self.message_limit = 8192
        super().__init__(message)


_CHILD_DIAGNOSTIC_BYTES = 4096
_PRIVATE_CHILD_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:\\[^\r\n\t\"']+|/(?:[^/\s\"']+/)+[^/\s\"']*)"
)


_GENERATED_TEST_PROTOCOL_FAILURE_PREFIXES = (
    "generated-test runner did not emit",
    "generated-test runner emitted",
)
_HOST_PROCESS_ENVIRONMENT_KEYS = (
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "WINDIR",
    "__CF_USER_TEXT_ENCODING",
)


def _host_process_environment() -> dict[str, str]:
    """Host keys a child interpreter needs when the packaged environment is empty."""

    environment = {"PATH": os.environ.get("PATH") or os.defpath}
    for name in _HOST_PROCESS_ENVIRONMENT_KEYS:
        value = os.environ.get(name)
        if value:
            environment[name] = value
    if os.name != "nt" and sys.platform != "darwin":
        environment.setdefault("LANG", "C.UTF-8")
        environment.setdefault("LC_ALL", "C.UTF-8")
    return environment


def _child_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Overlay one packaged environment onto host process essentials."""

    completed = _host_process_environment()
    completed.update(environment)
    return inherited_verbose_environment(completed)


class _RecordedHostDependencyObserver:
    """Replay the exact toolchain closure observed before lifecycle execution."""

    def __init__(self, observation: HostDependencyObservation) -> None:
        self.observation = observation

    def observe(
        self, _build: Mapping[str, object], *, root_ref: str
    ) -> HostDependencyObservation:
        if not isinstance(root_ref, str) or not root_ref:
            raise LocalStandardLifecycleError(
                "resolved dependency observation requires one Component root"
            )
        component_refs = {
            str(component["bom-ref"])
            for component in self.observation.components
            if isinstance(component.get("bom-ref"), str)
        }
        observed_roots = {
            source
            for source, _target in self.observation.edges
            if source not in component_refs
        }
        if len(observed_roots) > 1:
            raise LocalStandardLifecycleError(
                "toolchain dependency observation has competing root references"
            )
        original_root = next(iter(observed_roots), None)
        edges = tuple(
            sorted(
                {
                    (root_ref if source == original_root else source, target)
                    for source, target in self.observation.edges
                }
            )
        )
        return HostDependencyObservation(self.observation.components, edges)


@dataclass(frozen=True, slots=True)
class LocalResolvedExecutionCommand:
    """Exact shell-free command needed to rerun one accepted local export."""

    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "environment": {key: value for key, value in self.environment},
        }


_ARTIFACT_MANIFEST = "artifact-manifest.json"
_ARTIFACT_CHECKPOINT_SCHEMA = "literate-ai/local-standard-artifact-checkpoint@1"
_ARTIFACT_CHECKPOINT_ROOT = ".literate"
_ARTIFACT_CHECKPOINT_DIRNAME = "artifact-checkpoints"
_RESOLVED_SBOM = ".literate/resolved-sbom.cdx.json"
_NPM_EVIDENCE_MANIFEST = ".literate/npm/evidence-manifest.json"
_MAX_NPM_PROCESS_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_NPM_INSTALLED_ENTRIES = 200_000
_MAX_NPM_INSTALLED_FILE_BYTES = 128 * 1024 * 1024
_MAX_NPM_INSTALLED_TREE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalProjectPackageCustody:
    package_plan: PackagePlan
    package_result: PackageResult
    root: Path
    artifact_paths: Mapping[str, Path]
    root_plan: StandardComponentBuildPlan
    generated_test_suite: ValidatedGeneratedTestSuite
    tree_identity: ContentIdentity
    native_sdk_resources: NativeSdkPackageResources | None = None
    native_sdk_execution_scope: NativeSdkPackageExecutionScope | None = None
    python_dependency_observer: StandardPythonDependencyObserver | None = None
    python_artifact_tree_identity: ContentIdentity | None = None


def _directory_export_bytes(root: Path) -> bytes:
    """Encode one directory export as deterministic immutable ZIP bytes."""

    from literate_ai.adapters.directory_artifacts import directory_export_bytes

    try:
        return directory_export_bytes(root)
    except ValueError as exc:
        raise LocalStandardLifecycleError(str(exc)) from exc


def _require_generated_export(path: Path) -> None:
    """Reject generated build output defects with a stable repairable code."""

    try:
        valid = not path.is_symlink() and path.exists()
        if valid and path.is_dir():
            _directory_export_bytes(path)
        elif valid:
            valid = path.is_file()
    except (OSError, LocalStandardLifecycleError):
        valid = False
    if not valid:
        raise GeneratedCandidateCommandError(
            "builder.generated-source-rejected",
            "generated build did not produce the exact declared export",
        )


def _local_tree_identity(
    root: Path, *, excluded: frozenset[str] = frozenset()
) -> ContentIdentity:
    return canonical_identity(_local_tree_document(root, excluded=excluded))


def _local_tree_document(
    root: Path, *, excluded: frozenset[str] = frozenset()
) -> dict[str, object]:
    """Tree custody with case-sensitive component ordering on every host."""

    root = root.resolve(strict=True)
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).parts):
        if path_is_link_or_reparse(path):
            raise LocalStandardLifecycleError("generated trees cannot contain links")
        if not path.is_file() and not path.is_dir():
            raise LocalStandardLifecycleError("artifact trees require regular files")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            entries.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    if not entries:
        raise LocalStandardLifecycleError("generated tree is empty")
    return {"schema": "literate-ai/local-source-tree@1", "files": entries}


def local_tree_identity(root: Path) -> ContentIdentity:
    """Identity of every regular file in one local lifecycle tree."""

    return _local_tree_identity(root)


def local_generated_source_tree_identity(root: Path) -> ContentIdentity:
    """Public canonical generated-source identity for one local regular tree."""

    resolved = root.resolve(strict=True)
    files: dict[str, bytes] = {}
    for path in sorted(resolved.rglob("*")):
        relative = path.relative_to(resolved)
        if ".codegraph" in relative.parts:
            continue
        if path.is_symlink():
            raise LocalStandardLifecycleError("generated trees cannot contain links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise LocalStandardLifecycleError(
                "generated trees must contain only regular files"
            )
        files[relative.as_posix()] = path.read_bytes()
    try:
        return ContentIdentity.parse_uri(generated_source_tree_identity(files))
    except (TypeError, ValueError) as exc:
        raise LocalStandardLifecycleError(
            "generated tree does not form a canonical portable source tree"
        ) from exc


class LocalSourceTreeRegistry:
    """Process-local custody mapping from immutable tree identity to its directory."""

    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}
        self._evidence: dict[str, LocalGeneratedSourceCustody] = {}
        self._validation_inputs: dict[str, SourceEvidenceValidationInputs] = {}

    def register(
        self,
        candidate: GeneratedSourceCandidate,
        root: Path,
        *,
        source_generation_identity: ContentIdentity | None = None,
        recipe: object | None = None,
        validation_inputs: SourceEvidenceValidationInputs | None = None,
    ) -> None:
        if recipe is not None and validation_inputs is not None:
            raise LocalStandardLifecycleError(
                "source validation authority is ambiguous"
            )
        if validation_inputs is not None and not isinstance(
            validation_inputs, SourceEvidenceValidationInputs
        ):
            raise TypeError("source validation inputs must be typed")
        path = root.resolve(strict=True)
        if local_generated_source_tree_identity(path) != candidate.tree_identity:
            raise LocalStandardLifecycleError(
                "candidate identity does not match generated files"
            )
        existing = self._paths.get(candidate.tree_identity.uri)
        if existing is not None and existing != path:
            raise LocalStandardLifecycleError(
                "source identity has competing custody paths"
            )
        custody = None
        if recipe is not None:
            if not isinstance(
                getattr(recipe, "managed_sbom_graph", None), CycloneDxManagedGraph
            ):
                raise LocalStandardLifecycleError(
                    "generated source custody requires its managed SBOM graph"
                )
            validation_inputs = SourceEvidenceValidationInputs.from_recipe(recipe)
        if validation_inputs is not None:
            managed_graph = validation_inputs.managed_graph
            source_bom_path = path.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
            suite_path = path.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
            if (
                source_bom_path.is_symlink()
                or not source_bom_path.is_file()
                or suite_path.is_symlink()
                or not suite_path.is_file()
            ):
                raise LocalStandardLifecycleError(
                    "generated source custody requires its source SBOM and test suite"
                )
            source_bom_content = source_bom_path.read_bytes()
            suite_content = suite_path.read_bytes()
            source_bom, suite = validation_inputs.validate(
                source_bom_content, suite_content
            )
            suite_identity = ContentIdentity.parse_uri(suite.content_identity)
            if (
                source_bom.bom_identity != candidate.source_bom_identity
                or suite_identity != candidate.generated_test_suite_identity
            ):
                raise LocalStandardLifecycleError(
                    "generated source evidence differs from its candidate identities"
                )
            generation_identity = source_generation_identity or candidate.identity
            custody = LocalGeneratedSourceCustody(
                candidate=candidate,
                source_generation_identity=generation_identity,
                managed_graph=managed_graph,
                source_bom=source_bom,
                source_bom_content=source_bom_content,
                generated_test_suite=suite,
                generated_test_suite_content=suite_content,
            )
            existing_inputs = self._validation_inputs.get(candidate.tree_identity.uri)
            if existing_inputs is not None and existing_inputs != validation_inputs:
                raise LocalStandardLifecycleError(
                    "source identity has competing validation authority"
                )
            existing_evidence = self._evidence.get(candidate.tree_identity.uri)
            if existing_evidence is not None and existing_evidence != custody:
                raise LocalStandardLifecycleError(
                    "source identity has competing evidence custody"
                )
        self._paths[candidate.tree_identity.uri] = path
        if custody is not None:
            self._evidence[candidate.tree_identity.uri] = custody
            self._validation_inputs[candidate.tree_identity.uri] = validation_inputs

    def validation_inputs(
        self, identity: ContentIdentity
    ) -> SourceEvidenceValidationInputs:
        """Export captured authority while registered source remains current."""
        self.resolve(identity)
        try:
            return self._validation_inputs[identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "generated source has no validation authority"
            ) from exc

    def registered_root(self, identity: ContentIdentity) -> Path:
        """Return registration metadata; callers must verify current source bytes."""
        try:
            return self._paths[identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "generated source tree is not registered"
            ) from exc

    def resolve(self, identity: ContentIdentity) -> Path:
        path = self.registered_root(identity)
        if local_generated_source_tree_identity(path) != identity:
            raise LocalStandardLifecycleError("registered generated source changed")
        return path

    def evidence(self, identity: ContentIdentity) -> LocalGeneratedSourceCustody:
        self.resolve(identity)
        return self.registered_evidence(identity)

    def registered_evidence(
        self, identity: ContentIdentity
    ) -> LocalGeneratedSourceCustody:
        """Read registration evidence; verify current bytes separately."""
        try:
            return self._evidence[identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "generated source tree has no strict evidence custody"
            ) from exc


@dataclass(frozen=True, slots=True)
class LocalGeneratedSourceCustody:
    """Host-path-free custody of exact generated source evidence."""

    candidate: GeneratedSourceCandidate
    source_generation_identity: ContentIdentity
    managed_graph: CycloneDxManagedGraph
    source_bom: CycloneDxBomBinding
    source_bom_content: bytes = field(repr=False)
    generated_test_suite: ValidatedGeneratedTestSuite
    generated_test_suite_content: bytes = field(repr=False)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """The exact existing custody payload, without filesystem locations."""

        return {
            "schema": "literate-ai/local-generated-source-custody@1",
            "candidate_identity": self.candidate.identity.uri,
            "source_generation_identity": self.source_generation_identity.uri,
            "source_tree_identity": self.candidate.tree_identity.uri,
            "source_bom_identity": self.source_bom.bom_identity.uri,
            "managed_graph_identity": self.managed_graph.identity.uri,
            "generated_test_suite_identity": (
                self.generated_test_suite.content_identity
            ),
        }


@dataclass(frozen=True, slots=True)
class LocalIndependentAcceptanceCase:
    """One verifier-owned packaged-application input and exact known result."""

    case_id: str
    arguments_document: bytes = field(repr=False)
    expected_result_document: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.case_id or "\x00" in self.case_id:
            raise ValueError("independent acceptance case ID must be nonempty")
        try:
            arguments = json.loads(self.arguments_document)
            expected = json.loads(self.expected_result_document)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("independent acceptance case must contain JSON") from exc
        if not isinstance(arguments, list):
            raise ValueError("independent acceptance arguments must be an array")
        if (
            product_json_bytes(arguments) != self.arguments_document
            or product_json_bytes(expected) != self.expected_result_document
        ):
            raise ValueError("independent acceptance values must be canonical JSON")

    @classmethod
    def create(
        cls, case_id: str, arguments: list[object], expected_result: object
    ) -> LocalIndependentAcceptanceCase:
        return cls(
            case_id,
            product_json_bytes(arguments),
            product_json_bytes(expected_result),
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/local-independent-acceptance-case@1",
                "case_id": self.case_id,
                "arguments_identity": product_json_identity(
                    json.loads(self.arguments_document)
                ).uri,
                "expected_result_identity": product_json_identity(
                    json.loads(self.expected_result_document)
                ).uri,
            }
        )


def _parse_sse_events(text: str) -> tuple[dict[str, str], ...]:
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    data: list[str] = []
    for line in (*text.splitlines(), ""):
        if not line:
            if data:
                current["data"] = "\n".join(data)
            if current:
                events.append(current)
            current = {}
            data = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field in {"event", "id", "retry"}:
            current[field] = value
    return tuple(events)


class LocalIndependentAcceptanceOracle(Protocol):
    """Verifier boundary withheld from specifications, prompts, and generated source."""

    @property
    def identity(self) -> ContentIdentity: ...

    def cases(
        self, component_lock: ComponentLock
    ) -> tuple[LocalIndependentAcceptanceCase, ...]: ...


class RegisteredSourceGenerationRunner:
    """Decorate a source-only runner with exact local tree custody."""

    def __init__(
        self,
        delegate: Callable[[object], SourceGenerationRunOutput],
        registry: LocalSourceTreeRegistry,
    ) -> None:
        self.delegate = delegate
        self.registry = registry

    def __call__(self, prepared: object) -> SourceGenerationRunOutput:
        output = self.delegate(prepared)
        if not isinstance(output, SourceGenerationRunOutput):
            raise LocalStandardLifecycleError(
                "source runner returned an invalid output"
            )
        workspace = Path(prepared.workspace.locator)
        self.registry.register(
            output.candidate,
            workspace,
            source_generation_identity=output.identity,
            recipe=prepared.recipe,
        )
        return output

    def planned_cache_key(self, prepared: object):
        return self.delegate.planned_cache_key(prepared)

    def record_restored_cache_key(self, candidate: object, cache_key: object) -> None:
        self.delegate.record_restored_cache_key(candidate, cache_key)


@dataclass(frozen=True, slots=True)
class LocalComponentToolBinding:
    """Explicit host command bound to one exact observed toolchain identity."""

    executable: str
    arguments: tuple[str, ...] = ()
    authority_identity: ContentIdentity | None = None
    environment: tuple[tuple[str, str], ...] = ()
    _authority_guard: Callable[[], None] | None = field(
        default=None, repr=False, compare=False
    )
    _initial_identity: ContentIdentity = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executable, str)
            or not self.executable
            or "\x00" in self.executable
        ):
            raise ValueError(
                "local tool binding executable must be a nonempty argv token"
            )
        path = Path(self.executable)
        if not path.is_absolute():
            raise ValueError("local tool binding must use an absolute executable path")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("local tool binding executable must be a regular file")
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in self.arguments
        ):
            raise ValueError("local tool binding arguments must be safe argv tokens")
        if self.authority_identity is not None and not isinstance(
            self.authority_identity, ContentIdentity
        ):
            raise TypeError("local tool binding authority must be a ContentIdentity")
        if (
            not isinstance(self.environment, tuple)
            or any(
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or "\x00" in name + value
                or "=" in name
                for name, value in self.environment
            )
            or len({name.casefold() for name, _value in self.environment})
            != len(self.environment)
        ):
            raise ValueError(
                "local tool binding environment must be unique string pairs"
            )
        if self._authority_guard is not None and not callable(self._authority_guard):
            raise TypeError("local tool binding guard must be callable")
        selected_executable = (
            str(path) if self.authority_identity is not None else str(resolved)
        )
        object.__setattr__(self, "executable", selected_executable)
        object.__setattr__(self, "_initial_identity", self._current_identity())

    @classmethod
    def from_observed_toolchain(
        cls,
        toolchain: object,
        *,
        guard: Callable[[], None] | None = None,
    ) -> LocalComponentToolBinding:
        """Adapt an existing exact host-toolchain observation without weakening it."""

        command = getattr(toolchain, "command", None)
        raw_identity = getattr(toolchain, "identity", None)
        selected_guard = guard or getattr(toolchain, "require_unchanged", None)
        if (
            not isinstance(command, tuple)
            or not command
            or not isinstance(raw_identity, str)
            or not callable(selected_guard)
        ):
            raise TypeError(
                "observed toolchain must expose command, identity, and drift guard"
            )
        return cls(
            command[0],
            command[1:],
            ContentIdentity.parse_uri(raw_identity),
            tuple(getattr(toolchain, "environment", ())),
            selected_guard,
        )

    def _current_identity(self) -> ContentIdentity:
        path = Path(self.executable)
        data = path.read_bytes()
        return canonical_identity(
            {
                "schema": "literate-ai/local-tool-command@1",
                "arguments": list(self.arguments),
                "environment": dict(self.environment),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )

    @property
    def toolchain_identity(self) -> ContentIdentity:
        """Content-derived launcher identity suitable for a locked contract."""

        self.require_unchanged()
        return self.authority_identity or self._initial_identity

    @property
    def command(self) -> tuple[str, ...]:
        return (self.executable, *self.arguments)

    def require_unchanged(self) -> None:
        if self._current_identity() != self._initial_identity:
            raise LocalStandardLifecycleError(
                "locked local tool executable changed after binding"
            )
        if self._authority_guard is not None:
            try:
                self._authority_guard()
            except Exception as exc:
                raise LocalStandardLifecycleError(
                    "observed local toolchain changed after binding"
                ) from exc


if TYPE_CHECKING:
    from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
    from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
    from literate_ai.adapters.native_sdk_package_scope import (
        NativeSdkPackageExecutionScope,
    )


def required_command_toolchains(
    contracts: tuple[ComponentCommandContract, ...],
    command_phases: tuple[ComponentCommandPhase, ...],
    npm_targets: tuple[StandardNpmTarget, ...] = (),
) -> frozenset[str]:
    """Exact host command identities for one canonical execution scope."""
    if (
        not isinstance(command_phases, tuple)
        or any(not isinstance(phase, ComponentCommandPhase) for phase in command_phases)
        or command_phases
        != tuple(phase for phase in ComponentCommandPhase if phase in command_phases)
    ):
        raise ValueError("command phases must be a canonical phase tuple")
    if any(not isinstance(item, ComponentCommandContract) for item in contracts):
        raise TypeError("local commands must use ComponentCommandContract")
    required_toolchains = {
        item.tool_binding(phase).toolchain_identity.uri
        for item in contracts
        for phase in command_phases
    }
    for item in contracts:
        for entrypoint in item.entrypoint_contracts or ():
            required_toolchains.update(
                binding.toolchain_identity.uri
                for binding in entrypoint.tool_bindings
                if binding.phase in command_phases
            )
        if command_phases == tuple(ComponentCommandPhase):
            acceptance = item.library_acceptance_toolchain_identity
            if acceptance is not None:
                required_toolchains.add(acceptance.uri)
    if any(not isinstance(item, StandardNpmTarget) for item in npm_targets):
        raise TypeError("npm_targets must contain StandardNpmTarget values")
    if ComponentCommandPhase.BUILD in command_phases:
        required_toolchains.update(
            target.node_toolchain_identity.uri for target in npm_targets
        )
    return frozenset(required_toolchains)


class LocalStandardLifecyclePorts:
    """Production Standard ports backed by isolated directories and host processes."""

    def __init__(
        self,
        *,
        source_trees: LocalSourceTreeRegistry,
        object_root: Path,
        contracts: tuple[ComponentCommandContract, ...],
        tool_bindings: tuple[LocalComponentToolBinding, ...] = (),
        command_phases: tuple[ComponentCommandPhase, ...] = tuple(
            ComponentCommandPhase
        ),
        npm_targets: tuple[StandardNpmTarget, ...] = (),
        native_sdk_inputs: NativeSdkConsumerInputs | None = None,
        python_targets: tuple[StandardPythonTarget, ...] = (),
        python_wheelhouse: Path | None = None,
        provider_environment: Mapping[str, tuple[str, str]] | None = None,
        dependency_observation: HostDependencyObservation | None = None,
        independent_acceptance_oracle: object | None = None,
        browser_driver: object | None = None,
        ipc_surface_probe: object | None = None,
        shared_cache: BoundSharedCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        required_toolchains = required_command_toolchains(
            contracts, command_phases, npm_targets
        )
        self._command_phases = command_phases
        self.source_trees = source_trees
        if shared_cache is not None and not isinstance(shared_cache, BoundSharedCache):
            raise TypeError("shared cache must bind private configuration")
        self.shared_cache = shared_cache
        self.object_root = object_root.resolve()
        self.object_root.mkdir(parents=True, exist_ok=True)
        self.contracts = {item.component_revision.uri: item for item in contracts}
        if len(self.contracts) != len(contracts):
            raise ValueError("local Component command contracts must be unique")
        self.tool_bindings = {
            item.toolchain_identity.uri: item for item in tool_bindings
        }
        if len(self.tool_bindings) != len(tool_bindings):
            raise ValueError("local toolchain bindings must be unique")
        if set(self.tool_bindings) != required_toolchains:
            raise ValueError(
                "local tool bindings must cover every and only locked "
                "execution toolchain in the selected command phases"
            )
        self.npm_targets = {item.component_revision.uri: item for item in npm_targets}
        if len(self.npm_targets) != len(npm_targets):
            raise ValueError("Standard npm targets must name unique Components")
        for revision, target in self.npm_targets.items():
            contract = self.contracts.get(revision)
            if contract is None:
                raise ValueError("npm target names an unplanned Component")
            build_binding = contract.tool_binding(ComponentCommandPhase.BUILD)
            test_binding = contract.tool_binding(ComponentCommandPhase.TEST)
            execute_binding = contract.tool_binding(ComponentCommandPhase.EXECUTE)
            npm_binding = self.tool_bindings.get(build_binding.toolchain_identity.uri)
            if (
                contract.locked_build_authority_identity != target.identity
                or contract.build_system_resolver_identity
                != target.build_system_resolver_identity
                or contract.build_system_toolchain_identity
                != target.build_system_toolchain_identity
                or build_binding.toolchain_identity
                != target.build_system_toolchain_identity
                or test_binding.toolchain_identity != target.node_toolchain_identity
                or execute_binding.toolchain_identity != target.node_toolchain_identity
                or contract.language_compiler_identity != target.node_toolchain_identity
                or contract.language_runtime_identity != target.node_toolchain_identity
                or (
                    npm_binding is not None
                    and npm_binding.command != target.npm_command
                )
            ):
                raise ValueError(
                    "Standard npm target does not match locked command authority"
                )
        from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs

        if native_sdk_inputs is not None and not isinstance(
            native_sdk_inputs, NativeSdkConsumerInputs
        ):
            raise TypeError("native SDK inputs require live producer-backed custody")
        self._native_sdk_inputs = native_sdk_inputs
        if any(not isinstance(item, StandardPythonTarget) for item in python_targets):
            raise TypeError("python_targets must contain StandardPythonTarget values")
        self.python_targets = {
            item.component_revision.uri: item for item in python_targets
        }
        if len(self.python_targets) != len(python_targets) or set(
            self.python_targets
        ) & set(self.npm_targets):
            raise ValueError("Python targets must name unique, non-npm Components")
        self.python_wheelhouse = python_wheelhouse
        if python_wheelhouse is not None and not isinstance(python_wheelhouse, Path):
            raise TypeError("python_wheelhouse must be a Path")
        self._python_observers: dict[str, StandardPythonDependencyObserver] = {}
        self._python_execution_trees: dict[str, ContentIdentity] = {}
        for revision, target in self.python_targets.items():
            contract = self.contracts.get(revision)
            if contract is None or (
                contract.locked_build_authority_identity != target.identity
                or contract.build_system_resolver_identity
                != target.build_system_resolver_identity
                or contract.build_system_toolchain_identity
                != target.python_toolchain_identity
                or contract.language_compiler_identity
                != target.python_toolchain_identity
                or contract.language_runtime_identity
                != target.python_toolchain_identity
                or any(
                    contract.tool_binding(phase).toolchain_identity
                    != target.python_toolchain_identity
                    for phase in ComponentCommandPhase
                )
                or (
                    target.python_toolchain_identity.uri in self.tool_bindings
                    and self.tool_bindings[target.python_toolchain_identity.uri].command
                    != target.python_command
                )
            ):
                raise ValueError(
                    "Python target does not match locked command authority"
                )
        if self.python_targets and command_phases and python_wheelhouse is None:
            raise ValueError(
                "Python wheel targets require an explicit provisioned wheel directory"
            )
        self.provider_environment = dict(provider_environment or {})
        self.dependency_observation = (
            dependency_observation or HostDependencyObservation((), ())
        )
        if not isinstance(self.dependency_observation, HostDependencyObservation):
            raise TypeError(
                "dependency_observation must be a HostDependencyObservation"
            )
        self.independent_acceptance_oracle = independent_acceptance_oracle
        # A browser-tool-neutral driver port (ADR-0028/ADR-0004). Tests inject a
        # scripted fake; production defaults to the lazily imported Playwright
        # adapter so importing this module never requires Playwright.
        self.browser_driver = browser_driver
        # A protocol-neutral IPC-surface probe port (ADR-0029/ADR-0004). Tests
        # inject a fake fetch; production defaults to the plain HTTP probe so a
        # persistent-service that declares an IPC-surface conformance oracle is
        # fetched and driven without any protocol toolchain at import time.
        self.ipc_surface_probe = ipc_surface_probe
        environment_names: set[str] = set()
        for export_id, binding in self.provider_environment.items():
            if (
                not isinstance(export_id, str)
                or not export_id
                or not isinstance(binding, tuple)
                or len(binding) != 2
            ):
                raise ValueError("provider environment bindings must be typed pairs")
            environment_name, relative_path = binding
            relative = Path(relative_path)
            if (
                not environment_name.isidentifier()
                or environment_name.upper() != environment_name
                or environment_name in environment_names
                or relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
            ):
                raise ValueError("provider environment binding is unsafe or ambiguous")
            environment_names.add(environment_name)
        self.clock = clock
        self._evidence_recorder = None
        self._intent_artifacts: dict[str, tuple[ArtifactExport, ...]] = {}
        self._intent_package_artifacts: dict[str, tuple[ArtifactExport, ...]] = {}
        self._library_consumer_bindings: dict[
            str, tuple[LibraryConsumerBinding, ...]
        ] = {}
        self._plans_by_revision: dict[str, StandardComponentBuildPlan] = {}
        self._plan_inputs_by_revision: dict[str, StandardPlanFinalizationInputs] = {}
        self._sdk_build_authorizations: dict[
            str, tuple[BuildRequest, BuildAuthorization]
        ] = {}
        self._planned_exports: dict[str, ArtifactExport] = {}
        self._exports_by_identity: dict[str, ArtifactExport] = {}
        self._artifact_paths: dict[str, Path] = {}
        self._artifact_blob_paths: dict[str, Path] = {}
        self._artifact_blob_bytes: dict[str, bytes] = {}
        self._build_evidence: dict[str, StandardBuildEvidence] = {}
        self._test_evidence: dict[str, StandardGeneratedTestExecutionEvidence] = {}
        self._execution_evidence: dict[str, StandardExecutionEvidence] = {}
        self._build_observations: dict[str, ContentIdentity] = {}
        self._npm_resolution_builds: dict[str, dict[str, object]] = {}
        self._artifact_checkpoints: dict[str, ContentIdentity] = {}
        self._project_packages: dict[str, LocalProjectPackageCustody] = {}
        self.project_execution_stdout: dict[str, str] = {}
        self.execution_stdout: dict[str, str] = {}
        self.failure_diagnostics: dict[str, str] = {}
        self.build_cache_hits = 0
        self.build_cache_misses = 0
        self.build_cache_hit_seconds = 0.0
        self.build_seconds = 0.0

    def _record_failure_diagnostic(self, revision: str, diagnostic: str) -> None:
        self.failure_diagnostics[revision] = diagnostic
        run = attach_run()
        if run is None:
            return
        node_id = hashlib.sha256(revision.encode("utf-8")).hexdigest()[:12]
        context = run.node(
            f"lifecycle/failure/{node_id}",
            operation="lifecycle.failure",
            parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
            pins={"component_revision": revision},
        )
        with context as node:
            if isinstance(node, EvidenceNode):
                node.attach_text(
                    "diagnostic.json",
                    json.dumps(
                        {
                            "component_revision": revision,
                            "diagnostic": diagnostic,
                        },
                        sort_keys=True,
                    ),
                    role="failure-diagnostic",
                    media_type="application/json",
                )
                path = node.directory / "diagnostic.json"
                report_progress(f"Retained lifecycle diagnostic: {path.resolve()}")
                node.fail("lifecycle component failed")

    def _contract(self, revision: ContentIdentity) -> ComponentCommandContract:
        try:
            return self.contracts[revision.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "Component has no local command contract"
            ) from exc

    def locked_command_authority_is_current(self) -> bool:
        """Check that every neutral contract still has its measured launcher bytes."""

        if not self._command_phases:
            return False
        try:
            for binding in self.tool_bindings.values():
                binding.require_unchanged()
        except (LocalStandardLifecycleError, OSError):
            return False
        return True

    def validate(self, execution_plan: ComponentExecutionPlan) -> ContentIdentity:
        planned = {
            item.component_revision.uri for item in execution_plan.generation_plans
        }
        if planned != set(self.contracts):
            raise LocalStandardLifecycleError(
                "command contracts do not cover the exact plan"
            )
        return self._record_evidence(
            {"validator": "local-standard@1", "plan": execution_plan.identity.uri}
        )

    def _sdk_input_identities(
        self, contract: ComponentCommandContract
    ) -> tuple[ContentIdentity, ...]:
        if self._native_sdk_inputs is None:
            return ()
        return tuple(
            sorted(
                (
                    item.identity
                    for item in self._native_sdk_inputs.for_consumer(
                        contract.component_revision,
                        target_identity=contract.artifact_export.target_identity,
                    )
                ),
                key=lambda item: item.uri,
            )
        )

    @staticmethod
    def _sdk_bound_builder_id(
        contract: ComponentCommandContract,
        inputs: tuple[ContentIdentity, ...],
    ) -> str:
        from literate_ai.contracts.native_sdks import native_sdk_consumer_build_identity

        return native_sdk_consumer_build_identity(
            contract.locked_build_authority_identity, inputs
        ).uri

    def _require_intent_sdk_inputs(self, intent: StandardComponentBuildIntent) -> None:
        contract = self._contract(intent.component_revision)
        inputs = self._sdk_input_identities(contract)
        if inputs != intent.native_sdk_input_identities or (
            inputs
            and intent.build_request.builder_id
            != self._sdk_bound_builder_id(contract, inputs)
        ):
            raise LocalStandardLifecycleError(
                "build intent differs from the exact live native SDK inputs"
            )

    def build_intent_inputs(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        source_candidate: GeneratedSourceCandidate,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardBuildIntentInputs:
        contract = self._contract(generation_plan.component_revision)
        if self._native_sdk_inputs is not None and (
            execution_plan.component_lock_identity
            != self._native_sdk_inputs.snapshot.authority.lock.identity
        ):
            raise LocalStandardLifecycleError(
                "native SDK inputs differ from the execution plan Component lock"
            )
        sdk_inputs = self._sdk_input_identities(contract)
        npm_target = self.npm_targets.get(generation_plan.component_revision.uri)
        if generation_plan.component_revision.uri in self.python_targets:
            self._admit_python_source(
                generation_plan.component_revision, source_candidate.tree_identity
            )
        if npm_target is not None:
            self._admit_npm_source(
                generation_plan.component_revision,
                source_candidate.tree_identity,
                npm_target,
            )
        return StandardBuildIntentInputs(
            generation_plan.identity,
            source_candidate,
            contract,
            provider_artifacts,
            package_artifacts,
            sdk_inputs,
            dependency_resolution=(
                "npm"
                if npm_target is not None
                else "python"
                if generation_plan.component_revision.uri in self.python_targets
                else "none"
            ),
        )

    def create(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        source_candidate: GeneratedSourceCandidate,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardComponentBuildIntent:
        inputs = self.build_intent_inputs(
            execution_plan,
            generation_plan,
            source_candidate,
            provider_artifacts,
            package_artifacts,
        )
        return self._register_build_intent(generation_plan, inputs.create(), inputs)

    def accept_build_intent(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        source_candidate: GeneratedSourceCandidate,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
        intent: StandardComponentBuildIntent,
    ) -> StandardComponentBuildIntent:
        inputs = self.build_intent_inputs(
            execution_plan,
            generation_plan,
            source_candidate,
            provider_artifacts,
            package_artifacts,
        )
        expected = inputs.create()
        if intent != expected:
            raise LocalStandardLifecycleError(
                "worker build intent differs from current local authority"
            )
        return self._register_build_intent(generation_plan, expected, inputs)

    def _register_build_intent(
        self,
        generation_plan: ComponentGenerationPlan,
        intent: StandardComponentBuildIntent,
        inputs: StandardBuildIntentInputs,
    ) -> StandardComponentBuildIntent:
        contract = inputs.contract
        provider_artifacts = inputs.providers
        package_artifacts = inputs.packages
        library_bindings: list[LibraryConsumerBinding] = []
        for artifact in provider_artifacts:
            provider_contract = self.contracts.get(artifact.component_revision.uri)
            if provider_contract is None or not provider_contract.is_library:
                continue
            surface = provider_contract.library_import_surface
            assert surface is not None
            if artifact.target_identity != contract.artifact_export.target_identity:
                raise LocalStandardLifecycleError(
                    "library import target differs from the exact consumer target"
                )
            edges = tuple(
                edge
                for edge in generation_plan.direct_generation_edges
                if edge.provider_revision == artifact.component_revision
            )
            if not edges:
                raise LocalStandardLifecycleError(
                    "imported library artifact lacks a direct generation interface edge"
                )
            for edge in edges:
                capability = surface.capability(edge.capability)
                if capability.interface_identity != edge.public_interface_identity:
                    raise LocalStandardLifecycleError(
                        "library import interface differs from the exact consumer edge"
                    )
                library_bindings.append(
                    LibraryConsumerBinding(
                        consumer_component_revision=generation_plan.component_revision,
                        provider_component_revision=artifact.component_revision,
                        capability=edge.capability,
                        interface_identity=capability.interface_identity,
                        artifact_identity=artifact.identity,
                        import_surface_identity=surface.identity,
                        target_identity=artifact.target_identity,
                        dependency_artifact_identities=(
                            artifact.dependency_artifact_identities
                        ),
                    )
                )
        bindings = tuple(sorted(library_bindings, key=lambda item: item.identity.uri))
        if self._evidence_recorder is not None:
            self._record_evidence(intent.to_dict())
            self._record_evidence(intent.build_request.to_dict())
            self._record_evidence(contract.to_dict())
            for command in contract.commands:
                self._record_evidence(command.to_dict())
            if not contract.is_library:
                for entrypoint in contract.entrypoint_command_contracts():
                    self._record_evidence(entrypoint.to_dict())
                    for command in entrypoint.commands:
                        self._record_evidence(command.to_dict())
        self._intent_artifacts[intent.identity.uri] = provider_artifacts
        self._intent_package_artifacts[intent.identity.uri] = package_artifacts
        self._library_consumer_bindings[intent.identity.uri] = bindings
        return intent

    def library_consumer_bindings(
        self, intent: StandardComponentBuildIntent
    ) -> tuple[LibraryConsumerBinding, ...]:
        """Return exact import bindings admitted for one consumer build intent."""

        if not isinstance(intent, StandardComponentBuildIntent):
            raise TypeError("intent must be a StandardComponentBuildIntent")
        try:
            return self._library_consumer_bindings[intent.identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "consumer build intent has no retained library bindings"
            ) from exc

    def index(
        self, component_revision: ContentIdentity, source: ContentIdentity
    ) -> ContentIdentity:
        root = self.source_trees.resolve(source)
        return self._record_evidence(
            {
                "indexer": "local-tree@1",
                "component": component_revision.uri,
                "tree": source.uri,
                "path_count": sum(1 for p in root.rglob("*") if p.is_file()),
            }
        )

    def authorize(
        self, intent: StandardComponentBuildIntent, index: ContentIdentity
    ) -> StandardBuildAuthorization:
        inputs = self.authorization_inputs(intent, index)
        return self.accept_build_authorization(inputs, inputs.authorize())

    def authorization_inputs(self, intent, index) -> StandardAuthorizationInputs:
        self._require_intent_sdk_inputs(intent)
        return StandardAuthorizationInputs(intent, index, self.clock())

    def accept_build_authorization(
        self, inputs, authorization
    ) -> StandardBuildAuthorization:
        self._require_intent_sdk_inputs(inputs.intent)
        expected = inputs.authorize()
        if (
            not isinstance(authorization, StandardBuildAuthorization)
            or authorization.to_dict() != expected.to_dict()
        ):
            raise LocalStandardLifecycleError(
                "remote authorization differs from admitted controller inputs"
            )
        authorization.grant.require_valid(inputs.intent.build_request, now=self.clock())
        if self._evidence_recorder is not None:
            self._record_evidence(authorization.grant.to_dict())
            self._record_evidence(authorization.to_dict())
        return authorization

    def plan_finalization_inputs(
        self,
        intent: StandardComponentBuildIntent,
        authorization: StandardBuildAuthorization,
    ) -> StandardPlanFinalizationInputs:
        self._require_intent_sdk_inputs(intent)
        if intent.native_sdk_input_identities and (
            authorization.build_intent_identity != intent.identity
            or authorization.build_request_identity != intent.build_request_identity
        ):
            raise LocalStandardLifecycleError(
                "build authorization differs from the exact native SDK intent"
            )
        contract = self._contract(intent.component_revision)
        providers = self._intent_artifacts[intent.identity.uri]
        package_artifacts = self._intent_package_artifacts[intent.identity.uri]
        return StandardPlanFinalizationInputs(
            intent,
            authorization,
            contract,
            providers,
            package_artifacts,
            dependency_resolution=(
                "npm"
                if intent.component_revision.uri in self.npm_targets
                else "python"
                if intent.component_revision.uri in self.python_targets
                else "none"
            ),
        )

    def finalize(self, intent, authorization) -> StandardComponentBuildPlan:
        inputs = self.plan_finalization_inputs(intent, authorization)
        plan = inputs.finalize()
        return self._register_finalized_plan(inputs, plan)

    def accept_finalized_plan(
        self, intent, authorization, plan
    ) -> StandardComponentBuildPlan:
        """Recheck current local authority before retaining a remote result."""
        inputs = self.plan_finalization_inputs(intent, authorization)
        try:
            validate_standard_build_authority(plan, inputs, now=self.clock())
        except StandardBuildInputError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        return self._register_finalized_plan(inputs, plan)

    def _register_finalized_plan(
        self, inputs: StandardPlanFinalizationInputs, plan: StandardComponentBuildPlan
    ) -> StandardComponentBuildPlan:
        intent, authorization = inputs.intent, inputs.authorization
        self._plans_by_revision[intent.component_revision.uri] = plan
        self._plan_inputs_by_revision[intent.component_revision.uri] = inputs
        if intent.native_sdk_input_identities:
            self._sdk_build_authorizations[plan.identity.uri] = (
                intent.build_request,
                authorization.grant,
            )
        return plan

    def build_execution_inputs(
        self, plan: StandardComponentBuildPlan
    ) -> StandardPlanFinalizationInputs:
        """Read current controller custody without granting host command access."""
        if (
            not isinstance(plan, StandardComponentBuildPlan)
            or self._plans_by_revision.get(plan.component_revision.uri) != plan
        ):
            raise LocalStandardLifecycleError("BUILD plan is not currently registered")
        inputs = self._plan_inputs_by_revision.get(plan.component_revision.uri)
        if (
            inputs is None
            or self.plan_finalization_inputs(inputs.intent, inputs.authorization)
            != inputs
        ):
            raise LocalStandardLifecycleError(
                "retained BUILD inputs are no longer current"
            )
        try:
            validate_standard_build_authority(plan, inputs, now=self.clock())
        except StandardBuildInputError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        if self._plans_by_revision.get(plan.component_revision.uri) != plan:
            raise LocalStandardLifecycleError(
                "BUILD plan changed during input admission"
            )
        return inputs

    def _provider_materials(
        self,
        providers: tuple[ArtifactExport, ...],
        *,
        consumer_revision: ContentIdentity | None = None,
    ) -> tuple[dict[str, object], ...]:
        pending = list(providers)
        closure: dict[str, ArtifactExport] = {}
        while pending:
            item = pending.pop()
            if item.identity.uri in closure:
                continue
            closure[item.identity.uri] = item
            pending.extend(
                self._exports_by_identity[dependency.uri]
                for dependency in item.dependency_artifact_identities
            )
        bindings = tuple(
            binding
            for values in self._library_consumer_bindings.values()
            for binding in values
            if consumer_revision is not None
            and binding.consumer_component_revision == consumer_revision
        )
        materials = []
        for item in sorted(closure.values(), key=lambda value: value.export_id):
            value: dict[str, object] = {
                "export_id": item.export_id,
                "blob": item.blob.to_dict(),
                "role": item.role,
                "abi_identity": item.abi_identity.uri,
                "target_identity": item.target_identity.uri,
                "source_tree_identity": item.source_tree_identity.uri,
                "toolchain_identity": item.toolchain_identity.uri,
            }
            item_bindings = tuple(
                binding.to_dict()
                for binding in bindings
                if binding.artifact_identity == item.identity
            )
            if item_bindings:
                value["library_consumer_bindings"] = list(item_bindings)
            materials.append(value)
        return tuple(materials)

    def _environment(self, providers: tuple[ArtifactExport, ...]) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(self._provider_environment_values(providers))
        controlled = {
            name: value
            for name, value in environment.items()
            if name.casefold() not in {"pythonpath", "litai_compiler_cache_tool"}
        }
        return controlled_node_environment(controlled)

    @staticmethod
    def _binding_environment(
        environment: dict[str, str], binding: LocalComponentToolBinding
    ) -> dict[str, str]:
        if not binding.environment:
            return environment
        overridden = {name.casefold() for name, _value in binding.environment}
        selected = {
            name: value
            for name, value in environment.items()
            if name.casefold() not in overridden
        }
        selected.update(dict(binding.environment))
        return selected

    def _provider_environment_values(
        self, providers: tuple[ArtifactExport, ...]
    ) -> dict[str, str]:
        """Resolve only declared provider bindings, never ambient host secrets."""

        environment: dict[str, str] = {}
        pending = list(providers)
        closure: dict[str, ArtifactExport] = {}
        while pending:
            item = pending.pop()
            if item.identity.uri in closure:
                continue
            closure[item.identity.uri] = item
            pending.extend(
                self._exports_by_identity[dependency.uri]
                for dependency in item.dependency_artifact_identities
            )
        for item in closure.values():
            binding = self.provider_environment.get(item.export_id)
            if binding is None:
                raise LocalStandardLifecycleError(
                    f"provider artifact {item.export_id} has no runtime binding"
                )
            environment_name, relative_path = binding
            root = self._artifact_paths[item.identity.uri].resolve(strict=True)
            candidate = root / relative_path
            if candidate.is_symlink():
                raise LocalStandardLifecycleError("provider binding cannot be a link")
            target = candidate.resolve(strict=True)
            library_directory = (
                target.is_dir() and self._contract(item.component_revision).is_library
            )
            if (
                root not in target.parents
                or not (target.is_file() or library_directory)
                or target.is_symlink()
            ):
                raise LocalStandardLifecycleError(
                    f"provider artifact {item.export_id} binding is unavailable"
                )
            environment[environment_name] = str(target)
        return environment

    def _run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        providers: tuple[ArtifactExport, ...],
        binding: LocalComponentToolBinding | None = None,
        timeout_seconds: float = 60.0,
        extra_environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not command:
            raise LocalStandardLifecycleError("local command cannot be empty")
        environment = self._environment(providers)
        if binding is not None:
            environment = self._binding_environment(environment, binding)
        if extra_environment is not None:
            environment.update(extra_environment)
        environment = inherited_verbose_environment(environment)
        return self._run_with_environment(
            command, cwd=cwd, environment=environment, timeout_seconds=timeout_seconds
        )

    def _run_with_environment(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the exact environment already bound by the command boundary."""
        if not 0 < timeout_seconds < float("inf"):
            raise ValueError("local command timeout must be positive and finite")
        _started = datetime.now(UTC)
        trace_subprocess(command, cwd=cwd, environment=environment)
        completed = run_with_tree_kill(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            timeout=timeout_seconds,
        )
        trace_subprocess(
            command,
            cwd=cwd,
            environment=environment,
            status=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            started_at=_started,
        )
        if completed.returncode != 0:
            from literate_ai.diagnostics import redact_secrets

            raise LocalStandardLifecycleError(
                f"local command failed ({completed.returncode}): "
                f"{redact_secrets(completed.stderr.strip(), environment)}"
            )
        return completed

    def retain_evidence_with(self, recorder) -> None:
        """Enable bounded payload retention before this runtime starts building."""

        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceRecorder,
        )

        if not isinstance(recorder, QualificationEvidenceRecorder):
            raise TypeError("evidence recorder must be a QualificationEvidenceRecorder")
        if self._build_evidence or self._build_observations:
            raise LocalStandardLifecycleError("evidence capture must precede builds")
        self._evidence_recorder = recorder

    def retained_evidence_records(self) -> tuple[tuple[ContentIdentity, bytes], ...]:
        """Snapshot the recorder installed before provider and consumer admission."""
        if self._evidence_recorder is None:
            raise LocalStandardLifecycleError("bounded evidence capture is required")
        return self._evidence_recorder.entries

    def retain_evidence_record(self, identity: ContentIdentity, content: bytes) -> None:
        """Retain verified transfer bytes in the already installed bounded recorder."""
        if self._evidence_recorder is None:
            raise LocalStandardLifecycleError("bounded evidence capture is required")
        if (
            not isinstance(content, bytes)
            or ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            )
            != identity
        ):
            raise LocalStandardLifecycleError("transferred evidence identity differs")
        self._evidence_recorder.remember_bytes(content)

    def _record_evidence(self, value: object) -> ContentIdentity:
        if self._evidence_recorder is not None:
            return self._evidence_recorder.remember_json(value)
        return canonical_identity(value)

    def _record_product_evidence(self, value: object) -> ContentIdentity:
        if self._evidence_recorder is not None:
            return self._evidence_recorder.remember_bytes(product_json_bytes(value))
        return product_json_identity(value)

    def _process_observation(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        phase: str,
        plan_identity: ContentIdentity,
        execution_authority: StandardExecutionAuthority | None = None,
    ) -> ContentIdentity:
        """Record bounded process facts without embedding host-specific paths."""

        document = {
            "schema": "literate-ai/local-process-observation@1",
            "phase": phase,
            "plan_identity": plan_identity.uri,
            "returncode": result.returncode,
            "stdout_identity": self._record_evidence(result.stdout).uri,
            "stderr_identity": self._record_evidence(result.stderr).uri,
        }
        if execution_authority is not None:
            document["execution_authority_identity"] = execution_authority.identity.uri
        sdk_execution = getattr(result, "native_sdk_execution_identity", None)
        if sdk_execution is not None:
            document["native_sdk_execution_identity"] = sdk_execution.uri
        sdk_build = getattr(result, "native_sdk_build_identity", None)
        if sdk_build is not None:
            document["native_sdk_build_identity"] = sdk_build.uri
        cache = getattr(result, "compiler_cache_observation", None)
        if cache is not None:
            document["compiler_cache"] = cache
        return self._record_evidence(document)

    def _npm_process_identity(
        self,
        result: object,
        *,
        phase: str,
        arguments: tuple[str, ...],
        plan: StandardComponentBuildPlan,
    ) -> ContentIdentity:
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)
        returncode = getattr(result, "returncode", None)
        if (
            not isinstance(stdout, bytes)
            or not isinstance(stderr, bytes)
            or not isinstance(returncode, int)
        ):
            raise LocalStandardLifecycleError(
                "npm lifecycle command returned invalid bounded process evidence"
            )
        return self._record_evidence(
            {
                "schema": "literate-ai/local-npm-process-observation@1",
                "phase": phase,
                "plan_identity": plan.identity.uri,
                "returncode": returncode,
                "arguments": list(arguments),
            }
        )

    @staticmethod
    def _npm_evidence_file_records(
        root: Path,
    ) -> tuple[tuple[str, ContentIdentity], ...]:
        if path_is_link_or_reparse(root) or not root.is_dir():
            raise LocalStandardLifecycleError(
                "npm dependency evidence directory is unsafe"
            )
        records = []
        for path in sorted(root.iterdir()):
            if path.name == "evidence-manifest.json":
                continue
            if path_is_link_or_reparse(path) or not path.is_file():
                raise LocalStandardLifecycleError(
                    "npm dependency evidence contains a non-regular file"
                )
            content = path.read_bytes()
            if len(content) > _MAX_NPM_PROCESS_OUTPUT_BYTES:
                raise LocalStandardLifecycleError(
                    "npm dependency evidence exceeds its byte limit"
                )
            records.append(
                (
                    f".literate/npm/{path.name}",
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                )
            )
        return tuple(records)

    def _write_npm_dependency_evidence(
        self,
        plan: StandardComponentBuildPlan,
        artifact_root: Path,
        *,
        target: StandardNpmTarget,
        authority: StandardNpmSourceAuthority,
        manifest_bytes: bytes,
        lockfile_bytes: bytes,
        inventory_bytes: bytes,
        install_process_identity: ContentIdentity,
        inventory_process_identity: ContentIdentity,
        installed_tree_identity: ContentIdentity,
    ) -> dict[str, object]:
        evidence_root = artifact_root / ".literate" / "npm"
        if evidence_root.exists() or evidence_root.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved npm evidence path"
            )
        evidence_root.mkdir(parents=True)
        (evidence_root / "package.json").write_bytes(manifest_bytes)
        (evidence_root / "package-lock.json").write_bytes(lockfile_bytes)
        (evidence_root / "inventory.json").write_bytes(inventory_bytes)
        evidence = StandardNpmDependencyEvidence(
            authorization_identity=plan.request.authorization_identity,
            component_revision=plan.component_revision,
            build_plan_identity=plan.identity,
            source_tree_identity=plan.request.source_tree_identity,
            packaging_flavor_revision_identity=(
                target.packaging_flavor_revision_identity
            ),
            packaging_profile_identity=target.packaging_profile_identity,
            npm_target_identity=target.identity,
            npm_toolchain_identity=target.build_system_toolchain_identity,
            node_toolchain_identity=target.node_toolchain_identity,
            source_authority_identity=authority.identity,
            manifest_identity=authority.manifest_identity,
            lockfile_identity=authority.lockfile_identity,
            install_process_identity=install_process_identity,
            inventory_process_identity=inventory_process_identity,
            inventory_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(inventory_bytes).hexdigest()
            ),
            installed_tree_identity=installed_tree_identity,
            files=self._npm_evidence_file_records(evidence_root),
        )
        evidence_bytes = canonical_json_bytes(evidence.to_dict())
        (evidence_root / "evidence-manifest.json").write_bytes(evidence_bytes)
        if self._evidence_recorder is not None:
            self._record_evidence(target.identity_document())
            self._record_evidence(authority.identity_document())
            for payload in (
                manifest_bytes,
                lockfile_bytes,
                inventory_bytes,
                evidence_bytes,
            ):
                self._evidence_recorder.remember_bytes(payload)
        return {
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-npm-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "npm_source_authority_identity": authority.identity.uri,
            "npm_installed_graph_identity": evidence.inventory_identity.uri,
            "npm_installed_tree_identity": installed_tree_identity.uri,
            "standard_npm_evidence_manifest": _NPM_EVIDENCE_MANIFEST,
            "evidence_manifest_digest": "sha256:"
            + hashlib.sha256(evidence_bytes).hexdigest(),
        }

    def _validated_npm_evidence(
        self, plan: StandardComponentBuildPlan, artifact_root: Path
    ) -> dict[str, object]:
        target = self.npm_targets[plan.component_revision.uri]
        manifest_path = artifact_root.joinpath(*Path(_NPM_EVIDENCE_MANIFEST).parts)
        if path_is_link_or_reparse(manifest_path) or not manifest_path.is_file():
            raise LocalStandardLifecycleError(
                "Standard npm dependency evidence manifest is missing"
            )
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > _MAX_NPM_PROCESS_OUTPUT_BYTES:
            raise LocalStandardLifecycleError(
                "Standard npm dependency evidence manifest is oversized"
            )
        actual_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        pending = self._npm_resolution_builds.get(plan.identity.uri)
        if (
            pending is not None
            and pending.get("evidence_manifest_digest") != actual_digest
        ):
            raise LocalStandardLifecycleError(
                "Standard npm dependency evidence manifest changed"
            )
        try:
            document = json.loads(manifest_bytes)
            if (
                not isinstance(document, dict)
                or canonical_json_bytes(document) != manifest_bytes
            ):
                raise ValueError("noncanonical npm evidence")
            raw_files = document["files"]
            if not isinstance(raw_files, list):
                raise ValueError("invalid npm evidence files")
            evidence = StandardNpmDependencyEvidence(
                authorization_identity=ContentIdentity.parse_uri(
                    document["authorization_identity"]
                ),
                component_revision=ContentIdentity.parse_uri(
                    document["component_revision"]
                ),
                build_plan_identity=ContentIdentity.parse_uri(
                    document["build_plan_identity"]
                ),
                source_tree_identity=ContentIdentity.parse_uri(
                    document["source_tree_identity"]
                ),
                packaging_flavor_revision_identity=ContentIdentity.parse_uri(
                    document["packaging_flavor_revision_identity"]
                ),
                packaging_profile_identity=ContentIdentity.parse_uri(
                    document["packaging_profile_identity"]
                ),
                npm_target_identity=ContentIdentity.parse_uri(
                    document["npm_target_identity"]
                ),
                npm_toolchain_identity=ContentIdentity.parse_uri(
                    document["npm_toolchain_identity"]
                ),
                node_toolchain_identity=ContentIdentity.parse_uri(
                    document["node_toolchain_identity"]
                ),
                source_authority_identity=ContentIdentity.parse_uri(
                    document["source_authority_identity"]
                ),
                manifest_identity=ContentIdentity.parse_uri(
                    document["manifest_identity"]
                ),
                lockfile_identity=ContentIdentity.parse_uri(
                    document["lockfile_identity"]
                ),
                install_process_identity=ContentIdentity.parse_uri(
                    document["install_process_identity"]
                ),
                inventory_process_identity=ContentIdentity.parse_uri(
                    document["inventory_process_identity"]
                ),
                inventory_identity=ContentIdentity.parse_uri(
                    document["inventory_identity"]
                ),
                installed_tree_identity=ContentIdentity.parse_uri(
                    document["installed_tree_identity"]
                ),
                files=tuple(
                    (
                        str(item["path"]),
                        ContentIdentity.parse_uri(item["identity"]),
                    )
                    for item in raw_files
                    if isinstance(item, dict)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStandardLifecycleError(
                "Standard npm dependency evidence manifest is invalid"
            ) from exc
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        try:
            authority = load_npm_source_authority(source, target)
        except StandardNpmLifecycleError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        expected_authority = (
            plan.request.authorization_identity,
            plan.component_revision,
            plan.identity,
            plan.request.source_tree_identity,
            target.packaging_flavor_revision_identity,
            target.packaging_profile_identity,
            target.identity,
            target.build_system_toolchain_identity,
            target.node_toolchain_identity,
            authority.identity,
            authority.manifest_identity,
            authority.lockfile_identity,
        )
        observed_authority = (
            evidence.authorization_identity,
            evidence.component_revision,
            evidence.build_plan_identity,
            evidence.source_tree_identity,
            evidence.packaging_flavor_revision_identity,
            evidence.packaging_profile_identity,
            evidence.npm_target_identity,
            evidence.npm_toolchain_identity,
            evidence.node_toolchain_identity,
            evidence.source_authority_identity,
            evidence.manifest_identity,
            evidence.lockfile_identity,
        )
        if evidence.to_dict() != document or observed_authority != expected_authority:
            raise LocalStandardLifecycleError(
                "Standard npm dependency evidence names different authority"
            )
        actual_files = self._npm_evidence_file_records(manifest_path.parent)
        if evidence.files != actual_files:
            raise LocalStandardLifecycleError(
                "Standard npm dependency manifest does not cover its exact files"
            )
        retained_manifest = (manifest_path.parent / "package.json").read_bytes()
        retained_lock = (manifest_path.parent / "package-lock.json").read_bytes()
        inventory = (manifest_path.parent / "inventory.json").read_bytes()
        source_manifest = source.joinpath(*PurePosixPath(target.manifest).parts)
        source_lock = source.joinpath(*PurePosixPath(target.lockfile).parts)
        if (
            retained_manifest != source_manifest.read_bytes()
            or retained_lock != source_lock.read_bytes()
            or inventory != normalized_npm_inventory(authority)
            or evidence.inventory_identity
            != ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(inventory).hexdigest()
            )
        ):
            raise LocalStandardLifecycleError(
                "retained npm dependency evidence differs from source authority"
            )
        package_parent = PurePosixPath(target.manifest).parent
        export_root = (
            artifact_root
            / self._contract(plan.component_revision).artifact_export.export_id
        )
        installed = export_root.joinpath(*package_parent.parts) / "node_modules"
        if installed.is_symlink() or not installed.is_dir():
            raise LocalStandardLifecycleError(
                "installed npm dependency tree differs from retained evidence"
            )
        self._require_regular_npm_tree(installed)
        if local_tree_identity(installed) != evidence.installed_tree_identity:
            raise LocalStandardLifecycleError(
                "installed npm dependency tree differs from retained evidence"
            )
        return {
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-npm-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "npm_source_authority_identity": authority.identity.uri,
            "npm_installed_graph_identity": evidence.inventory_identity.uri,
            "npm_installed_tree_identity": evidence.installed_tree_identity.uri,
            "standard_npm_evidence_manifest": _NPM_EVIDENCE_MANIFEST,
            "evidence_manifest_digest": actual_digest,
        }

    def _write_resolved_sbom(
        self, plan: StandardComponentBuildPlan, artifact_root: Path
    ) -> CycloneDxBomBinding:
        if plan.component_revision.uri in self.python_targets:
            observer = self._python_observers[str(artifact_root.resolve())]
            return self._resolve_and_write_sbom(
                plan, artifact_root, python_observer=observer
            )
        if plan.component_revision.uri not in self.npm_targets:
            return self._resolve_and_write_sbom(plan, artifact_root)
        evidence = self._validated_npm_evidence(plan, artifact_root)
        manifest_digest = evidence["evidence_manifest_digest"]
        assert isinstance(manifest_digest, str)
        additional = {
            key: value
            for key, value in evidence.items()
            if key != "evidence_manifest_digest"
        }
        additional["npm_lifecycle_profile"] = True
        return self._resolve_and_write_sbom(
            plan,
            artifact_root,
            artifact_digest=manifest_digest,
            additional_build_evidence=additional,
        )

    @staticmethod
    def _source_text_files(root: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            files[path.relative_to(root).as_posix()] = content
        return files

    def _resolve_and_write_sbom(
        self,
        plan: StandardComponentBuildPlan,
        artifact_root: Path,
        *,
        dependency_observation: BuildDependencyObservation | None = None,
        artifact_digest: str | None = None,
        additional_build_evidence: Mapping[str, object] | None = None,
        python_observer: StandardPythonDependencyObserver | None = None,
    ) -> CycloneDxBomBinding:
        source = self.source_trees.evidence(plan.request.source_tree_identity)
        from literate_ai.adapters.native_sdk_dependencies import (
            merge_native_sdk_dependencies,
            merge_sdk_host_observations,
        )
        from literate_ai.contracts.sbom import project_component_lock_managed_graph

        contract = self._contract(plan.component_revision)
        if self._sdk_input_identities(contract) != (
            plan.materialization.native_sdk_input_identities
        ):
            raise LocalStandardLifecycleError(
                "resolved BOM plan differs from the exact live native SDK inputs"
            )
        sdk_evidence = ()
        if self._native_sdk_inputs is not None:
            expected_graph = project_component_lock_managed_graph(
                self._native_sdk_inputs.snapshot.authority.lock,
                plan.component_revision,
            )
            if source.managed_graph != expected_graph:
                raise LocalStandardLifecycleError(
                    "resolved BOM managed graph differs from native SDK authority"
                )
            sdk_evidence = self._native_sdk_inputs.dependency_evidence(
                plan.component_revision, parent=self.object_root
            )
        sdk_observation, sdk_resolutions = merge_native_sdk_dependencies(sdk_evidence)
        dependency_inventory = self.dependency_observation
        if sdk_evidence:
            host_observation = _RecordedHostDependencyObserver(
                dependency_inventory
            ).observe({}, root_ref=source.managed_graph.root_ref)
            sdk_observation = merge_sdk_host_observations(
                sdk_observation, host_observation
            )
            dependency_inventory = HostDependencyObservation((), ())
        for item in sdk_evidence:
            self._record_evidence(item.to_dict())
            self._record_evidence(item.repository_resolution.to_dict())
        destination = artifact_root.joinpath(*Path(_RESOLVED_SBOM).parts)
        evidence_directory = destination.parent
        if path_is_link_or_reparse(evidence_directory) or (
            evidence_directory.exists() and not evidence_directory.is_dir()
        ):
            raise LocalStandardLifecycleError(
                "build output occupied the reserved resolved-SBOM evidence directory"
            )
        if destination.exists() or destination.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved resolved-SBOM evidence path"
            )
        evidence_directory.mkdir(parents=True, exist_ok=True)
        if evidence_directory.resolve(strict=True).parent != artifact_root.resolve(
            strict=True
        ):
            raise LocalStandardLifecycleError(
                "reserved resolved-SBOM evidence directory escaped artifact custody"
            )
        source_root = self.source_trees.resolve(plan.request.source_tree_identity)
        files = self._source_text_files(source_root)
        if files.get(CYCLONEDX_SOURCE_SBOM_PATH) is None:
            raise LocalStandardLifecycleError(
                "admitted source tree lost its source CycloneDX BOM"
            )
        contract = self._contract(plan.component_revision)
        build_toolchain_identity = contract.tool_binding(
            ComponentCommandPhase.BUILD
        ).toolchain_identity.uri
        build: dict[str, object] = {
            "artifact_path": str(artifact_root),
            "artifact_digest": artifact_digest
            or local_tree_identity(artifact_root).uri,
            "source_bundle_digest": plan.request.source_tree_identity.uri,
            "toolchain_identity": build_toolchain_identity,
        }
        if dependency_observation is not None:
            build["dependency_observation"] = dependency_observation.to_dict()
        if additional_build_evidence is not None:
            reserved = set(build).intersection(additional_build_evidence)
            if reserved:
                raise LocalStandardLifecycleError(
                    "additional dependency evidence tried to replace build authority: "
                    + ", ".join(sorted(reserved))
                )
            build.update(additional_build_evidence)
        resolver = CycloneDxLifecycleResolver(
            managed_graph=source.managed_graph,
            observer=_RecordedHostDependencyObserver(dependency_inventory),
            evidence_path=destination,
            additional_components=sdk_observation.components,
            additional_edges=sdk_observation.edges,
            repository_resolutions=sdk_resolutions,
            # This module belongs to the verified SDK runtime driver, not pip.
            # Only this custody-checked SDK path supplies it; ordinary sources
            # must still declare every external import in their BOM.
            runtime_python_imports=(
                frozenset({"literate_ai_native_sdk"}) if sdk_evidence else frozenset()
            ),
            python_source_authority=(
                None if python_observer is None else python_observer.source
            ),
            python_dependency_observer=python_observer,
            allow_missing_cargo_lock=(
                additional_build_evidence is not None
                and additional_build_evidence.get("cargo_lifecycle_profile") is True
            ),
        )
        resolved = resolver.resolve(
            {
                "effective_revision_digest": plan.component_revision.uri,
                "source_bundle_digest": plan.request.source_tree_identity.uri,
                "files": files,
            },
            build,
        )
        binding = CycloneDxBomBinding.from_dict(resolved["resolved_bom"])
        return binding

    def _write_artifact_manifest(
        self,
        plan: StandardComponentBuildPlan,
        artifact_root: Path,
        provider_materials: tuple[dict[str, object], ...],
        process_observation: ContentIdentity,
    ) -> None:
        manifest_path = artifact_root / _ARTIFACT_MANIFEST
        if manifest_path.exists() or manifest_path.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved artifact evidence path"
            )
        resolved_sbom = self._write_resolved_sbom(plan, artifact_root)
        artifact_tree = self._record_evidence(_local_tree_document(artifact_root))
        observation = self._record_evidence(
            {
                "schema": "literate-ai/local-build-observation@1",
                "build_plan_identity": plan.identity.uri,
                "process_observation_identity": process_observation.uri,
                "artifact_tree_identity": artifact_tree.uri,
                "resolved_sbom_identity": resolved_sbom.bom_identity.uri,
            }
        )
        manifest_bytes = json.dumps(
            {
                "tree": artifact_tree.uri,
                "provider_materials": provider_materials,
                "build_observation": observation.uri,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        manifest_path.write_bytes(manifest_bytes)
        if self._evidence_recorder is not None:
            self._evidence_recorder.remember_bytes(manifest_bytes)
        self._build_observations[str(artifact_root.resolve())] = observation

    def _provider_roots(self, providers: tuple[ArtifactExport, ...]) -> tuple[str, ...]:
        return tuple(
            str(self._artifact_paths[item.identity.uri].resolve(strict=True))
            for item in providers
        )

    def _run_locked(
        self,
        contract: ComponentCommandContract,
        phase: ComponentCommandPhase,
        *,
        source_root: Path,
        object_root: Path,
        artifact_root: Path,
        export_path: Path,
        providers: tuple[ArtifactExport, ...],
        entrypoint_contract: ComponentEntrypointCommandContract | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Resolve typed roles to argv without a shell or ambient tool inference."""
        self._require_command_phase(phase)

        command = (
            contract if entrypoint_contract is None else entrypoint_contract
        ).command(phase)
        if (
            phase is not ComponentCommandPhase.BUILD
            and self._sdk_input_identities(contract)
        ) or ComponentCommandRole.NATIVE_SDK_INPUTS in command.roles:
            try:
                return self._run_sdk_locked(
                    contract,
                    phase,
                    source_root=source_root,
                    object_root=object_root,
                    artifact_root=artifact_root,
                    export_path=export_path,
                    providers=providers,
                    entrypoint_contract=entrypoint_contract,
                )
            except LocalStandardLifecycleError as exc:
                code = {
                    ComponentCommandPhase.BUILD: "builder.generated-source-rejected",
                    ComponentCommandPhase.TEST: "generated-test.failed",
                }.get(phase)
                if code is None:
                    raise
                raise GeneratedCandidateCommandError(code, str(exc)) from exc

        with log_operation(
            "lifecycle_phase",
            phase=phase.value,
            component=contract.component_revision.digest[:12],
        ):
            argv = self._locked_argv(
                contract,
                phase,
                source_root=source_root,
                object_root=object_root,
                artifact_root=artifact_root,
                export_path=export_path,
                providers=providers,
                entrypoint_contract=entrypoint_contract,
            )
            phase_binding = (
                contract.tool_binding(phase)
                if entrypoint_contract is None
                else entrypoint_contract.tool_binding(phase)
            )
            binding = self.tool_bindings[phase_binding.toolchain_identity.uri]
            binding.require_unchanged()
            try:
                from literate_ai.adapters.standard_project import (
                    native_cpp_cache_contract,
                )

                if (
                    phase is ComponentCommandPhase.BUILD
                    and self.shared_cache is not None
                    and self.shared_cache.compiler_tool is not None
                    and native_cpp_cache_contract(contract)
                ):
                    with compiler_cache_session(
                        self.shared_cache,
                        environment=self._binding_environment(
                            self._environment(providers), binding
                        ),
                        workspace=artifact_root,
                    ) as session:
                        environment = dict(session.environment)
                        environment["LITAI_COMPILER_CACHE_TOOL"] = (
                            self.shared_cache.compiler_tool.executable
                            if session.observation["available"]
                            else ""
                        )
                        result = self._run_with_environment(
                            argv, cwd=artifact_root, environment=environment
                        )
                        token = self.shared_cache.credential()
                        if token is not None:
                            result.stdout = result.stdout.replace(token, "<redacted>")
                            result.stderr = result.stderr.replace(token, "<redacted>")
                    result.compiler_cache_observation = session.observation
                else:
                    result = self._run(
                        argv, cwd=artifact_root, providers=providers, binding=binding
                    )
            except LocalStandardLifecycleError as exc:
                code = {
                    ComponentCommandPhase.BUILD: "builder.generated-source-rejected",
                    ComponentCommandPhase.TEST: "generated-test.failed",
                }.get(phase)
                if code is None:
                    raise
                raise GeneratedCandidateCommandError(code, str(exc)) from exc
            binding.require_unchanged()
            return result

    def _locked_argv(
        self,
        contract: ComponentCommandContract,
        phase: ComponentCommandPhase,
        *,
        source_root: Path,
        object_root: Path,
        artifact_root: Path,
        export_path: Path,
        providers: tuple[ArtifactExport, ...],
        entrypoint_contract: ComponentEntrypointCommandContract | None = None,
        native_sdk_manifest: Path | None = None,
        provider_roots: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        """Materialize one typed command without executing it."""

        command = (
            contract.command(phase)
            if entrypoint_contract is None
            else entrypoint_contract.command(phase)
        )
        phase_binding = (
            contract.tool_binding(phase)
            if entrypoint_contract is None
            else entrypoint_contract.tool_binding(phase)
        )
        binding = self.tool_bindings[phase_binding.toolchain_identity.uri]
        binding.require_unchanged()
        if (
            phase is not ComponentCommandPhase.BUILD
            and contract.component_revision.uri in self.python_targets
        ):
            artifact_key = str(artifact_root.resolve())
            expected_tree = self._python_execution_trees.get(artifact_key)
            if (
                expected_tree is None
                or local_tree_identity(artifact_root) != expected_tree
            ):
                raise LocalStandardLifecycleError(
                    "Python execution differs from its sealed artifact"
                )
            observer = self._python_observers.get(artifact_key)
            if observer is None:
                raise LocalStandardLifecycleError(
                    "Python execution lacks sealed dependency custody"
                )
            observer()
        values = {
            ComponentCommandRole.TOOL: binding.command,
            ComponentCommandRole.SOURCE_ROOT: (str(source_root),),
            ComponentCommandRole.OBJECT_ROOT: (str(object_root),),
            ComponentCommandRole.ARTIFACT_ROOT: (str(artifact_root),),
            ComponentCommandRole.EXPORT_PATH: (str(export_path),),
            ComponentCommandRole.PROVIDER_ARTIFACTS: (
                self._provider_roots(providers)
                if provider_roots is None
                else provider_roots
            ),
        }
        if native_sdk_manifest is not None:
            values[ComponentCommandRole.NATIVE_SDK_INPUTS] = (str(native_sdk_manifest),)
        try:
            bindings = {role: values[role] for role in command.roles}
            argv = command.substitute(bindings)
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStandardLifecycleError(
                f"locked {phase.value} command roles cannot be materialized"
            ) from exc
        binding.require_unchanged()
        return argv

    def _run_sdk_locked(
        self,
        contract: ComponentCommandContract,
        phase: ComponentCommandPhase,
        *,
        source_root: Path,
        object_root: Path,
        artifact_root: Path,
        export_path: Path,
        providers: tuple[ArtifactExport, ...],
        entrypoint_contract: ComponentEntrypointCommandContract | None,
    ) -> subprocess.CompletedProcess[str]:
        from literate_ai.adapters.native_sdk_execution import (
            isolated_sdk_environment,
            prepare_native_sdk_execution,
        )

        command = (
            contract if entrypoint_contract is None else entrypoint_contract
        ).command(phase)
        if ComponentCommandRole.NATIVE_SDK_INPUTS not in command.roles:
            raise LocalStandardLifecycleError(
                "SDK command has no locked native SDK input role"
            )
        plan = self._plans_by_revision.get(contract.component_revision.uri)
        if (
            plan is None
            or self._sdk_input_identities(contract)
            != plan.materialization.native_sdk_input_identities
        ):
            raise LocalStandardLifecycleError(
                "SDK command requires its exact finalized build plan"
            )
        if self.source_trees.resolve(
            plan.request.source_tree_identity
        ) != source_root.resolve(strict=True):
            raise LocalStandardLifecycleError(
                "SDK command source differs from finalized custody"
            )
        phase_binding = (
            contract if entrypoint_contract is None else entrypoint_contract
        ).tool_binding(phase)
        binding = self.tool_bindings[phase_binding.toolchain_identity.uri]

        def authorize(request, runtime_identity):
            now = self.clock()
            return BuildAuthorization(
                authorization_id=f"local-sdk:{canonical_identity(request.to_dict()).digest}",
                classification_digest=runtime_identity.uri,
                request_digest=canonical_identity(request.to_dict()).uri,
                effective_revision_digest=contract.component_revision.uri,
                actor="local-standard-lifecycle",
                reason="execute the configured command with verified SDK inputs",
                profile=SecurityProfile.CONSTRAINED,
                privileges=request.requested_privileges,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )

        owner = self._native_sdk_inputs
        if owner is None:
            raise LocalStandardLifecycleError(
                "SDK command requires live admitted inputs"
            )
        if owner is not None:
            from literate_ai.adapters.native_sdk_linked_runtime import (
                LinkedNativeSdkExecutionInputs,
                native_sdk_runtime_revisions,
            )
            from literate_ai.adapters.native_sdk_package_scope import (
                NativeSdkPackageExecutionScope,
            )
            from literate_ai.application.artifact_graph import (
                create_artifact_build_graph,
                realize_manifest,
            )

            revisions = native_sdk_runtime_revisions(
                owner.snapshot, contract.component_revision
            )
            if any(
                self._sdk_input_identities(self._contract(revision))
                for revision in revisions
                if revision != contract.component_revision
            ):
                if any(
                    revision.uri not in self._plans_by_revision
                    for revision in revisions
                ):
                    raise LocalStandardLifecycleError(
                        "linked SDK owner lacks its finalized build plan"
                    )
                plans = tuple(
                    self._plans_by_revision[revision.uri] for revision in revisions
                )
                manifests = tuple(
                    realize_manifest(
                        item.manifest,
                        tuple(
                            export
                            for export in self._exports_by_identity.values()
                            if export.component_revision == item.component_revision
                        ),
                    )
                    for item in plans
                )
                root_export = self._planned_exports[contract.component_revision.uri]
                if entrypoint_contract is not None:
                    root_export = next(
                        export
                        for export in self._exports_by_identity.values()
                        if export.component_revision == contract.component_revision
                        and export.export_id
                        == entrypoint_contract.artifact_export.export_id
                    )
                graph = create_artifact_build_graph(
                    build_system_driver_identity=plan.manifest.build_system_driver_identity,
                    manifests=manifests,
                    link_roots=(root_export.identity,),
                )
                scope = NativeSdkPackageExecutionScope(
                    owner.snapshot.authority.lock,
                    graph,
                    graph.link_plans[0],
                    plans,
                    tuple(self._contract(revision) for revision in revisions),
                    contract.component_revision,
                )
                scope.select_bindings(
                    owner.for_scope(contract.component_revision),
                    contract.component_revision,
                )
                owner = LinkedNativeSdkExecutionInputs(owner, scope)
                for item in plans:
                    self._record_evidence(item.to_dict())

        environment = inherited_verbose_environment(
            self._binding_environment(
                isolated_sdk_environment(self._environment(providers)), binding
            )
        )

        with prepare_native_sdk_execution(
            owner,
            contract.component_revision,
            target=contract.artifact_export.target_identity,
            parent=self.object_root,
        ) as inputs:
            argv = self._locked_argv(
                contract,
                phase,
                source_root=source_root,
                object_root=object_root,
                artifact_root=artifact_root,
                export_path=export_path,
                providers=providers,
                entrypoint_contract=entrypoint_contract,
                native_sdk_manifest=inputs.manifest,
            )
            result, evidence = inputs.run(
                argv,
                tool=binding,
                cwd=artifact_root,
                environment=environment,
                source_identity=plan.request.source_tree_identity,
                phase=phase.value,
                command_identity=command.identity,
                command_contract_identity=contract.identity,
                entrypoint_identity=None
                if entrypoint_contract is None
                else entrypoint_contract.entrypoint_identity,
                authorize=authorize,
                clock=self.clock,
                execute=lambda command, env: self._run_with_environment(
                    command, cwd=artifact_root, environment=env
                ),
                record=self._record_evidence,
            )
            result.native_sdk_execution_identity = evidence
            self._record_evidence(plan.to_dict())
            return result

    def _require_command_phase(self, phase: ComponentCommandPhase) -> None:
        if phase not in self._command_phases:
            raise LocalStandardLifecycleError(
                f"command phase {phase.value} is outside the local runtime scope"
            )

    def _require_full_command_scope(self) -> None:
        if self._command_phases != tuple(ComponentCommandPhase):
            raise LocalStandardLifecycleError(
                "independent acceptance requires the full local command scope"
            )

    def build(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardBuildOutput:
        self._require_command_phase(ComponentCommandPhase.BUILD)
        contract = self._contract(plan.component_revision)
        sdk_inputs = self._sdk_input_identities(contract)
        if sdk_inputs != plan.materialization.native_sdk_input_identities:
            raise LocalStandardLifecycleError(
                "build plan differs from the exact live native SDK inputs"
            )
        try:
            if sdk_inputs:
                self._require_sdk_build_plan(plan, contract)
            return self._build_locked(plan, provider_artifacts, contract)
        except Exception as exc:
            self._record_failure_diagnostic(
                plan.component_revision.uri, f"{type(exc).__name__}: {exc}"
            )
            raise

    def _require_sdk_build_plan(
        self, plan: StandardComponentBuildPlan, contract: ComponentCommandContract
    ) -> None:
        """Require the live plan and grant before any SDK consumer dispatch."""
        if self._plans_by_revision.get(plan.component_revision.uri) != plan:
            raise LocalStandardLifecycleError(
                "SDK build requires its exact finalized build plan"
            )
        authority = self._sdk_build_authorizations.get(plan.identity.uri)
        if authority is None or self._native_sdk_inputs is None:
            raise LocalStandardLifecycleError(
                "SDK build has no finalized authorization"
            )
        if (
            ComponentCommandRole.NATIVE_SDK_INPUTS
            in contract.command(ComponentCommandPhase.BUILD).roles
        ):
            raise LocalStandardLifecycleError(
                "SDK build cannot use a native runtime command"
            )
        self._require_sdk_build_authorized(plan)

    @contextmanager
    def _sdk_build_scope(
        self, plan: StandardComponentBuildPlan, contract: ComponentCommandContract
    ):
        """Retain data custody and build grants without exposing native imports."""
        from literate_ai.adapters.native_sdk_execution import (
            prepare_native_sdk_execution,
        )

        self._require_sdk_build_plan(plan, contract)
        assert self._native_sdk_inputs is not None
        retained = {}
        with prepare_native_sdk_execution(
            self._native_sdk_inputs,
            plan.component_revision,
            target=contract.artifact_export.target_identity,
            parent=self.object_root,
        ) as inputs:
            before = self._require_sdk_build_authorized(plan)
            yield retained
            after = self._require_sdk_build_authorized(plan)
            request, grant = self._sdk_build_authorizations[plan.identity.uri]
            manifest = json.loads(inputs.manifest.read_bytes())
            if self._record_evidence(manifest) != inputs.manifest_identity:
                raise LocalStandardLifecycleError("SDK build input manifest changed")
            for value in inputs.values:
                for record in (
                    value.dependencies.to_dict(),
                    value.binding.to_dict(),
                    value.binding.build.selection.to_dict(),
                    value.binding.build.resolution.build_plan.to_dict(),
                ):
                    self._record_evidence(record)
            document = {
                "schema": "literate-ai/native-sdk-consumer-build@1",
                "phase": "build",
                "command_binding": {
                    "phase": "build",
                    "command_identity": contract.command(
                        ComponentCommandPhase.BUILD
                    ).identity.to_dict(),
                    "command_contract_identity": contract.identity.to_dict(),
                    "entrypoint_identity": None,
                    "manifest": inputs.manifest_identity.to_dict(),
                    "runtime": inputs.runtime_identity.to_dict(),
                },
                "request": request.to_dict(),
                "authorization": grant.to_dict(),
                "runtime_identity": inputs.runtime_identity.to_dict(),
                "manifest_identity": inputs.manifest_identity.to_dict(),
                "dependencies": [
                    value.dependencies.to_dict() for value in inputs.values
                ],
                "checks": [before, after],
            }
        retained["identity"] = self._record_evidence(document)

    def _require_sdk_build_authorized(
        self, plan: StandardComponentBuildPlan
    ) -> dict[str, object]:
        request, authorization = self._sdk_build_authorizations[plan.identity.uri]
        assert self._native_sdk_inputs is not None
        if (
            request.requested_privileges != ("execute-build-tools",)
            or authorization.privileges != request.requested_privileges
            or authorization.profile is SecurityProfile.BLOCKED
        ):
            raise AuthorizationError("security.native_sdk_build_privilege_mismatch")
        now = self.clock()
        revocations = self._native_sdk_inputs.require_execution_authorized(
            plan.component_revision, request, authorization, now=now
        )
        return {"checked_at": now.isoformat(), "revocations": list(revocations)}

    def _run_npm_lifecycle_command(
        self,
        target: StandardNpmTarget,
        binding: LocalComponentToolBinding,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        object_workspace: Path,
        providers: tuple[ArtifactExport, ...],
    ):
        if binding.command != target.npm_command:
            raise LocalStandardLifecycleError(
                "npm target command differs from its locked tool binding"
            )
        npm_cache = object_workspace / "npm-cache"
        npm_cache.mkdir(parents=True, exist_ok=True)
        user_config = object_workspace / "npm-userconfig"
        global_config = object_workspace / "npm-globalconfig"
        user_config.write_text("", encoding="utf-8")
        global_config.write_text("", encoding="utf-8")
        ambient_environment = {
            name: value
            for name, value in self._environment(providers).items()
            if not name.casefold().startswith("npm_config_")
            and name.casefold()
            not in {
                "node_auth_token",
                "node_options",
                "node_path",
                "npm_token",
            }
        }
        environment = self._binding_environment(ambient_environment, binding)
        environment.update(
            {
                "NPM_CONFIG_CACHE": str(npm_cache.resolve()),
                "NPM_CONFIG_GLOBALCONFIG": str(global_config.resolve()),
                "NPM_CONFIG_USERCONFIG": str(user_config.resolve()),
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            }
        )
        binding.require_unchanged()
        try:
            result = run_bounded_process(
                (*target.npm_command, *arguments),
                cwd=cwd,
                environment=environment,
                timeout_seconds=900,
                stdout_limit_bytes=_MAX_NPM_PROCESS_OUTPUT_BYTES,
                stderr_limit_bytes=_MAX_NPM_PROCESS_OUTPUT_BYTES,
                error_prefix="builder.npm",
            )
        except BuildError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        finally:
            binding.require_unchanged()
        if result.returncode != 0:
            raise LocalStandardLifecycleError(
                f"npm command failed ({result.returncode}): "
                + result.stderr.decode("utf-8", errors="replace").strip()
            )
        return result

    @staticmethod
    def _open_python_wheel(path: Path):
        # Nonblocking open plus descriptor inspection rejects a raced FIFO as
        # well as links/reparse points. Bytes still need the lock hash and archive
        # checks in the private staging context; provisioning grants no trust.
        if path_is_link_or_reparse(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise LocalStandardLifecycleError("Provisioned wheel is not a regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        stream = os.fdopen(descriptor, "rb")
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LocalStandardLifecycleError(
                    "Provisioned wheel changed while opening"
                )
        except Exception:
            stream.close()
            raise
        return stream

    def _python_toolchain(self, target: StandardPythonTarget):
        binding = self.tool_bindings[target.python_toolchain_identity.uri]
        binding.require_unchanged()
        toolchain = discover_python_toolchain(pinned_command=binding.command)
        if toolchain.identity != target.python_toolchain_identity.uri:
            raise LocalStandardLifecycleError("Selected Python interpreter changed")
        return toolchain

    def _admit_python_source(self, revision, source_identity):
        target = self.python_targets[revision.uri]
        root = self.source_trees.resolve(source_identity)
        files = self._source_text_files(root)
        authority = prepare_python_source_authority(
            files, manifest_path=target.manifest, lock_path=target.lockfile
        )
        observe_python_wheel_target(self._python_toolchain(target)).require_lock(
            authority.lock
        )
        source = self.source_trees.evidence(source_identity)
        CycloneDxLifecycleResolver(
            managed_graph=source.managed_graph,
            observer=_RecordedHostDependencyObserver(self.dependency_observation),
            evidence_path=self.object_root / ".python-source-validation.cdx.json",
            python_source_authority=authority,
        ).validate_source(
            {
                "effective_revision_digest": revision.uri,
                "source_bundle_digest": source_identity.uri,
                "files": files,
            }
        )
        return root, authority

    def _build_python_target(self, plan, provider_artifacts, contract):
        target = self.python_targets[plan.component_revision.uri]
        if (
            contract.is_multi_entrypoint
            or len(plan.manifest.export_declarations) != 1
            or tuple(item.kind for item in plan.request.sub_actions)
            != (BuildSubActionKind.RESOLVE_DEPENDENCIES, BuildSubActionKind.COMPILE)
            or plan.request.requested_privileges
            != (BuildPrivilege.EXECUTE_BUILD_TOOLS,)
        ):
            raise LocalStandardLifecycleError(
                "Python build requires authorized offline resolve-before-compile"
            )
        declaration = plan.manifest.export_declarations[0]
        shape = contract.artifact_export
        if (
            declaration.export_id != shape.export_id
            or declaration.component_revision != contract.component_revision
            or declaration.role != shape.role
            or declaration.abi_identity != shape.abi_identity
            or declaration.target_identity != shape.target_identity
            or declaration.media_type != shape.media_type
            or declaration.producer_identity != shape.producer_identity
            or declaration.toolchain_identity != target.python_toolchain_identity
            or tuple(item.identity for item in provider_artifacts)
            != plan.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "Python build differs from locked export/provider authority"
            )
        source, authority = self._admit_python_source(
            plan.component_revision, plan.request.source_tree_identity
        )
        source_identity = local_generated_source_tree_identity(source)
        toolchain = self._python_toolchain(target)
        binding = StandardPythonBuildBinding(
            plan.request.authorization_identity,
            plan.component_revision,
            plan.identity,
            plan.request.source_tree_identity,
            contract.identity,
            target.packaging_flavor_revision_identity,
            target.packaging_profile_identity,
            target.python_toolchain_identity,
        )
        provider_materials = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        cache_identity = canonical_identity(
            {
                "schema": "literate-ai/local-standard-python-cache-key@1",
                "binding": binding.to_dict(),
                "target": target.identity.uri,
                "provider_materials": provider_materials,
            }
        )
        artifact = self.object_root / cache_identity.digest
        started = time.monotonic()

        def attach_observer(root, expected):
            observer = StandardPythonDependencyObserver(
                root, expected, binding, authority, toolchain
            )
            observer()
            self._python_observers[str(root.resolve())] = observer

        def validate_cache():
            # Called only after _reuse_cached_artifact has verified the complete
            # artifact against its external publication checkpoint. The manifest
            # bytes are therefore already sealed, not self-authorizing.
            expected = canonical_identity(
                json.loads(_read_python_manifest(artifact / "python-dependencies.json"))
            )
            attach_observer(artifact, expected)

        if artifact.exists():
            reused = self._reuse_cached_artifact(
                plan,
                provider_artifacts,
                provider_materials,
                cache_identity,
                artifact,
                extra_validate=validate_cache,
            )
            if reused is not None:
                self._python_execution_trees[str(artifact.resolve())] = (
                    self._artifact_checkpoints[cache_identity.uri]
                )
                self.build_cache_hits += 1
                self.build_cache_hit_seconds += time.monotonic() - started
                return reused
        wheelhouse = self.python_wheelhouse
        if (
            wheelhouse is None
            or path_is_link_or_reparse(wheelhouse)
            or not wheelhouse.is_dir()
        ):
            raise LocalStandardLifecycleError(
                "Provisioned Python wheel directory is unavailable"
            )
        staging = Path(
            tempfile.mkdtemp(prefix="standard-python-", dir=self.object_root)
        )
        workspace = staging / "artifact"
        objects = staging / "objects"
        workspace.mkdir()
        objects.mkdir()
        try:
            with ExitStack() as streams:
                sources = {
                    package.name: streams.enter_context(
                        self._open_python_wheel(wheelhouse / package.filename)
                    )
                    for package in authority.lock.packages
                }
                installer = streams.enter_context(
                    self._open_python_wheel(wheelhouse / PIP_INSTALLER.filename)
                )
                with install_python_wheels(
                    toolchain,
                    authority.lock,
                    sources,
                    installer_source=installer,
                    temporary_root=objects,
                ) as installed:
                    evidence = retain_standard_python_dependencies(
                        workspace,
                        binding=binding,
                        source=authority,
                        installed=installed,
                    )
            attach_observer(workspace, evidence.identity)
            result = self._run_locked(
                contract,
                ComponentCommandPhase.BUILD,
                source_root=source,
                object_root=objects,
                artifact_root=workspace,
                export_path=workspace / shape.export_id,
                providers=provider_artifacts,
            )
            _require_generated_export(workspace / shape.export_id)
            if local_generated_source_tree_identity(source) != source_identity:
                raise LocalStandardLifecycleError(
                    "Python build mutated admitted source"
                )
            self._python_observers[str(workspace.resolve())]()
            self._write_artifact_manifest(
                plan,
                workspace,
                provider_materials,
                self._process_observation(
                    result, phase="build", plan_identity=plan.identity
                ),
            )
            self._install_sealed_artifact(
                cache_identity,
                artifact,
                workspace,
                conflict="Python artifact has conflicting sealed bytes",
            )
            attach_observer(artifact, evidence.identity)
            sealed = local_tree_identity(artifact)
            output = self._build_output(plan, artifact)
            if local_tree_identity(artifact) != sealed:
                raise LocalStandardLifecycleError(
                    "Python artifact changed before publication"
                )
            checkpoint = self._publish_artifact_checkpoint(
                cache_identity, plan, artifact
            )
            if checkpoint != sealed:
                raise LocalStandardLifecycleError(
                    "Python artifact changed during publication"
                )
            self._python_execution_trees[str(artifact.resolve())] = checkpoint
            self.build_cache_misses += 1
            self.build_seconds += time.monotonic() - started
            return output
        finally:
            self._python_observers.pop(str(workspace.resolve()), None)
            shutil.rmtree(staging)

    def _admit_npm_source(
        self,
        component_revision: ContentIdentity,
        source_identity: ContentIdentity,
        target: StandardNpmTarget,
    ) -> tuple[Path, StandardNpmSourceAuthority]:
        """Validate exact manifest, lock, imports, and SBOM before authorization."""

        source = self.source_trees.resolve(source_identity)
        npm_configurations = tuple(
            path.relative_to(source).as_posix()
            for path in sorted(source.rglob(".npmrc"))
        )
        if npm_configurations:
            raise LocalStandardLifecycleError(
                "generated npm source contains unauthorized project configuration: "
                + ", ".join(npm_configurations)
            )
        try:
            authority = load_npm_source_authority(source, target)
        except StandardNpmLifecycleError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        source_evidence = self.source_trees.evidence(source_identity)
        source_files = self._source_text_files(source)
        try:
            CycloneDxLifecycleResolver(
                managed_graph=source_evidence.managed_graph,
                observer=_RecordedHostDependencyObserver(self.dependency_observation),
                evidence_path=self.object_root / ".npm-source-validation.cdx.json",
            ).validate_source(
                {
                    "effective_revision_digest": component_revision.uri,
                    "source_bundle_digest": source_identity.uri,
                    "files": source_files,
                }
            )
        except (CycloneDxBomError, DependencyObservationError) as exc:
            raise LocalStandardLifecycleError(
                "npm source dependency authority is inconsistent: " + str(exc)
            ) from exc
        return source, authority

    @staticmethod
    def _require_regular_npm_tree(root: Path) -> None:
        try:
            root_status = os.lstat(root)
        except OSError as exc:
            raise LocalStandardLifecycleError(
                "installed npm dependency tree is unavailable"
            ) from exc
        if path_is_link_or_reparse(root) or not stat.S_ISDIR(root_status.st_mode):
            raise LocalStandardLifecycleError(
                "installed npm dependency tree root is unsafe"
            )
        entry_count = 1
        total_bytes = 0

        def walk_error(exc: OSError) -> None:
            raise LocalStandardLifecycleError(
                "installed npm dependency tree cannot be enumerated"
            ) from exc

        for current, raw_directories, raw_files in os.walk(
            root, topdown=True, onerror=walk_error, followlinks=False
        ):
            current_path = Path(current)
            admitted_directories = []
            for name in sorted(raw_directories):
                path = current_path / name
                entry_count += 1
                if entry_count > _MAX_NPM_INSTALLED_ENTRIES:
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree exceeds its entry limit"
                    )
                try:
                    status = os.lstat(path)
                except OSError as exc:
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree changed during validation"
                    ) from exc
                if path_is_link_or_reparse(path) or not stat.S_ISDIR(status.st_mode):
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree contains a link, reparse "
                        "point, or special file"
                    )
                admitted_directories.append(name)
            raw_directories[:] = admitted_directories

            for name in sorted(raw_files):
                path = current_path / name
                entry_count += 1
                if entry_count > _MAX_NPM_INSTALLED_ENTRIES:
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree exceeds its entry limit"
                    )
                try:
                    before = os.lstat(path)
                except OSError as exc:
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree changed during validation"
                    ) from exc
                if path_is_link_or_reparse(path) or not stat.S_ISREG(before.st_mode):
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree contains a link, reparse "
                        "point, or special file"
                    )
                total_bytes += before.st_size
                if (
                    before.st_size > _MAX_NPM_INSTALLED_FILE_BYTES
                    or total_bytes > _MAX_NPM_INSTALLED_TREE_BYTES
                ):
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree exceeds its byte limit"
                    )
                try:
                    with path.open("rb") as stream:
                        opened = os.fstat(stream.fileno())
                        magic = stream.read(8)
                    after = os.lstat(path)
                except OSError as exc:
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree changed during validation"
                    ) from exc
                stable_fields = (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                )
                if not stat.S_ISREG(opened.st_mode) or any(
                    getattr(before, field) != getattr(opened, field)
                    or getattr(before, field) != getattr(after, field)
                    for field in stable_fields
                ):
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree changed during validation"
                    )
                if name_or_magic_is_native_or_wasm(path.name, magic):
                    raise LocalStandardLifecycleError(
                        "installed npm dependency tree contains a native or "
                        "WebAssembly payload"
                    )

    def _build_npm_target(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
        target: StandardNpmTarget,
    ) -> StandardBuildOutput:
        expected_build_argv = (
            "{tool}",
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--no-bin-links",
            "{source_root}",
            "{object_root}",
            "{export_path}",
        )
        if contract.command(ComponentCommandPhase.BUILD).argv != expected_build_argv:
            raise LocalStandardLifecycleError(
                "npm BUILD contract does not authorize the fixed install lifecycle"
            )
        if contract.is_multi_entrypoint:
            raise LocalStandardLifecycleError(
                "package-npm currently supports one Component entrypoint"
            )
        if (
            tuple(item.kind for item in plan.request.sub_actions)
            != (
                BuildSubActionKind.RESOLVE_DEPENDENCIES,
                BuildSubActionKind.COMPILE,
            )
            or BuildPrivilege.NETWORK_ACCESS not in plan.request.requested_privileges
        ):
            raise LocalStandardLifecycleError(
                "npm build plan does not authorize resolve-before-compile "
                "network access"
            )
        declaration = plan.manifest.export_declarations[0]
        shape = contract.artifact_export
        if (
            declaration.export_id,
            declaration.component_revision,
            declaration.role,
            declaration.abi_identity,
            declaration.target_identity,
            declaration.media_type,
            declaration.producer_identity,
            declaration.toolchain_identity,
        ) != (
            shape.export_id,
            contract.component_revision,
            shape.role,
            shape.abi_identity,
            shape.target_identity,
            shape.media_type,
            shape.producer_identity,
            contract.language_compiler_identity,
        ):
            raise LocalStandardLifecycleError(
                "build plan does not realize the exact locked npm export shape"
            )
        if tuple(item.identity for item in provider_artifacts) != (
            plan.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "npm builder received different provider artifacts"
            )
        source, authority = self._admit_npm_source(
            plan.component_revision,
            plan.request.source_tree_identity,
            target,
        )
        source_identity = local_generated_source_tree_identity(source)
        source_manifest = source.joinpath(*PurePosixPath(target.manifest).parts)
        source_lock = source.joinpath(*PurePosixPath(target.lockfile).parts)
        manifest_bytes = source_manifest.read_bytes()
        lockfile_bytes = source_lock.read_bytes()
        provider_materials = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        cache_identity = canonical_identity(
            {
                "schema": "literate-ai/local-standard-npm-cache-key@1",
                "command_contract_identity": contract.identity.uri,
                "npm_target_identity": target.identity.uri,
                "source_authority_identity": authority.identity.uri,
                "build_plan_identity": plan.identity.uri,
                "source_tree_identity": plan.request.source_tree_identity.uri,
                "provider_materials": provider_materials,
            }
        )
        artifact = self.object_root / cache_identity.digest
        started = time.monotonic()
        if artifact.exists():
            reused = self._reuse_cached_artifact(
                plan,
                provider_artifacts,
                provider_materials,
                cache_identity,
                artifact,
                extra_validate=lambda: self._validated_npm_evidence(plan, artifact),
            )
            if reused is not None:
                self.build_cache_hits += 1
                self.build_cache_hit_seconds += time.monotonic() - started
                return reused

        staging = Path(tempfile.mkdtemp(prefix="standard-npm-", dir=self.object_root))
        projection = staging / "projection"
        object_workspace = staging / "objects"
        artifact_workspace = staging / "artifact"
        try:
            shutil.copytree(source, projection)
            object_workspace.mkdir()
            artifact_workspace.mkdir()
            projected_manifest = projection.joinpath(
                *PurePosixPath(target.manifest).parts
            )
            projected_lock = projection.joinpath(*PurePosixPath(target.lockfile).parts)
            package_root = projected_manifest.parent
            npm_binding = self.tool_bindings[target.build_system_toolchain_identity.uri]
            node_binding = self.tool_bindings[target.node_toolchain_identity.uri]
            install = self._run_npm_lifecycle_command(
                target,
                npm_binding,
                (
                    "ci",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--no-bin-links",
                ),
                cwd=package_root,
                object_workspace=object_workspace,
                providers=provider_artifacts,
            )
            if (
                projected_manifest.read_bytes() != manifest_bytes
                or projected_lock.read_bytes() != lockfile_bytes
                or source_manifest.read_bytes() != manifest_bytes
                or source_lock.read_bytes() != lockfile_bytes
                or local_generated_source_tree_identity(source) != source_identity
            ):
                raise LocalStandardLifecycleError(
                    "npm ci changed admitted manifest or lock authority"
                )
            inventory = self._run_npm_lifecycle_command(
                target,
                npm_binding,
                ("ls", "--all", "--json"),
                cwd=package_root,
                object_workspace=object_workspace,
                providers=provider_artifacts,
            )
            try:
                inventory_bytes = validate_npm_inventory(inventory.stdout, authority)
            except StandardNpmLifecycleError as exc:
                raise LocalStandardLifecycleError(str(exc)) from exc
            if (
                projected_manifest.read_bytes() != manifest_bytes
                or projected_lock.read_bytes() != lockfile_bytes
                or local_generated_source_tree_identity(source) != source_identity
            ):
                raise LocalStandardLifecycleError(
                    "npm inventory changed admitted manifest or lock authority"
                )
            installed = package_root / "node_modules"
            if installed.is_symlink() or not installed.is_dir():
                raise LocalStandardLifecycleError(
                    "npm ci did not produce a local node_modules directory"
                )
            self._require_regular_npm_tree(installed)
            installed_tree_identity = local_tree_identity(installed)
            node_binding.require_unchanged()
            javascript_paths = tuple(
                path.relative_to(source)
                for path in sorted(source.rglob("*"))
                if path.is_file()
                and path.suffix.casefold() in JAVASCRIPT_SOURCE_SUFFIXES
            )
            if not javascript_paths:
                raise LocalStandardLifecycleError(
                    "npm Component contains no JavaScript source files"
                )
            checks = []
            for relative in javascript_paths:
                checks.append(
                    self._run(
                        (
                            *node_binding.command,
                            "--check",
                            str(projection / relative),
                        ),
                        cwd=artifact_workspace,
                        providers=provider_artifacts,
                        binding=node_binding,
                    )
                )
            node_binding.require_unchanged()
            export_path = artifact_workspace / contract.artifact_export.export_id
            shutil.copytree(source, export_path)
            artifact_package_root = export_path.joinpath(
                *PurePosixPath(target.manifest).parent.parts
            )
            shutil.copytree(installed, artifact_package_root / "node_modules")
            self._require_regular_npm_tree(artifact_package_root / "node_modules")
            if local_generated_source_tree_identity(source) != source_identity:
                raise LocalStandardLifecycleError(
                    "npm build mutated the admitted generated source tree"
                )
            evidence = self._write_npm_dependency_evidence(
                plan,
                artifact_workspace,
                target=target,
                authority=authority,
                manifest_bytes=manifest_bytes,
                lockfile_bytes=lockfile_bytes,
                inventory_bytes=inventory_bytes,
                install_process_identity=self._npm_process_identity(
                    install,
                    phase="npm-ci",
                    arguments=(
                        "ci",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                        "--no-bin-links",
                    ),
                    plan=plan,
                ),
                inventory_process_identity=self._npm_process_identity(
                    inventory,
                    phase="npm-ls",
                    arguments=("ls", "--all", "--json"),
                    plan=plan,
                ),
                installed_tree_identity=installed_tree_identity,
            )
            process_observation = self._record_evidence(
                {
                    "schema": "literate-ai/local-npm-build-observation@1",
                    "npm_target_identity": target.identity.uri,
                    "dependency_evidence_manifest": evidence[
                        "evidence_manifest_digest"
                    ],
                    "syntax_checks": [
                        self._process_observation(
                            check, phase="node-check", plan_identity=plan.identity
                        ).uri
                        for check in checks
                    ],
                }
            )
            self._npm_resolution_builds[plan.identity.uri] = evidence
            try:
                self._write_artifact_manifest(
                    plan, artifact_workspace, provider_materials, process_observation
                )
            finally:
                self._npm_resolution_builds.pop(plan.identity.uri, None)
            sealed_tree_identity: ContentIdentity
            self._install_sealed_artifact(
                cache_identity,
                artifact,
                artifact_workspace,
                conflict="npm-addressed local artifact has conflicting bytes",
            )
            sealed_tree_identity = local_tree_identity(artifact)
            shutil.rmtree(staging)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        output = self._build_output(plan, artifact)
        if local_tree_identity(artifact) != sealed_tree_identity:
            raise LocalStandardLifecycleError(
                "Standard npm artifact changed before checkpoint publication"
            )
        self._publish_artifact_checkpoint(cache_identity, plan, artifact)
        self.build_cache_misses += 1
        self.build_seconds += time.monotonic() - started
        return output

    def _build_locked(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
    ) -> StandardBuildOutput:
        if plan.component_revision.uri in self.python_targets:
            return self._build_python_target(plan, provider_artifacts, contract)
        npm_target = self.npm_targets.get(plan.component_revision.uri)
        if npm_target is not None:
            return self._build_npm_target(
                plan, provider_artifacts, contract, npm_target
            )
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        try:
            npm_manifests = dependency_bearing_package_manifests(source)
        except StandardNpmLifecycleError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        if npm_manifests:
            raise LocalStandardLifecycleError(
                "generated npm dependencies require an explicit package-npm target: "
                + ", ".join(npm_manifests)
            )
        try:
            validate_standard_build_inputs(plan, provider_artifacts, contract)
        except StandardBuildInputError as exc:
            raise LocalStandardLifecycleError(str(exc)) from exc
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        source_identity = local_generated_source_tree_identity(source)
        provider_materials = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        cache_key: dict[str, object] = {
            "schema": "literate-ai/local-standard-build-cache-key@2",
            "command_contract_identity": contract.identity.uri,
            "source_tree_identity": plan.request.source_tree_identity.uri,
            "provider_materials": provider_materials,
        }
        if (
            self.shared_cache is not None
            and self.shared_cache.compiler_tool is not None
        ):
            cache_key["compiler_cache_identity"] = self.shared_cache.identity.uri
        if plan.materialization.native_sdk_input_identities:
            cache_key["native_sdk_input_identities"] = [
                value.uri for value in plan.materialization.native_sdk_input_identities
            ]
        if self._native_sdk_inputs is not None:
            sdk_dependencies = self._native_sdk_inputs.for_scope(
                plan.component_revision
            )
            if sdk_dependencies:
                cache_key["native_sdk_dependency_identities"] = [
                    value.identity.uri for value in sdk_dependencies
                ]
        cache_identity = canonical_identity(cache_key)

        def input_scope():
            return (
                self._sdk_build_scope(plan, contract)
                if plan.materialization.native_sdk_input_identities
                else nullcontext()
            )

        artifact = self.object_root / cache_identity.digest
        started = time.monotonic()
        if artifact.exists():
            with input_scope():
                reused = self._reuse_cached_artifact(
                    plan,
                    provider_artifacts,
                    provider_materials,
                    cache_identity,
                    artifact,
                )
            if reused is not None:
                self.build_cache_hits += 1
                self.build_cache_hit_seconds += time.monotonic() - started
                return reused
        staging = Path(tempfile.mkdtemp(prefix="standard-build-", dir=self.object_root))
        object_workspace = staging / "objects"
        artifact_workspace = staging / "artifact"
        object_workspace.mkdir()
        artifact_workspace.mkdir()
        export_path = artifact_workspace / contract.artifact_export.export_id
        try:
            with input_scope() as sdk_build:
                build_result = self._run_locked(
                    contract,
                    ComponentCommandPhase.BUILD,
                    source_root=source,
                    object_root=object_workspace,
                    artifact_root=artifact_workspace,
                    export_path=export_path,
                    providers=provider_artifacts,
                )
                if plan.materialization.native_sdk_input_identities:
                    self._require_sdk_build_authorized(plan)
                for declaration in plan.manifest.export_declarations:
                    _require_generated_export(
                        artifact_workspace / declaration.export_id
                    )
                if local_generated_source_tree_identity(source) != source_identity:
                    raise LocalStandardLifecycleError(
                        "build command mutated the admitted generated source tree"
                    )
            if sdk_build is not None:
                build_result.native_sdk_build_identity = sdk_build["identity"]
            self._write_artifact_manifest(
                plan,
                artifact_workspace,
                provider_materials,
                self._process_observation(
                    build_result, phase="build", plan_identity=plan.identity
                ),
            )
            if plan.materialization.native_sdk_input_identities:
                self._require_sdk_build_authorized(plan)
            self._install_sealed_artifact(
                cache_identity,
                artifact,
                artifact_workspace,
                conflict="command-addressed local artifact has conflicting bytes",
            )
            shutil.rmtree(staging)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        sealed = local_tree_identity(artifact)
        output = self._build_output(plan, artifact)
        if local_tree_identity(artifact) != sealed:
            raise LocalStandardLifecycleError(
                "command-addressed local artifact changed before checkpoint publication"
            )
        self._publish_artifact_checkpoint(cache_identity, plan, artifact)
        self.build_cache_misses += 1
        self.build_seconds += time.monotonic() - started
        return output

    def _artifact_checkpoint_path(self, cache_identity: ContentIdentity) -> Path:
        return (
            self.object_root
            / _ARTIFACT_CHECKPOINT_ROOT
            / _ARTIFACT_CHECKPOINT_DIRNAME
            / f"{cache_identity.digest}.json"
        )

    def _publish_artifact_checkpoint(
        self,
        cache_identity: ContentIdentity,
        plan: StandardComponentBuildPlan,
        artifact: Path,
    ) -> ContentIdentity:
        tree = local_tree_identity(artifact)
        path = self._artifact_checkpoint_path(cache_identity)
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if path_is_link_or_reparse(parent) or not parent.is_dir():
            raise LocalStandardLifecycleError("artifact checkpoint directory is unsafe")
        if path.exists() and (path_is_link_or_reparse(path) or not path.is_file()):
            raise LocalStandardLifecycleError("artifact checkpoint path is unsafe")
        payload = canonical_json_bytes(
            {
                "schema": _ARTIFACT_CHECKPOINT_SCHEMA,
                "authorization_identity": plan.request.authorization_identity.uri,
                "artifact_tree_identity": tree.uri,
                "build_plan_identity": plan.identity.uri,
                "cache_identity": cache_identity.uri,
            }
        )
        temporary = parent / f".{cache_identity.digest}.json.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        self._artifact_checkpoints[cache_identity.uri] = tree
        return tree

    def _load_artifact_checkpoint(
        self,
        cache_identity: ContentIdentity,
        plan: StandardComponentBuildPlan,
    ) -> ContentIdentity | None:
        path = self._artifact_checkpoint_path(cache_identity)
        if not path.exists():
            return None
        if path_is_link_or_reparse(path) or not path.is_file():
            raise LocalStandardLifecycleError("artifact checkpoint is unsafe")
        try:
            payload = json.loads(path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStandardLifecycleError("artifact checkpoint is invalid") from exc
        required = {
            "schema",
            "authorization_identity",
            "artifact_tree_identity",
            "build_plan_identity",
            "cache_identity",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise LocalStandardLifecycleError("artifact checkpoint is invalid")
        if (
            payload.get("schema") != _ARTIFACT_CHECKPOINT_SCHEMA
            or payload.get("cache_identity") != cache_identity.uri
        ):
            raise LocalStandardLifecycleError("artifact checkpoint is invalid")
        try:
            tree = ContentIdentity.parse_uri(str(payload["artifact_tree_identity"]))
        except ValueError as exc:
            raise LocalStandardLifecycleError("artifact checkpoint is invalid") from exc
        self._artifact_checkpoints[cache_identity.uri] = tree
        return tree

    def _reuse_cached_artifact(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        provider_materials: tuple[dict[str, object], ...],
        cache_identity: ContentIdentity,
        artifact: Path,
        *,
        extra_validate: Callable[[], None] | None = None,
    ) -> StandardBuildOutput | None:
        if path_is_link_or_reparse(artifact) or not artifact.is_dir():
            raise LocalStandardLifecycleError("cached local artifact is unsafe")
        checkpoint = self._load_artifact_checkpoint(cache_identity, plan)
        if checkpoint is None:
            return None
        if local_tree_identity(artifact) != checkpoint:
            raise LocalStandardLifecycleError(
                "cached artifact changed after publication"
            )
        if extra_validate is not None:
            extra_validate()
        output = self._cached_build_output(
            plan, provider_artifacts, provider_materials, artifact
        )
        if local_tree_identity(artifact) != checkpoint:
            raise LocalStandardLifecycleError(
                "cached artifact changed during validation"
            )
        rebound = self._publish_artifact_checkpoint(cache_identity, plan, artifact)
        if rebound != checkpoint:
            raise LocalStandardLifecycleError(
                "cached artifact changed before checkpoint rebinding"
            )
        return output

    def _install_sealed_artifact(
        self,
        cache_identity: ContentIdentity,
        artifact: Path,
        artifact_workspace: Path,
        *,
        conflict: str,
    ) -> None:
        if artifact.exists():
            if path_is_link_or_reparse(artifact) or not artifact.is_dir():
                raise LocalStandardLifecycleError("cached local artifact is unsafe")
            if local_tree_identity(artifact) == local_tree_identity(artifact_workspace):
                shutil.rmtree(artifact_workspace)
                return
            checkpoint = self._artifact_checkpoint_path(cache_identity)
            if checkpoint.exists():
                if path_is_link_or_reparse(checkpoint) or not checkpoint.is_file():
                    raise LocalStandardLifecycleError("artifact checkpoint is unsafe")
                raise LocalStandardLifecycleError(conflict)
            shutil.rmtree(artifact)
            os.replace(artifact_workspace, artifact)
            return
        os.replace(artifact_workspace, artifact)

    @contextmanager
    def _provider_artifact_custody(self, providers, entries, admission_guard):
        """Scope already verified provider data to a current consumer operation."""
        from literate_ai.adapters.action_build_result import (
            MAX_BUILD_ARCHIVE_BYTES,
            MAX_BUILD_ARCHIVE_FILES,
            _verify_files,
        )
        from literate_ai.application.standard_provider_receipts import (
            select_build_provider_receipts,
        )

        receipts = tuple(item[0] for item in entries)
        if select_build_provider_receipts(providers, receipts) != receipts:
            raise LocalStandardLifecycleError("provider receipt closure differs")
        exports = {
            export.identity: export
            for receipt in receipts
            for export in receipt.build.exports
        }
        paths, blob_paths, blob_bytes, evidence, contracts = {}, {}, {}, {}, {}
        for receipt, reader, custody in entries:
            admission_guard()
            custody.require_unchanged()
            root = custody.root.resolve(strict=True)
            if root == self.object_root.resolve(strict=True) or not root.is_relative_to(
                self.object_root.resolve(strict=True)
            ):
                raise LocalStandardLifecycleError(
                    "provider artifact escapes object custody"
                )
            contract = self._contract(receipt.component_revision)
            contracts[receipt.component_revision] = contract
            plan = StandardComponentBuildPlan.from_dict(
                reader.read_json(receipt.build.build_plan_identity)
            )
            validate_standard_build_inputs(
                plan,
                tuple(exports[item] for item in plan.provider_artifact_identities),
                contract,
            )
            if any(
                action.action_id
                not in {
                    "build-" + contract.identity.digest[:24],
                    "resolve-" + contract.identity.digest[:24],
                }
                for action in plan.manifest.actions
            ):
                raise LocalStandardLifecycleError("provider command contract differs")
            _verify_files(custody.files, receipt.build, reader)
            by_path = {item.path: item for item in custody.files}
            for export in receipt.build.exports:
                if export.identity.uri in self._exports_by_identity:
                    raise LocalStandardLifecycleError(
                        "provider artifact is already registered"
                    )
                paths[export.identity.uri] = root
                if export.export_id in by_path:
                    blob_paths[export.blob.identity] = root / export.export_id
                else:
                    from literate_ai.adapters.directory_artifacts import (
                        DirectoryExportFile,
                        encode_directory_export,
                    )

                    prefix = export.export_id + "/"
                    blob_bytes[export.blob.identity] = encode_directory_export(
                        tuple(
                            DirectoryExportFile(
                                item.path[len(prefix) :], item.content, item.mode
                            )
                            for item in custody.files
                            if item.path.startswith(prefix)
                        ),
                        max_bytes=MAX_BUILD_ARCHIVE_BYTES,
                        max_entries=MAX_BUILD_ARCHIVE_FILES,
                    )
            evidence[receipt.build.identity.uri] = receipt.build

        def require_custody():
            admission_guard()
            for revision, contract in contracts.items():
                if self._contract(revision) != contract:
                    raise LocalStandardLifecycleError(
                        "provider contract changed during BUILD"
                    )
            for _receipt, _reader, custody in entries:
                custody.require_unchanged()
            admission_guard()

        require_custody()
        missing = object()
        changes = (
            (self._artifact_paths, paths),
            (self._artifact_blob_paths, blob_paths),
            (self._artifact_blob_bytes, blob_bytes),
            (
                self._exports_by_identity,
                {key.uri: value for key, value in exports.items()},
            ),
            (self._build_evidence, evidence),
        )
        previous = [
            (mapping, {key: mapping.get(key, missing) for key in values})
            for mapping, values in changes
        ]
        try:
            for mapping, values in changes:
                mapping.update(values)
            yield
            require_custody()
        finally:
            # A consumer can produce the same immutable bytes as a provider.
            # Its newly registered blob custody must survive provider removal.
            imported = {item.uri for item in exports}
            retained_blobs = {
                item.blob.identity
                for key, item in self._exports_by_identity.items()
                if key not in imported
            }
            for mapping, values in previous:
                for key, value in values.items():
                    if key in retained_blobs and (
                        (
                            mapping is self._artifact_blob_paths
                            and mapping.get(key) != blob_paths[key]
                        )
                        or (mapping is self._artifact_blob_bytes and value is missing)
                    ):
                        continue
                    if value is missing:
                        mapping.pop(key, None)
                    else:
                        mapping[key] = value

    def admit_transferred_build(
        self,
        *,
        plan: StandardComponentBuildPlan,
        inputs: StandardPlanFinalizationInputs,
        evidence: StandardBuildEvidence,
        evidence_reader,
        artifact: Path,
        admission_guard: Callable[[], None] | None = None,
    ) -> StandardBuildOutput:
        """Reopen transferred bytes before publishing local artifact custody."""
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            verify_qualification_build,
        )

        def require_authority():
            if admission_guard is not None:
                admission_guard()
            validate_standard_build_authority(plan, inputs, now=self.clock())
            if (
                self._plans_by_revision.get(plan.component_revision.uri) != plan
                or self.plan_finalization_inputs(inputs.intent, inputs.authorization)
                != inputs
            ):
                raise LocalStandardLifecycleError(
                    "transferred build differs from retained local authority"
                )

        require_authority()
        if not isinstance(evidence, StandardBuildEvidence):
            raise TypeError("transferred build requires exact Standard evidence")
        if not isinstance(evidence_reader, QualificationEvidenceReader):
            raise TypeError("transferred build requires bounded evidence custody")
        if evidence_reader.read_bytes(evidence.identity) != canonical_json_bytes(
            evidence.to_dict()
        ):
            raise LocalStandardLifecycleError(
                "transferred build evidence record differs"
            )
        verify_qualification_build(evidence_reader, plan=plan, build=evidence)
        if not isinstance(artifact, Path) or not artifact.is_absolute():
            raise LocalStandardLifecycleError(
                "transferred artifact root must be absolute"
            )
        require_safe_directory(artifact)
        root = artifact.resolve(strict=True)
        if not root.is_relative_to(self.object_root.resolve(strict=True)):
            raise LocalStandardLifecycleError(
                "transferred artifact is outside object custody"
            )
        tree = local_tree_identity(root)
        materials = self._provider_materials(
            inputs.providers, consumer_revision=plan.component_revision
        )

        def before_register():
            require_authority()
            if (
                self.source_trees.evidence(plan.request.source_tree_identity).identity
                != evidence.source_custody_identity
                or self._provider_materials(
                    inputs.providers, consumer_revision=plan.component_revision
                )
                != materials
            ):
                raise LocalStandardLifecycleError(
                    "transferred build inputs changed during admission"
                )
            if local_tree_identity(root) != tree:
                raise LocalStandardLifecycleError(
                    "transferred artifact changed during admission"
                )
            require_authority()

        return self._cached_build_output(
            plan,
            inputs.providers,
            materials,
            root,
            expected_evidence=evidence,
            before_register=before_register,
        )

    def _cached_build_output(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        provider_materials: tuple[dict[str, object], ...],
        artifact: Path,
        *,
        expected_evidence: StandardBuildEvidence | None = None,
        before_register: Callable[[], None] | None = None,
    ) -> StandardBuildOutput:
        if artifact.is_symlink() or not artifact.is_dir():
            raise LocalStandardLifecycleError("cached local artifact is unsafe")
        manifest_path = artifact / _ARTIFACT_MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise LocalStandardLifecycleError("cached local artifact has no manifest")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStandardLifecycleError(
                "cached local artifact manifest is invalid"
            ) from exc
        if tuple(item.identity for item in provider_artifacts) != (
            plan.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "cached build received different provider artifacts"
            )
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"tree", "provider_materials", "build_observation"}
            or manifest.get("provider_materials") != list(provider_materials)
            or manifest.get("tree")
            != _local_tree_identity(
                artifact, excluded=frozenset({_ARTIFACT_MANIFEST})
            ).uri
        ):
            raise LocalStandardLifecycleError(
                "cached local artifact differs from its exact manifest"
            )
        observation = manifest.get("build_observation")
        if not isinstance(observation, str):
            raise LocalStandardLifecycleError(
                "cached local artifact has no build observation"
            )
        ContentIdentity.parse_uri(observation)
        return self._build_output(
            plan,
            artifact,
            expected_evidence=expected_evidence,
            before_register=before_register,
        )

    def _build_output(
        self,
        plan: StandardComponentBuildPlan,
        artifact: Path,
        *,
        expected_evidence: StandardBuildEvidence | None = None,
        before_register: Callable[[], None] | None = None,
    ) -> StandardBuildOutput:
        artifact_root = artifact.resolve(strict=True)
        exports: list[ArtifactExport] = []
        paths: dict[str, Path] = {}
        blob_paths: dict[str, Path] = {}
        blob_bytes: dict[str, bytes] = {}
        for declaration in plan.manifest.export_declarations:
            export_path = artifact / declaration.export_id
            if export_path.is_symlink() or not export_path.exists():
                raise LocalStandardLifecycleError(
                    "built artifact does not contain its exact declared export file"
                )
            resolved_export = export_path.resolve(strict=True)
            if resolved_export.parent != artifact_root:
                raise LocalStandardLifecycleError(
                    "built artifact does not contain its exact declared export file"
                )
            if export_path.is_file():
                export_bytes = export_path.read_bytes()
            elif export_path.is_dir():
                export_bytes = _directory_export_bytes(export_path)
            else:
                raise LocalStandardLifecycleError(
                    "built artifact export is not a regular file or directory"
                )
            export = ArtifactExport(
                **{
                    name: getattr(declaration, name)
                    for name in (
                        "export_id",
                        "component_revision",
                        "role",
                        "abi_identity",
                        "target_identity",
                        "media_type",
                        "producer_identity",
                        "source_tree_identity",
                        "toolchain_identity",
                        "authorization_identity",
                        "dependency_artifact_identities",
                    )
                },
                blob=BlobRef(
                    hashlib.sha256(export_bytes).hexdigest(),
                    len(export_bytes),
                    media_type=declaration.media_type,
                ),
            )
            paths[export.identity.uri] = artifact
            if export_path.is_file():
                blob_paths[export.blob.identity] = export_path
            else:
                blob_bytes[export.blob.identity] = export_bytes
            exports.append(export)
        export_tuple = tuple(exports)
        planned_export = next(
            item
            for item in export_tuple
            if item.export_id
            == self._contract(plan.component_revision).artifact_export.export_id
        )
        source_custody = self.source_trees.evidence(plan.request.source_tree_identity)
        resolved_path = artifact.joinpath(*Path(_RESOLVED_SBOM).parts)
        if resolved_path.is_symlink() or not resolved_path.is_file():
            raise LocalStandardLifecycleError(
                "built artifact has no resolved CycloneDX SBOM"
            )
        resolved_content = resolved_path.read_bytes()
        resolved_sbom = validate_cyclonedx_bom(
            resolved_content,
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=source_custody.managed_graph,
            source_content=source_custody.source_bom_content,
            source_managed_graph=source_custody.managed_graph,
        )
        manifest = json.loads((artifact / _ARTIFACT_MANIFEST).read_bytes())
        build_observation = ContentIdentity.parse_uri(manifest["build_observation"])
        artifact_tree = self._record_evidence(_local_tree_document(artifact))
        artifact_custody = self._record_evidence(
            {
                "schema": "literate-ai/local-artifact-custody@1",
                "build_plan_identity": plan.identity.uri,
                "artifact_tree_identity": artifact_tree.uri,
                "export_identities": [item.identity.uri for item in export_tuple],
            }
        )
        evidence = StandardBuildEvidence(
            component_revision=plan.component_revision,
            build_plan_identity=plan.identity,
            source_tree_identity=plan.request.source_tree_identity,
            source_custody_identity=source_custody.identity,
            source_sbom=source_custody.source_bom,
            resolved_sbom=resolved_sbom,
            exports=export_tuple,
            resolved_sbom_export_identities=tuple(
                item.identity for item in export_tuple
            ),
            build_observation_identity=build_observation,
            artifact_custody_identity=artifact_custody,
        )
        if expected_evidence is not None and evidence != expected_evidence:
            raise LocalStandardLifecycleError(
                "transferred build differs from exact expected evidence"
            )
        if self._evidence_recorder is not None:
            # Preserve validated raw payloads under their existing byte identities.
            # JSON reserialization would change SBOM or generated-suite identity.
            for payload, expected in (
                (
                    source_custody.source_bom_content,
                    source_custody.source_bom.bom_identity,
                ),
                (resolved_content, resolved_sbom.bom_identity),
                (
                    source_custody.generated_test_suite_content,
                    ContentIdentity.parse_uri(
                        source_custody.generated_test_suite.content_identity
                    ),
                ),
            ):
                if self._evidence_recorder.remember_bytes(payload) != expected:
                    raise LocalStandardLifecycleError(
                        "retained source evidence differs from its custody identity"
                    )
            for document in (
                source_custody.identity_document(),
                source_custody.candidate.to_dict(),
                source_custody.managed_graph.to_dict(),
                plan.to_dict(),
                evidence.to_dict(),
            ):
                self._record_evidence(document)
        output = StandardBuildOutput(export_tuple, evidence.identity, evidence)
        if before_register is not None:
            before_register()
        self._artifact_paths.update(paths)
        self._artifact_blob_paths.update(blob_paths)
        self._artifact_blob_bytes.update(blob_bytes)
        self._exports_by_identity.update({item.identity.uri: item for item in exports})
        self._planned_exports[plan.component_revision.uri] = planned_export
        self._build_observations[str(artifact_root)] = build_observation
        self._build_evidence[evidence.identity.uri] = evidence
        return output

    def read_artifact_blob(self, reference: BlobRef) -> bytes:
        """Read one immutable compiled export under its verified BlobRef custody."""

        if not isinstance(reference, BlobRef):
            raise TypeError("reference must be a BlobRef")
        content = self._artifact_blob_bytes.get(reference.identity)
        if content is not None:
            if (
                len(content) != reference.size
                or hashlib.sha256(content).hexdigest() != reference.digest
            ):
                raise LocalStandardLifecycleError(
                    "artifact blob changed after admission"
                )
            return content
        try:
            path = self._artifact_blob_paths[reference.identity]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "artifact blob is not in local custody"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise LocalStandardLifecycleError("artifact blob custody path is unsafe")
        content = path.read_bytes()
        if (
            len(content) != reference.size
            or hashlib.sha256(content).hexdigest() != reference.digest
        ):
            raise LocalStandardLifecycleError("artifact blob changed after admission")
        return content

    def resolved_sbom_content(self, evidence: StandardBuildEvidence) -> bytes:
        """Return revalidated resolved CycloneDX bytes for exact build evidence."""

        if not isinstance(evidence, StandardBuildEvidence):
            raise TypeError("evidence must be StandardBuildEvidence")
        if self._build_evidence.get(evidence.identity.uri) != evidence:
            raise LocalStandardLifecycleError(
                "resolved SBOM has no exact local build-evidence custody"
            )
        try:
            roots = {
                self._artifact_paths[item.uri].resolve(strict=True)
                for item in evidence.export_identities
            }
        except (KeyError, OSError) as exc:
            raise LocalStandardLifecycleError(
                "resolved SBOM artifact custody is unavailable"
            ) from exc
        if len(roots) != 1:
            raise LocalStandardLifecycleError(
                "resolved SBOM exports do not share one artifact custody root"
            )
        root = roots.pop()
        path = root.joinpath(*Path(_RESOLVED_SBOM).parts)
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
            raise LocalStandardLifecycleError("resolved SBOM is missing or redirected")
        content = path.read_bytes()
        source_custody = self.source_trees.evidence(evidence.source_tree_identity)
        binding = validate_cyclonedx_bom(
            content,
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=source_custody.managed_graph,
            source_content=source_custody.source_bom_content,
            source_managed_graph=source_custody.managed_graph,
        )
        if binding != evidence.resolved_sbom:
            raise LocalStandardLifecycleError(
                "resolved SBOM bytes differ from exact build evidence"
            )
        return content

    def source_sbom_content(self, evidence: StandardBuildEvidence) -> bytes:
        """Return revalidated source CycloneDX bytes for exact build evidence."""

        if not isinstance(evidence, StandardBuildEvidence):
            raise TypeError("evidence must be StandardBuildEvidence")
        if self._build_evidence.get(evidence.identity.uri) != evidence:
            raise LocalStandardLifecycleError(
                "source SBOM has no exact local build-evidence custody"
            )
        source_custody = self.source_trees.evidence(evidence.source_tree_identity)
        if source_custody.source_bom != evidence.source_sbom:
            raise LocalStandardLifecycleError(
                "source SBOM bytes differ from exact build evidence"
            )
        return source_custody.source_bom_content

    def build_cache_report(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/local-standard-build-cache-report@1",
            "root": str(self.object_root),
            "hits": self.build_cache_hits,
            "misses": self.build_cache_misses,
            "hit_seconds": round(self.build_cache_hit_seconds, 6),
            "build_seconds": round(self.build_seconds, 6),
        }

    def artifact_path(self, export: ArtifactExport) -> Path:
        """Resolve one exact built export without exposing mutable adapter maps."""

        if not isinstance(export, ArtifactExport):
            raise TypeError("export must be an ArtifactExport")
        try:
            root = self._artifact_paths[export.identity.uri].resolve(strict=True)
        except (KeyError, OSError) as exc:
            raise LocalStandardLifecycleError(
                "built export artifact is unavailable"
            ) from exc
        target = (root / export.export_id).resolve(strict=True)
        if root != target and root not in target.parents:
            raise LocalStandardLifecycleError("built export path escaped custody")
        return target

    def execution_command(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        *,
        entrypoint_identity: ContentIdentity | None = None,
    ) -> LocalResolvedExecutionCommand:
        """Resolve the accepted export's ordinary product invocation prefix."""
        self._require_command_phase(ComponentCommandPhase.EXECUTE)

        if not isinstance(plan, StandardComponentBuildPlan):
            raise TypeError("plan must be a StandardComponentBuildPlan")
        if not exports:
            raise LocalStandardLifecycleError(
                "execution command requires at least one built export"
            )
        contract = self._contract(plan.component_revision)
        entrypoint_contract = contract.entrypoint_command_contract(entrypoint_identity)
        expected_export_ids = {
            item.artifact_export.export_id
            for item in contract.entrypoint_command_contracts()
        }
        exports_by_id = {item.export_id: item for item in exports}
        if set(exports_by_id) != expected_export_ids:
            raise LocalStandardLifecycleError(
                "execution command requires every and only declared export"
            )
        providers = self._intent_artifacts_for_plan(plan)
        try:
            selected_export = exports_by_id[
                entrypoint_contract.artifact_export.export_id
            ]
            artifact_root = self._artifact_paths[selected_export.identity.uri].resolve(
                strict=True
            )
        except (KeyError, OSError) as exc:
            raise LocalStandardLifecycleError(
                "execution command artifact custody is unavailable"
            ) from exc
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        argv = self._locked_argv(
            contract,
            ComponentCommandPhase.EXECUTE,
            source_root=source,
            object_root=artifact_root,
            artifact_root=artifact_root,
            export_path=(artifact_root / entrypoint_contract.artifact_export.export_id),
            providers=providers,
            entrypoint_contract=(
                entrypoint_contract if contract.is_multi_entrypoint else None
            ),
        )
        if argv and argv[-1] == LITAI_SMOKE_MODE_FLAG:
            argv = argv[:-1]
        return LocalResolvedExecutionCommand(
            argv,
            artifact_root,
            tuple(sorted(self._provider_environment_values(providers).items())),
        )

    def test(
        self, plan: StandardComponentBuildPlan, exports: tuple[ArtifactExport, ...]
    ) -> StandardGeneratedTestExecutionEvidence:
        self._require_command_phase(ComponentCommandPhase.TEST)
        try:
            return self._test_locked(plan, exports)
        except LocalStandardLifecycleError as error:
            if isinstance(error, GeneratedCandidateCommandError):
                attributed = error
            elif str(error).startswith(_GENERATED_TEST_PROTOCOL_FAILURE_PREFIXES):
                attributed = GeneratedCandidateCommandError(
                    "generated-test.failed", str(error)
                )
            else:
                attributed = error
            self._record_failure_diagnostic(
                plan.component_revision.uri,
                f"{type(attributed).__name__}: {attributed}",
            )
            if attributed is error:
                raise
            raise attributed from error
        except Exception as error:
            self._record_failure_diagnostic(
                plan.component_revision.uri, f"{type(error).__name__}: {error}"
            )
            raise

    def _test_locked(
        self, plan: StandardComponentBuildPlan, exports: tuple[ArtifactExport, ...]
    ) -> StandardGeneratedTestExecutionEvidence:
        contract = self._contract(plan.component_revision)
        if contract.is_multi_entrypoint:
            return self._test_multiple_entrypoints(plan, exports, contract)
        providers = self._intent_artifacts_for_plan(plan)
        artifact = self._artifact_paths[exports[0].identity.uri]
        before = local_tree_identity(artifact)
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        result = self._run_locked(
            contract,
            ComponentCommandPhase.TEST,
            source_root=source,
            object_root=artifact,
            artifact_root=artifact,
            export_path=artifact / contract.artifact_export.export_id,
            providers=providers,
        )
        if local_tree_identity(artifact) != before:
            raise LocalStandardLifecycleError("test command mutated the built artifact")
        build = self._build_evidence_for_exports(exports)
        source_custody = self.source_trees.evidence(plan.request.source_tree_identity)
        suite = source_custody.generated_test_suite
        process_observation = self._process_observation(
            result, phase="generated-test", plan_identity=plan.identity
        )
        try:
            test_result = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LocalStandardLifecycleError(
                "generated-test runner did not emit attributable case results"
            ) from exc
        raw_cases = test_result.get("cases") if isinstance(test_result, dict) else None
        if (
            not isinstance(test_result, dict)
            or set(test_result) != {"schema", "cases"}
            or test_result.get("schema") != "literate-ai/generated-test-results@1"
            or not isinstance(raw_cases, list)
        ):
            raise LocalStandardLifecycleError(
                "generated-test runner emitted an invalid case result protocol"
            )
        observed_cases: dict[str, dict[str, object]] = {}
        for raw_case in raw_cases:
            if (
                not isinstance(raw_case, dict)
                or set(raw_case) != {"case_id", "outcome"}
                or not isinstance(raw_case.get("case_id"), str)
                or raw_case.get("outcome") != "passed"
                or raw_case["case_id"] in observed_cases
            ):
                raise LocalStandardLifecycleError(
                    "generated-test runner emitted an invalid or failed case result"
                )
            observed_cases[raw_case["case_id"]] = raw_case
        if set(observed_cases) != set(suite.case_ids):
            raise LocalStandardLifecycleError(
                "generated-test runner did not execute every and only selected case"
            )
        cases_with_identities = tuple(
            sorted(
                (
                    (
                        self._record_evidence(
                            {
                                "schema": "literate-ai/generated-test-case@1",
                                "case_id": case.case_id,
                                "category": case.category,
                                "specification_refs": list(case.specification_refs),
                                "arguments": list(case.arguments),
                                "expected_result": case.expected_result,
                            }
                        ),
                        case,
                    )
                    for case in suite.cases
                ),
                key=lambda item: item[0].uri,
            )
        )
        cases = tuple(
            StandardGeneratedTestCaseEvidence(
                case.case_id,
                case_identity,
                self._record_evidence(
                    {
                        "schema": "literate-ai/generated-test-case-observation@1",
                        "observation_kind": "attributed-suite-case",
                        "suite_process_observation_identity": process_observation.uri,
                        "case_identity": case_identity.uri,
                        "case_result": observed_cases[case.case_id],
                    }
                ),
            )
            for case_identity, case in cases_with_identities
        )
        runner_identity = self._phase_runner_identity(
            contract, ComponentCommandPhase.TEST
        )
        evidence = StandardGeneratedTestExecutionEvidence(
            component_revision=plan.component_revision,
            generated_test_suite_identity=ContentIdentity.parse_uri(
                suite.content_identity
            ),
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
            runner_identity=runner_identity,
            test_custody_identity=self._record_evidence(
                {
                    "schema": "literate-ai/local-generated-test-custody@1",
                    "source_custody_identity": source_custody.identity.uri,
                    "suite_identity": suite.content_identity,
                    "runner_identity": runner_identity.uri,
                }
            ),
            selected_case_identities=tuple(item[0] for item in cases_with_identities),
            cases=cases,
            selected_count=len(cases),
            executed_count=len(cases),
            passed_count=len(cases),
        )
        if self._evidence_recorder is not None:
            self._record_evidence(evidence.to_dict())
            for unit in evidence.entrypoint_evidence or ():
                self._record_evidence(unit.to_dict())
        self._test_evidence[evidence.identity.uri] = evidence
        return evidence

    def build_evidence_for_test(self, plan, exports) -> StandardBuildEvidence:
        """Read registered BUILD evidence under current TEST input authority."""
        self.build_execution_inputs(plan)
        build = self._build_evidence_for_exports(exports)
        if (
            build.build_plan_identity != plan.identity
            or build.component_revision != plan.component_revision
            or build.source_tree_identity != plan.request.source_tree_identity
        ):
            raise LocalStandardLifecycleError(
                "TEST requires current registered BUILD evidence"
            )
        return build

    def admit_transferred_tests(
        self, *, plan, exports, evidence, records, admission_guard
    ) -> StandardGeneratedTestExecutionEvidence:
        """Register verified TEST custody without granting a local command phase."""
        from literate_ai.adapters.action_build_limits import (
            MAX_BUILD_EVIDENCE_BYTES,
            MAX_BUILD_EVIDENCE_RECORDS,
        )
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
        )
        from literate_ai.adapters.standard_test_admission import (
            verify_transferred_tests,
        )

        if not callable(admission_guard) or not isinstance(
            evidence, StandardGeneratedTestExecutionEvidence
        ):
            raise TypeError("transferred TEST requires typed evidence and a live guard")
        self.retained_evidence_records()
        admission_guard()
        inputs = self.build_execution_inputs(plan)
        build = self._build_evidence_for_exports(exports)
        source_custody = self.source_trees.evidence(plan.request.source_tree_identity)
        contract = self._contract(plan.component_revision)
        reader = QualificationEvidenceReader(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        verify_transferred_tests(
            reader,
            plan=plan,
            build=build,
            source_custody=source_custody,
            contract=contract,
            evidence=evidence,
        )
        for identity, content in records:
            self.retain_evidence_record(identity, content)
        admission_guard()
        if (
            self.build_execution_inputs(plan) != inputs
            or self._build_evidence_for_exports(exports) != build
            or self.source_trees.evidence(plan.request.source_tree_identity)
            != source_custody
            or self._contract(plan.component_revision) != contract
        ):
            raise LocalStandardLifecycleError("transferred TEST authority changed")
        admission_guard()
        self._test_evidence[evidence.identity.uri] = evidence
        return evidence

    def admit_transferred_execution(
        self,
        *,
        plan,
        exports,
        evidence,
        records,
        admission_guard,
        scope=None,
        provider_artifacts=None,
    ) -> StandardExecutionEvidence:
        """Publish verified EXECUTE custody and stdout without local execution."""
        from literate_ai.adapters.action_build_limits import (
            MAX_BUILD_EVIDENCE_BYTES,
            MAX_BUILD_EVIDENCE_RECORDS,
        )
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
        )
        from literate_ai.adapters.standard_execution_admission import (
            verify_transferred_execution,
        )

        if not callable(admission_guard) or not isinstance(
            evidence, StandardExecutionEvidence
        ):
            raise TypeError(
                "transferred EXECUTE requires typed evidence and a live guard"
            )
        if scope is not None and not isinstance(scope, StandardExecutionInputScope):
            raise TypeError("transferred EXECUTE scope must be typed")
        self.retained_evidence_records()
        admission_guard()
        inputs = self.build_execution_inputs(plan)
        build = self.build_evidence_for_test(plan, exports)
        custody = self.source_trees.evidence(plan.request.source_tree_identity)
        contract = self._contract(plan.component_revision)
        providers = (
            self._intent_artifacts_for_plan(plan)
            if provider_artifacts is None and scope is None
            else provider_artifacts
        )
        if not isinstance(providers, tuple) or any(
            not isinstance(item, ArtifactExport) for item in providers
        ):
            raise TypeError("transferred EXECUTE requires exact provider artifacts")
        reader = QualificationEvidenceReader(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        stdout = verify_transferred_execution(
            reader,
            plan=plan,
            build=build,
            source_custody=custody,
            contract=contract,
            evidence=evidence,
            scope=scope,
            provider_artifacts=providers,
            now=self.clock(),
        )
        self._require_execution_authority(evidence.execution_authority, providers)
        for identity, content in records:
            self.retain_evidence_record(identity, content)
        admission_guard()
        if (
            self.build_execution_inputs(plan) != inputs
            or self.build_evidence_for_test(plan, exports) != build
            or self.source_trees.evidence(plan.request.source_tree_identity) != custody
            or self._contract(plan.component_revision) != contract
        ):
            raise LocalStandardLifecycleError("transferred EXECUTE authority changed")
        if scope is None and self._intent_artifacts_for_plan(plan) != providers:
            raise LocalStandardLifecycleError("transferred EXECUTE providers changed")
        self._require_execution_authority(evidence.execution_authority, providers)
        admission_guard()
        self._execution_evidence[evidence.identity.uri] = evidence
        self.execution_stdout[plan.component_revision.uri] = stdout
        return evidence

    def _generated_test_case_membership(
        self,
        suite: ValidatedGeneratedTestSuite,
    ) -> tuple[tuple[ContentIdentity, object], ...]:
        return tuple(
            sorted(
                (
                    (
                        self._record_evidence(
                            {
                                "schema": "literate-ai/generated-test-case@1",
                                "case_id": case.case_id,
                                "category": case.category,
                                "specification_refs": list(case.specification_refs),
                                "arguments": list(case.arguments),
                                "expected_result": case.expected_result,
                            }
                        ),
                        case,
                    )
                    for case in suite.cases
                ),
                key=lambda item: item[0].uri,
            )
        )

    @staticmethod
    def _parse_generated_test_observation(
        stdout: str, suite: ValidatedGeneratedTestSuite
    ) -> dict[str, dict[str, object]]:
        try:
            test_result = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LocalStandardLifecycleError(
                "generated-test runner did not emit attributable case results"
            ) from exc
        raw_cases = test_result.get("cases") if isinstance(test_result, dict) else None
        if (
            not isinstance(test_result, dict)
            or set(test_result) != {"schema", "cases"}
            or test_result.get("schema") != "literate-ai/generated-test-results@1"
            or not isinstance(raw_cases, list)
        ):
            raise LocalStandardLifecycleError(
                "generated-test runner emitted an invalid case result protocol"
            )
        observed: dict[str, dict[str, object]] = {}
        for raw_case in raw_cases:
            if (
                not isinstance(raw_case, dict)
                or set(raw_case) != {"case_id", "outcome"}
                or not isinstance(raw_case.get("case_id"), str)
                or raw_case.get("outcome") != "passed"
                or raw_case["case_id"] in observed
            ):
                raise LocalStandardLifecycleError(
                    "generated-test runner emitted an invalid or failed case result"
                )
            observed[raw_case["case_id"]] = raw_case
        if set(observed) != set(suite.case_ids):
            raise LocalStandardLifecycleError(
                "generated-test runner did not execute every and only selected case"
            )
        return observed

    def _test_multiple_entrypoints(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
    ) -> StandardGeneratedTestExecutionEvidence:
        providers = self._intent_artifacts_for_plan(plan)
        by_export_id = {item.export_id: item for item in exports}
        entrypoints = tuple(
            sorted(
                contract.entrypoint_command_contracts(),
                key=lambda item: item.deployment_unit,
            )
        )
        expected_ids = {item.artifact_export.export_id for item in entrypoints}
        if set(by_export_id) != expected_ids:
            raise LocalStandardLifecycleError(
                "multi-entrypoint test requires every and only declared export"
            )
        artifact_roots = {
            self._artifact_paths[item.identity.uri].resolve(strict=True)
            for item in exports
        }
        if len(artifact_roots) != 1:
            raise LocalStandardLifecycleError(
                "multi-entrypoint exports do not share one artifact custody root"
            )
        artifact = next(iter(artifact_roots))
        before = local_tree_identity(artifact)
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        source_custody = self.source_trees.evidence(plan.request.source_tree_identity)
        suite = source_custody.generated_test_suite
        case_membership = self._generated_test_case_membership(suite)
        unit_evidence: list[StandardEntrypointGeneratedTestEvidence] = []
        aggregate_cases: list[StandardGeneratedTestCaseEvidence] = []
        for entrypoint in entrypoints:
            export = by_export_id[entrypoint.artifact_export.export_id]
            result = self._run_locked(
                contract,
                ComponentCommandPhase.TEST,
                source_root=source,
                object_root=artifact,
                artifact_root=artifact,
                export_path=artifact / export.export_id,
                providers=providers,
                entrypoint_contract=entrypoint,
            )
            observed = self._parse_generated_test_observation(result.stdout, suite)
            process_observation = self._process_observation(
                result,
                phase=f"generated-test:{entrypoint.deployment_unit}",
                plan_identity=plan.identity,
            )
            cases = tuple(
                StandardGeneratedTestCaseEvidence(
                    case.case_id,
                    case_identity,
                    self._record_evidence(
                        {
                            "schema": "literate-ai/generated-test-case-observation@1",
                            "observation_kind": "attributed-entrypoint-suite-case",
                            "entrypoint_identity": entrypoint.entrypoint_identity.uri,
                            "suite_process_observation_identity": (
                                process_observation.uri
                            ),
                            "case_identity": case_identity.uri,
                            "case_result": observed[case.case_id],
                        }
                    ),
                )
                for case_identity, case in case_membership
            )
            runner_identity = entrypoint.tool_binding(
                ComponentCommandPhase.TEST
            ).toolchain_identity
            custody_identity = self._record_evidence(
                {
                    "schema": "literate-ai/local-entrypoint-generated-test-custody@1",
                    "source_custody_identity": source_custody.identity.uri,
                    "suite_identity": suite.content_identity,
                    "entrypoint_identity": entrypoint.entrypoint_identity.uri,
                    "export_identity": export.identity.uri,
                    "runner_identity": runner_identity.uri,
                }
            )
            unit_evidence.append(
                StandardEntrypointGeneratedTestEvidence(
                    entrypoint_identity=entrypoint.entrypoint_identity,
                    deployment_unit=entrypoint.deployment_unit,
                    export_identity=export.identity,
                    command_identity=entrypoint.command(
                        ComponentCommandPhase.TEST
                    ).identity,
                    runner_identity=runner_identity,
                    process_observation_identity=process_observation,
                    test_custody_identity=custody_identity,
                    selected_case_identities=tuple(item[0] for item in case_membership),
                    cases=cases,
                    selected_count=len(cases),
                    executed_count=len(cases),
                    passed_count=len(cases),
                )
            )
            for case in cases:
                aggregate_identity = self._record_evidence(
                    {
                        "schema": "literate-ai/entrypoint-generated-test-case@1",
                        "entrypoint_identity": entrypoint.entrypoint_identity.uri,
                        "case_identity": case.case_identity.uri,
                    }
                )
                aggregate_cases.append(
                    StandardGeneratedTestCaseEvidence(
                        f"{entrypoint.entrypoint_identity.digest[:16]}:"
                        f"{case.case_id[:220]}",
                        aggregate_identity,
                        case.observation_identity,
                    )
                )
        if local_tree_identity(artifact) != before:
            raise LocalStandardLifecycleError("test command mutated the built artifact")
        aggregate_case_tuple = tuple(
            sorted(aggregate_cases, key=lambda item: item.case_identity.uri)
        )
        build = self._build_evidence_for_exports(exports)
        units = tuple(unit_evidence)
        evidence = StandardGeneratedTestExecutionEvidence(
            component_revision=plan.component_revision,
            generated_test_suite_identity=ContentIdentity.parse_uri(
                suite.content_identity
            ),
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
            runner_identity=self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-test-runners@1",
                    "entrypoint_evidence": [item.identity.uri for item in units],
                }
            ),
            test_custody_identity=self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-test-custody@1",
                    "entrypoint_evidence": [item.identity.uri for item in units],
                }
            ),
            selected_case_identities=tuple(
                item.case_identity for item in aggregate_case_tuple
            ),
            cases=aggregate_case_tuple,
            selected_count=len(aggregate_case_tuple),
            executed_count=len(aggregate_case_tuple),
            passed_count=len(aggregate_case_tuple),
            entrypoint_evidence=units,
        )
        if self._evidence_recorder is not None:
            self._record_evidence(evidence.to_dict())
            for unit in evidence.entrypoint_evidence or ():
                self._record_evidence(unit.to_dict())
        self._test_evidence[evidence.identity.uri] = evidence
        return evidence

    def authorize_execution_inputs(
        self, plan: StandardComponentBuildPlan, scope: StandardExecutionInputScope
    ) -> StandardExecutionAuthority:
        contract = self._contract(plan.component_revision)
        runtime = standard_execution_runtime_identity(contract)
        request = standard_execution_request(
            scope, plan.request.source_tree_identity, contract.identity, runtime
        )
        now = self.clock()
        grant = BuildAuthorization(
            authorization_id=f"local-execute:{canonical_identity(request.to_dict()).digest}",
            classification_digest=scope.identity.uri,
            request_digest=canonical_identity(request.to_dict()).uri,
            effective_revision_digest=scope.component_revision.uri,
            actor="local-standard-lifecycle",
            reason="execute configured commands with exact admitted provider inputs",
            profile=SecurityProfile.CONSTRAINED,
            privileges=request.requested_privileges,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        return StandardExecutionAuthority(
            scope, plan.request.source_tree_identity, contract.identity, runtime, grant
        )

    def execute_scoped(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        scope: StandardExecutionInputScope,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardExecutionEvidence:
        """Execute admitted late inputs without changing compilation provenance."""
        self._require_command_phase(ComponentCommandPhase.EXECUTE)
        if (
            not isinstance(scope, StandardExecutionInputScope)
            or scope.build_plan_identity != plan.identity
            or scope.component_revision != plan.component_revision
            or scope.build_provider_artifact_identities
            != plan.provider_artifact_identities
            or set(scope.export_identities) != {item.identity for item in exports}
            or not isinstance(provider_artifacts, tuple)
            or any(not isinstance(item, ArtifactExport) for item in provider_artifacts)
            or tuple(item.identity for item in provider_artifacts)
            != scope.provider_artifact_identities
            or self._build_evidence_for_exports(exports).build_plan_identity
            != plan.identity
        ):
            raise LocalStandardLifecycleError(
                "execution scope differs from exact build and provider inputs"
            )
        authority = self.authorize_execution_inputs(plan, scope)
        contract = self._contract(plan.component_revision)
        if (
            not isinstance(authority, StandardExecutionAuthority)
            or authority.input_scope != scope
            or authority.source_tree_identity != plan.request.source_tree_identity
            or authority.command_contract_identity != contract.identity
            or authority.runtime_identity
            != standard_execution_runtime_identity(contract)
        ):
            raise LocalStandardLifecycleError(
                "execution authority differs from current inputs and commands"
            )
        self._require_execution_authority(authority, provider_artifacts)
        self._record_evidence(contract.to_dict())
        self._record_evidence(scope.to_dict())
        self._record_evidence(authority.to_dict())
        return self._execute_with_inputs(plan, exports, provider_artifacts, authority)

    def _require_execution_authority(
        self,
        authority: StandardExecutionAuthority | None,
        providers: tuple[ArtifactExport, ...],
    ) -> None:
        if authority is None:
            return
        authority.require_valid(now=self.clock())
        if (
            tuple(item.identity for item in providers)
            != authority.input_scope.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError("execution provider inputs changed")
        pending = list(providers)
        scheduled = {item.identity for item in providers}
        while pending:
            item = pending.pop()
            if self._exports_by_identity.get(item.identity.uri) != item:
                raise LocalStandardLifecycleError(
                    "execution provider has no exact local custody"
                )
            root = self._artifact_paths.get(item.identity.uri)
            if root is None or path_is_link_or_reparse(root):
                raise LocalStandardLifecycleError(
                    "execution provider custody is unsafe"
                )
            export_path = root / item.export_id
            if path_is_link_or_reparse(export_path) or not export_path.exists():
                raise LocalStandardLifecycleError(
                    "execution provider custody is unsafe"
                )
            if export_path.resolve(strict=True).parent != root.resolve(strict=True):
                raise LocalStandardLifecycleError(
                    "execution provider escaped its custody root"
                )
            content = (
                _directory_export_bytes(export_path)
                if export_path.is_dir()
                else export_path.read_bytes()
                if export_path.is_file()
                else None
            )
            if (
                content is None
                or len(content) != item.blob.size
                or hashlib.sha256(content).hexdigest() != item.blob.digest
            ):
                raise LocalStandardLifecycleError(
                    "artifact blob changed after admission"
                )
            for identity in item.dependency_artifact_identities:
                dependency = self._exports_by_identity.get(identity.uri)
                if dependency is None or dependency.identity != identity:
                    raise LocalStandardLifecycleError(
                        "execution provider closure is incomplete"
                    )
                if identity not in scheduled:
                    if len(scheduled) >= 16384:
                        raise LocalStandardLifecycleError(
                            "execution provider closure is oversized"
                        )
                    scheduled.add(identity)
                    pending.append(dependency)

    def execute(
        self, plan: StandardComponentBuildPlan, exports: tuple[ArtifactExport, ...]
    ) -> StandardExecutionEvidence:
        self._require_command_phase(ComponentCommandPhase.EXECUTE)
        return self._execute_with_inputs(
            plan, exports, self._intent_artifacts_for_plan(plan), None
        )

    def _execute_with_inputs(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        providers: tuple[ArtifactExport, ...],
        authority: StandardExecutionAuthority | None,
    ) -> StandardExecutionEvidence:
        contract = self._contract(plan.component_revision)
        if contract.is_multi_entrypoint:
            return self._execute_multiple_entrypoints(
                plan, exports, contract, providers, authority
            )
        artifact = self._artifact_paths[exports[0].identity.uri]
        before = local_tree_identity(artifact)
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        try:
            self._require_execution_authority(authority, providers)
            result = self._run_locked(
                contract,
                ComponentCommandPhase.EXECUTE,
                source_root=source,
                object_root=artifact,
                artifact_root=artifact,
                export_path=artifact / contract.artifact_export.export_id,
                providers=providers,
            )
            self._require_execution_authority(authority, providers)
        except Exception as error:
            self._record_failure_diagnostic(
                plan.component_revision.uri, f"{type(error).__name__}: {error}"
            )
            raise
        if local_tree_identity(artifact) != before:
            raise LocalStandardLifecycleError(
                "execution command mutated the built artifact"
            )
        self.execution_stdout[plan.component_revision.uri] = result.stdout.strip()
        build = self._build_evidence_for_exports(exports)
        runtime_identity = self._phase_runner_identity(
            contract, ComponentCommandPhase.EXECUTE
        )
        execution_contract_identity = contract.command(
            ComponentCommandPhase.EXECUTE
        ).identity
        evidence = StandardExecutionEvidence(
            execution_authority=authority,
            component_revision=plan.component_revision,
            build_evidence_identity=build.identity,
            provider_artifact_identities=tuple(
                sorted((item.identity for item in providers), key=lambda item: item.uri)
            ),
            export_identities=build.export_identities,
            root_export_identity=exports[0].identity,
            execution_contract_identity=execution_contract_identity,
            runtime_identity=runtime_identity,
            artifact_custody_identity=build.artifact_custody_identity,
            observation_identity=self._process_observation(
                result,
                phase="execute",
                plan_identity=plan.identity,
                execution_authority=authority,
            ),
            stdout_identity=self._record_evidence(result.stdout),
            stderr_identity=self._record_evidence(result.stderr),
            exit_code=result.returncode,
        )
        if self._evidence_recorder is not None:
            self._record_evidence(evidence.to_dict())
            for unit in evidence.entrypoint_evidence or ():
                self._record_evidence(unit.to_dict())
        self._execution_evidence[evidence.identity.uri] = evidence
        return evidence

    def _execute_multiple_entrypoints(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
        providers: tuple[ArtifactExport, ...],
        authority: StandardExecutionAuthority | None,
    ) -> StandardExecutionEvidence:
        by_export_id = {item.export_id: item for item in exports}
        entrypoints = tuple(
            sorted(
                contract.entrypoint_command_contracts(),
                key=lambda item: item.deployment_unit,
            )
        )
        if set(by_export_id) != {
            item.artifact_export.export_id for item in entrypoints
        }:
            raise LocalStandardLifecycleError(
                "multi-entrypoint execution requires every and only declared export"
            )
        artifact_roots = {
            self._artifact_paths[item.identity.uri].resolve(strict=True)
            for item in exports
        }
        if len(artifact_roots) != 1:
            raise LocalStandardLifecycleError(
                "multi-entrypoint exports do not share one artifact custody root"
            )
        artifact = next(iter(artifact_roots))
        before = local_tree_identity(artifact)
        source = self.source_trees.resolve(plan.request.source_tree_identity)
        units: list[StandardEntrypointExecutionEvidence] = []
        stdout_by_unit: dict[str, str] = {}
        stderr_by_unit: dict[str, str] = {}
        for entrypoint in entrypoints:
            export = by_export_id[entrypoint.artifact_export.export_id]
            try:
                self._require_execution_authority(authority, providers)
                result = self._run_locked(
                    contract,
                    ComponentCommandPhase.EXECUTE,
                    source_root=source,
                    object_root=artifact,
                    artifact_root=artifact,
                    export_path=artifact / export.export_id,
                    providers=providers,
                    entrypoint_contract=entrypoint,
                )
                self._require_execution_authority(authority, providers)
            except Exception as error:
                self._record_failure_diagnostic(
                    plan.component_revision.uri,
                    f"{type(error).__name__}: {error}",
                )
                raise
            stdout_by_unit[entrypoint.deployment_unit] = result.stdout
            stderr_by_unit[entrypoint.deployment_unit] = result.stderr
            units.append(
                StandardEntrypointExecutionEvidence(
                    entrypoint_identity=entrypoint.entrypoint_identity,
                    deployment_unit=entrypoint.deployment_unit,
                    export_identity=export.identity,
                    execution_contract_identity=entrypoint.command(
                        ComponentCommandPhase.EXECUTE
                    ).identity,
                    runtime_identity=entrypoint.tool_binding(
                        ComponentCommandPhase.EXECUTE
                    ).toolchain_identity,
                    observation_identity=self._process_observation(
                        result,
                        phase=f"execute:{entrypoint.deployment_unit}",
                        plan_identity=plan.identity,
                        execution_authority=authority,
                    ),
                    stdout_identity=self._record_evidence(result.stdout),
                    stderr_identity=self._record_evidence(result.stderr),
                    exit_code=result.returncode,
                )
            )
        if local_tree_identity(artifact) != before:
            raise LocalStandardLifecycleError(
                "execution command mutated the built artifact"
            )
        units_tuple = tuple(units)
        self.execution_stdout[plan.component_revision.uri] = json.dumps(
            {name: value.strip() for name, value in sorted(stdout_by_unit.items())},
            sort_keys=True,
            separators=(",", ":"),
        )
        build = self._build_evidence_for_exports(exports)
        root_export = by_export_id[contract.artifact_export.export_id]
        evidence = StandardExecutionEvidence(
            execution_authority=authority,
            component_revision=plan.component_revision,
            build_evidence_identity=build.identity,
            provider_artifact_identities=tuple(
                sorted((item.identity for item in providers), key=lambda item: item.uri)
            ),
            export_identities=build.export_identities,
            root_export_identity=root_export.identity,
            execution_contract_identity=self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-execution-contract@1",
                    "entrypoint_evidence": [item.identity.uri for item in units_tuple],
                }
            ),
            runtime_identity=self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-runtime@1",
                    "runtimes": sorted(
                        {item.runtime_identity.uri for item in units_tuple}
                    ),
                }
            ),
            artifact_custody_identity=build.artifact_custody_identity,
            observation_identity=self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-execution-observation@1",
                    "entrypoint_evidence": [item.identity.uri for item in units_tuple],
                }
            ),
            stdout_identity=self._record_evidence(stdout_by_unit),
            stderr_identity=self._record_evidence(stderr_by_unit),
            exit_code=0,
            entrypoint_evidence=units_tuple,
        )
        if self._evidence_recorder is not None:
            self._record_evidence(evidence.to_dict())
            for unit in evidence.entrypoint_evidence or ():
                self._record_evidence(unit.to_dict())
        self._execution_evidence[evidence.identity.uri] = evidence
        return evidence

    def _build_evidence_for_exports(
        self, exports: tuple[ArtifactExport, ...]
    ) -> StandardBuildEvidence:
        identities = tuple(item.identity for item in exports)
        for evidence in self._build_evidence.values():
            if evidence.export_identities == identities:
                return evidence
        raise LocalStandardLifecycleError("built exports have no strict build evidence")

    def _phase_runner_identity(
        self, contract: ComponentCommandContract, phase: ComponentCommandPhase
    ) -> ContentIdentity:
        return contract.tool_binding(phase).toolchain_identity

    def _intent_artifacts_for_plan(
        self, plan: StandardComponentBuildPlan
    ) -> tuple[ArtifactExport, ...]:
        by_identity = {
            item.identity.uri: item
            for values in self._intent_artifacts.values()
            for item in values
        }
        return tuple(
            by_identity[item.uri] for item in plan.provider_artifact_identities
        )

    def accept(
        self,
        plan: StandardComponentBuildPlan,
        test_identity: ContentIdentity,
        execution_identity: ContentIdentity,
    ) -> StandardComponentAcceptanceEvidence:
        try:
            generated_tests = self._test_evidence[test_identity.uri]
            execution = self._execution_evidence[execution_identity.uri]
            build = self._build_evidence[generated_tests.build_evidence_identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "acceptance inputs have no strict stage evidence"
            ) from exc
        source_custody = self.source_trees.evidence(plan.request.source_tree_identity)
        evidence = StandardComponentAcceptanceEvidence(
            component_revision=plan.component_revision,
            source_generation_identity=source_custody.source_generation_identity,
            generated_test_suite_identity=ContentIdentity.parse_uri(
                source_custody.generated_test_suite.content_identity
            ),
            build=build,
            generated_tests=generated_tests,
            execution=execution,
            acceptance_policy_identity=self._record_evidence(
                {
                    "schema": "literate-ai/local-standard-acceptance-policy@1",
                    "requires": ["build", "generated-tests", "execution"],
                }
            ),
        )
        if self._evidence_recorder is not None:
            self._record_evidence(evidence.to_dict())
        return evidence

    def admit_transferred_acceptance(
        self,
        *,
        plan,
        test_identity,
        execution_identity,
        generation_plan,
        evidence,
        records,
        admission_guard,
    ) -> StandardComponentAcceptanceEvidence:
        """Admit acceptance only for exact current controller stage custody."""
        from literate_ai.adapters.action_build_result import (
            _capture_files,
            _verify_files,
        )
        from literate_ai.adapters.action_provider_build import (
            verify_accepted_component_records,
        )

        if not callable(admission_guard) or not isinstance(
            evidence, StandardComponentAcceptanceEvidence
        ):
            raise TypeError(
                "transferred ACCEPT requires typed evidence and a live guard"
            )
        self.retained_evidence_records()
        admission_guard()
        inputs = self.build_execution_inputs(plan)
        source = self.source_trees.evidence(plan.request.source_tree_identity)
        contract = self._contract(plan.component_revision)
        try:
            tests = self._test_evidence[test_identity.uri]
            execution = self._execution_evidence[execution_identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "ACCEPT requires registered TEST and EXECUTE evidence"
            ) from exc
        build = self.build_evidence_for_test(plan, evidence.build.exports)
        if (
            evidence.component_revision != plan.component_revision
            or evidence.build != build
            or evidence.generated_tests != tests
            or evidence.execution != execution
            or evidence.source_generation_identity != source.source_generation_identity
            or evidence.generated_test_suite_identity
            != ContentIdentity.parse_uri(source.generated_test_suite.content_identity)
        ):
            raise LocalStandardLifecycleError(
                "transferred ACCEPT stage custody differs"
            )

        def current():
            admission_guard()
            if (
                self.build_execution_inputs(plan) != inputs
                or self.build_evidence_for_test(plan, build.exports) != build
                or self._test_evidence.get(test_identity.uri) != tests
                or self._execution_evidence.get(execution_identity.uri) != execution
                or self.source_trees.evidence(plan.request.source_tree_identity)
                != source
                or self._contract(plan.component_revision) != contract
            ):
                raise LocalStandardLifecycleError(
                    "transferred ACCEPT authority changed"
                )

        reader = verify_accepted_component_records(
            records,
            evidence,
            self.source_trees.validation_inputs(plan.request.source_tree_identity),
            generation_plan,
        )
        current()
        roots = {self.artifact_path(item).parent for item in build.exports}
        if len(roots) != 1:
            raise LocalStandardLifecycleError("ACCEPT artifact roots differ")
        files, custody = _capture_files(roots.pop(), current)
        _verify_files(files, build, reader)
        for identity, content in records:
            self.retain_evidence_record(identity, content)
        current()
        custody.require_unchanged()
        return evidence

    def assemble_project_artifacts(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        results: tuple[StandardNodeLifecycleResult, ...],
    ) -> tuple[ArtifactBuildGraph, ExactLinkPlan]:
        """Assemble one exact multi-Component graph from accepted local exports."""

        if not isinstance(component_lock, ComponentLock):
            raise TypeError("component_lock must be a ComponentLock")
        if not isinstance(project_build_plan, StandardProjectBuildPlan):
            raise TypeError("project_build_plan must be a StandardProjectBuildPlan")
        if (
            component_lock.identity != execution_plan.component_lock_identity
            or project_build_plan.execution_plan_identity != execution_plan.identity
        ):
            raise LocalStandardLifecycleError(
                "project artifact assembly received foreign authority"
            )
        from literate_ai.application.release_artifacts import (
            plan_standard_assembly_dependencies,
        )

        assembly_dependencies = plan_standard_assembly_dependencies(
            execution_plan, results
        )
        by_revision = {item.component_revision.uri: item for item in results}
        planned = {
            item.component_revision.uri: item for item in project_build_plan.components
        }
        locked = {item.revision.identity.uri for item in component_lock.nodes}
        if set(by_revision) != set(planned) or set(planned) != locked:
            raise LocalStandardLifecycleError(
                "project artifact assembly requires every exact locked Component"
            )
        manifests = tuple(
            realize_manifest(plan.manifest, by_revision[uri].exports)
            for uri, plan in sorted(planned.items())
        )
        drivers = {item.build_system_driver_identity for item in manifests}
        if len(drivers) != 1:
            raise LocalStandardLifecycleError(
                "one project artifact graph requires one exact build-system driver"
            )
        root_result = by_revision[component_lock.root_revision.uri]
        if not root_result.exports:
            raise LocalStandardLifecycleError("root Component has no built export")
        root_contract = self._contract(component_lock.root_revision)
        primary_root = next(
            item
            for item in root_result.exports
            if item.export_id == root_contract.artifact_export.export_id
        )
        if len(root_result.exports) > 1:
            graph = create_artifact_build_graph(
                build_system_driver_identity=next(iter(drivers)),
                manifests=manifests,
                assembly_dependencies=assembly_dependencies,
                link_roots=(),
                link_root_groups=(
                    (
                        primary_root.identity,
                        *(
                            item.identity
                            for item in root_result.exports
                            if item is not primary_root
                        ),
                    ),
                ),
            )
        else:
            graph = create_artifact_build_graph(
                build_system_driver_identity=next(iter(drivers)),
                manifests=manifests,
                assembly_dependencies=assembly_dependencies,
                link_roots=(primary_root.identity,),
            )
        return graph, graph.link_plans[0]

    def _root_package_entrypoint(
        self, component_lock: ComponentLock
    ) -> tuple[str, str]:
        """Return the (name, kind) of the root Component's single packaged entrypoint.

        ACCEPTANCE-SERVICE-001 (#213): the package plan must reflect the
        Component's real declared entrypoint kind so independent acceptance
        routes a persistent-service to its service oracle instead of silently
        taking the exempt path. A Component with no declared entrypoint (a
        synthetic single-shot / library shape) keeps the historical
        "application" kind.
        """

        node = next(
            (
                item
                for item in component_lock.nodes
                if item.revision.identity == component_lock.root_revision
            ),
            None,
        )
        if node is None:
            return "application", "application"
        entrypoints = tuple(node.revision.definition.entrypoints)
        if len(entrypoints) != 1:
            # Zero entrypoints (library/synthetic) or an unexpected multi-entry
            # shape: preserve the historical application kind. Multi-entrypoint
            # Components are governed separately (MULTI-ENTRYPOINT-001 / #33).
            return "application", "application"
        entrypoint = entrypoints[0]
        return entrypoint.name, entrypoint.kind

    def _root_package_entrypoints(
        self,
        component_lock: ComponentLock,
        exports: Mapping[str, ArtifactExport],
        destinations: Mapping[str, str],
    ) -> tuple[PackageEntrypoint, ...]:
        root_contract = self._contract(component_lock.root_revision)
        if root_contract.is_library:
            return ()
        if not root_contract.is_multi_entrypoint:
            name, kind = self._root_package_entrypoint(component_lock)
            root_export = exports[root_contract.artifact_export.export_id]
            return (
                PackageEntrypoint(
                    name,
                    kind,
                    destinations[root_export.identity.uri],
                    root_export.identity,
                ),
            )
        node = next(
            item
            for item in component_lock.nodes
            if item.revision.identity == component_lock.root_revision
        )
        authored_by_unit = {
            item.resolved_deployment_unit: item
            for item in node.revision.definition.entrypoints
        }
        projected_by_unit = {
            item.deployment_unit: item
            for item in root_contract.entrypoint_command_contracts()
        }
        if set(authored_by_unit) != set(projected_by_unit):
            raise LocalStandardLifecycleError(
                "packaged entrypoints differ from locked Component authority"
            )
        packaged: list[PackageEntrypoint] = []
        for unit, entrypoint in authored_by_unit.items():
            projected = projected_by_unit[unit]
            export = exports[projected.artifact_export.export_id]
            packaged.append(
                PackageEntrypoint(
                    entrypoint.name,
                    entrypoint.kind,
                    destinations[export.identity.uri],
                    export.identity,
                    deployment_unit=unit,
                )
            )
        return tuple(sorted(packaged, key=lambda item: item.name))

    def create_project_package(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        artifact_graph: ArtifactBuildGraph,
        link_plan: ExactLinkPlan,
    ) -> tuple[PackagePlan, PackageResult]:
        """Create a verified directory package and immutable local package custody."""

        del execution_plan, project_build_plan
        exports = {
            item.identity.uri: item
            for manifest in artifact_graph.manifests
            for item in manifest.exports
        }
        if link_plan not in artifact_graph.link_plans:
            raise LocalStandardLifecycleError(
                "package link plan is absent from the artifact graph"
            )
        closure = tuple(
            exports[item.uri] for item in link_plan.ordered_artifact_identities
        )
        root_export = exports[link_plan.root_artifact_identity.uri]
        grouped_roots = set(link_plan.resolved_root_artifact_identities)
        destinations = {
            item.identity.uri: (
                f"root/{item.export_id}"
                if item.identity in grouped_roots
                else (
                    f"components/{item.component_revision.digest[:16]}/{item.export_id}"
                )
            )
            for item in closure
        }
        root_contract = self._contract(component_lock.root_revision)
        root_plan = self._plans_by_revision.get(component_lock.root_revision.uri)
        if root_plan is None:
            raise LocalStandardLifecycleError("root build plan has no local custody")
        # ACCEPTANCE-SERVICE-001 (#213): carry the Component's REAL declared
        # entrypoint kind into the package plan instead of hard-coding
        # "application". A persistent-service Component must package with
        # kind="persistent-service" so independent acceptance routes to its
        # service oracle rather than the exempt/portable-CLI path.
        exports_by_id = {item.export_id: item for item in closure}
        from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources

        sdk_resources = (
            None
            if self._native_sdk_inputs is None
            else NativeSdkPackageResources(
                self._native_sdk_inputs,
                component_lock_identity=component_lock.identity,
                root_revision=component_lock.root_revision,
                target_identity=root_export.target_identity,
                consumer_revisions=tuple(
                    sorted(
                        {item.component_revision for item in closure},
                        key=lambda v: v.uri,
                    )
                ),
            )
        )
        sdk_scope = None
        if sdk_resources is not None and any(
            b.build.selection.component_revision != component_lock.root_revision
            for b in sdk_resources.bindings
        ):
            from literate_ai.adapters.native_sdk_package_scope import (
                NativeSdkPackageExecutionScope,
            )

            owners = sorted(
                {item.component_revision for item in closure}, key=lambda v: v.uri
            )
            if any(owner.uri not in self._plans_by_revision for owner in owners):
                raise LocalStandardLifecycleError(
                    "packaged SDK owner lacks its exact Component build plan"
                )
            sdk_scope = NativeSdkPackageExecutionScope(
                component_lock,
                artifact_graph,
                link_plan,
                tuple(self._plans_by_revision[owner.uri] for owner in owners),
                tuple(self._contract(owner) for owner in owners),
            )
            sdk_scope.select_bindings(
                sdk_resources.bindings, component_lock.root_revision
            )
        python_resources: list[PackageInput] = []
        python_resource_blobs: dict[BlobRef, bytes] = {}
        python_observer: StandardPythonDependencyObserver | None = None
        python_artifact_tree: ContentIdentity | None = None
        if component_lock.root_revision.uri in self.python_targets:
            source_artifact_root = self.artifact_path(root_export).parent
            source_key = str(source_artifact_root.resolve(strict=True))
            python_observer = self._python_observers.get(source_key)
            python_artifact_tree = self._python_execution_trees.get(source_key)
            if python_observer is None or python_artifact_tree is None:
                raise LocalStandardLifecycleError(
                    "Python package requires sealed dependency and artifact custody"
                )
            python_observer()
            if local_tree_identity(source_artifact_root) != python_artifact_tree:
                raise LocalStandardLifecycleError(
                    "Python package source differs from its sealed artifact"
                )
            export_roots = tuple(
                self.artifact_path(item).resolve(strict=True)
                for item in closure
                if self.artifact_path(item).parent.resolve(strict=True)
                == source_artifact_root.resolve(strict=True)
            )
            package_prefix = PurePosixPath(
                destinations[root_export.identity.uri]
            ).parent
            for path in sorted(source_artifact_root.rglob("*")):
                if path_is_link_or_reparse(path):
                    raise LocalStandardLifecycleError(
                        "Python package resource cannot be a symbolic link"
                    )
                if path.is_dir():
                    continue
                resolved = path.resolve(strict=True)
                if any(
                    resolved == export_root or export_root in resolved.parents
                    for export_root in export_roots
                ):
                    continue
                if not path.is_file():
                    raise LocalStandardLifecycleError(
                        "Python package resource must be a regular file"
                    )
                content = path.read_bytes()
                reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
                relative = path.relative_to(source_artifact_root).as_posix()
                item = PackageInput(
                    (package_prefix / PurePosixPath(relative)).as_posix(),
                    "python-runtime-resource",
                    PackageFileKind.RESOURCE,
                    python_observer.expected_identity,
                    root_export.target_identity,
                    reference,
                    os.name != "nt" and bool(path.stat().st_mode & 0o111),
                )
                python_resources.append(item)
                python_resource_blobs[reference] = content
            if local_tree_identity(source_artifact_root) != python_artifact_tree:
                raise LocalStandardLifecycleError(
                    "Python package source changed during resource capture"
                )
        plan = create_package_plan(
            artifact_graph,
            root_component_revision=component_lock.root_revision,
            component_lock_identity=component_lock.identity,
            target_identity=root_export.target_identity,
            root_artifact_identity=root_export.identity,
            package_kind=(
                PackageKind.DIRECTORY
                if root_contract.is_library
                else PackageKind.RUNTIME_BUNDLE
            ),
            packager_identity=canonical_identity(
                {"packager": "local-directory-package@1"}
            ),
            destinations=destinations,
            resource_inputs=(
                *python_resources,
                *(() if sdk_resources is None else sdk_resources.inputs),
            ),
            entrypoints=self._root_package_entrypoints(
                component_lock, exports_by_id, destinations
            ),
            runtime_requirements=(
                ()
                if root_contract.is_library
                else (
                    RuntimeRequirement(
                        "root-language-runtime",
                        RuntimeRequirementKind.INTERPRETER,
                        "locked root language runtime",
                        root_contract.language_runtime_identity,
                        False,
                    ),
                )
            )
            + (() if sdk_resources is None else sdk_resources.runtime_requirements),
            native_library_root=(
                destinations[root_export.identity.uri]
                if root_contract.native_layout is not None
                else None
            ),
            native_library_layout=root_contract.native_layout,
        )

        def read_package_blob(reference: BlobRef) -> bytes:
            if reference in python_resource_blobs:
                return python_resource_blobs[reference]
            if sdk_resources is not None and sdk_resources.owns_blob(reference):
                return sdk_resources.read_blob(reference)
            return self.read_artifact_blob(reference)

        result = DirectoryPackageAdapter().package(plan, read_blob=read_package_blob)
        package_parent = self.object_root / "project-packages"
        package_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f"{plan.identity.digest[:16]}-", dir=package_parent)
        )
        artifact_paths: dict[str, Path] = {}
        try:
            for export in closure:
                self.read_artifact_blob(export.blob)
                source = self.artifact_path(export)
                destination = staging.joinpath(
                    *Path(destinations[export.identity.uri]).parts
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_symlink():
                    raise LocalStandardLifecycleError(
                        "package input cannot be a symbolic link"
                    )
                if source.is_dir():
                    shutil.copytree(source, destination)
                    copied = _directory_export_bytes(destination)
                elif source.is_file():
                    shutil.copy2(source, destination)
                    copied = destination.read_bytes()
                else:
                    raise LocalStandardLifecycleError(
                        "package input must be a regular file or directory"
                    )
                if (
                    len(copied) != export.blob.size
                    or hashlib.sha256(copied).hexdigest() != export.blob.digest
                ):
                    raise LocalStandardLifecycleError(
                        "materialized package input differs from immutable artifact"
                    )
                artifact_paths[export.identity.uri] = destination
            for item in python_resources:
                destination = staging.joinpath(*PurePosixPath(item.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if path_is_link_or_reparse(destination.parent):
                    raise LocalStandardLifecycleError(
                        "Python package resource parent is unsafe"
                    )
                with destination.open("xb") as stream:
                    stream.write(python_resource_blobs[item.blob])
                destination.chmod(0o755 if item.executable else 0o644)
            if sdk_resources is not None:
                sdk_resources.materialize(staging)
            if python_observer is not None:
                staged_python_root = artifact_paths[root_export.identity.uri].parent
                if local_tree_identity(staged_python_root) != python_artifact_tree:
                    raise LocalStandardLifecycleError(
                        "Packaged Python artifact differs from sealed build custody"
                    )
                StandardPythonDependencyObserver(
                    staged_python_root,
                    python_observer.expected_identity,
                    python_observer.binding,
                    python_observer.source,
                    python_observer.toolchain,
                )()
            staged_identity = local_tree_identity(staging)
            destination_root = package_parent / plan.identity.digest
            if destination_root.exists():
                if (
                    destination_root.is_symlink()
                    or not destination_root.is_dir()
                    or local_tree_identity(destination_root) != staged_identity
                ):
                    raise LocalStandardLifecycleError(
                        "existing package custody differs from its exact plan"
                    )
                shutil.rmtree(staging)
            else:
                staging.rename(destination_root)
            final_paths = {
                uri: destination_root / path.relative_to(staging)
                for uri, path in artifact_paths.items()
            }
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        source_custody = self.source_trees.evidence(
            root_plan.request.source_tree_identity
        )
        custody = LocalProjectPackageCustody(
            plan,
            result,
            destination_root,
            final_paths,
            root_plan,
            source_custody.generated_test_suite,
            local_tree_identity(destination_root),
            sdk_resources,
            sdk_scope,
            (
                None
                if python_observer is None
                else StandardPythonDependencyObserver(
                    final_paths[root_export.identity.uri].parent,
                    python_observer.expected_identity,
                    python_observer.binding,
                    python_observer.source,
                    python_observer.toolchain,
                )
            ),
            python_artifact_tree,
        )
        self._project_packages[result.identity.uri] = custody
        return plan, result

    def project_package_custody(
        self, plan: PackagePlan, result: PackageResult
    ) -> LocalProjectPackageCustody:
        try:
            custody = self._project_packages[result.identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "project package has no local immutable custody"
            ) from exc
        if (
            custody.package_plan != plan
            or custody.package_result != result
            or local_tree_identity(custody.root) != custody.tree_identity
        ):
            raise LocalStandardLifecycleError("project package custody changed")
        return custody

    def _packaged_environment(
        self, custody: LocalProjectPackageCustody
    ) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        for identity_uri, path in custody.artifact_paths.items():
            export = self._exports_by_identity[identity_uri]
            binding = self.provider_environment.get(export.export_id)
            if binding is None:
                continue
            name, relative_path = binding
            relative = Path(relative_path)
            target = path.parent / relative
            if target.is_symlink() or not target.exists():
                raise LocalStandardLifecycleError(
                    f"packaged provider binding is unavailable: {export.export_id}"
                )
            environment[name] = str(target.resolve(strict=True))
        return environment

    def _packaged_argv(
        self,
        custody: LocalProjectPackageCustody,
        phase: ComponentCommandPhase,
        package_entrypoint: PackageEntrypoint | None = None,
        *,
        native_sdk_manifest: Path | None = None,
    ) -> tuple[str, ...]:
        plan = custody.root_plan
        contract = self._contract(plan.component_revision)
        entrypoint_contract = None
        if package_entrypoint is None:
            root_export = self._planned_exports[plan.component_revision.uri]
        else:
            try:
                root_export = self._exports_by_identity[
                    package_entrypoint.source_identity.uri
                ]
            except KeyError as exc:
                raise LocalStandardLifecycleError(
                    "packaged entrypoint has no realized export"
                ) from exc
            if contract.is_multi_entrypoint:
                entrypoint_contract = next(
                    (
                        item
                        for item in contract.entrypoint_command_contracts()
                        if item.artifact_export.export_id == root_export.export_id
                    ),
                    None,
                )
                if entrypoint_contract is None:
                    raise LocalStandardLifecycleError(
                        "packaged entrypoint has no locked runtime authority"
                    )
        root_path = custody.artifact_paths[root_export.identity.uri]
        if custody.python_dependency_observer is not None:
            artifact_root = root_path.parent.resolve(strict=True)
            expected_tree = custody.python_artifact_tree_identity
            if (
                expected_tree is None
                or local_tree_identity(artifact_root) != expected_tree
            ):
                raise LocalStandardLifecycleError(
                    "Packaged Python artifact differs from sealed package custody"
                )
            template = custody.python_dependency_observer
            observer = StandardPythonDependencyObserver(
                artifact_root,
                template.expected_identity,
                template.binding,
                template.source,
                template.toolchain,
            )
            observer()
            artifact_key = str(artifact_root)
            self._python_observers[artifact_key] = observer
            self._python_execution_trees[artifact_key] = expected_tree
        providers = self._intent_artifacts_for_plan(plan)
        return self._locked_argv(
            contract,
            phase,
            source_root=custody.root,
            object_root=custody.root,
            artifact_root=root_path.parent,
            export_path=root_path,
            providers=providers,
            entrypoint_contract=entrypoint_contract,
            native_sdk_manifest=native_sdk_manifest,
            provider_roots=tuple(
                str(
                    custody.artifact_paths[item.identity.uri].parent.resolve(
                        strict=True
                    )
                )
                for item in providers
            ),
        )

    def _packaged_service_argv(
        self,
        custody: LocalProjectPackageCustody,
        package_entrypoint: PackageEntrypoint | None = None,
    ) -> tuple[str, ...]:
        """Materialize the persistent-service serve launch command (issue #214).

        A persistent-service is packaged from the same runtime driver as a
        portable application, so its EXECUTE argv ends with the one-shot
        ``--litai-smoke`` dispatcher mode. Launching that verbatim runs a single
        smoke case and exits, so readiness polling can never observe a listening
        server. Swapping the trailing smoke mode for the framework-owned
        ``--litai-serve`` mode makes the generated artifact bind host/port and
        stay alive, without weakening acceptance: an artifact that ignores
        ``--litai-serve`` (or fails to bind) still exits or never answers, which
        the readiness probe and process check fail closed on.
        """

        argv = self._packaged_argv(
            custody, ComponentCommandPhase.EXECUTE, package_entrypoint
        )
        try:
            return direct_service_process_argv(argv)
        except ValueError as exc:
            raise LocalStandardLifecycleError(
                f"{exc}; cannot derive its serve launch"
            ) from exc

    def _run_packaged(
        self,
        custody: LocalProjectPackageCustody,
        phase: ComponentCommandPhase,
        *,
        extra_arguments: tuple[str, ...] = (),
        package_entrypoint: PackageEntrypoint | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._require_command_phase(phase)
        if (
            custody.native_sdk_resources is not None
            and custody.native_sdk_resources.inputs
        ):
            return self._run_packaged_sdk(
                custody,
                phase,
                extra_arguments=extra_arguments,
                package_entrypoint=package_entrypoint,
            )
        before = local_tree_identity(custody.root)
        command = (
            *self._packaged_argv(custody, phase, package_entrypoint),
            *extra_arguments,
        )
        environment = _child_process_environment(self._packaged_environment(custody))
        trace_subprocess(command, cwd=custody.root, environment=environment)
        completed = run_with_tree_kill(
            command,
            cwd=custody.root,
            env=environment,
            text=True,
            timeout=60,
        )
        trace_subprocess(
            command,
            cwd=custody.root,
            environment=environment,
            status=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            raise LocalStandardLifecycleError(
                f"packaged {phase.value} failed ({completed.returncode}): "
                f"{completed.stderr.strip()}"
            )
        if local_tree_identity(custody.root) != before:
            raise LocalStandardLifecycleError(
                f"packaged {phase.value} mutated immutable package custody"
            )
        return completed

    def _run_packaged_sdk(
        self,
        custody,
        phase,
        *,
        extra_arguments=(),
        package_entrypoint=None,
        strip_smoke=False,
    ):
        with self._packaged_sdk_command(
            custody,
            phase,
            extra_arguments=extra_arguments,
            package_entrypoint=package_entrypoint,
            strip_smoke=strip_smoke,
        ) as scope:
            completed = self._run_with_environment(
                scope.argv, cwd=custody.root, environment=dict(scope.environment)
            )
        completed.native_sdk_execution_identity = scope.evidence_identity
        return completed

    @contextmanager
    def _packaged_sdk_command(
        self,
        custody,
        phase,
        *,
        extra_arguments=(),
        package_entrypoint=None,
        serve=False,
        environment_overrides=(),
        library_oracle=None,
        harness=None,
        strip_smoke=False,
        cwd_override=None,
    ):
        from literate_ai.adapters.native_sdk_execution import isolated_sdk_environment
        from literate_ai.adapters.native_sdk_package_runtime import (
            prepare_packaged_native_sdk_execution,
        )

        plan = custody.root_plan
        contract = self._contract(plan.component_revision)
        selected = contract
        entrypoint_identity = None
        if contract.is_multi_entrypoint:
            if package_entrypoint is None:
                raise LocalStandardLifecycleError(
                    "packaged SDK launch requires an entrypoint"
                )
            export = self._exports_by_identity[package_entrypoint.source_identity.uri]
            selected = next(
                item
                for item in contract.entrypoint_command_contracts()
                if item.artifact_export.export_id == export.export_id
            )
            entrypoint_identity = selected.entrypoint_identity
        cwd = custody.root if cwd_override is None else Path(cwd_override)
        phase_value = phase.value
        if library_oracle is not None:
            from literate_ai.adapters.native_sdk_library import (
                library_acceptance_command,
            )

            command_identity = self._record_evidence(
                library_acceptance_command(contract, library_oracle)
            )
            binding = self.tool_bindings[
                contract.library_acceptance_toolchain_identity.uri
            ]
            if (
                harness is None
                or harness.read_bytes() != library_oracle.harness_content
            ):
                raise LocalStandardLifecycleError("library acceptance harness changed")
            cwd = harness.parent
            phase_value = "library-acceptance"
        else:
            command = selected.command(phase)
            if ComponentCommandRole.NATIVE_SDK_INPUTS not in command.roles:
                raise LocalStandardLifecycleError(
                    "packaged SDK command has no locked SDK input role"
                )
            command_identity = command.identity
            binding = self.tool_bindings[
                selected.tool_binding(phase).toolchain_identity.uri
            ]
        package = {
            "schema": "literate-ai/native-sdk-package-execution@1",
            "package_plan": custody.package_plan.identity.to_dict(),
            "package_result": custody.package_result.identity.to_dict(),
            "component_build_plan": plan.identity.to_dict(),
            "tree_identity": custody.tree_identity.to_dict(),
        }
        sdk_scope = custody.native_sdk_execution_scope
        if sdk_scope is not None:
            sdk_scope.require_package(custody.package_plan)
            package.update(
                schema="literate-ai/native-sdk-package-execution@2",
                execution_scope=sdk_scope.identity.to_dict(),
            )
        package_identity = canonical_identity(package)

        def require_current():
            self.project_package_custody(custody.package_plan, custody.package_result)
            if sdk_scope is not None:
                sdk_scope.require_package(custody.package_plan)
                sdk_scope.select_bindings(
                    custody.native_sdk_resources.bindings, plan.component_revision
                )

        def authorize(request, runtime):
            now = self.clock()
            return BuildAuthorization(
                authorization_id=f"local-packaged-sdk:{canonical_identity(request.to_dict()).digest}",
                classification_digest=runtime.uri,
                request_digest=canonical_identity(request.to_dict()).uri,
                effective_revision_digest=plan.component_revision.uri,
                actor="local-standard-lifecycle",
                reason="execute the verified package with fresh SDK runtime authority",
                profile=SecurityProfile.CONSTRAINED,
                privileges=request.requested_privileges,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )

        with prepare_packaged_native_sdk_execution(
            custody.native_sdk_resources,
            custody.root,
            plan.component_revision,
            target=contract.artifact_export.target_identity,
            parent=self.object_root,
            require_current=require_current,
            execution_scope=sdk_scope,
        ) as inputs:
            if tuple(
                sorted(
                    (item.binding.identity for item in inputs.values),
                    key=lambda item: item.uri,
                )
            ) != (
                plan.materialization.native_sdk_input_identities
                if sdk_scope is None
                else sdk_scope.input_identities
            ):
                raise LocalStandardLifecycleError(
                    "packaged SDK inputs differ from the build plan"
                )
            if library_oracle is not None:
                from literate_ai.adapters.lifecycle.standard_runtime import (
                    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
                )

                argv = (
                    *binding.command,
                    "-I",
                    "-B",
                    "-c",
                    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
                    str(inputs.manifest),
                    str(custody.root),
                    str(harness),
                    "file",
                    "harness",
                )
            else:
                argv = self._packaged_argv(
                    custody,
                    phase,
                    package_entrypoint,
                    native_sdk_manifest=inputs.manifest,
                )
                if serve:
                    argv = direct_service_process_argv(argv)
                elif strip_smoke:
                    if not argv or argv[-1] != LITAI_SMOKE_MODE_FLAG:
                        raise LocalStandardLifecycleError(
                            "packaged SDK product invocation has no smoke argument"
                        )
                    argv = argv[:-1]
            argv = (*argv, *extra_arguments)
            environment = inherited_verbose_environment(
                self._binding_environment(
                    isolated_sdk_environment(self._packaged_environment(custody)),
                    binding,
                )
            )
            environment.update(environment_overrides)
            if serve or library_oracle is not None:
                environment = _child_process_environment(environment)
            with inputs.command_scope(
                argv,
                tool=binding,
                cwd=cwd,
                environment=environment,
                source_identity=plan.request.source_tree_identity,
                phase=phase_value,
                command_identity=command_identity,
                command_contract_identity=contract.identity,
                entrypoint_identity=entrypoint_identity,
                package_identity=package_identity,
                authorize=authorize,
                clock=self.clock,
                record=self._record_evidence,
            ) as scope:
                try:
                    yield scope
                finally:
                    if library_oracle is not None and (
                        harness.read_bytes() != library_oracle.harness_content
                    ):
                        raise LocalStandardLifecycleError(
                            "library acceptance harness changed"
                        )
            self._record_evidence(package)
            if sdk_scope is not None:
                self._record_evidence(sdk_scope.to_dict())
                for owner_plan in sdk_scope.plans:
                    self._record_evidence(owner_plan.to_dict())
            self._record_evidence(_local_tree_document(custody.root))
            self._record_evidence(plan.to_dict())

    @contextmanager
    def _packaged_service_command(
        self, custody, package_entrypoint, *, arguments, environment
    ):
        from literate_ai.adapters.native_sdk_execution import NativeSdkCommandScope

        resources = getattr(custody, "native_sdk_resources", None)
        if resources is not None and resources.inputs:
            with self._packaged_sdk_command(
                custody,
                ComponentCommandPhase.EXECUTE,
                package_entrypoint=package_entrypoint,
                serve=True,
                extra_arguments=arguments,
                environment_overrides=environment,
            ) as scope:
                yield scope
            return
        command = (
            *self._packaged_service_argv(custody, package_entrypoint),
            *arguments,
        )
        child_environment = self._packaged_environment(custody)
        child_environment.update(environment)
        yield NativeSdkCommandScope(
            command, _child_process_environment(child_environment)
        )

    def test_root_integration(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
    ) -> ContentIdentity:
        self._require_command_phase(ComponentCommandPhase.TEST)
        del execution_plan, project_build_plan
        custody = self.project_package_custody(package_plan, package_result)
        root_revision = getattr(component_lock, "root_revision", None)
        root_contract = self.contracts.get(getattr(root_revision, "uri", ""))
        if (
            root_contract is not None
            and root_contract.is_library
            and root_contract.native_layout is not None
        ):
            return self._record_evidence(
                {
                    "schema": "literate-ai/packaged-library-root-integration@1",
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "package_tree_identity": custody.tree_identity.uri,
                    "generated_test_suite_identity": (
                        custody.generated_test_suite.content_identity
                    ),
                    "status": "independent-consumer-required",
                }
            )
        if len(package_plan.entrypoints) > 1:
            observations = []
            for entrypoint in package_plan.entrypoints:
                completed = self._run_packaged(
                    custody,
                    ComponentCommandPhase.TEST,
                    package_entrypoint=entrypoint,
                )
                self._parse_generated_test_observation(
                    completed.stdout, custody.generated_test_suite
                )
                observations.append(
                    {
                        "deployment_unit": entrypoint.deployment_unit,
                        "entrypoint_identity": entrypoint.source_identity.uri,
                        "observation_identity": self._process_observation(
                            completed,
                            phase=(
                                "packaged-root-generated-test:"
                                f"{entrypoint.deployment_unit or entrypoint.name}"
                            ),
                            plan_identity=package_plan.identity,
                        ).uri,
                    }
                )
            return self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-root-integration@1",
                    "package_plan_identity": package_plan.identity.uri,
                    "entrypoints": observations,
                }
            )
        completed = self._run_packaged(custody, ComponentCommandPhase.TEST)
        try:
            value = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LocalStandardLifecycleError(
                "packaged root integration test emitted invalid JSON"
            ) from exc
        cases = value.get("cases") if isinstance(value, dict) else None
        observed = {
            item.get("case_id")
            for item in cases or ()
            if isinstance(item, dict)
            and set(item) == {"case_id", "outcome"}
            and item.get("outcome") == "passed"
        }
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "cases"}
            or value.get("schema") != "literate-ai/generated-test-results@1"
            or not isinstance(cases, list)
            or len(observed) != len(cases)
            or observed != set(custody.generated_test_suite.case_ids)
        ):
            raise LocalStandardLifecycleError(
                "packaged root integration test did not pass every exact generated case"
            )
        return self._process_observation(
            completed,
            phase="packaged-root-generated-test",
            plan_identity=package_plan.identity,
        )

    def execute_packaged_project(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
    ) -> ContentIdentity:
        self._require_command_phase(ComponentCommandPhase.EXECUTE)
        del execution_plan, project_build_plan
        root_revision = getattr(component_lock, "root_revision", None)
        root_contract = self.contracts.get(getattr(root_revision, "uri", ""))
        if (
            root_contract is not None
            and root_contract.is_library
            and root_contract.native_layout is not None
        ):
            custody = self.project_package_custody(package_plan, package_result)
            return self._record_evidence(
                {
                    "schema": "literate-ai/packaged-library-execution@1",
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "package_tree_identity": custody.tree_identity.uri,
                    "status": "not-applicable-no-entrypoint",
                }
            )
        if len(package_plan.entrypoints) > 1:
            custody = self.project_package_custody(package_plan, package_result)
            observations = []
            stdout_by_unit: dict[str, str] = {}
            for entrypoint in package_plan.entrypoints:
                unit = entrypoint.deployment_unit or entrypoint.name
                if entrypoint.kind in {
                    NATIVE_CLI_ENTRYPOINT_KIND,
                    PERSISTENT_SERVICE_ENTRYPOINT_KIND,
                    WEB_APPLICATION_ENTRYPOINT_KIND,
                }:
                    observations.append(
                        {
                            "deployment_unit": unit,
                            "entrypoint_identity": entrypoint.source_identity.uri,
                            "owner": "independent-acceptance",
                            "status": "deferred",
                        }
                    )
                    continue
                completed = self._run_packaged(
                    custody,
                    ComponentCommandPhase.EXECUTE,
                    package_entrypoint=entrypoint,
                )
                if not completed.stdout.strip():
                    raise LocalStandardLifecycleError(
                        f"packaged entrypoint {unit!r} produced no observable output"
                    )
                stdout_by_unit[unit] = completed.stdout.strip()
                observations.append(
                    {
                        "deployment_unit": unit,
                        "entrypoint_identity": entrypoint.source_identity.uri,
                        "observation_identity": self._process_observation(
                            completed,
                            phase=f"packaged-project-execution:{unit}",
                            plan_identity=package_plan.identity,
                        ).uri,
                        "status": "executed",
                    }
                )
            self.project_execution_stdout[package_result.identity.uri] = json.dumps(
                stdout_by_unit, sort_keys=True, separators=(",", ":")
            )
            return self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-packaged-execution@1",
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "entrypoints": observations,
                }
            )
        if len(package_plan.entrypoints) == 1 and package_plan.entrypoints[0].kind in {
            NATIVE_CLI_ENTRYPOINT_KIND,
            PERSISTENT_SERVICE_ENTRYPOINT_KIND,
        }:
            entrypoint_kind = package_plan.entrypoints[0].kind
            return self._record_evidence(
                {
                    "schema": "literate-ai/packaged-execution-deferred@1",
                    "entrypoint_kind": entrypoint_kind,
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "owner": "independent-acceptance",
                }
            )
        custody = self.project_package_custody(package_plan, package_result)
        completed = self._run_packaged(custody, ComponentCommandPhase.EXECUTE)
        if not completed.stdout.strip():
            raise LocalStandardLifecycleError(
                "packaged application execution produced no observable output"
            )
        self.project_execution_stdout[package_result.identity.uri] = (
            completed.stdout.strip()
        )
        return self._process_observation(
            completed,
            phase="packaged-project-execution",
            plan_identity=package_plan.identity,
        )

    def _service_acceptance_identity(self, document, scope):
        if scope.evidence_identity is None:
            return self._record_evidence(document)
        document = {
            **document,
            "schema": document["schema"].removesuffix("@1") + "@2",
            "native_sdk_execution_identity": scope.evidence_identity.uri,
        }
        return self._record_evidence(document)

    def _accept_persistent_service(
        self,
        custody: LocalProjectPackageCustody,
        package_plan: PackagePlan,
        package_result: PackageResult,
        contract: object,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
        package_entrypoint: PackageEntrypoint | None = None,
    ) -> ContentIdentity:
        from literate_ai.adapters.component_acceptance import (
            PersistentServiceAcceptance,
        )

        if not isinstance(contract, PersistentServiceAcceptance):
            raise LocalStandardLifecycleError(
                "persistent-service contract is not typed"
            )
        contract_identity = contract.identity
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        def expand(value: str) -> str:
            return value.replace("{port}", str(port)).replace("{base_url}", base_url)

        with self._packaged_service_command(
            custody,
            package_entrypoint,
            arguments=tuple(expand(value) for value in contract.arguments),
            environment={name: expand(value) for name, value in contract.environment},
        ) as scope:
            command = scope.argv
            child_environment = dict(scope.environment)
            observations: list[dict[str, object]] = []
            started = time.monotonic()
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                ownership = create_process_tree_ownership()
                try:
                    trace_subprocess(
                        command, cwd=custody.root, environment=child_environment
                    )
                    process = subprocess.Popen(
                        command,
                        cwd=custody.root,
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        **ownership.popen_options,
                    )
                except OSError as exc:
                    ownership.release()
                    raise LocalStandardLifecycleError(
                        "persistent-service acceptance process could not start"
                    ) from exc
                ownership.bind(process.pid)
                monitor_stop = threading.Event()
                monitor_error: list[str] = []

                def monitor() -> None:
                    while not monitor_stop.wait(0.02):
                        if (
                            os.fstat(stdout.fileno()).st_size
                            > contract.stdout_limit_bytes
                        ):
                            monitor_error.append(
                                "persistent-service stdout exceeded its byte budget"
                            )
                        elif (
                            os.fstat(stderr.fileno()).st_size
                            > contract.stderr_limit_bytes
                        ):
                            monitor_error.append(
                                "persistent-service stderr exceeded its byte budget"
                            )
                        elif (
                            time.monotonic() - started
                            > contract.process_timeout_seconds
                        ):
                            monitor_error.append(
                                "persistent-service acceptance exceeded its "
                                "process deadline"
                            )
                        if monitor_error:
                            terminate_process_tree(
                                process,
                                environment=child_environment,
                                ownership=ownership,
                            )
                            return

                monitor_thread = threading.Thread(target=monitor, daemon=True)
                monitor_thread.start()
                try:
                    readiness_deadline = started + contract.startup_timeout_seconds
                    while True:
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="readiness",
                        )
                        remaining = readiness_deadline - time.monotonic()
                        if remaining <= 0:
                            raise LocalStandardLifecycleError(
                                "persistent-service readiness probe did not pass "
                                "before its deadline"
                            )
                        try:
                            self._observe_service_request(
                                contract.readiness,
                                base_url,
                                observations,
                                timeout_seconds=min(1.0, remaining),
                            )
                            break
                        except (
                            OSError,
                            urllib.error.URLError,
                            LocalStandardLifecycleError,
                        ) as exc:
                            if time.monotonic() >= readiness_deadline:
                                raise LocalStandardLifecycleError(
                                    "persistent-service readiness probe did not pass "
                                    "before its deadline"
                                ) from exc
                            time.sleep(0.05)
                    for request in contract.requests:
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="requests",
                        )
                        self._observe_service_request(request, base_url, observations)
                finally:
                    monitor_stop.set()
                    self._stop_service_process(
                        process,
                        contract.shutdown_timeout_seconds,
                        child_environment,
                        ownership,
                    )
                    ownership.release()
                    monitor_thread.join(timeout=1)
                    self._check_service_output(stdout, stderr, contract)
                if monitor_error:
                    raise LocalStandardLifecycleError(monitor_error[0])
        if contract.identity != contract_identity:
            raise LocalStandardLifecycleError(
                "persistent-service acceptance contract changed"
            )
        if local_tree_identity(custody.root) != custody.tree_identity:
            raise LocalStandardLifecycleError(
                "persistent-service acceptance mutated immutable package custody"
            )
        return self._service_acceptance_identity(
            {
                "schema": "literate-ai/local-persistent-service-acceptance@1",
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": root_integration_test_identity.uri,
                "packaged_execution_identity": packaged_execution_identity.uri,
                "contract_identity": contract.identity.uri,
                "observations": observations,
            },
            scope,
        )

    @staticmethod
    def _bounded_child_text(stream, limit: int) -> str:
        stream.flush()
        position = stream.tell()
        try:
            stream.seek(0)
            payload = stream.read(min(limit, _CHILD_DIAGNOSTIC_BYTES))
        finally:
            stream.seek(position)
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", "replace")
        else:
            text = str(payload)
        return _PRIVATE_CHILD_PATH.sub(
            "<private-path>", text.replace("\x00", "")
        ).strip()

    @staticmethod
    def _check_service_output(stdout, stderr, contract: object) -> None:
        stdout.flush()
        stderr.flush()
        if os.fstat(stdout.fileno()).st_size > contract.stdout_limit_bytes:
            raise LocalStandardLifecycleError(
                "persistent-service stdout exceeded its byte budget"
            )
        if os.fstat(stderr.fileno()).st_size > contract.stderr_limit_bytes:
            raise LocalStandardLifecycleError(
                "persistent-service stderr exceeded its byte budget"
            )

    def _check_service_process(
        self,
        process,
        stdout,
        stderr,
        contract: object,
        started: float,
        *,
        phase: str = "acceptance",
    ) -> None:
        self._check_service_output(stdout, stderr, contract)
        status = process.poll()
        if status is not None:
            stdout_text = self._bounded_child_text(stdout, contract.stdout_limit_bytes)
            stderr_text = self._bounded_child_text(stderr, contract.stderr_limit_bytes)
            contract_identity = getattr(
                getattr(contract, "identity", None), "uri", None
            )
            detail = stderr_text or stdout_text or "(no child output)"
            message = (
                "persistent-service exited before acceptance completed "
                f"(phase={phase}, status={status}"
                + (
                    f", contract={contract_identity}"
                    if isinstance(contract_identity, str)
                    else ""
                )
                + f"): {detail}"
            )
            raise PersistentServiceAcceptanceError(
                "lifecycle.persistent-service.exited",
                message,
                phase=phase,
                returncode=status,
                stdout=stdout_text,
                stderr=stderr_text,
                contract_identity=contract_identity
                if isinstance(contract_identity, str)
                else None,
            )
        if time.monotonic() - started > contract.process_timeout_seconds:
            raise LocalStandardLifecycleError(
                "persistent-service acceptance exceeded its process deadline"
            )

    @staticmethod
    def _stop_service_process(
        process,
        shutdown_timeout_seconds: float,
        environment: Mapping[str, str],
        ownership: ProcessTreeOwnership | None = None,
    ) -> None:
        try:
            if process.poll() is not None:
                return
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                # CREATE_NEW_PROCESS_GROUP is set at spawn. CTRL_BREAK is the
                # graceful Windows analogue of SIGTERM; process.terminate() is
                # TerminateProcess and cannot run a Python signal handler.
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
            process.wait(timeout=shutdown_timeout_seconds)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            # The root may exit while descendants keep serving or holding SDK
            # files. Always retire the owned group/job before releasing custody.
            terminate_process_tree(
                process, environment=environment, ownership=ownership
            )
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

    @staticmethod
    def _observe_service_request(
        probe: object,
        base_url: str,
        observations: list[dict[str, object]],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        from literate_ai.adapters.component_acceptance import ServiceHttpProbe

        if not isinstance(probe, ServiceHttpProbe):
            raise LocalStandardLifecycleError(
                "persistent-service HTTP probe is not typed"
            )
        outbound = urllib.request.Request(
            base_url + probe.path,
            data=probe.body,
            headers=dict(probe.headers),
            method=probe.method,
        )
        request_timeout = (
            probe.timeout_seconds
            if timeout_seconds is None
            else min(probe.timeout_seconds, timeout_seconds)
        )
        try:
            response = open_service_request(outbound, timeout=request_timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(probe.response_limit_bytes + 1)
            status = response.status
            headers = {
                name.casefold(): value for name, value in response.headers.items()
            }
        if len(body) > probe.response_limit_bytes:
            raise LocalStandardLifecycleError(
                "persistent-service HTTP response exceeded its byte budget"
            )
        if status != probe.expected_status:
            raise LocalStandardLifecycleError(
                f"persistent-service HTTP status differed: expected "
                f"{probe.expected_status}, observed {status}"
            )
        for name, expected in probe.expected_headers:
            if headers.get(name.casefold()) != expected:
                raise LocalStandardLifecycleError(
                    f"persistent-service HTTP header differed: {name}"
                )
        text: str | None = None
        if probe.expected_text_contains or probe.expected_sse_events:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LocalStandardLifecycleError(
                    "persistent-service HTTP response was not valid UTF-8"
                ) from exc
        for expected in probe.expected_text_contains:
            assert text is not None
            if expected not in text:
                raise LocalStandardLifecycleError(
                    "persistent-service HTTP response omitted expected text"
                )
        result_identity: str | None = None
        if probe.expected_json_present:
            try:
                result = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LocalStandardLifecycleError(
                    "persistent-service HTTP response was not valid JSON"
                ) from exc
            try:
                matches = product_json_values_equal(result, probe.expected_json)
            except ValueError as exc:
                raise LocalStandardLifecycleError(
                    "persistent-service HTTP response or expectation is not finite JSON"
                ) from exc
            if not matches:
                raise LocalStandardLifecycleError(
                    "persistent-service HTTP JSON response differed; "
                    f"expected={product_json_identity(probe.expected_json).uri}; "
                    f"actual={product_json_identity(result).uri}"
                )
            result_identity = product_json_identity(result).uri
        if probe.expected_sse_events:
            assert text is not None
            events = _parse_sse_events(text)
            expected_events = tuple(dict(item) for item in probe.expected_sse_events)
            if events[: len(expected_events)] != expected_events:
                raise LocalStandardLifecycleError(
                    "persistent-service SSE events differed from the expected prefix"
                )
        observations.append(
            {
                "request_identity": canonical_identity(
                    {
                        "method": probe.method,
                        "path": probe.path,
                        "headers": dict(probe.headers),
                        "body_identity": (
                            None
                            if probe.body is None
                            else f"sha256:{hashlib.sha256(probe.body).hexdigest()}"
                        ),
                    }
                ).uri,
                "status": status,
                "headers_identity": canonical_identity(headers).uri,
                "body_identity": f"sha256:{hashlib.sha256(body).hexdigest()}",
                "result_identity": result_identity,
            }
        )
        return body

    def _resolve_browser_driver(self) -> object:
        """Return the injected driver, or lazily build the Playwright adapter.

        Importing the Playwright adapter here (not at module top) keeps
        ``standard_local`` importable without Playwright installed; a missing
        engine surfaces later as a typed ``BrowserAcceptanceError`` when the
        driver actually launches, so acceptance fails closed rather than skips.
        """

        if self.browser_driver is not None:
            return self.browser_driver
        from literate_ai.adapters.browser_acceptance import PlaywrightPageDriver

        return PlaywrightPageDriver()

    def _accept_browser_frontend(
        self,
        custody: LocalProjectPackageCustody,
        package_plan: PackagePlan,
        package_result: PackageResult,
        contract: object,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
        package_entrypoint: PackageEntrypoint | None = None,
    ) -> ContentIdentity:
        """Launch a served frontend and drive it in a real browser (ADR-0028).

        The deployment unit is launched via the same ``--litai-serve`` path as a
        persistent service, but instead of text/HTTP probes a browser-tool-neutral
        driver opens the page at each declared viewport and reports what the
        browser observed; the pure decision in ``decide_browser_acceptance`` fails
        closed on any rejected observation. The evidence receipt binds viewport,
        interaction-contract identity, browser/tool identity, screenshots,
        captured console failures, and outcome.
        """

        from literate_ai.adapters.browser_acceptance import (
            BrowserAcceptanceError,
            decide_browser_acceptance,
        )
        from literate_ai.adapters.component_acceptance import (
            BrowserInteractionAcceptance,
        )

        if not isinstance(contract, BrowserInteractionAcceptance):
            raise LocalStandardLifecycleError(
                "browser-interaction contract is not typed"
            )
        driver = self._resolve_browser_driver()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        def expand(value: str) -> str:
            return value.replace("{port}", str(port)).replace("{base_url}", base_url)

        with self._packaged_service_command(
            custody,
            package_entrypoint,
            arguments=tuple(expand(value) for value in contract.arguments),
            environment={name: expand(value) for name, value in contract.environment},
        ) as scope:
            command = scope.argv
            child_environment = dict(scope.environment)
            viewport_evidence: list[dict[str, object]] = []
            started = time.monotonic()
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                ownership = create_process_tree_ownership()
                try:
                    trace_subprocess(
                        command, cwd=custody.root, environment=child_environment
                    )
                    process = subprocess.Popen(
                        command,
                        cwd=custody.root,
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        **ownership.popen_options,
                    )
                except OSError as exc:
                    ownership.release()
                    raise LocalStandardLifecycleError(
                        "browser-frontend acceptance process could not start"
                    ) from exc
                ownership.bind(process.pid)
                try:
                    readiness_deadline = started + contract.startup_timeout_seconds
                    while True:
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="readiness",
                        )
                        if self._frontend_is_ready(base_url + contract.readiness_path):
                            break
                        if time.monotonic() >= readiness_deadline:
                            raise LocalStandardLifecycleError(
                                "browser-frontend readiness probe did not pass before "
                                "its deadline"
                            )
                        time.sleep(0.05)
                    for viewport in contract.viewports:
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="browser",
                        )
                        try:
                            observation = driver.observe(base_url, viewport, contract)
                            decision = decide_browser_acceptance(
                                contract, viewport, observation
                            )
                        except BrowserAcceptanceError as exc:
                            raise LocalStandardLifecycleError(
                                f"{exc.code}: {exc.message}"
                            ) from exc
                        viewport_evidence.append(decision)
                finally:
                    self._stop_service_process(
                        process,
                        contract.shutdown_timeout_seconds,
                        child_environment,
                        ownership,
                    )
                    ownership.release()
                    self._check_service_output(stdout, stderr, contract)
        if local_tree_identity(custody.root) != custody.tree_identity:
            raise LocalStandardLifecycleError(
                "browser-frontend acceptance mutated immutable package custody"
            )
        tool_identity = driver.tool_identity
        if not isinstance(tool_identity, ContentIdentity):
            raise LocalStandardLifecycleError(
                "browser driver has no typed tool identity"
            )
        return self._service_acceptance_identity(
            {
                "schema": "literate-ai/local-browser-interaction-acceptance@1",
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": root_integration_test_identity.uri,
                "packaged_execution_identity": packaged_execution_identity.uri,
                "contract_identity": contract.identity.uri,
                "browser_tool_identity": tool_identity.uri,
                "viewport_observations": viewport_evidence,
            },
            scope,
        )

    @staticmethod
    def _frontend_is_ready(readiness_url: str) -> bool:
        try:
            with open_service_request(readiness_url, timeout=2) as response:
                response.read(1024)
                return response.status < 400
        except urllib.error.HTTPError as error:
            error.close()
            return False
        except (OSError, urllib.error.URLError):
            return False

    def _accept_ipc_surface_conformance(
        self,
        custody: LocalProjectPackageCustody,
        package_plan: PackagePlan,
        package_result: PackageResult,
        contract: object,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
        package_entrypoint: PackageEntrypoint | None = None,
    ) -> ContentIdentity:
        """Launch a served IPC surface, fetch its description, decide conformance.

        ADR-0029: an IPC surface is served by a ``persistent-service`` deployment
        unit launched via the same ``--litai-serve`` path as a plain service, but
        instead of asserting text/HTTP probes directly the verifier fetches the
        surface's served self-description and drives the declared request cases,
        then hands the observation to the protocol-neutral, engine-free decision in
        ``decide_ipc_surface_conformance``. That decision fails closed when the
        served description does not match the declared schema identity or a response
        did not validate against the declared schema. The evidence receipt binds the
        protocol tag, the declared-schema and served-description identities, the
        surface version + compatibility promise, and the per-case outcomes.
        """

        from literate_ai.adapters._grpc_oracle import GrpcReflectionProbe
        from literate_ai.adapters.component_acceptance import (
            IpcSurfaceConformanceAcceptance,
        )
        from literate_ai.adapters.ipc_surface_acceptance import (
            HttpIpcSurfaceProbe,
            IpcSurfaceAcceptanceError,
            IpcSurfaceProbe,
            decide_ipc_surface_conformance,
        )

        if not isinstance(contract, IpcSurfaceConformanceAcceptance):
            raise LocalStandardLifecycleError(
                "ipc-surface conformance contract is not typed"
            )
        probe = self.ipc_surface_probe
        if probe is None:
            if contract.protocol == "grpc" and isinstance(
                contract.description_probe, GrpcReflectionProbe
            ):
                from literate_ai.adapters.grpc_surface_acceptance import (
                    GrpcIpcSurfaceProbe,
                )

                try:
                    probe = GrpcIpcSurfaceProbe()
                except IpcSurfaceAcceptanceError as exc:
                    raise LocalStandardLifecycleError(
                        f"{exc.code}: {exc.message}"
                    ) from exc
            elif contract.protocol == "rest":
                probe = HttpIpcSurfaceProbe(fetch=self._http_get_body)
            else:
                raise LocalStandardLifecycleError(
                    "IPC conformance requires an explicit protocol probe "
                    "for this protocol"
                )
        protocol_readiness = (
            callable(getattr(probe, "is_ready", None))
            and getattr(type(probe), "is_ready", None) is not IpcSurfaceProbe.is_ready
        )
        if contract.protocol != "rest" and not protocol_readiness:
            raise LocalStandardLifecycleError(
                "non-REST IPC probe requires explicit protocol readiness"
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        def expand(value: str) -> str:
            return value.replace("{port}", str(port)).replace("{base_url}", base_url)

        with self._packaged_service_command(
            custody,
            package_entrypoint,
            arguments=tuple(expand(value) for value in contract.arguments),
            environment={name: expand(value) for name, value in contract.environment},
        ) as scope:
            command = scope.argv
            child_environment = dict(scope.environment)
            started = time.monotonic()
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                ownership = create_process_tree_ownership()
                try:
                    trace_subprocess(
                        command, cwd=custody.root, environment=child_environment
                    )
                    process = subprocess.Popen(
                        command,
                        cwd=custody.root,
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        **ownership.popen_options,
                    )
                except OSError as exc:
                    ownership.release()
                    raise LocalStandardLifecycleError(
                        "ipc-surface acceptance process could not start"
                    ) from exc
                ownership.bind(process.pid)
                try:
                    readiness_timeout = min(
                        contract.startup_timeout_seconds,
                        contract.process_timeout_seconds,
                    )
                    readiness_deadline = started + readiness_timeout
                    while True:
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="readiness",
                        )
                        remaining = min(
                            readiness_timeout,
                            readiness_deadline - time.monotonic(),
                        )
                        if remaining <= 0:
                            raise LocalStandardLifecycleError(
                                "ipc-surface readiness exceeded its deadline"
                            )
                        try:
                            if protocol_readiness:
                                ready = probe.is_ready(
                                    base_url, contract, timeout_seconds=remaining
                                )
                            else:
                                ready = (
                                    self._http_get_body(
                                        base_url,
                                        replace(
                                            contract.description_probe,
                                            timeout_seconds=min(
                                                contract.description_probe.timeout_seconds,
                                                remaining,
                                            ),
                                        ),
                                    )
                                    is not None
                                )
                        except IpcSurfaceAcceptanceError as exc:
                            raise LocalStandardLifecycleError(
                                f"{exc.code}: {exc.message}"
                            ) from exc
                        if type(ready) is not bool:
                            raise LocalStandardLifecycleError(
                                "ipc-surface readiness must return a boolean"
                            )
                        if time.monotonic() >= readiness_deadline:
                            raise LocalStandardLifecycleError(
                                "ipc-surface readiness exceeded its deadline"
                            )
                        if ready:
                            break
                        time.sleep(
                            max(0.0, min(0.05, readiness_deadline - time.monotonic()))
                        )
                    self._check_service_process(
                        process,
                        stdout,
                        stderr,
                        contract,
                        started,
                        phase="ipc",
                    )
                    try:
                        remaining = contract.process_timeout_seconds - (
                            time.monotonic() - started
                        )
                        if remaining <= 0:
                            raise LocalStandardLifecycleError(
                                "persistent-service acceptance exceeded "
                                "its process deadline"
                            )
                        bounded_observe = getattr(probe, "observe_with_timeout", None)
                        if callable(bounded_observe):
                            observation = bounded_observe(
                                base_url, contract, timeout_seconds=remaining
                            )
                        else:
                            observation = probe.observe(base_url, contract)
                        self._check_service_process(
                            process,
                            stdout,
                            stderr,
                            contract,
                            started,
                            phase="ipc",
                        )
                        decision = decide_ipc_surface_conformance(contract, observation)
                    except IpcSurfaceAcceptanceError as exc:
                        raise LocalStandardLifecycleError(
                            f"{exc.code}: {exc.message}"
                        ) from exc
                finally:
                    self._stop_service_process(
                        process,
                        contract.shutdown_timeout_seconds,
                        child_environment,
                        ownership,
                    )
                    ownership.release()
                    self._check_service_output(stdout, stderr, contract)
        if local_tree_identity(custody.root) != custody.tree_identity:
            raise LocalStandardLifecycleError(
                "ipc-surface acceptance mutated immutable package custody"
            )
        tool_identity = probe.tool_identity
        if not isinstance(tool_identity, ContentIdentity):
            raise LocalStandardLifecycleError(
                "ipc-surface probe has no typed tool identity"
            )
        native_observation = {}
        if isinstance(contract.description_probe, GrpcReflectionProbe):
            description_hex = observation.served_description_bytes.hex()
            native_observation = {
                "native_observation": {
                    "served_description_hex": description_hex,
                    "protocol_detail": observation.protocol_detail,
                }
            }
        return self._service_acceptance_identity(
            {
                **native_observation,
                "schema": "literate-ai/local-ipc-surface-conformance-acceptance@1",
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": root_integration_test_identity.uri,
                "packaged_execution_identity": packaged_execution_identity.uri,
                "contract_identity": contract.identity.uri,
                "ipc_surface_probe_identity": tool_identity.uri,
                "conformance": decision,
            },
            scope,
        )

    @staticmethod
    def _http_get_body(base_url: str, probe: object) -> bytes | None:
        """Return bytes only after the declared HTTP expectations pass.

        An unavailable connection remains a readiness observation. A served
        response that violates the verifier's contract fails closed through the
        same bounded checks used by persistent-service acceptance.
        """
        try:
            return LocalStandardLifecyclePorts._observe_service_request(
                probe, base_url, []
            )
        except (OSError, urllib.error.URLError):
            return None

    @staticmethod
    def _native_cli_tree(
        root: Path, *, maximum_bytes: int
    ) -> dict[str, dict[str, object]]:
        """Snapshot one bounded regular-file/directory case tree without links."""

        observations: dict[str, dict[str, object]] = {}
        pending = [root]
        total = 0
        while pending:
            parent = pending.pop()
            try:
                children = sorted(parent.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise LocalStandardLifecycleError(
                    "native-cli acceptance could not inspect its case tree"
                ) from exc
            for child in children:
                relative = child.relative_to(root).as_posix()
                if len(observations) >= 1024 or path_is_link_or_reparse(child):
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance case tree contains a link/reparse "
                        "point or too many entries"
                    )
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance case tree changed during inspection"
                    ) from exc
                mode = stat.S_IMODE(metadata.st_mode)
                if child.is_dir():
                    observations[relative] = {
                        "kind": "directory",
                        "mode": mode,
                        "size_bytes": 0,
                        "content_identity": None,
                    }
                    pending.append(child)
                    continue
                if not child.is_file():
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance case tree contains a non-file entry"
                    )
                size = metadata.st_size
                total += size
                if total > maximum_bytes or size > maximum_bytes:
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance filesystem exceeded its byte budget"
                    )
                content = child.read_bytes()
                if len(content) != size:
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance file changed during inspection"
                    )
                observations[relative] = {
                    "kind": "file",
                    "mode": mode,
                    "size_bytes": size,
                    "content_identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
        return dict(sorted(observations.items()))

    @staticmethod
    def _require_native_cli_content(
        actual: bytes, expected: object, *, label: str
    ) -> None:
        exact = getattr(expected, "exact", None)
        contains = getattr(expected, "contains", ())
        not_contains = getattr(expected, "not_contains", ())
        if exact is not None:
            if actual != exact:
                raise LocalStandardLifecycleError(
                    f"native-cli acceptance {label} exact bytes differed"
                )
            return
        try:
            text = actual.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalStandardLifecycleError(
                f"native-cli acceptance {label} was not valid UTF-8"
            ) from exc
        if any(item not in text for item in contains) or any(
            item in text for item in not_contains
        ):
            raise LocalStandardLifecycleError(
                f"native-cli acceptance {label} predicate failed"
            )

    def _accept_native_cli(
        self,
        custody: LocalProjectPackageCustody,
        package_plan: PackagePlan,
        package_result: PackageResult,
        contract: object,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
        package_entrypoint: PackageEntrypoint | None,
    ) -> ContentIdentity:
        from literate_ai.adapters.component_acceptance import NativeCliAcceptance

        if not isinstance(contract, NativeCliAcceptance):
            raise LocalStandardLifecycleError(
                "native-cli acceptance requires a verifier-owned contract"
            )
        selected = package_entrypoint
        if selected is None:
            matching = tuple(
                item
                for item in package_plan.entrypoints
                if item.name == contract.entrypoint.name
                and item.kind == NATIVE_CLI_ENTRYPOINT_KIND
            )
            if len(matching) != 1:
                raise LocalStandardLifecycleError(
                    "native-cli acceptance could not select one packaged entrypoint"
                )
            selected = matching[0]
        if (
            selected.name != contract.entrypoint.name
            or selected.kind != NATIVE_CLI_ENTRYPOINT_KIND
            or package_result.entrypoints != package_plan.entrypoints
            or package_plan.target_identity != contract.target_identity
        ):
            raise LocalStandardLifecycleError(
                "native-cli package entrypoint or target differs from its contract"
            )
        try:
            executable = custody.artifact_paths[selected.source_identity.uri]
        except KeyError as exc:
            raise LocalStandardLifecycleError(
                "native-cli entrypoint has no packaged artifact custody"
            ) from exc
        if (
            path_is_link_or_reparse(executable)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise LocalStandardLifecycleError(
                "native-cli packaged entrypoint is not one executable regular file"
            )
        executable_bytes = executable.read_bytes()
        executable_identity = "sha256:" + hashlib.sha256(executable_bytes).hexdigest()
        observations: list[dict[str, object]] = []
        sdk_resources = custody.native_sdk_resources
        sdk_execution = sdk_resources is not None and bool(sdk_resources.inputs)
        base_command = None
        base_environment = None
        if not sdk_execution:
            base_command = self._packaged_argv(
                custody, ComponentCommandPhase.EXECUTE, selected
            )
            if base_command and base_command[-1] == LITAI_SMOKE_MODE_FLAG:
                base_command = base_command[:-1]
            if not base_command:
                raise LocalStandardLifecycleError(
                    "native-cli packaged command is empty"
                )
            base_environment = _child_process_environment(
                self._packaged_environment(custody)
            )
            base_environment.update(dict(contract.environment))

        for case in contract.cases:
            with tempfile.TemporaryDirectory(
                prefix="litai-native-cli-acceptance-", dir=self.object_root
            ) as temporary:
                work = Path(temporary)
                for fixture in case.fixtures:
                    destination = work.joinpath(*PurePosixPath(fixture.path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(fixture.content)
                    destination.chmod(fixture.mode)
                before = self._native_cli_tree(
                    work, maximum_bytes=contract.filesystem_limit_bytes
                )

                def expand(argument: str, *, case_root: Path = work) -> str:
                    value = argument.replace("{work}", str(case_root))

                    def replace_fixture(match: re.Match[str]) -> str:
                        relative = match.group(1)
                        return str(case_root.joinpath(*PurePosixPath(relative).parts))

                    return re.sub(r"\{file:([^{}]+)\}", replace_fixture, value)

                arguments = tuple(expand(item) for item in case.arguments)
                with ExitStack() as stack:
                    if sdk_execution:
                        scope = stack.enter_context(
                            self._packaged_sdk_command(
                                custody,
                                ComponentCommandPhase.EXECUTE,
                                package_entrypoint=selected,
                                strip_smoke=True,
                                extra_arguments=arguments,
                                environment_overrides=dict(contract.environment),
                                cwd_override=work,
                            )
                        )
                        command = scope.argv
                        environment = dict(scope.environment)
                    else:
                        assert base_command is not None
                        assert base_environment is not None
                        command = (*base_command, *arguments)
                        environment = dict(base_environment)
                    stdin_file = stack.enter_context(tempfile.TemporaryFile())
                    stdout_file = stack.enter_context(tempfile.TemporaryFile())
                    stderr_file = stack.enter_context(tempfile.TemporaryFile())
                    if case.stdin is not None:
                        stdin_file.write(case.stdin)
                    stdin_file.seek(0)
                    try:
                        completed = run_with_tree_kill(
                            command,
                            cwd=work,
                            env=environment,
                            stdin=stdin_file,
                            stdout=stdout_file,
                            stderr=stderr_file,
                            timeout=case.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise LocalStandardLifecycleError(
                            f"native-cli acceptance timed out for {case.case_id!r}"
                        ) from exc
                    stdout_size = stdout_file.tell()
                    stderr_size = stderr_file.tell()
                    if (
                        stdout_size > contract.stdout_limit_bytes
                        or stderr_size > contract.stderr_limit_bytes
                    ):
                        raise LocalStandardLifecycleError(
                            "native-cli acceptance stream exceeded its byte budget"
                        )
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read()
                    stderr = stderr_file.read()
                expected_exit = case.expected_exit_code
                exit_matches = (
                    completed.returncode != 0
                    if expected_exit == "nonzero"
                    else completed.returncode == expected_exit
                )
                if not exit_matches:
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance exit status differed for "
                        f"{case.case_id!r}: expected {expected_exit}, observed "
                        f"{completed.returncode}"
                    )
                self._require_native_cli_content(
                    stdout, case.stdout, label=f"stdout for {case.case_id!r}"
                )
                self._require_native_cli_content(
                    stderr, case.stderr, label=f"stderr for {case.case_id!r}"
                )
                after = self._native_cli_tree(
                    work, maximum_bytes=contract.filesystem_limit_bytes
                )
                if any(after.get(path) != value for path, value in before.items()):
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance changed or removed an input fixture"
                    )
                created = {
                    path: value for path, value in after.items() if path not in before
                }
                expected_paths = {item.path for item in case.filesystem}
                if set(created) != expected_paths:
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance filesystem delta differed from the "
                        "complete declared projection"
                    )
                for expected in case.filesystem:
                    observed = created[expected.path]
                    if (
                        observed["kind"] != expected.kind
                        or observed["mode"] != expected.mode
                        or observed["size_bytes"] > expected.maximum_bytes
                    ):
                        raise LocalStandardLifecycleError(
                            "native-cli acceptance filesystem metadata differed for "
                            f"{expected.path!r}"
                        )
                    if expected.kind == "file":
                        content = work.joinpath(
                            *PurePosixPath(expected.path).parts
                        ).read_bytes()
                        assert expected.content is not None
                        self._require_native_cli_content(
                            content,
                            expected.content,
                            label=f"file {expected.path!r}",
                        )
                if (
                    local_tree_identity(custody.root) != custody.tree_identity
                    or executable.read_bytes() != executable_bytes
                ):
                    raise LocalStandardLifecycleError(
                        "native-cli acceptance changed immutable package custody"
                    )
                observations.append(
                    {
                        "case_identity": case.identity.uri,
                        "exit_code": completed.returncode,
                        "stdout_identity": "sha256:"
                        + hashlib.sha256(stdout).hexdigest(),
                        "stderr_identity": "sha256:"
                        + hashlib.sha256(stderr).hexdigest(),
                        "filesystem": created,
                    }
                )
        return self._record_evidence(
            {
                "schema": "literate-ai/local-native-cli-acceptance@2",
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": root_integration_test_identity.uri,
                "packaged_execution_identity": packaged_execution_identity.uri,
                "contract_identity": contract.identity.uri,
                "entrypoint_identity": contract.entrypoint_identity.uri,
                "packaged_executable_identity": executable_identity,
                "target_identity": contract.target_identity.uri,
                "observations": observations,
            }
        )

    def accept_project_independently(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
        *,
        _selected_entrypoint: PackageEntrypoint | None = None,
        _selected_oracle: object | None = None,
    ) -> ContentIdentity:
        self._require_full_command_scope()
        from literate_ai.adapters.component_acceptance import (
            INDEPENDENT_ACCEPTANCE_EXEMPT_SCHEMA,
            ComponentAcceptanceError,
            ComponentAcceptanceOracleBundle,
            LintRenderAcceptance,
        )

        root_revision = getattr(component_lock, "root_revision", None)
        root_contract = self.contracts.get(getattr(root_revision, "uri", ""))
        if (
            _selected_entrypoint is None
            and root_contract is not None
            and root_contract.is_library
        ):
            from literate_ai.adapters.component_acceptance import LibraryAcceptance

            oracle = self.independent_acceptance_oracle
            if not isinstance(oracle, LibraryAcceptance):
                raise LocalStandardLifecycleError(
                    "importable library acceptance requires a verifier-owned "
                    "library oracle"
                )
            custody = self.project_package_custody(package_plan, package_result)
            return self._accept_library(
                component_lock,
                custody,
                package_plan,
                package_result,
                oracle,
                root_integration_test_identity,
                packaged_execution_identity,
            )
        if _selected_entrypoint is None and len(package_plan.entrypoints) > 1:
            bundle = self.independent_acceptance_oracle
            if not isinstance(bundle, ComponentAcceptanceOracleBundle):
                raise LocalStandardLifecycleError(
                    "multi-entrypoint acceptance requires a verifier-owned "
                    "oracle bundle"
                )
            observations = []
            for entrypoint in package_plan.entrypoints:
                unit = entrypoint.deployment_unit
                if unit is None:
                    raise LocalStandardLifecycleError(
                        "multi-entrypoint package lost its deployment-unit binding"
                    )
                try:
                    binding = bundle.for_deployment_unit(unit)
                except ComponentAcceptanceError as exc:
                    raise LocalStandardLifecycleError(
                        f"{exc.code}: {exc.message}"
                    ) from exc
                if (
                    binding.entrypoint_name != entrypoint.name
                    or binding.entrypoint_kind != entrypoint.kind
                ):
                    raise LocalStandardLifecycleError(
                        "packaged entrypoint differs from its verifier-owned binding"
                    )
                observation = self.accept_project_independently(
                    component_lock,
                    execution_plan,
                    project_build_plan,
                    package_plan,
                    package_result,
                    root_integration_test_identity,
                    packaged_execution_identity,
                    _selected_entrypoint=entrypoint,
                    _selected_oracle=binding.oracle,
                )
                observations.append(
                    {
                        "deployment_unit": unit,
                        "entrypoint_identity": entrypoint.source_identity.uri,
                        "oracle_binding_identity": binding.identity.uri,
                        "observation_identity": observation.uri,
                    }
                )
            return self._record_evidence(
                {
                    "schema": "literate-ai/multi-entrypoint-independent-acceptance@1",
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "oracle_bundle_identity": bundle.identity.uri,
                    "entrypoints": observations,
                }
            )
        entrypoints = (
            package_plan.entrypoints
            if _selected_entrypoint is None
            else (_selected_entrypoint,)
        )
        oracle = (
            self.independent_acceptance_oracle
            if _selected_entrypoint is None
            else _selected_oracle
        )
        if isinstance(oracle, LintRenderAcceptance):
            try:
                observation = oracle.accept(component_lock)
            except ComponentAcceptanceError as exc:
                raise LocalStandardLifecycleError(f"{exc.code}: {exc.message}") from exc
            return self._record_evidence(
                {
                    "schema": (
                        "literate-ai/local-independent-lint-render-acceptance@1"
                    ),
                    "package_plan_identity": package_plan.identity.uri,
                    "package_result_identity": package_result.identity.uri,
                    "root_integration_test_identity": (
                        root_integration_test_identity.uri
                    ),
                    "packaged_execution_identity": packaged_execution_identity.uri,
                    "oracle_identity": oracle.identity.uri,
                    "observation_identity": observation.uri,
                }
            )
        if not entrypoints:
            # A library-only Component (#114) has no single-shot invocation for a
            # CLI JSON oracle. Absent a lint-and-render snapshot contract it stays
            # exempt, same as the non-portable-application case below (#32).
            return self._record_evidence(
                {
                    "schema": INDEPENDENT_ACCEPTANCE_EXEMPT_SCHEMA,
                    "entrypoint_kind": None,
                    "package_plan_identity": package_plan.identity.uri,
                }
            )
        entrypoint_kind = entrypoints[0].kind
        if entrypoint_kind == PERSISTENT_SERVICE_ENTRYPOINT_KIND:
            from literate_ai.adapters.component_acceptance import (
                IpcSurfaceConformanceAcceptance,
                PersistentServiceAcceptance,
            )

            contract = oracle
            # ADR-0029: a persistent-service may declare an IPC-surface conformance
            # oracle instead of the plain HTTP probe -- same entrypoint kind, same
            # --litai-serve launch, but the served self-description is fetched and
            # the responses validated against the declared schema.
            if isinstance(contract, IpcSurfaceConformanceAcceptance):
                contract.require_current(component_lock)
                custody = self.project_package_custody(package_plan, package_result)
                return self._accept_ipc_surface_conformance(
                    custody,
                    package_plan,
                    package_result,
                    contract,
                    root_integration_test_identity,
                    packaged_execution_identity,
                    _selected_entrypoint,
                )
            if not isinstance(contract, PersistentServiceAcceptance):
                raise LocalStandardLifecycleError(
                    "persistent-service acceptance requires a verifier-owned contract"
                )
            contract.require_current(component_lock)
            custody = self.project_package_custody(package_plan, package_result)
            return self._accept_persistent_service(
                custody,
                package_plan,
                package_result,
                contract,
                root_integration_test_identity,
                packaged_execution_identity,
                _selected_entrypoint,
            )
        if entrypoint_kind == WEB_APPLICATION_ENTRYPOINT_KIND:
            from literate_ai.adapters.component_acceptance import (
                BrowserInteractionAcceptance,
            )

            contract = oracle
            if not isinstance(contract, BrowserInteractionAcceptance):
                raise LocalStandardLifecycleError(
                    "browser-interaction acceptance requires a verifier-owned contract"
                )
            contract.require_current(component_lock)
            custody = self.project_package_custody(package_plan, package_result)
            return self._accept_browser_frontend(
                custody,
                package_plan,
                package_result,
                contract,
                root_integration_test_identity,
                packaged_execution_identity,
                _selected_entrypoint,
            )
        if entrypoint_kind == NATIVE_CLI_ENTRYPOINT_KIND:
            from literate_ai.adapters.component_acceptance import NativeCliAcceptance

            if not isinstance(oracle, NativeCliAcceptance):
                raise LocalStandardLifecycleError(
                    "native-cli acceptance requires a verifier-owned contract"
                )
            try:
                oracle.require_current(component_lock)
            except ComponentAcceptanceError as exc:
                raise LocalStandardLifecycleError(f"{exc.code}: {exc.message}") from exc
            custody = self.project_package_custody(package_plan, package_result)
            return self._accept_native_cli(
                custody,
                package_plan,
                package_result,
                oracle,
                root_integration_test_identity,
                packaged_execution_identity,
                _selected_entrypoint,
            )
        if entrypoint_kind != PORTABLE_APPLICATION_ENTRYPOINT_KIND:
            # Entrypoint kinds without defined verifier interaction semantics remain
            # explicitly exempt rather than inheriting the portable CLI contract.
            return self._record_evidence(
                {
                    "schema": INDEPENDENT_ACCEPTANCE_EXEMPT_SCHEMA,
                    "entrypoint_kind": entrypoint_kind,
                    "package_plan_identity": package_plan.identity.uri,
                }
            )
        custody = self.project_package_custody(package_plan, package_result)
        if oracle is None:
            raise LocalStandardLifecycleError(
                "independent project acceptance requires a verifier-owned oracle"
            )
        oracle_identity = oracle.identity
        if not isinstance(oracle_identity, ContentIdentity):
            raise LocalStandardLifecycleError(
                "independent acceptance oracle has no typed identity"
            )
        cases = oracle.cases(component_lock)
        if (
            not cases
            or any(
                not isinstance(item, LocalIndependentAcceptanceCase) for item in cases
            )
            or len({item.case_id for item in cases}) != len(cases)
        ):
            raise LocalStandardLifecycleError(
                "independent acceptance oracle must return unique typed cases"
            )
        if oracle.identity != oracle_identity:
            raise LocalStandardLifecycleError(
                "independent acceptance oracle identity changed during resolution"
            )
        sdk_resources = custody.native_sdk_resources
        sdk_execution = sdk_resources is not None and bool(sdk_resources.inputs)
        command = None
        if not sdk_execution:
            command = self._packaged_argv(
                custody, ComponentCommandPhase.EXECUTE, _selected_entrypoint
            )
            if command and command[-1] == LITAI_SMOKE_MODE_FLAG:
                command = command[:-1]
        observations = []
        for case in cases:
            if sdk_execution:
                completed = self._run_packaged_sdk(
                    custody,
                    ComponentCommandPhase.EXECUTE,
                    extra_arguments=(case.arguments_document.decode("utf-8"),),
                    package_entrypoint=_selected_entrypoint,
                    strip_smoke=True,
                )
            else:
                assert command is not None
                completed = run_with_tree_kill(
                    (
                        *command,
                        case.arguments_document.decode("utf-8"),
                    ),
                    cwd=custody.root,
                    env=_child_process_environment(self._packaged_environment(custody)),
                    text=True,
                    timeout=60,
                )
            if completed.returncode != 0:
                raise LocalStandardLifecycleError(
                    "independent packaged acceptance invocation failed: "
                    + completed.stderr.strip()
                )
            if local_tree_identity(custody.root) != custody.tree_identity:
                raise LocalStandardLifecycleError(
                    "independent acceptance mutated immutable package custody"
                )
            try:
                result = json.loads(completed.stdout)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LocalStandardLifecycleError(
                    "independent acceptance did not emit one JSON result"
                ) from exc
            if not product_json_values_equal(
                result, json.loads(case.expected_result_document)
            ):
                expected = json.loads(case.expected_result_document)
                raise LocalStandardLifecycleError(
                    f"independent acceptance result differs for {case.case_id!r}; "
                    f"expected={self._record_product_evidence(expected).uri}; "
                    f"actual={self._record_product_evidence(result).uri}; "
                    f"oracle={oracle_identity.uri}"
                )
            observation = {
                "case_id": case.case_id,
                "case_identity": case.identity.uri,
                "stdout_identity": self._record_evidence(completed.stdout).uri,
                "stderr_identity": self._record_evidence(completed.stderr).uri,
                "result_identity": self._record_product_evidence(result).uri,
            }
            native_execution_identity = getattr(
                completed, "native_sdk_execution_identity", None
            )
            if native_execution_identity is not None:
                observation["native_sdk_execution_identity"] = (
                    native_execution_identity.uri
                )
            observations.append(observation)
        return self._record_evidence(
            {
                "schema": "literate-ai/local-independent-project-acceptance@1",
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": (root_integration_test_identity.uri),
                "packaged_execution_identity": packaged_execution_identity.uri,
                "oracle_identity": oracle_identity.uri,
                "observations": observations,
            }
        )

    def _accept_library(
        self,
        component_lock: ComponentLock,
        custody: LocalProjectPackageCustody,
        package_plan: PackagePlan,
        package_result: PackageResult,
        oracle: object,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
    ) -> ContentIdentity:
        """Run one verifier-owned in-language harness against the sealed package."""

        from literate_ai.adapters.component_acceptance import LibraryAcceptance

        if not isinstance(oracle, LibraryAcceptance):
            raise TypeError("oracle must be a LibraryAcceptance")
        # Revalidate mutable product values before staging or invoking the harness.
        oracle_identity = oracle.identity
        if self._evidence_recorder is not None:
            recorded_harness = self._evidence_recorder.remember_bytes(
                oracle.harness_content
            )
            if recorded_harness != oracle.harness_identity:
                raise LocalStandardLifecycleError(
                    "library acceptance harness differs from its bound identity"
                )
            self._record_product_evidence(oracle.identity_document())
        root_node = next(
            item
            for item in component_lock.nodes
            if item.revision.identity == component_lock.root_revision
        )
        contract = self._contract(component_lock.root_revision)
        surface = contract.library_import_surface
        assert surface is not None
        if self._evidence_recorder is not None:
            self._record_evidence(surface.to_dict())
        expected_interfaces = tuple(
            sorted(
                (item.identity for item in root_node.revision.public_interfaces),
                key=lambda item: item.uri,
            )
        )
        if (
            oracle.component != root_node.revision.coordinate.name
            or oracle.specification_set_identity
            != root_node.revision.specification_set_identity
            or oracle.public_interface_identities != expected_interfaces
            or oracle.import_surface_identity != surface.identity
            or oracle.language != surface.language
        ):
            raise LocalStandardLifecycleError(
                "library acceptance oracle differs from exact locked authority"
            )
        capabilities = {item.capability for item in surface.capabilities}
        if any(item.capability not in capabilities for item in oracle.cases):
            raise LocalStandardLifecycleError(
                "library acceptance oracle names an undeclared capability"
            )
        root_export = self._planned_exports[component_lock.root_revision.uri]
        try:
            export = custody.artifact_paths[root_export.identity.uri].resolve(
                strict=True
            )
        except (KeyError, OSError) as exc:
            raise LocalStandardLifecycleError(
                "library acceptance package custody is unavailable"
            ) from exc
        if not export.is_dir():
            raise LocalStandardLifecycleError(
                "library acceptance requires a directory package export"
            )
        cases_document = product_json_bytes(
            [
                {
                    "case_id": item.case_id,
                    "capability": item.capability,
                    "arguments": item.arguments,
                }
                for item in oracle.cases
            ]
        ).decode("utf-8")
        surface_document = canonical_json_bytes(surface.to_dict()).decode("utf-8")
        suffix = {"python": ".py", "javascript": ".js", "rust": ".rs", "cpp": ".cpp"}[
            oracle.language
        ]
        acceptance_toolchain = contract.library_acceptance_toolchain_identity
        assert acceptance_toolchain is not None
        binding = self.tool_bindings[acceptance_toolchain.uri]
        binding.require_unchanged()
        environment = _child_process_environment({})
        environment.pop("PYTHONPATH", None)
        environment.pop("NODE_PATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        temporary = Path(tempfile.mkdtemp(prefix="litai-library-acceptance-"))
        try:
            harness = temporary / f"harness{suffix}"
            harness.write_bytes(oracle.harness_content)
            if oracle.language == "cpp":
                from .cpp_acceptance import CppAcceptanceError, compile_cpp_verifier

                environment = self._binding_environment(environment, binding)
                executable = temporary / (
                    "verifier.exe" if os.name == "nt" else "verifier"
                )
                try:
                    environment = compile_cpp_verifier(
                        compiler=binding.command,
                        environment=environment,
                        export=export,
                        layout=contract.native_layout,
                        harness=harness,
                        executable=executable,
                    )
                except CppAcceptanceError as exc:
                    raise LocalStandardLifecycleError(str(exc)) from exc
                binding.require_unchanged()
                command = (
                    str(executable),
                    str(export),
                    surface_document,
                    cases_document,
                )
            elif oracle.language == "rust":
                targets = getattr(self, "cargo_targets", {})
                target = targets.get(component_lock.root_revision.uri)
                if target is None or not target.library:
                    raise LocalStandardLifecycleError(
                        "Rust library acceptance requires exact Cargo authority"
                    )
                manifest = export.joinpath(*PurePosixPath(target.manifest).parts)
                try:
                    package_name = tomllib.loads(manifest.read_text(encoding="utf-8"))[
                        "package"
                    ]["name"]
                except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
                    raise LocalStandardLifecycleError(
                        "Rust library package manifest is invalid"
                    ) from exc
                (temporary / "src").mkdir()
                harness.replace(temporary / "src/main.rs")
                (temporary / "Cargo.toml").write_text(
                    '[package]\nname="litai-library-acceptance"\nversion="0.0.0"\n'
                    'edition="2021"\n[dependencies]\n'
                    + surface.package
                    + "={package="
                    + json.dumps(package_name)
                    + ",path="
                    + json.dumps(str(manifest.parent))
                    + "}\n",
                    encoding="utf-8",
                )
                command = (
                    *binding.command,
                    "run",
                    "--quiet",
                    "--offline",
                    "--manifest-path",
                    str(temporary / "Cargo.toml"),
                    "--target-dir",
                    str(temporary / "target"),
                    "--",
                    str(export),
                    surface_document,
                    cases_document,
                )
            else:
                command = (
                    *binding.command,
                    str(harness),
                    str(export),
                    surface_document,
                    cases_document,
                )
            resources = getattr(custody, "native_sdk_resources", None)
            if resources is not None and resources.inputs:
                with self._packaged_sdk_command(
                    custody,
                    ComponentCommandPhase.EXECUTE,
                    library_oracle=oracle,
                    harness=harness,
                    extra_arguments=(str(export), surface_document, cases_document),
                ) as scope:
                    completed = run_with_tree_kill(
                        scope.argv,
                        cwd=temporary,
                        env=dict(scope.environment),
                        text=True,
                        timeout=60,
                    )
                completed.native_sdk_execution_identity = scope.evidence_identity
            else:
                completed = run_with_tree_kill(
                    command,
                    cwd=temporary,
                    env=environment,
                    text=True,
                    timeout=60,
                )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        binding.require_unchanged()
        if completed.returncode != 0:
            raise LocalStandardLifecycleError(
                "independent library acceptance harness failed: "
                + completed.stderr.strip()
            )
        if local_tree_identity(custody.root) != custody.tree_identity:
            raise LocalStandardLifecycleError(
                "independent library acceptance mutated immutable package custody"
            )
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LocalStandardLifecycleError(
                "independent library acceptance emitted invalid JSON"
            ) from exc
        raw_cases = result.get("cases") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or set(result) != {"schema", "cases"}
            or result.get("schema") != "literate-ai/library-acceptance-results@1"
            or not isinstance(raw_cases, list)
        ):
            raise LocalStandardLifecycleError(
                "independent library acceptance emitted an invalid protocol"
            )
        observed = {}
        for item in raw_cases:
            if (
                not isinstance(item, dict)
                or set(item) != {"case_id", "capability", "result"}
                or not isinstance(item.get("case_id"), str)
                or item["case_id"] in observed
            ):
                raise LocalStandardLifecycleError(
                    "independent library acceptance emitted a malformed case"
                )
            observed[item["case_id"]] = item
        expected = {item.case_id: item for item in oracle.cases}
        if set(observed) != set(expected):
            raise LocalStandardLifecycleError(
                "independent library acceptance did not run every exact case"
            )
        if oracle.identity != oracle_identity:
            raise LocalStandardLifecycleError("library acceptance oracle changed")
        observations = []
        for case_id, case in expected.items():
            item = observed[case_id]
            if item["capability"] != case.capability or not product_json_values_equal(
                item["result"], case.expected_result
            ):
                raise LocalStandardLifecycleError(
                    f"independent library acceptance result differs for {case_id!r}"
                )
            observations.append(
                {
                    "case_id": case_id,
                    "capability": case.capability,
                    "case_identity": self._record_product_evidence(
                        {
                            "arguments": case.arguments,
                            "expected_result": case.expected_result,
                        }
                    ).uri,
                    "result_identity": self._record_product_evidence(
                        item["result"]
                    ).uri,
                }
            )
        sdk_execution = getattr(completed, "native_sdk_execution_identity", None)
        sdk_fields = (
            {}
            if sdk_execution is None
            else {"native_sdk_execution_identity": sdk_execution.uri}
        )
        return self._record_evidence(
            {
                "schema": "literate-ai/local-independent-library-acceptance@1"
                if sdk_execution is None
                else "literate-ai/local-independent-library-acceptance@2",
                **sdk_fields,
                "package_plan_identity": package_plan.identity.uri,
                "package_result_identity": package_result.identity.uri,
                "root_integration_test_identity": root_integration_test_identity.uri,
                "packaged_execution_identity": packaged_execution_identity.uri,
                "oracle_identity": oracle_identity.uri,
                "harness_identity": oracle.harness_identity.uri,
                "artifact_identity": root_export.identity.uri,
                "import_surface_identity": surface.identity.uri,
                "observations": observations,
            }
        )

    def publish(self, membership: object) -> ContentIdentity:
        identity = getattr(membership, "identity", None)
        if not isinstance(identity, ContentIdentity):
            raise LocalStandardLifecycleError("source-cache membership is not typed")
        return identity

    def admit(self, results: tuple[object, ...]) -> ContentIdentity:
        return self._record_evidence({"admit": [item.identity.uri for item in results]})

    def issue(self, receipt: object) -> ContentIdentity:
        identity = getattr(receipt, "identity", None)
        if not isinstance(identity, ContentIdentity):
            raise LocalStandardLifecycleError("aggregate receipt is not typed")
        return identity


__all__ = [
    "LocalComponentToolBinding",
    "LocalProjectPackageCustody",
    "LocalSourceTreeRegistry",
    "LocalResolvedExecutionCommand",
    "LocalStandardLifecycleError",
    "PersistentServiceAcceptanceError",
    "LocalStandardLifecyclePorts",
    "RegisteredSourceGenerationRunner",
    "local_generated_source_tree_identity",
    "local_tree_identity",
]

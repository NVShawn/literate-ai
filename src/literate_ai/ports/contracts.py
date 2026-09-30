"""Executed application boundaries and their canonical JSON result contracts.

A protocol belongs here only when an application service actually receives an
implementation through dependency injection.  Provider capability names that have no
consumer are architecture vocabulary, not executable Python interfaces.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    ClassVar,
    NotRequired,
    Protocol,
    Required,
    TypedDict,
    cast,
    runtime_checkable,
)

from literate_ai.contracts import (
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxRepositorySourceResolution,
    SourceIntelligenceArtifact,
    SourceIntelligenceStageStatus,
    canonical_identity,
)
from literate_ai.generated_tests import ValidatedGeneratedTestSuite
from literate_ai.security import (
    BuildRequest,
    ObservationExecutionAuthorization,
    ObservationRequest,
)

JsonObject = Mapping[str, object]

_SHA256_URI = re.compile(r"^sha256:[0-9a-f]{64}$")
_PORTABLE_EVIDENCE_KIND = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_PORTABLE_LOGICAL_NAME = re.compile(
    r"^[A-Za-z0-9.](?:[A-Za-z0-9._/-]{0,1022}[A-Za-z0-9._-])?$"
)
AUTHORIZED_EXECUTION_PROFILE = "authorized-execution"
NON_EXECUTING_EXACT_TREE_PROFILE = "non-executing-exact-tree"


class ModelInvocation(TypedDict, total=False):
    """Exact stage invocation handed to an already-selected model provider."""

    stage_id: Required[str]
    input_identity: Required[JsonObject]
    route_decision: Required[JsonObject]
    instructions: Required[str]
    input: Required[JsonObject]
    response_schema_name: Required[str]
    response_schema: Required[JsonObject]
    content_kind: Required[str]
    metadata: Required[JsonObject]
    max_output_tokens: NotRequired[int]


class GeneratedArtifact(TypedDict):
    """Exact generated tree crossing into the guarded host lifecycle."""

    effective_revision_digest: str
    tree_identity: str
    source_bundle_digest: str
    generated_test_suite_path: str
    generated_test_suite_identity: str
    source_sbom_path: str
    files: Mapping[str, str]
    source_snapshot_ids: Sequence[str]
    evidence_ids: Sequence[str]


class ValidationResult(TypedDict, total=False):
    passed: Required[bool]
    findings: Required[Sequence[JsonObject]]
    validator_ids: NotRequired[Sequence[str]]
    categories: NotRequired[Sequence[str]]


class ClassificationResult(TypedDict, total=False):
    classification_digest: Required[str]
    effective_revision_digest: Required[str]
    source_digests: Required[Sequence[str]]
    profile: Required[str]


class BuildRequestDocument(TypedDict, total=False):
    schema: Required[str]
    effective_revision_digest: Required[str]
    source_bundle_digest: Required[str]
    builder_id: Required[str]
    toolchain_digest: Required[str]
    sandbox_profile: Required[str]
    requested_privileges: Required[Sequence[str]]
    allowed_outputs: Required[Sequence[str]]
    artifact: NotRequired[GeneratedArtifact]


class BuildAuthorizationDocument(TypedDict, total=False):
    authorization_id: Required[str]
    classification_digest: Required[str]
    request_digest: Required[str]
    effective_revision_digest: Required[str]
    profile: Required[str]


class BuildResult(TypedDict, total=False):
    artifact_digest: Required[str]
    source_bundle_digest: Required[str]
    authorization_id: Required[str]
    compiled_files: Required[Sequence[str]]
    dependency_observation: NotRequired[JsonObject]


@dataclass(frozen=True, slots=True)
class BuildDependencyEvidenceArtifact:
    """One content-addressed logical artifact behind a build dependency graph."""

    kind: str
    logical_name: str
    content_identity: str

    def __post_init__(self) -> None:
        if not _PORTABLE_EVIDENCE_KIND.fullmatch(self.kind):
            raise PortContractError(
                "ports.build-dependencies.artifact-kind-invalid",
                "build dependency evidence kind must be a lower-case portable ID",
            )
        _require_safe_logical_name(self.logical_name)
        _digest(self.content_identity, "build dependency evidence content identity")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "logical_name": self.logical_name,
            "content_identity": self.content_identity,
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "build dependency evidence artifact"
    ) -> BuildDependencyEvidenceArtifact:
        data = _exact_mapping(
            value,
            path,
            frozenset({"kind", "logical_name", "content_identity"}),
        )
        return cls(
            _text(data["kind"], f"{path} kind"),
            _text(data["logical_name"], f"{path} logical name"),
            _digest(data["content_identity"], f"{path} content identity"),
        )


@dataclass(frozen=True, slots=True)
class BuildInputConsumption:
    """Exact generated files consumed by one completed build-system operation."""

    consumer_id: str
    source_bundle_digest: str
    files: tuple[str, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:build-input-consumption"

    def __post_init__(self) -> None:
        _text(self.consumer_id, "build input consumer ID")
        _digest(self.source_bundle_digest, "build input source bundle")
        _require_sorted_unique(self.files, "build input consumed files")
        if not self.files:
            raise PortContractError(
                "ports.build-inputs.files-empty",
                "build input consumption requires at least one exact generated file",
            )
        for path in self.files:
            _require_safe_logical_name(path)

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "consumer_id": self.consumer_id,
            "source_bundle_digest": self.source_bundle_digest,
            "files": list(self.files),
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "build input consumption"
    ) -> BuildInputConsumption:
        data = _exact_mapping(
            value,
            path,
            frozenset({"schema", "consumer_id", "source_bundle_digest", "files"}),
        )
        if data["schema"] != cls.SCHEMA:
            raise PortContractError(
                "ports.build-inputs.schema-invalid",
                f"{path} has an unsupported schema",
            )
        return cls(
            _text(data["consumer_id"], f"{path} consumer ID"),
            _digest(data["source_bundle_digest"], f"{path} source bundle"),
            _string_sequence(data["files"], f"{path} files", allow_empty=False),
        )


@dataclass(frozen=True, slots=True)
class BzlmodModule:
    """One exact selected Bazel module and its normalized outgoing edges."""

    key: str
    name: str
    version: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.key, "Bzlmod module key")
        _text(self.name, "Bzlmod module name")
        _text(self.version, "Bzlmod module exact version")
        _require_sorted_unique(self.dependencies, "Bzlmod module dependencies")

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "dependencies": list(self.dependencies),
        }

    @classmethod
    def from_dict(cls, value: object, *, path: str = "Bzlmod module") -> BzlmodModule:
        data = _exact_mapping(
            value,
            path,
            frozenset({"key", "name", "version", "dependencies"}),
        )
        dependencies = _string_sequence(
            data["dependencies"], f"{path} dependencies", allow_empty=True
        )
        return cls(
            _text(data["key"], f"{path} key"),
            _text(data["name"], f"{path} name"),
            _text(data["version"], f"{path} exact version"),
            dependencies,
        )


@dataclass(frozen=True, slots=True)
class BzlmodRootModule:
    """The generated root module and the exact selected modules it declares."""

    name: str
    version: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.name, "Bzlmod root module name")
        _text(self.version, "Bzlmod root module exact version")
        _require_sorted_unique(self.dependencies, "Bzlmod root module dependencies")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "dependencies": list(self.dependencies),
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "Bzlmod root module"
    ) -> BzlmodRootModule:
        data = _exact_mapping(
            value,
            path,
            frozenset({"name", "version", "dependencies"}),
        )
        return cls(
            _text(data["name"], f"{path} name"),
            _text(data["version"], f"{path} exact version"),
            _string_sequence(
                data["dependencies"], f"{path} dependencies", allow_empty=True
            ),
        )


@dataclass(frozen=True, slots=True)
class BuildDependencyObservation:
    """Canonical, immutable Bzlmod evidence returned by an authorized builder."""

    resolver_id: str
    source_bundle_digest: str
    build_toolchain_identity: str
    resolver_toolchain_identity: str
    root_module: BzlmodRootModule
    modules: tuple[BzlmodModule, ...]
    evidence_artifacts: tuple[BuildDependencyEvidenceArtifact, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:bzlmod-dependency-observation"
    GRAPH_SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:bzlmod-resolved-graph"
    EVIDENCE_SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v1:bzlmod-evidence-artifacts"
    )
    REQUIRED_EVIDENCE_ARTIFACTS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("bazel-module-lock", ".literate/bazel/MODULE.bazel.lock"),
        ("bazel-module-graph", ".literate/bazel/module-graph.json"),
        (
            "bazel-repository-definitions",
            ".literate/bazel/repositories.ndjson",
        ),
    )

    def __post_init__(self) -> None:
        _text(self.resolver_id, "build dependency resolver ID")
        _digest(self.source_bundle_digest, "build dependency source bundle")
        _digest(
            self.build_toolchain_identity,
            "build dependency application toolchain identity",
        )
        _digest(
            self.resolver_toolchain_identity,
            "build dependency resolver toolchain identity",
        )
        if not isinstance(self.root_module, BzlmodRootModule):
            raise PortContractError(
                "ports.build-dependencies.root-module-invalid",
                "build dependency observation requires one immutable root module",
            )
        if not isinstance(self.modules, tuple) or any(
            not isinstance(item, BzlmodModule) for item in self.modules
        ):
            raise PortContractError(
                "ports.build-dependencies.modules-invalid",
                "build dependency observation modules must be immutable Bzlmod modules",
            )
        if not isinstance(self.evidence_artifacts, tuple) or any(
            not isinstance(item, BuildDependencyEvidenceArtifact)
            for item in self.evidence_artifacts
        ):
            raise PortContractError(
                "ports.build-dependencies.artifacts-invalid",
                "build dependency evidence artifacts must be immutable records",
            )
        module_keys = tuple(item.key for item in self.modules)
        if module_keys != tuple(sorted(module_keys)) or len(module_keys) != len(
            set(module_keys)
        ):
            raise PortContractError(
                "ports.build-dependencies.module-order-invalid",
                "Bzlmod modules must have unique sorted keys",
            )
        _require_complete_bzlmod_graph(self.root_module, self.modules)
        artifact_roles = tuple(
            (item.kind, item.logical_name) for item in self.evidence_artifacts
        )
        if artifact_roles != self.REQUIRED_EVIDENCE_ARTIFACTS:
            raise PortContractError(
                "ports.build-dependencies.artifact-set-invalid",
                "Bzlmod dependency observation requires the exact canonical public "
                "lock, raw module graph, and resolved repository definition artifacts",
            )

    @property
    def graph_identity(self) -> str:
        return canonical_identity(self._graph_material()).uri

    @property
    def evidence_identity(self) -> str:
        return canonical_identity(self._evidence_material()).uri

    @property
    def observation_identity(self) -> str:
        return canonical_identity(self._observation_material()).uri

    def _graph_material(self) -> dict[str, object]:
        return {
            "schema": self.GRAPH_SCHEMA,
            "root_module": self.root_module.to_dict(),
            "modules": [item.to_dict() for item in self.modules],
        }

    def _evidence_material(self) -> dict[str, object]:
        return {
            "schema": self.EVIDENCE_SCHEMA,
            "evidence_artifacts": [item.to_dict() for item in self.evidence_artifacts],
        }

    def _observation_material(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "resolver_id": self.resolver_id,
            "source_bundle_digest": self.source_bundle_digest,
            "build_toolchain_identity": self.build_toolchain_identity,
            "resolver_toolchain_identity": self.resolver_toolchain_identity,
            "root_module": self.root_module.to_dict(),
            "modules": [item.to_dict() for item in self.modules],
            "graph_identity": self.graph_identity,
            "evidence_artifacts": [item.to_dict() for item in self.evidence_artifacts],
            "evidence_identity": self.evidence_identity,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._observation_material(),
            "observation_identity": self.observation_identity,
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "build dependency observation"
    ) -> BuildDependencyObservation:
        data = _exact_mapping(
            value,
            path,
            frozenset(
                {
                    "schema",
                    "resolver_id",
                    "source_bundle_digest",
                    "build_toolchain_identity",
                    "resolver_toolchain_identity",
                    "root_module",
                    "modules",
                    "graph_identity",
                    "evidence_artifacts",
                    "evidence_identity",
                    "observation_identity",
                }
            ),
        )
        if data["schema"] != cls.SCHEMA:
            raise PortContractError(
                "ports.build-dependencies.schema-unsupported",
                "build dependency observation uses an unsupported schema",
            )
        raw_modules = _sequence(data["modules"], f"{path} modules")
        raw_artifacts = _sequence(
            data["evidence_artifacts"], f"{path} evidence artifacts"
        )
        observation = cls(
            resolver_id=_text(data["resolver_id"], f"{path} resolver ID"),
            source_bundle_digest=_digest(
                data["source_bundle_digest"], f"{path} source bundle"
            ),
            build_toolchain_identity=_digest(
                data["build_toolchain_identity"],
                f"{path} application build toolchain identity",
            ),
            resolver_toolchain_identity=_digest(
                data["resolver_toolchain_identity"],
                f"{path} resolver toolchain identity",
            ),
            root_module=BzlmodRootModule.from_dict(
                data["root_module"], path=f"{path} root module"
            ),
            modules=tuple(
                BzlmodModule.from_dict(item, path=f"{path} modules[{index}]")
                for index, item in enumerate(raw_modules)
            ),
            evidence_artifacts=tuple(
                BuildDependencyEvidenceArtifact.from_dict(
                    item, path=f"{path} evidence artifacts[{index}]"
                )
                for index, item in enumerate(raw_artifacts)
            ),
        )
        if _digest(data["graph_identity"], f"{path} graph identity") != (
            observation.graph_identity
        ):
            raise PortContractError(
                "ports.build-dependencies.graph-identity-mismatch",
                "Bzlmod graph identity differs from its normalized graph",
            )
        if _digest(data["evidence_identity"], f"{path} evidence identity") != (
            observation.evidence_identity
        ):
            raise PortContractError(
                "ports.build-dependencies.evidence-identity-mismatch",
                "build dependency evidence identity differs from its artifacts",
            )
        supplied_identity = _digest(data["observation_identity"], f"{path} identity")
        if supplied_identity != observation.observation_identity:
            raise PortContractError(
                "ports.build-dependencies.identity-mismatch",
                "build dependency observation identity differs from its evidence",
            )
        return observation


class DependencySourceValidationResult(TypedDict):
    """Pre-build dependency evidence for the exact generated source candidate."""

    resolver_id: str
    effective_revision_digest: str
    source_bundle_digest: str
    source_bom: JsonObject
    validation_identity: str


class DependencyResolutionResult(TypedDict):
    """Strict post-build dependency evidence for one exact built candidate."""

    resolver_id: str
    effective_revision_digest: str
    source_bundle_digest: str
    artifact_digest: str
    source_validation_identity: str
    source_bom: JsonObject
    resolved_bom: JsonObject
    repository_resolutions: Sequence[JsonObject]
    resolution_identity: str
    python_dependency_evidence_identity: NotRequired[str]


class ObservationRequestDocument(TypedDict):
    """Canonical wire form of a request to execute one exact test harness."""

    effective_revision_digest: str
    source_digests: Sequence[str]
    runner_id: str
    harness_digest: str
    sandbox_profile: str
    requested_privileges: Sequence[str]
    allowed_outputs: Sequence[str]


class ExecutionAuthorizationDocument(TypedDict, total=False):
    """Canonical wire form of the grant authorizing an observation request."""

    authorization_id: Required[str]
    classification_digest: Required[str]
    request_digest: Required[str]
    effective_revision_digest: Required[str]
    actor: Required[str]
    reason: Required[str]
    profile: Required[str]
    privileges: Required[Sequence[str]]
    issued_at: Required[str]
    expires_at: Required[str]
    warning: Required[str | None]
    revoked: Required[bool]


class TestCaseResult(TypedDict, total=False):
    """One case decision, with mandatory typed evidence when code was executed."""

    case_id: Required[str]
    passed: Required[bool]
    observation_request: NotRequired[ObservationRequestDocument]
    execution_authorization: NotRequired[ExecutionAuthorizationDocument]


class GeneratedTestResult(TypedDict, total=False):
    """Result of running generated tests against one exact built artifact."""

    passed: Required[bool]
    runner_id: Required[str]
    execution_profile: Required[str]
    classification_digest: Required[str]
    effective_revision_digest: Required[str]
    source_bundle_digest: Required[str]
    artifact_digest: Required[str]
    dependency_resolution_identity: Required[str]
    verified_tree_identity: Required[str]
    test_suite_identity: Required[str]
    total: Required[int]
    passed_count: Required[int]
    failed_count: Required[int]
    skipped_count: Required[int]
    case_results: Required[Sequence[TestCaseResult]]
    profile_reason: NotRequired[str]


class AcceptanceResult(TypedDict, total=False):
    """Independent acceptance result for one exact built candidate."""

    passed: Required[bool]
    runner_id: Required[str]
    execution_profile: Required[str]
    classification_digest: Required[str]
    effective_revision_digest: Required[str]
    source_bundle_digest: Required[str]
    artifact_digest: Required[str]
    dependency_resolution_identity: Required[str]
    verified_tree_identity: Required[str]
    test_suite_identity: Required[str]
    total: Required[int]
    passed_count: Required[int]
    failed_count: Required[int]
    skipped_count: Required[int]
    case_results: Required[Sequence[TestCaseResult]]
    profile_reason: NotRequired[str]


class PreparedTreeResult(TypedDict):
    tree_digest: str
    manifest: Sequence[Sequence[str]]
    dependency_resolution_identity: str
    source_bom_binding_identity: str
    resolved_bom_binding_identity: str


class CommittedTreeResult(TypedDict):
    tree_digest: str
    reference: str
    path: str
    source_intelligence: JsonObject | None
    source_intelligence_status: JsonObject
    dependency_resolution_identity: str
    source_bom_binding_identity: str
    resolved_bom_binding_identity: str


class PortContractError(ValueError):
    """An adapter returned JSON that is unsafe or ambiguous for its lifecycle seam."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def require_validation_result(value: JsonObject) -> ValidationResult:
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise PortContractError(
            "ports.validation.passed-invalid",
            "validation result must contain a boolean passed decision",
        )
    findings = _object_sequence(value.get("findings"), "validation findings")
    return cast(
        ValidationResult,
        {**dict(value), "passed": passed, "findings": findings},
    )


def require_classification_result(
    value: JsonObject,
    *,
    effective_revision_digest: str,
    source_bundle_digest: str,
) -> ClassificationResult:
    classification_digest = _digest(
        value.get("classification_digest"), "classification digest"
    )
    actual_revision = _digest(
        value.get("effective_revision_digest"), "classified revision"
    )
    if actual_revision != effective_revision_digest:
        raise PortContractError(
            "ports.classification.revision-mismatch",
            "classification does not bind the exact effective revision",
        )
    source_digests = _digest_sequence(
        value.get("source_digests"), "classified source digests"
    )
    if source_bundle_digest not in source_digests:
        raise PortContractError(
            "ports.classification.source-mismatch",
            "classification does not cover the exact generated source bundle",
        )
    profile = _text(value.get("profile"), "classification profile")
    return cast(
        ClassificationResult,
        {
            **dict(value),
            "classification_digest": classification_digest,
            "effective_revision_digest": actual_revision,
            "source_digests": source_digests,
            "profile": profile,
        },
    )


def require_build_authorization_result(
    value: JsonObject,
    *,
    classification_digest: str,
    effective_revision_digest: str,
    request_digest: str,
) -> BuildAuthorizationDocument:
    authorization_id = _text(value.get("authorization_id"), "authorization ID")
    if _digest(value.get("classification_digest"), "authorized classification") != (
        classification_digest
    ):
        raise PortContractError(
            "ports.authorization.classification-mismatch",
            "build authorization targets another classification",
        )
    if _digest(value.get("effective_revision_digest"), "authorized revision") != (
        effective_revision_digest
    ):
        raise PortContractError(
            "ports.authorization.revision-mismatch",
            "build authorization targets another effective revision",
        )
    if _digest(value.get("request_digest"), "authorized request") != request_digest:
        raise PortContractError(
            "ports.authorization.request-mismatch",
            "build authorization targets another request",
        )
    profile = _text(value.get("profile"), "authorization profile")
    return cast(
        BuildAuthorizationDocument,
        {
            **dict(value),
            "authorization_id": authorization_id,
            "classification_digest": classification_digest,
            "effective_revision_digest": effective_revision_digest,
            "request_digest": request_digest,
            "profile": profile,
        },
    )


def require_build_request_document(value: JsonObject) -> BuildRequestDocument:
    if "schema" in value and value["schema"] != BuildRequest.SCHEMA:
        raise PortContractError(
            "ports.build-request.schema-unsupported",
            "build request uses an unsupported schema identity",
        )
    effective_revision_digest = _digest(
        value.get("effective_revision_digest"), "build request revision"
    )
    source_bundle_digest = _digest(
        value.get("source_bundle_digest"), "build request source bundle"
    )
    builder_id = _text(value.get("builder_id"), "build request builder")
    toolchain_digest = _digest(value.get("toolchain_digest"), "build request toolchain")
    sandbox_profile = _text(
        value.get("sandbox_profile"), "build request sandbox profile"
    )
    requested_privileges = _string_sequence(
        value.get("requested_privileges"),
        "requested build privileges",
        allow_empty=True,
    )
    allowed_outputs = _string_sequence(
        value.get("allowed_outputs"), "allowed build outputs"
    )
    return cast(
        BuildRequestDocument,
        {
            "schema": BuildRequest.SCHEMA,
            "effective_revision_digest": effective_revision_digest,
            "source_bundle_digest": source_bundle_digest,
            "builder_id": builder_id,
            "toolchain_digest": toolchain_digest,
            "sandbox_profile": sandbox_profile,
            "requested_privileges": list(requested_privileges),
            "allowed_outputs": list(allowed_outputs),
        },
    )


def require_build_result(
    value: JsonObject,
    *,
    source_bundle_digest: str,
    authorization_id: str,
    toolchain_identity: str | None = None,
) -> BuildResult:
    artifact_digest = _digest(value.get("artifact_digest"), "build artifact digest")
    if _digest(value.get("source_bundle_digest"), "built source bundle") != (
        source_bundle_digest
    ):
        raise PortContractError(
            "ports.build.source-mismatch",
            "build result does not bind the exact generated source bundle",
        )
    if _text(value.get("authorization_id"), "build authorization ID") != (
        authorization_id
    ):
        raise PortContractError(
            "ports.build.authorization-mismatch",
            "build result does not bind the exact authorization",
        )
    compiled_files = _string_sequence(
        value.get("compiled_files"), "compiled files", allow_empty=False
    )
    dependency_observation: dict[str, object] | None = None
    if "dependency_observation" in value:
        observation = BuildDependencyObservation.from_dict(
            value["dependency_observation"]
        )
        if observation.source_bundle_digest != source_bundle_digest:
            raise PortContractError(
                "ports.build-dependencies.source-mismatch",
                "build dependency observation binds another source bundle",
            )
        result_toolchain: str | None = None
        if "toolchain_identity" in value:
            result_toolchain = _digest(
                value["toolchain_identity"], "build result toolchain identity"
            )
        if toolchain_identity is not None:
            expected_toolchain = _digest(
                toolchain_identity, "expected build dependency toolchain identity"
            )
            if result_toolchain is not None and result_toolchain != expected_toolchain:
                raise PortContractError(
                    "ports.build.toolchain-mismatch",
                    "build result binds another authorized application toolchain",
                )
        else:
            expected_toolchain = result_toolchain
        if (
            expected_toolchain is not None
            and observation.build_toolchain_identity != expected_toolchain
        ):
            raise PortContractError(
                "ports.build-dependencies.toolchain-mismatch",
                "build dependency observation binds another application build "
                "toolchain",
            )
        dependency_observation = observation.to_dict()
    normalized: dict[str, object] = {
        **dict(value),
        "artifact_digest": artifact_digest,
        "source_bundle_digest": source_bundle_digest,
        "authorization_id": authorization_id,
        "compiled_files": compiled_files,
    }
    if dependency_observation is not None:
        normalized["dependency_observation"] = dependency_observation
    return cast(
        BuildResult,
        normalized,
    )


def require_dependency_source_validation_result(
    value: JsonObject,
    *,
    resolver_id: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
    source_bom_identity: str,
    managed_graph_identity: str,
    composition_identity: str,
    root_ref: str,
) -> DependencySourceValidationResult:
    """Require a source BOM binding against current run authority before build."""

    if _text(value.get("resolver_id"), "dependency resolver ID") != resolver_id:
        raise PortContractError(
            "ports.dependencies.resolver-mismatch",
            "source dependency evidence came from another resolver",
        )
    for field, expected, label in (
        ("effective_revision_digest", effective_revision_digest, "resolved revision"),
        ("source_bundle_digest", source_bundle_digest, "source bundle"),
    ):
        if _digest(value.get(field), label) != expected:
            code_field = field.removesuffix("_digest").replace("_", "-")
            raise PortContractError(
                f"ports.dependencies.{code_field}-mismatch",
                f"source dependency evidence does not bind the exact {label}",
            )
    try:
        source_bom = CycloneDxBomBinding.from_dict(
            _mapping(value.get("source_bom"), "source BOM binding")
        )
    except (TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.dependencies.bom-binding-invalid",
            "source dependency evidence contains an invalid CycloneDX binding",
        ) from exc
    if source_bom.lifecycle is not CycloneDxLifecycle.SOURCE:
        raise PortContractError(
            "ports.dependencies.source-lifecycle-mismatch",
            "source dependency evidence is not a pre-build CycloneDX binding",
        )
    expected_authority = (
        _digest(managed_graph_identity, "managed SBOM graph identity"),
        _digest(composition_identity, "Component composition identity"),
        _text(root_ref, "managed SBOM root reference"),
    )
    actual_authority = (
        source_bom.managed_graph_identity.uri,
        source_bom.resolved_graph_identity.uri,
        source_bom.root_ref,
    )
    if actual_authority != expected_authority:
        raise PortContractError(
            "ports.dependencies.managed-graph-mismatch",
            "source dependency evidence does not bind the current managed "
            "Component graph",
        )
    if source_bom.bom_identity.uri != source_bom_identity:
        raise PortContractError(
            "ports.dependencies.source-bom-mismatch",
            "source dependency evidence does not bind the generated source BOM bytes",
        )
    identity_material: dict[str, object] = {
        "resolver_id": resolver_id,
        "effective_revision_digest": effective_revision_digest,
        "source_bundle_digest": source_bundle_digest,
        "source_bom": source_bom.to_dict(),
    }
    validation_identity = _digest(
        value.get("validation_identity"), "dependency source-validation identity"
    )
    if validation_identity != canonical_identity(identity_material).uri:
        raise PortContractError(
            "ports.dependencies.source-validation-identity-mismatch",
            "source dependency validation identity does not match its exact evidence",
        )
    return cast(
        DependencySourceValidationResult,
        {
            **dict(value),
            **identity_material,
            "validation_identity": validation_identity,
        },
    )


def require_dependency_resolution_result(
    value: JsonObject,
    *,
    resolver_id: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
    artifact_digest: str,
    source_bom_identity: str,
    source_validation: DependencySourceValidationResult,
    managed_graph_identity: str,
    composition_identity: str,
    root_ref: str,
) -> DependencyResolutionResult:
    """Require strict source/post-build BOM bindings for the exact build."""

    if _text(value.get("resolver_id"), "dependency resolver ID") != resolver_id:
        raise PortContractError(
            "ports.dependencies.resolver-mismatch",
            "dependency evidence came from another resolver",
        )
    for field, expected, label in (
        ("effective_revision_digest", effective_revision_digest, "resolved revision"),
        ("source_bundle_digest", source_bundle_digest, "resolved source bundle"),
        ("artifact_digest", artifact_digest, "resolved build artifact"),
    ):
        if _digest(value.get(field), label) != expected:
            code_field = field.removesuffix("_digest").replace("_", "-")
            raise PortContractError(
                f"ports.dependencies.{code_field}-mismatch",
                f"dependency evidence does not bind the exact {label}",
            )
    try:
        source_bom = CycloneDxBomBinding.from_dict(
            _mapping(value.get("source_bom"), "source BOM binding")
        )
        resolved_bom = CycloneDxBomBinding.from_dict(
            _mapping(value.get("resolved_bom"), "resolved BOM binding")
        )
        repository_resolutions = tuple(
            CycloneDxRepositorySourceResolution.from_dict(
                item,
                path=f"repository_resolutions[{index}]",
            )
            for index, item in enumerate(
                _object_sequence(
                    value.get("repository_resolutions"),
                    "repository source resolutions",
                )
            )
        )
    except (TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.dependencies.bom-binding-invalid",
            "dependency evidence contains an invalid CycloneDX binding "
            "or repository projection",
        ) from exc
    dependency_identities = tuple(
        item.dependency_identity.uri for item in repository_resolutions
    )
    if len(dependency_identities) != len(set(dependency_identities)):
        raise PortContractError(
            "ports.dependencies.repository-resolution-duplicate",
            "dependency evidence contains multiple repository projections "
            "for one dependency",
        )
    if source_bom.lifecycle is not CycloneDxLifecycle.SOURCE:
        raise PortContractError(
            "ports.dependencies.source-lifecycle-mismatch",
            "source dependency evidence is not a pre-build CycloneDX binding",
        )
    if resolved_bom.lifecycle is not CycloneDxLifecycle.RESOLVED:
        raise PortContractError(
            "ports.dependencies.resolved-lifecycle-mismatch",
            "resolved dependency evidence is not a post-build CycloneDX binding",
        )
    if source_bom.bom_identity.uri != source_bom_identity:
        raise PortContractError(
            "ports.dependencies.source-bom-mismatch",
            "dependency evidence does not bind the generated source BOM bytes",
        )
    try:
        validated_source_bom = CycloneDxBomBinding.from_dict(
            source_validation["source_bom"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.dependencies.source-validation-invalid",
            "dependency resolution lacks valid pre-build dependency evidence",
        ) from exc
    source_validation_identity = _digest(
        value.get("source_validation_identity"),
        "dependency source-validation identity",
    )
    if (
        source_validation_identity != source_validation.get("validation_identity")
        or source_bom != validated_source_bom
    ):
        raise PortContractError(
            "ports.dependencies.source-validation-mismatch",
            "dependency resolution changed its pre-build source BOM evidence",
        )
    if resolved_bom.source_bom_identity != source_bom.bom_identity:
        raise PortContractError(
            "ports.dependencies.source-transition-mismatch",
            "resolved dependency evidence does not bind the exact source BOM",
        )
    if (
        source_bom.managed_graph_identity != resolved_bom.managed_graph_identity
        or source_bom.resolved_graph_identity != resolved_bom.resolved_graph_identity
        or source_bom.root_ref != resolved_bom.root_ref
    ):
        raise PortContractError(
            "ports.dependencies.managed-graph-mismatch",
            "source and resolved BOMs do not bind the same managed Component graph",
        )
    expected_authority = (
        _digest(managed_graph_identity, "managed SBOM graph identity"),
        _digest(composition_identity, "Component composition identity"),
        _text(root_ref, "managed SBOM root reference"),
    )
    if (
        source_bom.managed_graph_identity.uri,
        source_bom.resolved_graph_identity.uri,
        source_bom.root_ref,
    ) != expected_authority:
        raise PortContractError(
            "ports.dependencies.managed-graph-mismatch",
            "dependency resolution does not bind the current managed Component graph",
        )
    identity_material: dict[str, object] = {
        "resolver_id": resolver_id,
        "effective_revision_digest": effective_revision_digest,
        "source_bundle_digest": source_bundle_digest,
        "artifact_digest": artifact_digest,
        "source_validation_identity": source_validation_identity,
        "source_bom": source_bom.to_dict(),
        "resolved_bom": resolved_bom.to_dict(),
        "repository_resolutions": [item.to_dict() for item in repository_resolutions],
    }
    if "python_dependency_evidence_identity" in value:
        identity_material["python_dependency_evidence_identity"] = _digest(
            value["python_dependency_evidence_identity"],
            "Python installed dependency evidence identity",
        )
    normalized: dict[str, object] = {
        **dict(value),
        **identity_material,
    }
    resolution_identity = _digest(
        value.get("resolution_identity"), "dependency resolution identity"
    )
    expected_resolution_identity = canonical_identity(identity_material).uri
    if resolution_identity != expected_resolution_identity:
        raise PortContractError(
            "ports.dependencies.identity-mismatch",
            "dependency resolution identity does not match its exact evidence",
        )
    normalized["resolution_identity"] = resolution_identity
    return cast(DependencyResolutionResult, normalized)


def require_generated_test_result(
    value: JsonObject,
    *,
    runner_id: str,
    classification_digest: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
    artifact_digest: str,
    dependency_resolution_identity: str,
    tree_identity: str,
    test_suite_identity: str,
    expected_case_ids: Sequence[str],
) -> GeneratedTestResult:
    """Require a complete, internally consistent result for the exact build."""

    return cast(
        GeneratedTestResult,
        _require_test_execution_result(
            value,
            seam="generated-tests",
            runner_id=runner_id,
            classification_digest=classification_digest,
            effective_revision_digest=effective_revision_digest,
            source_bundle_digest=source_bundle_digest,
            artifact_digest=artifact_digest,
            dependency_resolution_identity=dependency_resolution_identity,
            tree_identity=tree_identity,
            test_suite_identity=test_suite_identity,
            expected_case_ids=expected_case_ids,
        ),
    )


def require_acceptance_result(
    value: JsonObject,
    *,
    runner_id: str,
    classification_digest: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
    artifact_digest: str,
    dependency_resolution_identity: str,
    tree_identity: str,
    test_suite_identity: str,
    expected_execution_profile: str,
) -> AcceptanceResult:
    """Require independent acceptance evidence for the exact candidate build."""

    return cast(
        AcceptanceResult,
        _require_test_execution_result(
            value,
            seam="acceptance",
            runner_id=runner_id,
            classification_digest=classification_digest,
            effective_revision_digest=effective_revision_digest,
            source_bundle_digest=source_bundle_digest,
            artifact_digest=artifact_digest,
            dependency_resolution_identity=dependency_resolution_identity,
            tree_identity=tree_identity,
            test_suite_identity=test_suite_identity,
            expected_execution_profile=expected_execution_profile,
        ),
    )


def _require_test_execution_result(
    value: JsonObject,
    *,
    seam: str,
    runner_id: str,
    classification_digest: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
    artifact_digest: str,
    dependency_resolution_identity: str,
    tree_identity: str,
    test_suite_identity: str,
    expected_execution_profile: str | None = None,
    expected_case_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise PortContractError(
            f"ports.{seam}.passed-invalid",
            f"{seam} result must contain a boolean passed decision",
        )
    if _text(value.get("runner_id"), f"{seam} runner ID") != runner_id:
        raise PortContractError(
            f"ports.{seam}.runner-mismatch",
            f"{seam} result came from another runner",
        )
    if _digest(value.get("classification_digest"), f"{seam} classification") != (
        classification_digest
    ):
        raise PortContractError(
            f"ports.{seam}.classification-mismatch",
            f"{seam} result targets another classification",
        )
    if _digest(value.get("source_bundle_digest"), f"{seam} source bundle") != (
        source_bundle_digest
    ):
        raise PortContractError(
            f"ports.{seam}.source-mismatch",
            f"{seam} result targets another source bundle",
        )
    if _digest(value.get("artifact_digest"), f"{seam} build artifact") != (
        artifact_digest
    ):
        raise PortContractError(
            f"ports.{seam}.artifact-mismatch",
            f"{seam} result targets another built artifact",
        )
    if (
        _digest(
            value.get("dependency_resolution_identity"),
            f"{seam} dependency resolution",
        )
        != dependency_resolution_identity
    ):
        raise PortContractError(
            f"ports.{seam}.dependency-resolution-mismatch",
            f"{seam} result targets another resolved dependency closure",
        )
    if _digest(value.get("verified_tree_identity"), f"{seam} verified tree") != (
        tree_identity
    ):
        raise PortContractError(
            f"ports.{seam}.tree-mismatch",
            f"{seam} result targets another generated tree",
        )
    if _digest(value.get("test_suite_identity"), f"{seam} suite identity") != (
        test_suite_identity
    ):
        raise PortContractError(
            f"ports.{seam}.suite-mismatch",
            f"{seam} result targets another suite artifact",
        )
    execution_profile = _text(value.get("execution_profile"), f"{seam} profile")
    if execution_profile not in {
        AUTHORIZED_EXECUTION_PROFILE,
        NON_EXECUTING_EXACT_TREE_PROFILE,
    }:
        raise PortContractError(
            f"ports.{seam}.profile-invalid",
            f"{seam} result has an unsupported execution profile",
        )
    if (
        expected_execution_profile is not None
        and execution_profile != expected_execution_profile
    ):
        raise PortContractError(
            f"ports.{seam}.profile-mismatch",
            f"{seam} result does not use its pinned execution profile",
        )
    if execution_profile == NON_EXECUTING_EXACT_TREE_PROFILE:
        _text(value.get("profile_reason"), f"{seam} non-executing reason")
    if _digest(
        value.get("effective_revision_digest"), f"{seam} effective revision"
    ) != (effective_revision_digest):
        raise PortContractError(
            f"ports.{seam}.revision-mismatch",
            f"{seam} result targets another effective revision",
        )
    total = _integer(value.get("total"), f"{seam} total", minimum=1)
    passed_count = _integer(value.get("passed_count"), f"{seam} passed count")
    failed_count = _integer(value.get("failed_count"), f"{seam} failed count")
    skipped_count = _integer(value.get("skipped_count"), f"{seam} skipped count")
    if passed_count + failed_count + skipped_count != total:
        raise PortContractError(
            f"ports.{seam}.count-mismatch",
            f"{seam} counts must account for every test",
        )
    if passed != (passed_count == total and failed_count == 0 and skipped_count == 0):
        raise PortContractError(
            f"ports.{seam}.decision-mismatch",
            f"{seam} decision does not match its counts",
        )
    case_results = _require_test_case_results(
        value.get("case_results"),
        seam=seam,
        execution_profile=execution_profile,
        classification_digest=classification_digest,
        effective_revision_digest=effective_revision_digest,
        source_bundle_digest=source_bundle_digest,
    )
    if len(case_results) != passed_count + failed_count:
        raise PortContractError(
            f"ports.{seam}.case-count-mismatch",
            f"{seam} case results must cover every executed test",
        )
    observed_passed = sum(item["passed"] is True for item in case_results)
    observed_failed = sum(item["passed"] is False for item in case_results)
    if observed_passed != passed_count or observed_failed != failed_count:
        raise PortContractError(
            f"ports.{seam}.case-decision-mismatch",
            f"{seam} case decisions do not match aggregate counts",
        )
    if expected_case_ids is not None:
        expected = tuple(expected_case_ids)
        actual = tuple(str(item["case_id"]) for item in case_results)
        if total != len(expected) or actual != expected:
            raise PortContractError(
                f"ports.{seam}.suite-cases-mismatch",
                f"{seam} result does not cover the admitted suite in order",
            )
    return {
        **dict(value),
        "passed": passed,
        "runner_id": runner_id,
        "execution_profile": execution_profile,
        "classification_digest": classification_digest,
        "effective_revision_digest": effective_revision_digest,
        "source_bundle_digest": source_bundle_digest,
        "artifact_digest": artifact_digest,
        "dependency_resolution_identity": dependency_resolution_identity,
        "verified_tree_identity": tree_identity,
        "test_suite_identity": test_suite_identity,
        "total": total,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "case_results": case_results,
    }


def _require_test_case_results(
    value: object,
    *,
    seam: str,
    execution_profile: str,
    classification_digest: str,
    effective_revision_digest: str,
    source_bundle_digest: str,
) -> tuple[dict[str, object], ...]:
    cases = _object_sequence(value, f"{seam} case results")
    result: list[dict[str, object]] = []
    case_ids: set[str] = set()
    for case in cases:
        normalized = dict(case)
        case_id = _text(case.get("case_id"), f"{seam} case ID")
        if case_id in case_ids:
            raise PortContractError(
                f"ports.{seam}.case-id-duplicate",
                f"{seam} case IDs must be unique",
            )
        case_ids.add(case_id)
        case_passed = case.get("passed")
        if not isinstance(case_passed, bool):
            raise PortContractError(
                f"ports.{seam}.case-decision-invalid",
                f"{seam} cases must contain a boolean passed decision",
            )
        if execution_profile == AUTHORIZED_EXECUTION_PROFILE:
            try:
                request_value = _mapping(
                    case.get("observation_request"), f"{seam} observation request"
                )
                authorization_value = _mapping(
                    case.get("execution_authorization"),
                    f"{seam} execution authorization",
                )
                request = ObservationRequest.from_dict(request_value)
                authorization = ObservationExecutionAuthorization.from_dict(
                    authorization_value
                )
            except (PortContractError, TypeError, ValueError) as exc:
                raise PortContractError(
                    f"ports.{seam}.execution-evidence-invalid",
                    f"{seam} execution evidence is malformed",
                ) from exc
            if (
                request.effective_revision_digest != effective_revision_digest
                or source_bundle_digest not in request.source_digests
                or authorization.effective_revision_digest != effective_revision_digest
                or authorization.classification_digest != classification_digest
                or authorization.request_digest
                != canonical_identity(request.to_dict()).uri
                or authorization.revoked
                or authorization.privileges
                != tuple(sorted(request.requested_privileges))
            ):
                raise PortContractError(
                    f"ports.{seam}.execution-evidence-mismatch",
                    f"{seam} execution evidence does not bind the exact candidate",
                )
            normalized["observation_request"] = request.to_dict()
            normalized["execution_authorization"] = authorization.to_dict()
        elif "observation_request" in case or "execution_authorization" in case:
            raise PortContractError(
                f"ports.{seam}.non-executing-evidence-invalid",
                f"{seam} non-executing profile cannot claim execution evidence",
            )
        normalized["case_id"] = case_id
        normalized["passed"] = case_passed
        result.append(normalized)
    return tuple(result)


def require_prepared_tree_result(
    value: JsonObject,
    *,
    source_bundle_digest: str,
    files: Mapping[str, bytes],
    dependency_resolution: DependencyResolutionResult,
) -> PreparedTreeResult:
    if _digest(value.get("tree_digest"), "prepared tree digest") != (
        source_bundle_digest
    ):
        raise PortContractError(
            "ports.workspace.prepared-tree-mismatch",
            "prepared workspace tree does not bind the generated source bundle",
        )
    raw_manifest = value.get("manifest")
    if isinstance(raw_manifest, (str, bytes, bytearray)) or not isinstance(
        raw_manifest, Sequence
    ):
        raise PortContractError(
            "ports.workspace.manifest-invalid",
            "prepared workspace result must contain a manifest",
        )
    manifest: list[tuple[str, str]] = []
    for item in raw_manifest:
        if (
            isinstance(item, (str, bytes, bytearray))
            or not isinstance(item, Sequence)
            or len(item) != 2
        ):
            raise PortContractError(
                "ports.workspace.manifest-invalid",
                "prepared workspace manifest entries must be path/digest pairs",
            )
        path = _text(item[0], "prepared manifest path")
        digest = _digest(item[1], "prepared manifest digest")
        manifest.append((path, digest))
    if not manifest:
        raise PortContractError(
            "ports.workspace.manifest-empty",
            "prepared workspace manifest cannot be empty",
        )
    expected_manifest = tuple(
        (path, f"sha256:{hashlib.sha256(content).hexdigest()}")
        for path, content in sorted(files.items())
    )
    if tuple(manifest) != expected_manifest:
        raise PortContractError(
            "ports.workspace.manifest-mismatch",
            "prepared workspace manifest does not describe the generated files",
        )
    resolution_identity, source_binding_identity, resolved_binding_identity = (
        _dependency_admission_identities(dependency_resolution)
    )
    for field, expected in (
        ("dependency_resolution_identity", resolution_identity),
        ("source_bom_binding_identity", source_binding_identity),
        ("resolved_bom_binding_identity", resolved_binding_identity),
    ):
        if _digest(value.get(field), f"prepared {field.replace('_', ' ')}") != expected:
            raise PortContractError(
                "ports.workspace.dependency-evidence-mismatch",
                "prepared workspace does not bind the exact dependency evidence",
            )
    return cast(
        PreparedTreeResult,
        {
            **dict(value),
            "tree_digest": source_bundle_digest,
            "manifest": tuple(manifest),
            "dependency_resolution_identity": resolution_identity,
            "source_bom_binding_identity": source_binding_identity,
            "resolved_bom_binding_identity": resolved_binding_identity,
        },
    )


def require_committed_tree_result(
    value: JsonObject,
    *,
    tree_digest: str,
    reference: str,
    dependency_resolution: DependencyResolutionResult,
) -> CommittedTreeResult:
    if _digest(value.get("tree_digest"), "committed tree digest") != tree_digest:
        raise PortContractError(
            "ports.workspace.committed-tree-mismatch",
            "workspace commit returned another tree",
        )
    if _text(value.get("reference"), "workspace reference") != reference:
        raise PortContractError(
            "ports.workspace.reference-mismatch",
            "workspace commit returned another reference",
        )
    path = _text(value.get("path"), "committed workspace path")
    source_intelligence_status = _source_intelligence_status(
        value.get("source_intelligence_status")
    )
    raw_source_intelligence = value.get("source_intelligence")
    source_intelligence = (
        require_source_intelligence_result(
            _mapping(raw_source_intelligence, "committed source intelligence"),
            source_tree_identity=tree_digest,
        )
        if source_intelligence_status["state"] == "current"
        else None
    )
    if source_intelligence_status["state"] != "current" and (
        raw_source_intelligence is not None
    ):
        raise PortContractError(
            "ports.source-intelligence.status-mismatch",
            "non-current source-intelligence status cannot carry evidence",
        )
    resolution_identity, source_binding_identity, resolved_binding_identity = (
        _dependency_admission_identities(dependency_resolution)
    )
    for field, expected in (
        ("dependency_resolution_identity", resolution_identity),
        ("source_bom_binding_identity", source_binding_identity),
        ("resolved_bom_binding_identity", resolved_binding_identity),
    ):
        if (
            _digest(value.get(field), f"committed {field.replace('_', ' ')}")
            != expected
        ):
            raise PortContractError(
                "ports.workspace.dependency-evidence-mismatch",
                "committed workspace does not bind the exact dependency evidence",
            )
    return cast(
        CommittedTreeResult,
        {
            **dict(value),
            "tree_digest": tree_digest,
            "reference": reference,
            "path": path,
            "source_intelligence": source_intelligence,
            "source_intelligence_status": source_intelligence_status,
            "dependency_resolution_identity": resolution_identity,
            "source_bom_binding_identity": source_binding_identity,
            "resolved_bom_binding_identity": resolved_binding_identity,
        },
    )


def _source_intelligence_status(value: object) -> JsonObject:
    try:
        return SourceIntelligenceStageStatus.from_dict(value).to_dict()
    except (TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.source-intelligence.status-invalid",
            "source-intelligence status is malformed or inconsistent",
        ) from exc


def _dependency_admission_identities(
    result: DependencyResolutionResult,
) -> tuple[str, str, str]:
    resolution_identity = _digest(
        result.get("resolution_identity"), "dependency resolution identity"
    )
    try:
        source = CycloneDxBomBinding.from_dict(result["source_bom"])
        resolved = CycloneDxBomBinding.from_dict(result["resolved_bom"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.workspace.dependency-evidence-invalid",
            "workspace dependency evidence bindings are invalid",
        ) from exc
    return resolution_identity, source.identity.uri, resolved.identity.uri


def require_source_intelligence_result(
    value: JsonObject,
    *,
    source_tree_identity: str,
) -> JsonObject:
    """Validate provider-neutral evidence for an exact source tree."""

    try:
        intelligence = SourceIntelligenceArtifact.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise PortContractError(
            "ports.source-intelligence.document-invalid",
            "source-intelligence result is malformed or internally inconsistent",
        ) from exc
    if intelligence.source_tree_identity != source_tree_identity:
        raise PortContractError(
            "ports.source-intelligence.tree-mismatch",
            "source-intelligence result describes another source tree",
        )
    return intelligence.to_dict()


@runtime_checkable
class ModelProvider(Protocol):
    provider_id: str

    def complete_structured(self, request: ModelInvocation) -> JsonObject: ...


@runtime_checkable
class Validator(Protocol):
    validator_id: str

    def validate(self, artifact: GeneratedArtifact) -> JsonObject: ...


@runtime_checkable
class Classifier(Protocol):
    classifier_id: str

    def classify(
        self, source: GeneratedArtifact, findings: Sequence[JsonObject]
    ) -> JsonObject: ...


@runtime_checkable
class BuildAuthorizer(Protocol):
    def authorize(
        self,
        classification: ClassificationResult,
        build_request: BuildRequestDocument,
    ) -> JsonObject: ...


@runtime_checkable
class Builder(Protocol):
    builder_id: str

    def build(
        self,
        request: BuildRequestDocument,
        authorization: BuildAuthorizationDocument,
    ) -> JsonObject: ...


@runtime_checkable
class BuildInputConsumptionAwareBuilder(Protocol):
    """Builder seam for evidence produced by a completed outer build system."""

    builder_id: str

    def build_with_input_consumption(
        self,
        request: BuildRequestDocument,
        authorization: BuildAuthorizationDocument,
        consumption: BuildInputConsumption,
    ) -> JsonObject: ...


@runtime_checkable
class DependencyResolver(Protocol):
    resolver_id: str

    def validate_source(self, artifact: GeneratedArtifact) -> JsonObject: ...

    def resolve(
        self,
        artifact: GeneratedArtifact,
        build: BuildResult,
    ) -> JsonObject: ...


@runtime_checkable
class GeneratedTestRunner(Protocol):
    runner_id: str

    def run(
        self,
        artifact: GeneratedArtifact,
        build: BuildResult,
        classification: ClassificationResult,
        dependency_resolution: DependencyResolutionResult,
        test_suite: ValidatedGeneratedTestSuite,
    ) -> JsonObject: ...


@runtime_checkable
class AcceptanceRunner(Protocol):
    runner_id: str
    acceptance_suite_identity: str

    def run(
        self,
        artifact: GeneratedArtifact,
        build: BuildResult,
        classification: ClassificationResult,
        generated_tests: GeneratedTestResult,
        dependency_resolution: DependencyResolutionResult,
    ) -> JsonObject: ...


@runtime_checkable
class WorkspaceCommitter(Protocol):
    def prepare(
        self,
        files: Mapping[str, bytes],
        dependency_resolution: DependencyResolutionResult,
    ) -> JsonObject: ...

    def commit(
        self,
        prepared: PreparedTreeResult,
        reference: str,
        dependency_resolution: DependencyResolutionResult,
    ) -> JsonObject: ...

    def recover(self) -> Sequence[JsonObject]: ...


@runtime_checkable
class EventStore(Protocol):
    def append(self, stream_id: str, event: JsonObject) -> None: ...

    def stream(self, stream_id: str) -> Iterable[JsonObject]: ...


def _exact_mapping(
    value: object, label: str, required: frozenset[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PortContractError(
            "ports.build-dependencies.object-invalid",
            f"{label} must be an object with string keys",
        )
    actual = frozenset(value)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PortContractError(
            "ports.build-dependencies.fields-invalid",
            f"{label} has invalid fields: {'; '.join(details)}",
        )
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PortContractError(
            "ports.build-dependencies.sequence-invalid",
            f"{label} must be an array",
        )
    return value


def _require_safe_logical_name(value: str) -> None:
    if (
        not _PORTABLE_LOGICAL_NAME.fullmatch(value)
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise PortContractError(
            "ports.build-dependencies.artifact-name-invalid",
            "build dependency evidence logical name must be a normalized relative "
            "portable path",
        )


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise PortContractError(
            "ports.build-dependencies.sequence-invalid",
            f"{label} must be an immutable sequence of non-empty strings",
        )
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise PortContractError(
            "ports.build-dependencies.sequence-order-invalid",
            f"{label} must be unique and sorted",
        )


def _require_complete_bzlmod_graph(
    root: BzlmodRootModule, modules: tuple[BzlmodModule, ...]
) -> None:
    by_key = {module.key: module for module in modules}
    for source, dependencies in (
        ("<root>", root.dependencies),
        *((module.key, module.dependencies) for module in modules),
    ):
        missing = sorted(set(dependencies) - set(by_key))
        if missing:
            raise PortContractError(
                "ports.build-dependencies.graph-reference-invalid",
                f"Bzlmod graph node {source!r} references unknown modules: "
                + ", ".join(missing),
            )
        if source in dependencies:
            raise PortContractError(
                "ports.build-dependencies.graph-cycle",
                f"Bzlmod graph node {source!r} depends on itself",
            )

    reachable: set[str] = set()
    pending = list(reversed(root.dependencies))
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        reachable.add(key)
        pending.extend(reversed(by_key[key].dependencies))
    if reachable != set(by_key):
        raise PortContractError(
            "ports.build-dependencies.graph-incomplete",
            "Bzlmod graph contains modules unreachable from the root module",
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortContractError(
            "ports.result.text-invalid", f"{label} must be a non-empty string"
        )
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label)
    if not _SHA256_URI.fullmatch(text):
        raise PortContractError(
            "ports.result.digest-invalid", f"{label} must be a sha256 identity"
        )
    return text


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PortContractError(
            "ports.result.integer-invalid",
            f"{label} must be an integer greater than or equal to {minimum}",
        )
    return value


def _object_sequence(value: object, label: str) -> tuple[JsonObject, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PortContractError(
            "ports.result.sequence-invalid", f"{label} must be an array of objects"
        )
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise PortContractError(
                "ports.result.sequence-invalid",
                f"{label} must contain only objects",
            )
        result.append(dict(item))
    return tuple(result)


def _mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise PortContractError(
            "ports.result.object-invalid", f"{label} must be an object"
        )
    return dict(value)


def _string_sequence(
    value: object, label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PortContractError(
            "ports.result.sequence-invalid", f"{label} must be an array of strings"
        )
    result = tuple(_text(item, label) for item in value)
    if not allow_empty and not result:
        raise PortContractError(
            "ports.result.sequence-empty", f"{label} cannot be empty"
        )
    if len(result) != len(set(result)):
        raise PortContractError(
            "ports.result.sequence-duplicate", f"{label} cannot contain duplicates"
        )
    return result


def _digest_sequence(value: object, label: str) -> tuple[str, ...]:
    values = _string_sequence(value, label)
    return tuple(_digest(item, label) for item in values)


__all__ = [
    "AUTHORIZED_EXECUTION_PROFILE",
    "AcceptanceResult",
    "AcceptanceRunner",
    "BuildAuthorizationDocument",
    "BuildAuthorizer",
    "BuildDependencyEvidenceArtifact",
    "BuildDependencyObservation",
    "BuildRequestDocument",
    "BuildResult",
    "Builder",
    "BzlmodModule",
    "BzlmodRootModule",
    "ClassificationResult",
    "Classifier",
    "CommittedTreeResult",
    "EventStore",
    "ExecutionAuthorizationDocument",
    "GeneratedArtifact",
    "GeneratedTestResult",
    "GeneratedTestRunner",
    "JsonObject",
    "ModelInvocation",
    "ModelProvider",
    "NON_EXECUTING_EXACT_TREE_PROFILE",
    "PortContractError",
    "ObservationRequestDocument",
    "PreparedTreeResult",
    "TestCaseResult",
    "ValidationResult",
    "Validator",
    "WorkspaceCommitter",
    "require_build_authorization_result",
    "require_build_request_document",
    "require_build_result",
    "require_acceptance_result",
    "require_classification_result",
    "require_committed_tree_result",
    "require_generated_test_result",
    "require_source_intelligence_result",
    "require_prepared_tree_result",
    "require_validation_result",
]

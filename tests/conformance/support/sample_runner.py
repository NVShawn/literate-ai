"""Framework conformance driver for the specification-only sample catalog."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from literate_ai.adapters.builders import (
    BAZEL_DEPENDENCY_EVIDENCE_OUTPUT,
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BazelConformanceBuildAdapter,
    BuildError,
    RustJavaScriptToolchain,
    discover_bazel_toolchain,
    discover_cpp_toolchain,
    discover_node_toolchain,
    discover_python_toolchain,
    discover_rust_toolchain,
)
from literate_ai.adapters.builders.cpp import bazel_sdk_build_options
from literate_ai.adapters.cache import (
    CachedBuildAdapter,
    CachedCodingCliSourceGenerator,
)
from literate_ai.adapters.cache.rebuild import (
    SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_MODE,
    SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT,
    RebuildSourceCacheProtocolError,
    read_rebuild_source_cache_control,
    read_rebuild_source_cache_decision,
    write_rebuild_source_cache_derivation_manifest,
    write_rebuild_source_cache_lifecycle,
)
from literate_ai.adapters.component_lock_planning import (
    ComponentCatalogSnapshot,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.conan_packaging import ConanPackageAdapter, ConanToolBinding
from literate_ai.adapters.dependencies import (
    CycloneDxLifecycleResolver,
    DependencyObservationError,
    PortableHostDependencyObserver,
)
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.intelligence import (
    DisabledGenerationIndexer,
    select_source_intelligence_provider,
)
from literate_ai.adapters.lifecycle import (
    AuthorizedHostArtifactRunner,
    CppBuildAdapter,
    GeneratedTreeSecurityClassifier,
    HostArtifactExecution,
    HostArtifactExecutionError,
    HostAuxiliaryArtifact,
    JavaScriptBuildAdapter,
    LifecycleEventStoreAdapter,
    LocalIndependentAcceptanceCase,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    PipelineValidatorAdapter,
    PolicyBuildAuthorizer,
    PythonBuildAdapter,
    RustBuildAdapter,
    RustJavaScriptBuildAdapter,
    SwiftBuildAdapter,
    WorkspaceTreeAdapter,
)
from literate_ai.adapters.live_test_selection import (
    apply_live_test_selection,
    log_live_session_models,
    resolve_live_test_selection,
    try_resolve_live_test_selection,
    verify_live_model_resolves,
)
from literate_ai.adapters.models import (
    SUPPORTED_IMPLEMENTATION_LANGUAGES,
    CodingCliError,
    CodingCliGeneration,
    CodingCliSourceGenerator,
    GenerationRecipe,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
    portable_source_entrypoint,
)
from literate_ai.adapters.packaging import (
    NpmPackageAdapter,
    WheelPackageAdapter,
    native_archive_package_plan,
    npm_archive_package_plan,
)
from literate_ai.adapters.project_lifecycle_driver import (
    lifecycle_driver_implementation_identity,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.adapters.specifications import (
    SPECIFICATION_PROVIDER_ERRORS,
    LoadedSpecification,
    OpenSpecProvider,
    load_specification_provider,
)
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    StandardProjectExecutionRequest,
    admit_locked_authored_assets,
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.adapters.worker_capabilities import probe_worker_capabilities
from literate_ai.application import (
    ComponentSourceEvidenceReadiness,
    GeneratedTestSuitePolicy,
    GenerationContextBinding,
    GenerationFailure,
    GenerationOrchestrator,
    GenerationRequest,
    GenerationStatus,
    HostComponentLifecycleSession,
    compile_generation_execution_plan,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
    project_locked_generation_authority,
)
from literate_ai.application.skill_closure import (
    SkillClosureError,
    load_admitted_skill_catalog,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleResult,
)
from literate_ai.application.standard_project_services import (
    StandardProjectApplicationService,
)
from literate_ai.application.standard_test_receipts import (
    project_standard_project_test_receipt,
)
from literate_ai.artifacts import (
    ArtifactResolver,
    BundleDependency,
    BundleKind,
    BundleManifest,
    BundleStore,
)
from literate_ai.cache_directories import resolve_cache_directories
from literate_ai.composition import ComponentComposer, CompositionError, FlavorResolver
from literate_ai.contracts import (
    PROJECT_TEST_RUNNER_EVIDENCE_KIND,
    REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
    REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND,
    SAMPLE_HARNESS_ENTRYPOINT_KINDS,
    Capability,
    CapabilityRequirement,
    ComponentChangeSurface,
    ComponentCoordinate,
    ComponentDefinition,
    ComponentInvalidationDecision,
    ComponentLock,
    ComponentRevision,
    ComponentRevisionRef,
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    ContributionKind,
    ContributionReference,
    CycloneDxManagedGraph,
    DependencyKind,
    Entrypoint,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
    FlavorAxis,
    FlavorCardinality,
    FlavorCoordinate,
    FlavorDefinition,
    FlavorRevision,
    FlavorSlot,
    GenerationComplexityBudget,
    HashAlgorithm,
    LockedComponentRevision,
    ModelScopeBinding,
    NvidiaProbeStatus,
    ProjectDefinition,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    RebuildSourceCacheControl,
    RebuildSourceCacheDecision,
    RebuildSourceCacheDerivationManifest,
    RebuildSourceCacheLifecycleBinding,
    RebuildSourceCacheLifecycleMember,
    RepositorySourceDependency,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SourceIntelligenceArtifact,
    SourceIntelligenceStage,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
    StandardLifecyclePolicy,
    TargetConstraint,
    TargetProfile,
    ToolchainConstraint,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
    merge_toolchain_constraints,
    rebuild_project_authority_identity,
    source_cache_model_selector,
)
from literate_ai.diagnostics import report_progress
from literate_ai.evidence_ledger import (
    EvidenceNode,
    attach_run,
    record_retained_output,
    retained_directory,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    MAJOR_REBUILD_GENERATION_MODE,
    GeneratedTestSuiteError,
    validate_generated_test_suite,
)
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    RoutingError,
    StageModelPolicy,
)
from literate_ai.ports import AUTHORIZED_EXECUTION_PROFILE
from literate_ai.projects import (
    PROJECT_FILENAME,
    PinnedInputClosure,
    PinnedInputClosureError,
    discover_project,
    project_boundary,
)
from literate_ai.publication import (
    FilesystemPublicationTarget,
    ImportPolicy,
    ImportRequest,
    PublicationError,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
)
from literate_ai.registry import (
    ComponentDescriptor,
    DescriptorRegistry,
    FlavorDescriptor,
)
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildRequest,
    BuildRequestDeclaration,
    FindingSeverity,
    LiveBuildAuthorizationVerifier,
    LiveObservationExecutionAuthorizationVerifier,
    OriginAttestation,
    RuleBasedSourceScanner,
    SecurityClassification,
    SecurityFinding,
    SecurityPolicy,
    SecurityProfile,
    baseline_cpp_rules,
    baseline_javascript_rules,
    baseline_python_rules,
    baseline_rust_rules,
    baseline_swift_rules,
)
from literate_ai.sources import SourceSnapshotter
from literate_ai.storage import AppendOnlyEventStore, FileSystemCAS, StorageSafetyError
from literate_ai.test_receipts import write_project_test_receipt_provisional
from literate_ai.test_runner_authority import (
    sample_test_runner_source_closure_identity,
)
from literate_ai.validation import (
    CppPortableLifetimeValidator,
    CppSourceValidator,
    CppTranslationUnitIncludeValidator,
    JavaScriptSourceValidator,
    PythonSyntaxValidator,
    RustSourceValidator,
    SwiftSourceValidator,
    ValidationPipeline,
)
from literate_ai.workflows import (
    StageDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStatus,
)
from literate_ai.workspace import WorkspaceTreeStore
from tests.conformance.support.runtime_oracles import (
    _critical_path_result,
    _dependency_plan_result,
    _framework_readiness_result,
    _ledger_result,
    _log_tally_result,
    _release_analysis_result,
    _release_dashboard_from_analysis,
    _release_dashboard_result,
    create_post_build_probe,
)

SAMPLE_SCHEMA = "literate-ai/conformance-sample@5"
SAMPLE_HARNESS_DIRECTORY = "_harness"
EXECUTION_CONTRACT_SCHEMA = "literate-ai/sample-execution-interface@3"
EXECUTION_ORACLE_SCHEMA = "literate-ai/sample-execution-oracle@1"
REPORT_SCHEMA = "literate-ai/conformance-report@7"
STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA = (
    "literate-ai/standard-sample-execution-report@1"
)
DURABLE_SPLIT_SERVICE_EXECUTION_REPORT_SCHEMA = (
    "literate-ai/durable-split-service-execution@1"
)
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_EXPECTED_LIFECYCLE_STEPS = (
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
_SAMPLE_PLANNER_ENDPOINT_ID = "sample-deterministic-spec-planner"
_MAX_GENERATED_CANDIDATE_ATTEMPTS = 3
_RETRYABLE_MODEL_STAGE_CONTRACT_FAILURE_CODES = frozenset(
    {
        "coding_cli.generated_cpp_bazel_output_collision",
        "coding_cli.generated_cpp_bazel_hdrs_unsupported",
        "coding_cli.generated_cpp_bazel_workspace_include_unsupported",
        "generated_tests.acceptance_signature_missing",
        "generated_tests.duplicate_arguments",
        "generated_tests.expected_result_shape_mismatch",
    }
)
_GENERATED_SOURCE_BUILD_FAILURE_CODES = frozenset(
    {
        "builder.bazel_analyze_failed",
        "builder.cpp_generated_source_rejected",
        "builder.rust_compile_failed",
        "builder.swift_generated_source_rejected",
    }
)
_GENERATED_SOURCE_VALIDATION_FAILURE_CODES = frozenset(
    {
        "dependencies.import-bom-mismatch",
        "dependencies.bzlmod-authority-unsupported",
        "dependencies.bzlmod-dependency-duplicate",
        "dependencies.bzlmod-generated-lock-forbidden",
        "dependencies.bzlmod-literal-invalid",
        "dependencies.bzlmod-module-ambiguous",
        "dependencies.bzlmod-module-invalid",
        "dependencies.bzlmod-module-location-invalid",
        "dependencies.bzlmod-module-name-invalid",
        "dependencies.bzlmod-source-intent-invalid",
        "dependencies.bzlmod-source-composition-invalid",
        "dependencies.bzlmod-workspace-unsupported",
    }
)
_GENERATED_SOURCE_VALIDATION_FAILURE_SUMMARIES = {
    "dependencies.import-bom-mismatch": (
        "generated source imports a module absent from both generated source paths "
        "and declared dependency metadata"
    ),
}
_RETRYABLE_GENERATED_SOURCE_FINDING_CODES = frozenset(
    {
        "validation.cpp_companion_header_missing",
        "validation.cpp_nonowning_text_member",
        "validation.cpp_translation_unit_included",
    }
)


class SampleFailure(RuntimeError):
    pass


def _validated_project_revision(root: Path) -> ContentIdentity:
    try:
        return validated_project_authority_identity(root)
    except ProjectValidationError as exc:
        raise SampleFailure(exc.message) from exc


def _receipted_project_revision(
    root: Path, component_lock_identities: tuple[ContentIdentity, ...]
) -> ContentIdentity:
    """Bind current validated authority to the exact outer-planned lock set."""

    return rebuild_project_authority_identity(
        _validated_project_revision(root),
        component_lock_identities,
    )


def _specification_contents(
    loaded: LoadedSpecification,
) -> tuple[tuple[str, str | bytes], ...]:
    context = () if loaded.context_document is None else (loaded.context_document,)
    return (*context, *loaded.contents)


class _GeneratedBehaviorMismatch(SampleFailure):
    """One generated expectation disagreed with its compiled implementation."""

    def __init__(
        self, *, case_id: str, expected_result: object, observed_result: object
    ) -> None:
        self.case_id = case_id
        self.expected_result_identity = canonical_identity(expected_result).uri
        self.observed_result_identity = canonical_identity(observed_result).uri
        self.rejection_kind = "generated-test-behavior-mismatch"
        self.case_evidence = {
            "case_id": self.case_id,
            "expected_result_identity": self.expected_result_identity,
            "observed_result_identity": self.observed_result_identity,
        }
        super().__init__(
            "compiled generated candidate disagreed with its generated expectation "
            f"for {case_id!r}: expected {self.expected_result_identity}, observed "
            f"{self.observed_result_identity}"
        )


class _GeneratedApplicationRejected(SampleFailure):
    """The exact generated application rejected its own admitted test case."""

    def __init__(
        self,
        *,
        case_id: str,
        expected_result: object,
        execution_error: HostArtifactExecutionError,
    ) -> None:
        digests = (execution_error.stdout_digest, execution_error.stderr_digest)
        if (
            execution_error.code != "host_execution.nonzero_exit"
            or execution_error.role not in {"application", "backend"}
            or type(execution_error.returncode) is not int
            or execution_error.returncode == 0
            or any(
                not isinstance(digest, str)
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])
                for digest in digests
            )
        ):
            raise ValueError(
                "generated application rejection lacks exact process evidence"
            )
        self.rejection_kind = "generated-application-nonzero-exit"
        self.case_evidence = {
            "case_id": case_id,
            "expected_result_identity": canonical_identity(expected_result).uri,
            "execution_failure_code": execution_error.code,
            "execution_role": execution_error.role,
            "returncode": execution_error.returncode,
            "stdout_digest": execution_error.stdout_digest,
            "stderr_digest": execution_error.stderr_digest,
        }
        super().__init__(
            "exact generated application returned nonzero for generated case "
            f"{case_id!r}"
        )


class _IndependentApplicationRejected(SampleFailure):
    """An exact verifier-only invocation rejected the generated application."""

    def __init__(
        self,
        *,
        verification_source: str,
        case_id: str,
        expected_result: object,
        execution_error: HostArtifactExecutionError,
    ) -> None:
        if verification_source not in {"pinned-oracle", "post-build-runtime-oracle"}:
            raise ValueError(
                "independent process rejection requires verifier authority"
            )
        validated = _GeneratedApplicationRejected(
            case_id=case_id,
            expected_result=expected_result,
            execution_error=execution_error,
        )
        self.rejection_kind = "independent-verifier-application-nonzero-exit"
        self.case_evidence = {
            **validated.case_evidence,
            "verification_source": verification_source,
        }
        super().__init__(
            "exact generated application returned nonzero for independent case "
            f"{case_id!r}"
        )


def _generated_application_rejection(
    *,
    verification_source: str,
    case_id: str,
    expected_result: object,
    execution_error: HostArtifactExecutionError,
) -> _GeneratedApplicationRejected | _IndependentApplicationRejected | None:
    """Return a candidate marker only for an exact attributable process rejection."""

    if execution_error.code != "host_execution.nonzero_exit":
        return None
    try:
        if verification_source == "generated-implementation-test":
            return _GeneratedApplicationRejected(
                case_id=case_id,
                expected_result=expected_result,
                execution_error=execution_error,
            )
        if verification_source in {"pinned-oracle", "post-build-runtime-oracle"}:
            return _IndependentApplicationRejected(
                verification_source=verification_source,
                case_id=case_id,
                expected_result=expected_result,
                execution_error=execution_error,
            )
        return None
    except ValueError:
        return None


class _GeneratedCandidateRejected(SampleFailure):
    """A disposable generated test disproved its exact source candidate."""

    def __init__(
        self,
        mismatch: _GeneratedBehaviorMismatch | _GeneratedApplicationRejected,
        *,
        source_bundle_digest: str,
        artifact_digest: str,
        generated_test_suite_identity: str,
    ) -> None:
        self.rejection_kind = mismatch.rejection_kind
        self.case_evidence = dict(mismatch.case_evidence)
        self.source_bundle_digest = source_bundle_digest
        self.artifact_digest = artifact_digest
        self.generated_test_suite_identity = generated_test_suite_identity
        super().__init__(str(mismatch))


class _IndependentBehaviorMismatch(SampleFailure):
    """Verifier-only evidence disproved one exact generated candidate."""

    def __init__(
        self,
        *,
        case_id: str,
        verification_source: str,
        expected_result: object,
        observed_result: object,
    ) -> None:
        if verification_source not in {"pinned-oracle", "post-build-runtime-oracle"}:
            raise ValueError("independent mismatch requires verifier-only authority")
        self.rejection_kind = "independent-verifier-behavior-mismatch"
        self.case_evidence = {
            "case_id": case_id,
            "verification_source": verification_source,
            "expected_result_identity": canonical_identity(expected_result).uri,
            "observed_result_identity": canonical_identity(observed_result).uri,
        }
        super().__init__(
            "compiled candidate differs from independent verifier evidence for "
            f"{case_id!r}"
        )


class _IndependentCandidateRejected(SampleFailure):
    """A verifier-only case rejected source without disclosing its values."""

    def __init__(
        self,
        mismatch: _IndependentBehaviorMismatch | _IndependentApplicationRejected,
        *,
        source_bundle_digest: str,
        artifact_digest: str,
        independent_acceptance_suite_identity: str,
    ) -> None:
        self.case_evidence = dict(mismatch.case_evidence)
        self.rejection_kind = mismatch.rejection_kind
        self.source_bundle_digest = source_bundle_digest
        self.artifact_digest = artifact_digest
        self.independent_acceptance_suite_identity = (
            independent_acceptance_suite_identity
        )
        super().__init__(str(mismatch))


@dataclass(frozen=True, slots=True)
class _ExecutionVariant:
    variant_id: str
    slot_values: tuple[tuple[str, str], ...]
    languages: tuple[str, ...]

    def value_for(self, slot_id: str) -> str:
        values = self.values_for(slot_id)
        if len(values) != 1:
            raise SampleFailure(
                f"sample slot {slot_id!r} does not have one scalar value"
            )
        return values[0]

    def values_for(self, slot_id: str) -> tuple[str, ...]:
        return tuple(
            value
            for selected_slot, value in self.slot_values
            if selected_slot == slot_id
        )

    def has_value(self, slot_id: str) -> bool:
        return any(
            selected_slot == slot_id for selected_slot, _value in self.slot_values
        )

    def slot_values_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for slot_id, _value in self.slot_values:
            values = self.values_for(slot_id)
            result[slot_id] = values[0] if len(values) == 1 else list(values)
        return result


_LANGUAGE_FLAVOR_COORDINATES = {
    language: f"flavor://literate-ai/lang-{language}"
    for language in SUPPORTED_IMPLEMENTATION_LANGUAGES
}
_BUILD_FLAVOR_COORDINATES = {
    "make": "flavor://literate-ai/build-make",
    "bazel": "flavor://literate-ai/build-bazel",
    "cmake": "flavor://literate-ai/build-cmake",
}
_OS_FLAVOR_COORDINATES = {
    "linux": "flavor://literate-ai/os-linux",
    "macos": "flavor://literate-ai/os-macos",
    "windows": "flavor://literate-ai/os-windows",
}
_PACKAGE_FLAVOR_COORDINATES = {
    provider: f"flavor://literate-ai/package-{provider}"
    for provider in (
        "apt",
        "brew",
        "chocolatey",
        "conan",
        "npm",
        "pip",
        "winget",
    )
}
_PACKAGE_FLAVOR_COORDINATES["crates"] = "flavor://literate-ai/package-cargo"
_DEPLOY_FLAVOR_COORDINATES = {
    "docker": "flavor://literate-ai/deploy-docker",
    "python-container-base": "flavor://literate-ai/container-python",
}
_UI_FLAVOR_COORDINATES = {
    "react": "flavor://literate-ai/ui-react",
}
_NON_LANGUAGE_LANG_ALIASES = frozenset(
    {
        "lang-javascript-react",
        "flavor://literate-ai/lang-javascript-react",
        "flavor://samples/lang-javascript-react",
    }
)


def _language_target(value: str) -> str:
    """Resolve a canonical language coordinate, retaining legacy target aliases."""

    for language, coordinate in _LANGUAGE_FLAVOR_COORDINATES.items():
        if value in {
            language,
            f"lang-{language}",
            coordinate,
            coordinate.replace("flavor://literate-ai/", "flavor://samples/"),
        }:
            return language
    raise SampleFailure(f"unsupported sample language Flavor: {value}")


def _axis_target(axis: FlavorAxis, value: str) -> str:
    if axis is FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM:
        return _language_target(value)
    if axis is FlavorAxis.BUILD_SYSTEM:
        for target, coordinate in _BUILD_FLAVOR_COORDINATES.items():
            if value in {target, coordinate}:
                return target
    if axis is FlavorAxis.PLATFORM_OS:
        for target, coordinate in _OS_FLAVOR_COORDINATES.items():
            if value in {target, f"os-{target}", coordinate}:
                return target
    if axis is FlavorAxis.PACKAGING:
        for target, coordinate in _PACKAGE_FLAVOR_COORDINATES.items():
            if value in {target, f"package-{target}", f"package.{target}", coordinate}:
                return target
    if axis is FlavorAxis.DEPLOYMENT:
        for target, coordinate in _DEPLOY_FLAVOR_COORDINATES.items():
            if value in {target, f"deploy-{target}", coordinate}:
                return target
    if axis is FlavorAxis.IMPLEMENTATION_UI_FRAMEWORK:
        for target, coordinate in _UI_FLAVOR_COORDINATES.items():
            if value in {
                target,
                f"ui-{target}",
                "lang-javascript-react",
                coordinate,
            }:
                return target
        raise SampleFailure(f"unsupported sample UI Flavor: {value}")
    return value


def _matrix_languages(flavor_selectors: Sequence[str]) -> tuple[str, ...]:
    requested: list[str] = []
    for raw in flavor_selectors:
        selector = raw.removeprefix("+")
        if selector in {
            "lang.*",
            "flavor://samples/lang-*",
            "flavor://literate-ai/lang-*",
        }:
            return tuple(SUPPORTED_IMPLEMENTATION_LANGUAGES)
        if selector in _NON_LANGUAGE_LANG_ALIASES:
            continue
        if selector.startswith(
            ("lang-", "flavor://samples/lang-", "flavor://literate-ai/lang-")
        ):
            language = _language_target(selector)
            if language not in requested:
                requested.append(language)
    return tuple(requested) or ("python",)


def _filter_pinned_languages(
    pinned_languages: tuple[str, ...], flavor_selectors: Sequence[str]
) -> tuple[str, ...]:
    """Apply ordered +/- selectors without expanding explicit sample authority."""

    selected = list(pinned_languages)
    pinned = frozenset(pinned_languages)
    for raw in flavor_selectors:
        if not raw.startswith(("+", "-")):
            continue
        operation, selector = raw[0], raw[1:]
        if selector in {
            "lang.*",
            "flavor://samples/lang-*",
            "flavor://literate-ai/lang-*",
        }:
            selected = list(pinned_languages) if operation == "+" else []
            continue
        if selector in _NON_LANGUAGE_LANG_ALIASES:
            continue
        if not selector.startswith(
            ("lang-", "flavor://samples/lang-", "flavor://literate-ai/lang-")
        ):
            continue
        language = _language_target(selector)
        if operation == "-":
            selected = [item for item in selected if item != language]
        elif language in pinned and language not in selected:
            selected.append(language)
    if not selected:
        raise SampleFailure("language Flavor selectors removed every pinned language")
    return tuple(selected)


def _matrix_builds(flavor_selectors: Sequence[str]) -> tuple[str, ...]:
    requested: list[str] = []
    for raw in flavor_selectors:
        selector = raw.removeprefix("+")
        if selector in {"build.*", "flavor://literate-ai/build-*"}:
            return ("make", "bazel")
        for target, coordinate in _BUILD_FLAVOR_COORDINATES.items():
            if selector in {coordinate, coordinate.rsplit("/", 1)[-1]}:
                if target not in requested:
                    requested.append(target)
    return tuple(requested) or ("make",)


def _require_host_os_selection(flavor_selectors: Sequence[str]) -> None:
    host = _host_os()
    for raw in flavor_selectors:
        selector = raw.removeprefix("+")
        if selector in {
            "os.*",
            "flavor://samples/os-*",
            "flavor://literate-ai/os-*",
        }:
            continue
        for os_name, coordinate in _OS_FLAVOR_COORDINATES.items():
            if (
                selector
                in {
                    f"os-{os_name}",
                    coordinate,
                    coordinate.replace("flavor://literate-ai/", "flavor://samples/"),
                }
                and os_name != host
            ):
                raise SampleFailure(
                    f"selected OS Flavor {selector!r} is incompatible with host "
                    f"{_OS_FLAVOR_COORDINATES[host]}"
                )


@dataclass(frozen=True, slots=True)
class _SampleInputClosures:
    generation: PinnedInputClosure
    verifier: PinnedInputClosure

    def __post_init__(self) -> None:
        generation = self.generation.to_dict()["inputs"]
        if any(
            str(label).startswith("verifier:")
            for item in generation
            if isinstance(item, Mapping)
            for label in item.get("labels", ())
        ):
            raise SampleFailure(
                "private sample verifier authority entered generation context"
            )


def sample_harness_root(sample_root: Path) -> Path:
    """Return the central verifier-only authority directory for one sample."""

    project_root = project_boundary(sample_root, legacy=sample_root.parent)
    catalog_root = (
        project_root
        if project_root.name == "samples"
        and sample_root.parent.resolve(strict=True) == project_root.resolve(strict=True)
        else project_root / "samples"
    )
    root = catalog_root / SAMPLE_HARNESS_DIRECTORY / sample_root.name
    if root.is_symlink():
        raise SampleFailure("sample harness directory cannot be a symbolic link")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise SampleFailure(
            f"sample harness is unavailable: {sample_root.name}"
        ) from exc
    expected_parent = (catalog_root / SAMPLE_HARNESS_DIRECTORY).resolve(strict=True)
    if not resolved.is_relative_to(expected_parent) or not resolved.is_dir():
        raise SampleFailure("sample harness escapes its central authority root")
    return resolved


def sample_harness_manifest(sample_root: Path) -> Path:
    """Locate verifier metadata without searching author-facing Component files."""

    path = sample_harness_root(sample_root) / "sample.json"
    if path.is_symlink() or not path.is_file():
        raise SampleFailure("sample harness manifest is unavailable")
    return path


def sample_harness_oracle(sample_root: Path, reference: ContentReference) -> Path:
    """Resolve the canonical private oracle strictly beneath the harness root."""

    if (
        reference.kind != "acceptance-oracle"
        or reference.uri != "acceptance/oracle.json"
    ):
        raise SampleFailure("sample must use its canonical verifier-only oracle")
    root = sample_harness_root(sample_root)
    configured = root.joinpath(*Path(reference.uri).parts)
    if configured.is_symlink():
        raise SampleFailure("sample acceptance oracle cannot be a symbolic link")
    try:
        path = configured.resolve(strict=True)
    except OSError as exc:
        raise SampleFailure("sample acceptance oracle is unavailable") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise SampleFailure("sample acceptance oracle escapes its harness")
    return path


def _require_sample_inputs_unchanged(
    closure: PinnedInputClosure, *, label: str
) -> None:
    try:
        closure.require_unchanged()
    except PinnedInputClosureError as exc:
        raise SampleFailure(f"{label} input closure changed: {exc.message}") from exc


def _sample_input_closure_report(closure: PinnedInputClosure) -> dict[str, object]:
    return {
        "identity": closure.identity,
        "file_count": closure.file_count,
        "total_bytes": closure.total_bytes,
    }


_RESULT_PRIMITIVES = frozenset(
    {
        "boolean",
        "integer",
        "null",
        "sha256-hex",
        "sha256-prefix-12",
        "string",
    }
)
_MAXIMUM_RESULT_SHAPE_DEPTH = 16


def _require_valid_result_shape(
    shape: object, *, path: str = "$", depth: int = 0
) -> None:
    if depth > _MAXIMUM_RESULT_SHAPE_DEPTH:
        raise SampleFailure("sample result shape exceeds its nesting limit")
    if isinstance(shape, str):
        alternatives = shape.split("|")
        if (
            not alternatives
            or alternatives != sorted(set(alternatives))
            or any(item not in _RESULT_PRIMITIVES for item in alternatives)
        ):
            raise SampleFailure(f"sample result shape has an invalid type at {path}")
        return
    if isinstance(shape, list):
        if len(shape) != 1:
            raise SampleFailure(
                f"sample result array shape must contain one item at {path}"
            )
        _require_valid_result_shape(shape[0], path=f"{path}[]", depth=depth + 1)
        return
    if isinstance(shape, dict):
        if not shape or any(not isinstance(key, str) or not key for key in shape):
            raise SampleFailure(f"sample result object shape is invalid at {path}")
        for key, child in shape.items():
            _require_valid_result_shape(child, path=f"{path}.{key}", depth=depth + 1)
        return
    raise SampleFailure(f"sample result shape is invalid at {path}")


def _require_result_matches_shape(
    value: object, shape: object, *, path: str = "$"
) -> None:
    if isinstance(shape, str):
        alternatives = shape.split("|")
        is_text = type(value) is str
        is_lower_hex = is_text and all(
            character in "0123456789abcdef" for character in value
        )
        matches = {
            "boolean": type(value) is bool,
            "integer": type(value) is int,
            "null": value is None,
            "sha256-hex": is_lower_hex and len(value) == 64,
            "sha256-prefix-12": is_lower_hex and len(value) == 12,
            "string": type(value) is str,
        }
        if not any(matches[item] for item in alternatives):
            raise SampleFailure(f"sample result does not match its shape at {path}")
        return
    if isinstance(shape, list):
        if not isinstance(value, list):
            raise SampleFailure(f"sample result does not match its shape at {path}")
        for index, item in enumerate(value):
            _require_result_matches_shape(item, shape[0], path=f"{path}[{index}]")
        return
    assert isinstance(shape, dict)
    if not isinstance(value, dict) or set(value) != set(shape):
        raise SampleFailure(f"sample result does not match its shape at {path}")
    for key, child in shape.items():
        _require_result_matches_shape(value[key], child, path=f"{path}.{key}")


def _verification_cases(
    generated_test_suite,
    invocations: Sequence[object],
    oracle_results: Sequence[object],
    runtime_probe,
) -> list[tuple[str, list[object], object, str]]:
    """Compose generated checks and independent verifier checks without conflation."""

    cases: list[tuple[str, list[object], object, str]] = [
        (
            case.case_id,
            list(case.arguments),
            case.expected_result,
            "generated-implementation-test",
        )
        for case in generated_test_suite.cases
    ]
    cases.extend(
        _independent_verification_cases(
            invocations,
            oracle_results,
            runtime_probe,
            reserved_case_ids=tuple(item[0] for item in cases),
        )
    )
    return cases


def _independent_verification_cases(
    invocations: Sequence[object],
    oracle_results: Sequence[object],
    runtime_probe,
    *,
    reserved_case_ids: Sequence[str] = (),
) -> list[tuple[str, list[object], object, str]]:
    """Compose verifier-only fixed and post-build cases outside model authority."""

    cases: list[tuple[str, list[object], object, str]] = []
    for invocation, expected in zip(invocations, oracle_results, strict=True):
        if not isinstance(invocation, Mapping) or not isinstance(expected, Mapping):
            raise SampleFailure("verifier case is not an object")
        arguments = invocation.get("arguments")
        if not isinstance(arguments, list):
            raise SampleFailure("verifier case arguments are not an array")
        cases.append(
            (
                str(invocation.get("case_id")),
                arguments,
                expected.get("expected_result"),
                "pinned-oracle",
            )
        )
    cases.append(
        (
            runtime_probe.case_id,
            runtime_probe.arguments,
            runtime_probe.expected_result,
            "post-build-runtime-oracle",
        )
    )
    case_ids = [*reserved_case_ids, *(item[0] for item in cases)]
    if len(case_ids) != len(set(case_ids)):
        raise SampleFailure(
            "generated implementation and verifier test case IDs must be disjoint"
        )
    return cases


def _primary_pinned_case(
    case_results: Sequence[Mapping[str, object]],
    primary_case_id: str,
) -> Mapping[str, object]:
    """Select the acceptance result explicitly; report ordering is not authority."""

    for result in case_results:
        if (
            result.get("verification_source") == "pinned-oracle"
            and result.get("case_id") == primary_case_id
        ):
            return result
    raise SampleFailure(
        f"pinned primary acceptance result is missing for {primary_case_id!r}"
    )


class _ExactSampleReadiness:
    def __init__(
        self,
        specification_id: str,
        flavor_specification_ids: Sequence[str],
        skill_ids: Sequence[str],
        execution_contract_id: str,
    ) -> None:
        self.specification_id = specification_id
        self.flavor_specification_ids = tuple(flavor_specification_ids)
        self.skill_ids = tuple(skill_ids)
        self.execution_contract_id = execution_contract_id

    def inspect(
        self,
        authority: LockedGenerationAuthority,
        component_revision: ContentIdentity,
    ) -> ComponentSourceEvidenceReadiness:
        if not any(
            item.revision.identity == component_revision
            for item in authority.lock.nodes
        ):
            raise SampleFailure("readiness target is absent from locked authority")
        return ComponentSourceEvidenceReadiness(
            component_revision=component_revision,
            source_snapshot_ids=(),
            evidence_ids=(
                self.specification_id,
                *self.flavor_specification_ids,
                *self.skill_ids,
                self.execution_contract_id,
            ),
            source_ready=True,
            evidence_ready=True,
        )


class _DerivationPlanned(RuntimeError):
    def __init__(
        self,
        cache_key: SourceDerivationCacheKey,
        component_lock_identity: ContentIdentity,
    ) -> None:
        self.cache_key = cache_key
        self.component_lock_identity = component_lock_identity
        super().__init__(cache_key.identity.uri)


class _DerivationPlanningSourceGenerator:
    """Compute the exact generation request without invoking a coding agent."""

    def __init__(self, generator: CodingCliSourceGenerator) -> None:
        self.generator = generator
        self.selection = generator.selection

    def generate(
        self,
        recipe: GenerationRecipe,
        *,
        output_root: Path,
        execution_plan,
        stage_request: Mapping[str, object] | None = None,
    ) -> CodingCliGeneration:
        del output_root
        if stage_request is None:
            raise SampleFailure("derivation planning requires the exact model stage")
        request_identity = self.generator.planned_request_identity(
            recipe,
            execution_plan=execution_plan,
            stage_request=stage_request,
        )
        raise _DerivationPlanned(
            SourceDerivationCacheKey(
                recipe_identity=ContentIdentity.parse_uri(recipe.identity),
                execution_plan_identity=execution_plan.identity,
                coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
                    self.selection.tool_binding_identity
                ),
                model_binding=SourceCacheModelBinding(
                    self.selection.name,
                    source_cache_model_selector(
                        recipe.model_for(self.selection.name),
                        path="sample.derivation_plan.model",
                    ),
                ),
                request_identity=request_identity,
            ),
            recipe.component_lock_identity,
        )


def _planned_derivation(error: BaseException) -> _DerivationPlanned | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, _DerivationPlanned):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


class _SpecificationCompilerModel:
    """Generation-model port backed by one selected coding CLI or test fixture."""

    def __init__(
        self,
        *,
        recipe: GenerationRecipe,
        source_generator,
        generation_root: Path,
        specification: SpecificationSet,
        flavor_specification_ids: Sequence[str],
        skill_ids: Sequence[str],
        execution_contract_id: str,
        requirement_id: str,
        scenario_id: str,
        execution_plan,
        candidate_feedback: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.recipe = recipe
        self.source_generator = source_generator
        self.generation_root = generation_root
        self.specification = specification
        self.flavor_specification_ids = tuple(flavor_specification_ids)
        self.skill_ids = tuple(skill_ids)
        self.execution_contract_id = execution_contract_id
        self.requirement_id = requirement_id
        self.scenario_id = scenario_id
        self.execution_plan = execution_plan
        self.candidate_feedback = tuple(dict(item) for item in candidate_feedback)
        self.calls: list[str] = []
        self.generated_files: dict[str, str] | None = None
        self.generation: CodingCliGeneration | None = None
        selection = getattr(source_generator, "selection", None)
        name = getattr(selection, "name", "external-adapter")
        self.provider_id = f"model-provider:coding-cli/{name}@1"

    def complete_structured(
        self, request: Mapping[str, object]
    ) -> Mapping[str, object]:
        expected_evidence = [
            self.specification.identity.uri,
            *self.flavor_specification_ids,
            *self.skill_ids,
            self.execution_contract_id,
        ]
        if request.get("source_snapshot_ids") != []:
            raise SampleFailure("greenfield sample generation received cached source")
        if request.get("evidence_ids") != expected_evidence:
            raise SampleFailure(
                "sample model received the wrong specification evidence"
            )
        instructions = request.get("instructions")
        if (
            not isinstance(instructions, str)
            or self.recipe.identity not in instructions
        ):
            raise SampleFailure("coding CLI request is not bound to the Flavor recipe")
        stage = request.get("stage_id")
        route = request.get("route_decision")
        expected_route = next(
            (
                decision
                for model_stage, decision in zip(
                    self.execution_plan.model_stages,
                    self.execution_plan.route_decisions,
                    strict=True,
                )
                if model_stage.stage_id == stage
            ),
            None,
        )
        if (
            expected_route is None
            or not isinstance(route, Mapping)
            or route.get("selected_endpoint_id") != expected_route.selected_endpoint_id
        ):
            raise SampleFailure(
                "sample model dispatcher received the wrong exact route"
            )
        if stage == "plan":
            if expected_route.selected_endpoint_id != _SAMPLE_PLANNER_ENDPOINT_ID:
                raise SampleFailure(
                    "sample planning was not routed to the local planner"
                )
            self.calls.append("plan")
            result = {
                "specification_id": self.specification.identity.uri,
                "recipe_identity": self.recipe.identity,
                "flavor_specification_ids": list(self.flavor_specification_ids),
                "generation_skill_ids": list(self.skill_ids),
                "requirement_id": self.requirement_id,
                "scenario_id": self.scenario_id,
                "required_entrypoints": list(self.recipe.all_required_entrypoints),
            }
            if self.candidate_feedback:
                result["previous_candidate_rejections"] = list(self.candidate_feedback)
            return result
        if stage == "generate":
            if expected_route.selected_endpoint_id == _SAMPLE_PLANNER_ENDPOINT_ID:
                raise SampleFailure("sample generation was routed to the local planner")
            prior = request.get("prior_stage_outputs")
            plan = prior.get("plan") if isinstance(prior, Mapping) else None
            if (
                not isinstance(plan, Mapping)
                or plan.get("specification_id") != self.specification.identity.uri
                or plan.get("recipe_identity") != self.recipe.identity
                or plan.get("flavor_specification_ids")
                != list(self.flavor_specification_ids)
                or plan.get("generation_skill_ids") != list(self.skill_ids)
                or plan.get("requirement_id") != self.requirement_id
                or plan.get("scenario_id") != self.scenario_id
                or plan.get("required_entrypoints")
                != list(self.recipe.all_required_entrypoints)
                or plan.get("previous_candidate_rejections", [])
                != list(self.candidate_feedback)
            ):
                raise SampleFailure(
                    "sample generation is not bound to its OpenSpec plan"
                )
            self.calls.append("generate")
            self.generation = self.source_generator.generate(
                self.recipe,
                output_root=self.generation_root,
                execution_plan=self.execution_plan,
                stage_request=request,
            )
            expected_route = next(
                route.digest
                for model_stage, route in zip(
                    self.execution_plan.model_stages,
                    self.execution_plan.route_decisions,
                    strict=True,
                )
                if model_stage.stage_id == "generate"
            )
            if (
                self.generation.execution_plan_identity
                != self.execution_plan.identity.uri
                or self.generation.requested_model_stages != ("generate",)
                or self.generation.requested_route_decision_digests != (expected_route,)
            ):
                raise SampleFailure(
                    "coding CLI request was not bound to the orchestrated "
                    "generation stage"
                )
            self.generated_files = dict(self.generation.files)
            source_intelligence_status = self.generation.source_intelligence_status
            if source_intelligence_status == "current":
                if self.generation.source_intelligence is None:
                    raise SampleFailure(
                        "coding CLI reported source-intelligence status "
                        "'current', but the required artifact was absent"
                    )
            elif source_intelligence_status in {"off", "unavailable"}:
                if self.generation.source_intelligence is not None:
                    raise SampleFailure(
                        "coding CLI reported source-intelligence status "
                        f"'{source_intelligence_status}', but the artifact was present"
                    )
                if (
                    source_intelligence_status == "unavailable"
                    and not self.generation.source_intelligence_reason_code
                ):
                    raise SampleFailure(
                        "coding CLI reported source-intelligence status "
                        "'unavailable', but the required reason code was absent"
                    )
            else:
                raise SampleFailure(
                    "coding CLI observed source-intelligence status "
                    f"{source_intelligence_status!r}; required 'current', 'off', "
                    "or 'unavailable'"
                )
            if self.generation.source_sbom is None:
                raise SampleFailure("coding CLI omitted mandatory source SBOM evidence")
            return {
                "files": self.generated_files,
                "source_intelligence": (
                    self.generation.source_intelligence.to_dict()
                    if self.generation.source_intelligence is not None
                    else None
                ),
                "source_intelligence_status": source_intelligence_status,
                "source_intelligence_reason_code": (
                    self.generation.source_intelligence_reason_code
                ),
                "source_sbom": self.generation.source_sbom.to_dict(),
                "specification_id": self.specification.identity.uri,
                "recipe_identity": self.recipe.identity,
                "flavor_specification_ids": list(self.flavor_specification_ids),
                "generation_skill_ids": list(self.skill_ids),
                "execution_contract_id": self.execution_contract_id,
            }
        raise SampleFailure(f"unknown sample model stage: {stage!r}")


def _coding_cli_generation_provenance(
    model: _SpecificationCompilerModel,
) -> dict[str, object]:
    """Return exact, validated evidence for the coding CLI invocation."""

    generation = model.generation
    selection = getattr(model.source_generator, "selection", None)
    if generation is None or selection is None:
        raise SampleFailure("sample omitted coding CLI generation provenance")
    source_intelligence_status = generation.source_intelligence_status
    if source_intelligence_status == "current":
        if generation.source_intelligence is None:
            raise SampleFailure(
                "sample reported source-intelligence status 'current', but the "
                "required artifact was absent"
            )
    elif source_intelligence_status in {"off", "unavailable"}:
        if generation.source_intelligence is not None:
            raise SampleFailure(
                "sample reported source-intelligence status "
                f"'{source_intelligence_status}', but the artifact was present"
            )
        if (
            source_intelligence_status == "unavailable"
            and not generation.source_intelligence_reason_code
        ):
            raise SampleFailure(
                "sample reported source-intelligence status 'unavailable', but "
                "the required reason code was absent"
            )
    else:
        raise SampleFailure(
            "sample observed source-intelligence status "
            f"{source_intelligence_status!r}; required 'current', 'off', or "
            "'unavailable'"
        )
    if generation.source_sbom is None:
        raise SampleFailure("sample omitted source-SBOM provenance")
    try:
        ContentIdentity.parse_uri(generation.request_identity or "")
        ContentIdentity.parse_uri(generation.command_identity or "")
        ContentIdentity.parse_uri(generation.executable_identity or "")
        ContentIdentity.parse_uri(generation.coding_cli_selection_identity or "")
        ContentIdentity.parse_uri(generation.coding_cli_tool_binding_identity or "")
        ContentIdentity.parse_uri(generation.generated_test_suite_identity or "")
        if source_intelligence_status == "current":
            assert generation.source_intelligence is not None
            ContentIdentity.parse_uri(
                generation.source_intelligence.source_tree_identity
            )
            ContentIdentity.parse_uri(
                generation.source_intelligence.source_snapshot_identity
            )
            ContentIdentity.parse_uri(generation.source_intelligence.artifact_binding)
            ContentIdentity.parse_uri(
                generation.source_intelligence.intelligence_identity
            )
    except (TypeError, ValueError) as exc:
        raise SampleFailure(
            "sample coding CLI provenance contains an invalid content identity"
        ) from exc
    isolation = selection.isolation
    expected_command_identity = canonical_identity(tuple(generation.command)).uri
    if (
        generation.coding_cli != selection.name
        or generation.recipe_identity != model.recipe.identity
        or not generation.command
        or generation.command[0] != selection.executable
        or generation.executable != selection.executable
        or generation.executable_identity != selection.executable_identity
        or generation.command_identity != expected_command_identity
        or generation.coding_cli_selection_identity != selection.identity
        or generation.coding_cli_tool_binding_identity
        != selection.tool_binding_identity
        or generation.isolation_profile != isolation.profile
        or generation.hermetic != isolation.hermetic
        or generation.generation_mode != MAJOR_REBUILD_GENERATION_MODE
    ):
        raise SampleFailure(
            "sample coding CLI provenance does not match the exact selected invocation"
        )
    selection.require_unchanged()
    isolation_evidence = {
        **isolation.to_dict(),
        "environment_keys": list(generation.environment_keys),
    }
    return {
        "coding_cli": generation.coding_cli,
        "coding_model": source_cache_model_selector(
            generation.model,
            path="sample.generation_provenance.model",
        ),
        "coding_cli_executable": generation.executable,
        "coding_cli_executable_identity": generation.executable_identity,
        "coding_cli_selection_identity": generation.coding_cli_selection_identity,
        "coding_cli_tool_binding_identity": (
            generation.coding_cli_tool_binding_identity
        ),
        "coding_cli_isolation": {
            "identity": canonical_identity(isolation_evidence).uri,
            **isolation_evidence,
        },
        "request_identity": generation.request_identity,
        "command_identity": generation.command_identity,
        "execution_plan_identity": generation.execution_plan_identity,
        "requested_model_stages": list(generation.requested_model_stages),
        "requested_route_decision_digests": list(
            generation.requested_route_decision_digests
        ),
        "generation_mode": generation.generation_mode,
        "generated_test_suite_identity": generation.generated_test_suite_identity,
        "generated_source_intelligence": (
            generation.source_intelligence.to_dict()
            if generation.source_intelligence is not None
            else None
        ),
        "source_intelligence_status": source_intelligence_status,
        "source_intelligence_reason_code": generation.source_intelligence_reason_code,
        "source_sbom": generation.source_sbom.to_dict(),
    }


def _execution_contract(
    sample_root: Path,
    definition: ComponentDefinition,
    loaded: LoadedSpecification,
) -> tuple[dict[str, object], RecipeDocument]:
    legacy_reference = None
    if definition.acceptance_contracts:
        if (
            len(definition.acceptance_contracts) != 1
            or definition.acceptance_contracts[0].kind != "acceptance-contract"
            or definition.acceptance_contracts[0].uri != "acceptance/execution.json"
        ):
            raise SampleFailure("legacy sample acceptance contract is not canonical")
        legacy_reference = definition.acceptance_contracts[0]
    harness_root = sample_harness_root(sample_root)
    contract_path = harness_root / "acceptance" / "execution.json"
    if contract_path.is_symlink():
        raise SampleFailure("sample execution acceptance contract cannot be a symlink")
    try:
        resolved_path = contract_path.resolve(strict=True)
    except OSError as exc:
        raise SampleFailure(
            "sample execution acceptance contract is unavailable"
        ) from exc
    if not resolved_path.is_relative_to(harness_root) or not resolved_path.is_file():
        raise SampleFailure("sample execution interface escapes its harness")
    content = resolved_path.read_bytes()
    try:
        text = content.decode("utf-8")
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SampleFailure(
            "sample execution acceptance contract is invalid JSON"
        ) from exc
    required = {
        "schema",
        "requirement_id",
        "scenario_id",
        "entrypoint",
        "invocations",
        "result_shape",
        "target",
        "dependencies",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != EXECUTION_CONTRACT_SCHEMA
    ):
        raise SampleFailure("sample requires one strict host execution interface")
    requirement_id = value["requirement_id"]
    scenario_id = value["scenario_id"]
    if not isinstance(requirement_id, str) or not isinstance(scenario_id, str):
        raise SampleFailure("sample execution must identify an OpenSpec scenario")
    requirement = next(
        (
            item
            for item in loaded.specification_set.requirements
            if item.requirement_id == requirement_id
        ),
        None,
    )
    if requirement is None or scenario_id not in {
        item.scenario_id for item in requirement.scenarios
    }:
        raise SampleFailure("sample execution does not resolve to its loaded OpenSpec")
    entrypoint_name = value["entrypoint"]
    entrypoint = next(
        (item for item in definition.entrypoints if item.name == entrypoint_name), None
    )
    if (
        entrypoint is None
        or entrypoint.kind not in SAMPLE_HARNESS_ENTRYPOINT_KINDS
        or entrypoint.path != entrypoint.name
    ):
        raise SampleFailure(
            "sample execution requires a logical portable, persistent-service, "
            "or web-application entrypoint"
        )
    invocations = value["invocations"]
    result_shape = value["result_shape"]
    target = value["target"]
    dependencies = value["dependencies"]
    if not isinstance(invocations, list) or len(invocations) < 2:
        raise SampleFailure("sample execution requires multiple invocation cases")
    invocation_ids: list[str] = []
    for invocation in invocations:
        if not isinstance(invocation, dict) or set(invocation) != {
            "case_id",
            "arguments",
        }:
            raise SampleFailure("sample invocation case has invalid fields")
        case_id = invocation["case_id"]
        arguments = invocation["arguments"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(arguments, list)
        ):
            raise SampleFailure("sample invocation case has invalid values")
        invocation_ids.append(case_id)
    if len(set(invocation_ids)) != len(invocation_ids):
        raise SampleFailure("sample invocation case IDs must be unique")
    _require_valid_result_shape(result_shape)
    if not isinstance(target, dict) or any(not isinstance(key, str) for key in target):
        raise SampleFailure("sample execution target must map axes to values")
    slots_by_axis: dict[str, list[FlavorSlot]] = {}
    for slot in definition.flavor_slots:
        slots_by_axis.setdefault(slot.axis.value, []).append(slot)
    required_axes = {
        slot.axis.value
        for slot in definition.flavor_slots
        if slot.cardinality is FlavorCardinality.EXACTLY_ONE
    }
    if not required_axes <= set(target) or not set(target) <= set(slots_by_axis):
        raise SampleFailure(
            "sample execution must select every required declared Flavor axis"
        )
    for axis, item in target.items():
        slots = slots_by_axis[axis]
        if len(slots) > 1:
            if (
                not isinstance(item, dict)
                or set(item) != {slot.slot_id for slot in slots}
                or any(
                    not isinstance(value, str) or not value for value in item.values()
                )
            ):
                raise SampleFailure(
                    "a repeated Flavor axis requires one scalar target per slot ID"
                )
            values = tuple(item.values())
            if axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value:
                for language_pin in values:
                    _language_target(language_pin)
        elif axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value:
            if (
                not isinstance(item, list)
                or any(not isinstance(value, str) for value in item)
                or len(set(item)) != len(item)
            ):
                raise SampleFailure(
                    "sample language execution pins must be unique strings"
                )
            for language_pin in item:
                _language_target(language_pin)
        else:
            slot = slots[0]
            if isinstance(item, str):
                values = (item,)
            elif (
                isinstance(item, list)
                and all(isinstance(value, str) and value for value in item)
                and len(set(item)) == len(item)
            ):
                values = tuple(item)
            else:
                raise SampleFailure(
                    "non-language Flavor targets must be a scalar or unique string list"
                )
            count = len(values)
            accepted = {
                FlavorCardinality.EXACTLY_ONE: count == 1,
                FlavorCardinality.ZERO_OR_ONE: count <= 1,
                FlavorCardinality.ONE_OR_MORE: count >= 1,
                FlavorCardinality.BOUNDED: (
                    slot.minimum is not None
                    and slot.maximum is not None
                    and slot.minimum <= count <= slot.maximum
                ),
            }[slot.cardinality]
            if not accepted:
                raise SampleFailure(
                    "sample execution target violates "
                    f"slot {slot.slot_id!r} cardinality"
                )
    declared_dependencies = {
        requirement.capability for requirement in definition.requires
    }
    supported_dependencies = {"literate-ai", *declared_dependencies}
    if (
        not isinstance(dependencies, list)
        or any(
            not isinstance(item, str) or item not in supported_dependencies
            for item in dependencies
        )
        or len(set(dependencies)) != len(dependencies)
    ):
        raise SampleFailure("sample execution has an unsupported runtime dependency")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise SampleFailure("sample execution interface is not canonical JSON") from exc
    document = RecipeDocument.create("acceptance/execution.json", text)
    if (
        (
            legacy_reference is not None
            and document.identity != legacy_reference.identity.uri
        )
        or "oracle_results" in document.content
        or "expected_result" in document.content
    ):
        raise SampleFailure("sample execution oracle leaked into source generation")
    return dict(value), document


def _execution_variants(
    definition: ComponentDefinition,
    execution: Mapping[str, object],
    flavor_selectors: Sequence[str] = (),
) -> tuple[_ExecutionVariant, ...]:
    """Expand a language matrix or one explicit multi-role topology."""

    target = execution["target"]
    assert isinstance(target, Mapping)
    language_axis = FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM
    language_slots = tuple(
        slot for slot in definition.flavor_slots if slot.axis is language_axis
    )
    configured_languages = target[language_axis.value]
    if isinstance(configured_languages, list):
        if len(language_slots) != 1:
            raise SampleFailure(
                "a language matrix requires exactly one implementation role"
            )
        language_slot = language_slots[0]
        pinned_languages = tuple(
            _language_target(language) for language in configured_languages
        )
        languages = (
            _filter_pinned_languages(pinned_languages, flavor_selectors)
            if pinned_languages
            else _matrix_languages(flavor_selectors)
        )
        configured_package = target.get(FlavorAxis.PACKAGING.value)
        if configured_package is not None:
            package = _axis_target(FlavorAxis.PACKAGING, str(configured_package))
            required_language = {
                "crates": "rust",
                "npm": "javascript",
                "pip": "python",
            }.get(package)
            if required_language is not None:
                languages = tuple(
                    language for language in languages if language == required_language
                )
        toolchain_slots = tuple(
            slot
            for slot in definition.flavor_slots
            if slot.axis is FlavorAxis.TOOLCHAIN
        )
        build_slots = tuple(
            slot
            for slot in definition.flavor_slots
            if slot.axis is FlavorAxis.BUILD_SYSTEM
        )
        configured_build = target.get(FlavorAxis.BUILD_SYSTEM.value)
        builds = (
            (_axis_target(FlavorAxis.BUILD_SYSTEM, str(configured_build)),)
            if configured_build is not None
            else _matrix_builds(flavor_selectors)
        )
        variants = []
        for language in languages:
            assert isinstance(language, str)
            for build in builds:
                slot_values: list[tuple[str, str]] = []
                for slot in definition.flavor_slots:
                    if slot.axis.value not in target:
                        continue
                    if slot is language_slot:
                        slot_values.append((slot.slot_id, language))
                        continue
                    configured = target[slot.axis.value]
                    configured_values = (
                        tuple(configured)
                        if isinstance(configured, list)
                        else (configured,)
                    )
                    slot_values.extend(
                        (slot.slot_id, _axis_target(slot.axis, str(value)))
                        for value in configured_values
                    )
                if configured_build is None:
                    if len(build_slots) != 1:
                        raise SampleFailure(
                            "an unpinned build requires one optional build-system slot"
                        )
                    slot_values.append((build_slots[0].slot_id, build))
                if language == "swift":
                    if len(toolchain_slots) != 1:
                        raise SampleFailure(
                            "Swift sample execution requires one optional "
                            "toolchain slot"
                        )
                    realization = {
                        "macos": "swift-apple",
                        "linux": "swift-linux",
                        "windows": "swift-windows",
                    }[_host_os()]
                    slot_values.append((toolchain_slots[0].slot_id, realization))
                variant_id = f"{language}-{build}" if len(builds) > 1 else language
                variants.append(
                    _ExecutionVariant(variant_id, tuple(slot_values), (language,))
                )
        return tuple(variants)

    if not isinstance(configured_languages, Mapping):
        raise SampleFailure("sample implementation topology is invalid")
    role_values = tuple(
        (slot.slot_id, _language_target(str(configured_languages[slot.slot_id])))
        for slot in language_slots
    )
    languages = tuple(value for _slot, value in role_values)
    if set(role_values) == {
        ("backend-language", "rust"),
        ("frontend-language", "javascript"),
    }:
        variant_id = "rust-javascript-full-stack"
    else:
        variant_id = "-".join(f"{slot_id}-{value}" for slot_id, value in role_values)
    base_slot_values: list[tuple[str, str]] = []
    for slot in definition.flavor_slots:
        if slot.axis.value not in target:
            continue
        if slot.axis is language_axis:
            base_slot_values.append((slot.slot_id, dict(role_values)[slot.slot_id]))
            continue
        configured = target[slot.axis.value]
        configured_values = (
            tuple(configured) if isinstance(configured, list) else (configured,)
        )
        base_slot_values.extend(
            (slot.slot_id, _axis_target(slot.axis, str(value)))
            for value in configured_values
        )
    build_slots = tuple(
        slot for slot in definition.flavor_slots if slot.axis is FlavorAxis.BUILD_SYSTEM
    )
    configured_build = target.get(FlavorAxis.BUILD_SYSTEM.value)
    builds = (
        (_axis_target(FlavorAxis.BUILD_SYSTEM, str(configured_build)),)
        if configured_build is not None
        else _matrix_builds(flavor_selectors)
    )
    variants = []
    for build in builds:
        slot_values = list(base_slot_values)
        if configured_build is None:
            if len(build_slots) != 1:
                raise SampleFailure(
                    "an unpinned build requires one optional build-system slot"
                )
            slot_values.append((build_slots[0].slot_id, build))
        selected_id = f"{variant_id}-{build}" if len(builds) > 1 else variant_id
        variants.append(_ExecutionVariant(selected_id, tuple(slot_values), languages))
    return tuple(variants)


def _execution_oracle(
    sample_root: Path,
    reference: ContentReference,
    interface_identity: ContentIdentity,
    invocation_ids: Sequence[str],
    result_shape: object,
) -> tuple[dict[str, object], ContentReference]:
    if not isinstance(reference, ContentReference):
        raise SampleFailure("sample metadata lacks a valid acceptance oracle pin")
    path = sample_harness_oracle(sample_root, reference)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise SampleFailure("sample acceptance oracle identity changed")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SampleFailure("sample acceptance oracle is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "execution_interface_identity", "oracle_results"}
        or value.get("schema") != EXECUTION_ORACLE_SCHEMA
        or value.get("execution_interface_identity") != interface_identity.uri
    ):
        raise SampleFailure("sample acceptance oracle is not bound to its interface")
    oracle_results = value["oracle_results"]
    if not isinstance(oracle_results, list) or len(oracle_results) != len(
        invocation_ids
    ):
        raise SampleFailure("sample acceptance oracle must cover every invocation")
    oracle_ids: list[str] = []
    for oracle in oracle_results:
        if not isinstance(oracle, dict) or set(oracle) != {
            "case_id",
            "expected_result",
        }:
            raise SampleFailure("sample oracle result has invalid fields")
        case_id = oracle["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise SampleFailure("sample oracle result has an invalid case ID")
        oracle_ids.append(case_id)
        _require_result_matches_shape(oracle["expected_result"], result_shape)
    if oracle_ids != list(invocation_ids):
        raise SampleFailure("sample oracle order must match its invocation cases")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise SampleFailure("sample acceptance oracle is not canonical JSON") from exc
    return dict(value), reference


def _sample_oracle_reference(metadata: Mapping[str, object]) -> ContentReference:
    try:
        return ContentReference.from_dict(metadata["acceptance_oracle"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SampleFailure(
            "sample metadata lacks a valid acceptance oracle pin"
        ) from exc


def _host_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise SampleFailure(f"sample host platform is unsupported: {sys.platform}")


@dataclass(frozen=True, slots=True)
class _ResolvedToolchainConstraint:
    source: str
    reference: ContentReference
    constraint: ToolchainConstraint
    contribution: ContributionReference | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "source": self.source,
            "content_identity": self.reference.identity.uri,
        }
        if self.contribution is not None:
            value["contribution_id"] = self.contribution.contribution_id
            value["slot"] = self.contribution.slot
        return value


@dataclass(frozen=True, slots=True)
class _EffectiveToolchainConstraint:
    constraint: ToolchainConstraint
    sources: tuple[_ResolvedToolchainConstraint, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "constraint": self.constraint.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass(frozen=True, slots=True)
class _SharedFlavor:
    root: Path
    definition: FlavorDefinition
    loaded: LoadedSpecification
    descriptor: FlavorDescriptor
    input_closure: PinnedInputClosure
    models: tuple[tuple[str, str], ...] = ()
    skills: tuple[RecipeSkill, ...] = ()
    toolchain_constraints: tuple[_ResolvedToolchainConstraint, ...] = ()

    @property
    def specification_identity(self) -> str:
        return self.loaded.specification_set.identity.uri

    def recipe_flavor(self) -> RecipeFlavor:
        prefix = f"flavors/{self.definition.coordinate.name}"
        return RecipeFlavor(
            self.definition.coordinate.name,
            self.definition.primary_axis.value,
            self.descriptor.axis_value,
            tuple(
                RecipeDocument.create(
                    f"{prefix}/{path}",
                    content.decode("utf-8") if isinstance(content, bytes) else content,
                )
                for path, content in _specification_contents(self.loaded)
            ),
            self.models,
            self.definition.conflicts,
            self.definition.coordinate.uri,
            self.skills,
            revision_identity=self.descriptor.revision_identity.uri,
            specification_set_identity=self.loaded.specification_set.identity.uri,
        )


@dataclass(frozen=True, slots=True)
class _BoundSharedFlavor:
    flavor: _SharedFlavor
    slot_ids: tuple[str, ...] = ()

    @property
    def specification_identity(self) -> str:
        return self.flavor.specification_identity

    def recipe_flavor(self) -> RecipeFlavor:
        return replace(self.flavor.recipe_flavor(), slot_ids=self.slot_ids)


def _coding_models(
    root: Path, definition: FlavorDefinition
) -> tuple[tuple[str, str], ...]:
    references = tuple(
        item for item in definition.authoring_inputs if item.kind == "model-selection"
    )
    if len(references) > 1:
        raise SampleFailure("Flavor has multiple coding-model specifications")
    if not references:
        return ()
    reference = references[0]
    configured_path = root / reference.uri
    path = configured_path.resolve()
    if (
        not path.is_relative_to(root.resolve())
        or configured_path.is_symlink()
        or not path.is_file()
    ):
        raise SampleFailure("Flavor coding-model specification is unavailable")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise SampleFailure("Flavor coding-model specification identity changed")
    value = json.loads(content)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "models"}
        or value.get("schema") != "literate-ai/coding-model-selection@1"
        or not isinstance(value.get("models"), dict)
        or any(
            key not in {"codex", "claude", "cursor-agent", "opencode"}
            for key in value["models"]
        )
    ):
        raise SampleFailure("Flavor coding-model specification is invalid")
    try:
        return tuple(
            sorted(
                (
                    key,
                    source_cache_model_selector(
                        model,
                        path=f"sample.Flavor.models[{key!r}]",
                    ),
                )
                for key, model in value["models"].items()
            )
        )
    except ContractValidationError as exc:
        raise SampleFailure("Flavor coding-model specification is invalid") from exc


def _coding_skills(
    root: Path,
    definition: ComponentDefinition | FlavorDefinition,
    *,
    boundary: Path,
    source: str,
) -> tuple[RecipeSkill, ...]:
    references = tuple(
        item
        for item in definition.authoring_inputs
        if item.kind == "specification-to-source-skill"
    )
    resolved_boundary = boundary.resolve(strict=True)
    skills: list[RecipeSkill] = []
    for reference in references:
        configured_path = root / reference.uri
        path = configured_path.resolve()
        if (
            not path.is_relative_to(resolved_boundary)
            or configured_path.is_symlink()
            or not path.is_file()
        ):
            raise SampleFailure(
                f"pinned specification-to-source skill is unavailable: {source}"
            )
        try:
            skills.append(
                RecipeSkill.from_reference(
                    reference,
                    path.read_bytes(),
                    source=source,
                )
            )
        except (OSError, CodingCliError, ContractValidationError) as exc:
            raise SampleFailure(
                f"pinned specification-to-source skill is invalid: {source}"
            ) from exc
    # Component Markdown preserves author-facing declaration order; recipe skills
    # must instead be dependency-first for deterministic resolution.
    pending = {item.skill_id: item for item in skills}
    ordered: list[RecipeSkill] = []
    while pending:
        ready = sorted(
            [
                item
                for item in pending.values()
                if all(
                    dependency.skill_id not in pending
                    for dependency in item.dependencies
                )
            ],
            key=lambda item: item.skill_id,
        )
        if not ready:
            raise SampleFailure(
                f"pinned specification-to-source skills cycle: {source}"
            )
        for item in ready:
            ordered.append(item)
            del pending[item.skill_id]
    return tuple(ordered)


def _pinned_project_content(
    root: Path,
    reference: ContentReference,
    *,
    boundary: Path,
    source: str,
) -> bytes:
    configured = root / reference.uri
    try:
        path = configured.resolve(strict=True)
        resolved_boundary = boundary.resolve(strict=True)
    except OSError as exc:
        raise SampleFailure(f"pinned {source} is unavailable") from exc
    if (
        configured.is_symlink()
        or not path.is_file()
        or not path.is_relative_to(resolved_boundary)
    ):
        raise SampleFailure(
            f"pinned {source} must be a regular file inside the project boundary"
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SampleFailure(f"pinned {source} is unreadable") from exc
    if hashlib.sha256(content).hexdigest() != reference.identity.digest:
        raise SampleFailure(f"pinned {source} identity changed")
    return content


def _typed_toolchain_constraint(
    root: Path,
    reference: ContentReference,
    *,
    boundary: Path,
    source: str,
    contribution: ContributionReference | None = None,
) -> _ResolvedToolchainConstraint:
    if reference.kind != "toolchain-constraint":
        raise SampleFailure(
            f"{source} must use a toolchain-constraint content reference"
        )
    content = _pinned_project_content(root, reference, boundary=boundary, source=source)
    try:
        constraint = ToolchainConstraint.from_dict(
            json.loads(content.decode("utf-8")), path=f"{source} toolchain constraint"
        )
    except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
        raise SampleFailure(f"{source} has an invalid toolchain constraint") from exc
    if contribution is not None and contribution.slot != constraint.toolchain:
        raise SampleFailure(
            f"{source} contribution slot does not match its typed toolchain name"
        )
    return _ResolvedToolchainConstraint(source, reference, constraint, contribution)


def _authoring_toolchain_constraints(
    root: Path,
    definition: ComponentDefinition,
    *,
    boundary: Path,
) -> tuple[_ResolvedToolchainConstraint, ...]:
    return tuple(
        _typed_toolchain_constraint(
            root,
            reference,
            boundary=boundary,
            source=f"Component {definition.coordinate.uri}",
        )
        for reference in definition.authoring_inputs
        if reference.kind == "toolchain-constraint"
    )


def _component_repository_source_dependencies(
    root: Path,
    definition: ComponentDefinition,
    *,
    boundary: Path,
) -> tuple[RepositorySourceDependency, ...]:
    dependencies: list[RepositorySourceDependency] = []
    for index, reference in enumerate(definition.source_dependencies):
        source = (
            f"Component {definition.coordinate.uri} repository source "
            f"dependency {index}"
        )
        if reference.kind != REPOSITORY_SOURCE_DEPENDENCY_CONTENT_KIND:
            raise SampleFailure(
                f"{source} must use a repository-source-dependency reference"
            )
        content = _pinned_project_content(
            root, reference, boundary=boundary, source=source
        )
        try:
            dependency = RepositorySourceDependency.from_dict(
                json.loads(content.decode("utf-8")), path=source
            )
        except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
            raise SampleFailure(f"{source} is invalid") from exc
        if dependency.integration_contract is not None:
            _pinned_project_content(
                root,
                dependency.integration_contract,
                boundary=boundary,
                source=f"{source} integration contract",
            )
        dependencies.append(dependency)
    identifiers = tuple(item.dependency_id for item in dependencies)
    if len(identifiers) != len(set(identifiers)):
        raise SampleFailure("Component repository dependency IDs must be unique")
    return tuple(dependencies)


def _flavor_toolchain_constraints(
    root: Path,
    definition: FlavorDefinition,
    *,
    boundary: Path,
) -> tuple[_ResolvedToolchainConstraint, ...]:
    resolved: list[_ResolvedToolchainConstraint] = []
    for contribution in definition.contributions:
        source = (
            f"Flavor {definition.coordinate.uri} contribution "
            f"{contribution.contribution_id}"
        )
        if contribution.kind is ContributionKind.TOOLCHAIN:
            resolved.append(
                _typed_toolchain_constraint(
                    root,
                    contribution.content,
                    boundary=boundary,
                    source=source,
                    contribution=contribution,
                )
            )
        else:
            _pinned_project_content(
                root,
                contribution.content,
                boundary=boundary,
                source=source,
            )
    return tuple(resolved)


def _shared_flavor_input_closure(
    root: Path,
    definition_path: Path,
    definition: FlavorDefinition,
    loaded: LoadedSpecification,
    manifest_content: bytes,
) -> PinnedInputClosure:
    closure = PinnedInputClosure()
    boundary = project_boundary(root, legacy=root)
    prefix = f"flavor:{definition.coordinate.uri}"
    try:
        project = discover_project(root)
        if project is not None:
            project_manifest = project.root / PROJECT_FILENAME
            closure.pin(
                project_manifest,
                boundary=project.root,
                label=f"{prefix}:project-manifest",
                expected_content=project_manifest.read_bytes(),
            )
        closure.pin(
            definition_path,
            boundary=boundary,
            label=f"{prefix}:manifest",
            expected_content=manifest_content,
        )
        for relative, content in loaded.contents:
            closure.pin(
                root.joinpath(*Path(relative).parts),
                boundary=boundary,
                label=f"{prefix}:specification:{relative}",
                expected_content=content,
            )
        references = (
            *(
                (f"authoring-input:{index}", reference)
                for index, reference in enumerate(definition.authoring_inputs)
            ),
            *(
                (f"contribution:{item.contribution_id}", item.content)
                for item in definition.contributions
            ),
        )
        for role, reference in references:
            content = _pinned_project_content(
                root,
                reference,
                boundary=boundary,
                source=f"{prefix}:{role}",
            )
            closure.pin(
                root / reference.uri,
                boundary=boundary,
                label=f"{prefix}:{role}:{reference.kind}",
                expected_content=content,
                expected_identity=reference.identity,
            )
    except (OSError, PinnedInputClosureError) as exc:
        raise SampleFailure(
            f"shared Flavor input closure is invalid: {definition.coordinate.uri}"
        ) from exc
    return closure


def _shared_flavors(samples_root: Path) -> tuple[_SharedFlavor, ...]:
    flavors: list[_SharedFlavor] = []
    keys: set[tuple[FlavorAxis, str]] = set()
    project = discover_project(samples_root)
    catalog_roots = (
        project.roots("flavor") if project is not None else (samples_root / "_flavors",)
    )
    definition_paths = sorted(
        path
        for catalog_root in catalog_roots
        for path in catalog_root.glob("*/flavor.md")
    )
    for definition_path in definition_paths:
        root = definition_path.parent
        if definition_path.is_symlink() or not definition_path.is_file():
            raise SampleFailure(f"shared Flavor is unavailable: {root.name}")
        manifest_content = definition_path.read_bytes()
        definition = parse_flavor_markdown(
            manifest_content, source=definition_path.as_posix()
        ).resolve(
            lambda uri, flavor_root=root: flavor_root.joinpath(
                *Path(uri).parts
            ).read_bytes()
        )
        if len(definition.supported_targets) != 1:
            raise SampleFailure(f"shared Flavor must select one target: {root.name}")
        axis_value = definition.supported_targets[0]
        key = (definition.primary_axis, axis_value)
        if key in keys:
            raise SampleFailure(f"duplicate shared Flavor target: {root.name}")
        keys.add(key)
        loaded = OpenSpecProvider().load(
            root, tuple(item.uri for item in definition.specification_fragments)
        )
        expected = tuple(item.identity for item in definition.specification_fragments)
        actual = tuple(item.identity for item in loaded.specification_set.artifacts)
        if expected != actual:
            raise SampleFailure(f"shared Flavor specification drift: {root.name}")
        loaded.require_unchanged(root)
        revision = FlavorRevision(
            definition,
            ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(manifest_content).hexdigest()
            ),
            (),
        )
        descriptor = FlavorDescriptor(revision.identity, definition, axis_value)
        boundary = project_boundary(root, legacy=root)
        flavors.append(
            _SharedFlavor(
                root,
                definition,
                loaded,
                descriptor,
                _shared_flavor_input_closure(
                    root, definition_path, definition, loaded, manifest_content
                ),
                _coding_models(root, definition),
                _coding_skills(
                    root,
                    definition,
                    boundary=boundary,
                    source=f"Flavor {definition.coordinate.uri}",
                ),
                _flavor_toolchain_constraints(
                    root,
                    definition,
                    boundary=boundary,
                ),
            )
        )
    required = {
        (FlavorAxis.PLATFORM_OS, "linux"),
        (FlavorAxis.PLATFORM_OS, "macos"),
        (FlavorAxis.PLATFORM_OS, "windows"),
        (FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "python"),
        (FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "cpp"),
        (FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust"),
        (FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "javascript"),
        (FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "swift"),
        (FlavorAxis.TOOLCHAIN, "swift-apple"),
        (FlavorAxis.TOOLCHAIN, "swift-linux"),
        (FlavorAxis.TOOLCHAIN, "swift-windows"),
    }
    if not required <= keys:
        raise SampleFailure("shared OS and language Flavor catalog is incomplete")
    return tuple(flavors)


def _sample_runtime_recipe(
    sample_root: Path,
    definition: ComponentDefinition,
    specification: SpecificationSet,
    execution: Mapping[str, object],
    variant: _ExecutionVariant,
    input_closure: PinnedInputClosure | None = None,
):
    base = ComponentRevision(definition, specification, None, (), ())
    shared = _shared_flavors(sample_root.parent)
    if input_closure is not None:
        try:
            for flavor in shared:
                input_closure.include(flavor.input_closure)
        except PinnedInputClosureError as exc:
            raise SampleFailure(
                f"sample Flavor input closure is invalid: {exc.message}"
            ) from exc
    selectors: list[str] = []
    constraints: list[TargetConstraint] = []
    resolved_targets: dict[str, object] = {}
    axis_counts = Counter(slot.axis for slot in definition.flavor_slots)
    for slot in definition.flavor_slots:
        if not variant.has_value(slot.slot_id):
            continue
        slot_scoped = axis_counts[slot.axis] > 1
        resolved_values: list[str] = []
        requested_values = variant.values_for(slot.slot_id)
        for requested in requested_values:
            value = (
                _host_os()
                if (slot.axis is FlavorAxis.PLATFORM_OS and requested == "host")
                else requested
            )
            if slot.axis is FlavorAxis.PLATFORM_OS and value != _host_os():
                raise SampleFailure(
                    f"sample platform target {value!r} is incompatible with host "
                    f"{_host_os()!r}"
                )
            # TargetProfile has one constraint target per axis or slot. Repeated
            # selections for a ONE_OR_MORE slot therefore travel through the
            # exact Flavor selectors below; the provider configuration still
            # binds the complete resolved target set into the profile identity.
            if len(requested_values) == 1:
                constraints.append(
                    TargetConstraint(
                        slot.axis,
                        value,
                        slot_id=slot.slot_id if slot_scoped else None,
                    )
                )
            # The execution interface is the sample's exact target authority. In
            # particular, Bazel-authored conformance samples must not inherit a
            # different project-level build-system default.
            if not (slot.axis is FlavorAxis.BUILD_SYSTEM and value == "make"):
                selectors.append(f"+{slot.slot_id}:{value}")
            resolved_values.append(value)
        resolved_targets[slot.slot_id if slot_scoped else slot.axis.value] = (
            resolved_values[0] if len(resolved_values) == 1 else resolved_values
        )
    target_name = f"{definition.coordinate.name}-host-e2e"
    target = TargetProfile(
        target_name,
        "1.0.0",
        "sample-execution-contract",
        canonical_identity(
            {
                "execution": dict(execution),
                "variant": variant.variant_id,
                "resolved_target": resolved_targets,
            }
        ),
        tuple(constraints),
    )
    planner = FilesystemComponentLockPlanner()
    plan, snapshot = planner.plan_with_snapshot(
        sample_root,
        target_name=target_name,
        flavor_selectors=tuple(selectors),
    )
    result = ComponentLockResolver().resolve(
        plan,
        expected_input_evidence_identity=plan.identity,
    )
    authority = project_locked_generation_authority(
        result.lock,
        root_authoring=snapshot.root_authoring,
        authorings=result.lock.authorings,
        flavor_catalog=snapshot.flavor_revisions,
        target_name=target_name,
        flavor_selectors=tuple(selectors),
    )
    selected_identities = {item.identity.uri for item in authority.selected_flavors}
    selected = tuple(
        item
        for item in shared
        if item.descriptor.revision_identity.uri in selected_identities
    )
    if not selected or len(selected) != len(selected_identities):
        raise SampleFailure("sample recipe did not resolve an exact Flavor set")
    return (
        base,
        target,
        resolved_targets,
        authority,
        snapshot,
        plan,
        selected,
    )


def _ordered_selected_flavors(
    definition: ComponentDefinition,
    selected: Sequence[_SharedFlavor],
    variant: _ExecutionVariant,
) -> tuple[_BoundSharedFlavor, ...]:
    by_target = {
        (item.definition.primary_axis, item.descriptor.axis_value): item
        for item in selected
    }
    bindings: dict[str, tuple[_SharedFlavor, list[str]]] = {}
    axis_counts = Counter(slot.axis for slot in definition.flavor_slots)
    for slot in definition.flavor_slots:
        if not variant.has_value(slot.slot_id):
            continue
        for value in variant.values_for(slot.slot_id):
            if slot.axis is FlavorAxis.PLATFORM_OS:
                value = _host_os()
            flavor = by_target.get((slot.axis, value))
            if flavor is None:
                # An optional slot whose capability contract excludes all shared
                # Flavors is legitimately zero-selected by the lock planner.
                if slot.cardinality is FlavorCardinality.ZERO_OR_ONE:
                    continue
                raise SampleFailure(
                    f"resolved Flavor for slot {slot.slot_id!r} is unavailable"
                )
            identity = flavor.descriptor.revision_identity.uri
            existing = bindings.get(identity)
            if existing is None:
                existing = (flavor, [])
                bindings[identity] = existing
            if axis_counts[slot.axis] > 1:
                existing[1].append(slot.slot_id)
    ordered = tuple(
        _BoundSharedFlavor(flavor, tuple(sorted(slot_ids)))
        for flavor, slot_ids in bindings.values()
    )
    return tuple(
        sorted(
            ordered,
            key=lambda item: (
                item.flavor.definition.primary_axis.value,
                item.slot_ids,
                item.flavor.definition.coordinate.name,
            ),
        )
    )


def _effective_toolchain_constraints(
    selected_flavors: Sequence[_BoundSharedFlavor],
    component_constraints: tuple[_ResolvedToolchainConstraint, ...],
) -> tuple[_EffectiveToolchainConstraint, ...]:
    sources = list(component_constraints)
    sources.extend(
        constraint
        for item in selected_flavors
        for constraint in item.flavor.toolchain_constraints
    )

    by_name: dict[str, list[_ResolvedToolchainConstraint]] = {}
    for source in sources:
        by_name.setdefault(source.constraint.toolchain, []).append(source)
    result: list[_EffectiveToolchainConstraint] = []
    for name in sorted(by_name):
        selected_sources = tuple(by_name[name])
        try:
            merged = merge_toolchain_constraints(
                tuple(item.constraint for item in selected_sources),
                path=f"effective toolchain {name}",
            )
        except ContractValidationError as exc:
            raise SampleFailure(
                f"Component and selected Flavors have conflicting {name} constraints"
            ) from exc
        result.append(_EffectiveToolchainConstraint(merged, selected_sources))
    return tuple(result)


def _required_toolchain_constraint(
    constraints: Sequence[_EffectiveToolchainConstraint], toolchain: str
) -> _EffectiveToolchainConstraint:
    matches = tuple(
        item for item in constraints if item.constraint.toolchain == toolchain
    )
    if len(matches) != 1:
        raise SampleFailure(
            f"sample requires one effective typed {toolchain} toolchain constraint"
        )
    return matches[0]


def _toolchain_discovery_options(
    constraint: ToolchainConstraint, *, operator_environment: str
) -> dict[str, object]:
    options: dict[str, object] = {}
    if (
        constraint.command is not None
        and not os.environ.get(operator_environment, "").strip()
    ):
        options["pinned_command"] = constraint.command
    if constraint.minimum_version is not None:
        options["minimum_version"] = constraint.minimum_version
    if constraint.required_version is not None:
        options["required_version"] = constraint.required_version
    return options


@lru_cache(maxsize=1)
def _framework_skill_catalog() -> tuple[RecipeSkill, ...]:
    """Load admitted specification-to-source skills from the framework project.

    Sample Components pin a subset of those skills; language Flavors pin others
    (for example ``python-portable-application``). Recipe construction closes
    exact parent dependencies against this catalog the same way live generation
    does, so a Flavor skill whose parent is not restated on ``component.md``
    still composes.
    """

    project = discover_project(Path(__file__).resolve().parents[3])
    if project is None:
        raise SampleFailure("framework skill catalog is unavailable")
    try:
        return load_admitted_skill_catalog(project)
    except SkillClosureError as exc:
        raise SampleFailure(
            f"framework skill catalog is invalid: {exc.message}"
        ) from exc


def _generation_recipe(
    *,
    sample_root: Path,
    definition: ComponentDefinition,
    loaded: LoadedSpecification,
    acceptance_document: RecipeDocument,
    selected_flavors: Sequence[_BoundSharedFlavor],
    base_skills: tuple[RecipeSkill, ...],
    variant: _ExecutionVariant,
    authority: LockedGenerationAuthority,
    catalog: ComponentCatalogSnapshot,
    resolved_inputs: tuple[tuple[str, str], ...],
    model_scope: ModelScopeBinding | None = None,
) -> GenerationRecipe:
    if variant.variant_id == "rust-javascript-full-stack":
        required_entrypoint = None
        required_entrypoints = (
            "source/backend/main.rs",
            "source/frontend/main.js",
        )
    else:
        try:
            required_entrypoint = portable_source_entrypoint(variant.languages[0])
        except ValueError as exc:
            raise SampleFailure(
                "unsupported sample implementation language: " + variant.languages[0]
            ) from exc
        required_entrypoints = ()
    lock = authority.lock
    nodes = {item.revision.identity.uri: item for item in lock.nodes}
    interface_documents: list[RecipeDocument] = []
    for edge in lock.edges:
        if (
            edge.consumer_revision != lock.root_revision
            or edge.kind is not DependencyKind.GENERATION
        ):
            continue
        provider = nodes[edge.provider_revision.uri]
        references = tuple(
            item
            for item in provider.revision.public_interfaces
            if item.identity == edge.public_interface_identity
        )
        if len(references) != 1:
            raise SampleFailure(
                "direct generation dependency lacks one exact public interface"
            )
        reference = references[0]
        content = catalog.component_content(
            provider.revision.authoring_identity, reference
        )
        interface_documents.append(
            RecipeDocument.create(
                f"dependency-interfaces/{provider.revision.identity.digest}/"
                f"{reference.uri}",
                content.decode("utf-8"),
            )
        )
    execution_documents: tuple[RecipeDocument, ...] = ()
    if any(
        item.flavor.definition.primary_axis is FlavorAxis.ACCELERATOR
        and item.flavor.descriptor.axis_value == "nvidia-cuda"
        for item in selected_flavors
    ):
        execution_documents = (_nvidia_stack_selection_document(variant),)
    specification_documents = tuple(
        RecipeDocument.create(
            path,
            content.decode("utf-8") if isinstance(content, bytes) else content,
        )
        for path, content in _specification_contents(loaded)
    )
    if all(document.path != "component.md" for document in specification_documents):
        specification_documents = (
            RecipeDocument.create(
                "component.md",
                (sample_root / "component.md").read_text(encoding="utf-8"),
            ),
            *specification_documents,
        )
    recipe = GenerationRecipe(
        recipe_id=f"{definition.coordinate.name}-{_host_os()}-{variant.variant_id}",
        application_id=definition.coordinate.name,
        documents=(
            *specification_documents,
            *interface_documents,
            acceptance_document,
            *execution_documents,
        ),
        component_lock_identity=lock.identity,
        flavors=tuple(item.recipe_flavor() for item in selected_flavors),
        required_entrypoint=required_entrypoint,
        models=(),
        skills=base_skills,
        resolved_inputs=resolved_inputs,
        required_entrypoints=required_entrypoints,
        managed_sbom_graph=CycloneDxManagedGraph.from_component_lock(lock),
        model_scope=model_scope,
        skill_catalog=_framework_skill_catalog(),
    )
    covered_stages = {
        stage for skill in recipe.resolved_skills for stage in skill.stages
    }
    if not {"plan", "generate"}.issubset(covered_stages):
        raise SampleFailure(
            "sample generation skills must cover plan and generate stages"
        )
    return recipe


def _sample_execution_plan(
    sample_root: Path,
    definition: ComponentDefinition,
    locked_revision: LockedComponentRevision,
    recipe: GenerationRecipe,
    source_generator,
):
    selection = getattr(source_generator, "selection", None)
    coding_cli = getattr(selection, "name", "external-adapter")
    endpoint = ModelEndpoint(
        endpoint_id=f"sample-coding-cli-{coding_cli}",
        provider=f"coding-cli/{coding_cli}",
        model=source_cache_model_selector(
            recipe.model_for(coding_cli),
            path="sample.execution_plan.model",
        ),
        base_url=f"cli://{coding_cli}",
        locality=Locality.UNKNOWN,
        capabilities=("structured-output", "source-generation"),
        context_tokens=1_000_000,
        model_revision=None,
    )
    planner_endpoint = ModelEndpoint(
        endpoint_id=_SAMPLE_PLANNER_ENDPOINT_ID,
        provider="literate-ai/deterministic-spec-planner",
        model="pinned-sample-plan-v2",
        base_url="unix:///in-process/literate-ai/sample-plan",
        locality=Locality.LOCAL,
        capabilities=("structured-output",),
        context_tokens=1_000_000,
        model_revision=canonical_identity(
            {
                "planner": "pinned-sample-plan-v2",
                "output_fields": [
                    "specification_id",
                    "recipe_identity",
                    "flavor_specification_ids",
                    "generation_skill_ids",
                    "requirement_id",
                    "scenario_id",
                    "required_entrypoints",
                ],
            }
        ).uri,
    )
    locked_definition = replace(
        definition,
        workflow_definition=locked_revision.workflow_definition,
        routing_policy=locked_revision.routing_policy,
    )
    boundary = project_boundary(sample_root, legacy=sample_root.parent)
    workflow_path = boundary / locked_revision.workflow_definition.uri
    routing_path = boundary / locked_revision.routing_policy.uri
    plan = compile_generation_execution_plan(
        component=locked_definition,
        workflow_content=workflow_path.read_bytes(),
        routing_content=routing_path.read_bytes(),
        endpoint=endpoint,
        generation_prompt=recipe.prompt(),
        stage_endpoints={"plan": planner_endpoint},
    )
    stage_ids = {item.stage_id for item in plan.model_stages}
    if any(
        not set(skill.stages).issubset(stage_ids) for skill in recipe.resolved_skills
    ):
        raise SampleFailure("generation skill targets a stage outside the workflow")
    return plan


def _classification_from_output(
    value: Mapping[str, object],
) -> SecurityClassification:
    return SecurityClassification(
        effective_revision_digest=str(value["effective_revision_digest"]),
        source_digests=tuple(str(item) for item in value["source_digests"]),
        dependency_classification_digests=tuple(
            str(item) for item in value["dependency_classification_digests"]
        ),
        origin_attestation_digests=tuple(
            str(item) for item in value["origin_attestation_digests"]
        ),
        finding_ids=tuple(str(item) for item in value["finding_ids"]),
        policy_digest=str(value["policy_digest"]),
        profile=SecurityProfile(str(value["profile"])),
        maximum_severity=FindingSeverity(int(value["maximum_severity"])),
        permitted_privileges=tuple(
            str(item) for item in value.get("permitted_privileges", ())
        ),
        decision_reason=str(value.get("decision_reason", "")),
    )


@dataclass(frozen=True, slots=True)
class _CompiledSampleArtifact:
    built_toolchain: str
    compiled_files: tuple[str, ...]
    compiled_entrypoint: str
    required_compiled_files: tuple[str, ...]
    execution: HostArtifactExecution


def _compiled_sample_artifact(
    *,
    sample_root: Path,
    runtime_root: Path,
    execution_contract: Mapping[str, object],
    variant: _ExecutionVariant,
    build: Mapping[str, object],
    seed_arguments: list[object],
    runtime_command: tuple[str, ...] | None,
    runtime_toolchain,
    expected_toolchain_identity: str,
    toolchain_output_field: str,
    composite_toolchain: RustJavaScriptToolchain | None,
) -> _CompiledSampleArtifact:
    """Validate one build result and bind its exact executable observation."""

    built_toolchain = build.get(toolchain_output_field)
    if built_toolchain != expected_toolchain_identity:
        raise SampleFailure("sample build used a different exact toolchain")
    if variant.variant_id == "rust-javascript-full-stack":
        if composite_toolchain is None or (
            build.get("compiler_identity") != composite_toolchain.rust.identity
            or build.get("runtime_identity") != composite_toolchain.node.identity
        ):
            raise SampleFailure(
                "full-stack build used a different compiler or runtime identity"
            )
    if runtime_command is not None and variant.variant_id in {
        "javascript",
        "rust-javascript-full-stack",
    }:
        output_command = build.get("runtime_command")
        if (
            not isinstance(output_command, list)
            or tuple(output_command) != runtime_command
        ):
            raise SampleFailure("sample build reported a different runtime command")

    compiled_value = build.get("compiled_files")
    if not isinstance(compiled_value, list) or any(
        not isinstance(item, str) for item in compiled_value
    ):
        raise SampleFailure("sample build reported invalid compiled files")
    compiled_files = tuple(compiled_value)
    backend_relative: str | None = None
    if variant.variant_id == "python":
        compiled_relative = "source/main.pyc"
        required_compiled_files = (compiled_relative,)
    elif variant.variant_id in {"cpp", "rust", "swift"}:
        compiled_relative = str(build.get("executable_file", ""))
        required_compiled_files = (compiled_relative,)
    elif variant.variant_id == "javascript":
        compiled_relative = str(build.get("entrypoint_file", ""))
        required_compiled_files = (compiled_relative,)
    else:
        compiled_relative = str(build.get("frontend_entrypoint_file", ""))
        backend_relative = str(build.get("backend_executable_file", ""))
        required_compiled_files = (compiled_relative, backend_relative)
    if any(path not in compiled_files for path in required_compiled_files):
        raise SampleFailure("declared sample entrypoint was not compiled")

    artifact_root = Path(str(build["artifact_path"])).resolve(strict=True)
    for relative in required_compiled_files:
        compiled_entrypoint = artifact_root.joinpath(*Path(relative).parts)
        if not compiled_entrypoint.is_file():
            raise SampleFailure("compiled sample entrypoint artifact is missing")

    dependencies = execution_contract["dependencies"]
    assert isinstance(dependencies, list)
    support_paths = (
        (sample_root.resolve().parents[1] / "src",)
        if dependencies == ["literate-ai"]
        else ()
    )
    auxiliary_artifacts: tuple[HostAuxiliaryArtifact, ...] = ()
    if backend_relative is not None:
        auxiliary_artifacts = (
            HostAuxiliaryArtifact.create(
                artifact_digest=str(build["artifact_digest"]),
                artifact_root=artifact_root,
                entrypoint=backend_relative,
                role="backend",
            ),
        )
    if runtime_toolchain is not None:
        runtime_toolchain.require_unchanged()
    baseline_execution = HostArtifactExecution.create(
        language=variant.variant_id,
        artifact_digest=str(build["artifact_digest"]),
        artifact_root=artifact_root,
        entrypoint=compiled_relative,
        arguments=seed_arguments,
        support_paths=support_paths,
        runtime_root=runtime_root,
        runtime_command=runtime_command,
        runtime_toolchain=(
            runtime_toolchain
            if variant.variant_id in {"javascript", "rust-javascript-full-stack"}
            else None
        ),
        auxiliary_artifacts=auxiliary_artifacts,
    )
    assert isinstance(built_toolchain, str)
    return _CompiledSampleArtifact(
        built_toolchain,
        compiled_files,
        compiled_relative,
        required_compiled_files,
        baseline_execution,
    )


def _execute_sample_case(
    *,
    baseline_execution: HostArtifactExecution,
    case_id: str,
    arguments: list[object],
    expected_result: object,
    verification_source: str,
    result_shape: object,
    variant: _ExecutionVariant,
    runtime_toolchain,
    effective_revision_digest: str,
    classification: SecurityClassification,
    policy: SecurityPolicy,
    allow_host_execution: bool,
) -> dict[str, object]:
    """Execute one exact case without conflating generated and verifier authority."""

    _require_result_matches_shape(expected_result, result_shape)
    host_execution = baseline_execution.with_arguments(arguments)
    if runtime_toolchain is not None:
        runtime_toolchain.require_unchanged()
    observation = host_execution.observation_request(
        effective_revision_digest=effective_revision_digest,
        source_digests=classification.source_digests,
    )
    execution_authorization = policy.authorize_observation(
        classification,
        observation,
        actor="sample-conformance-runner",
        reason=(
            "execute one current generated implementation test against the exact "
            "compiled sample entrypoint as an acknowledged host process"
            if verification_source == "generated-implementation-test"
            else "execute one withheld verifier case against the exact compiled "
            "sample entrypoint as an acknowledged host process"
        ),
        issued_at=FIXED_TIME,
        expires_at=FIXED_TIME + timedelta(minutes=5),
        yolo_acknowledged=True,
    )
    try:
        execution_result = AuthorizedHostArtifactRunner(
            host_execution_acknowledged=allow_host_execution,
            authorization_verifier=LiveObservationExecutionAuthorizationVerifier(
                lambda: AuthorizationRevocationSet()
            ),
            clock=lambda: FIXED_TIME + timedelta(seconds=1),
        ).run(host_execution, observation, execution_authorization)
    except HostArtifactExecutionError as exc:
        candidate_rejection = _generated_application_rejection(
            verification_source=verification_source,
            case_id=case_id,
            expected_result=expected_result,
            execution_error=exc,
        )
        if candidate_rejection is not None:
            raise candidate_rejection from exc
        raise
    if runtime_toolchain is not None:
        runtime_toolchain.require_unchanged()
    result = execution_result.result
    auxiliary_results = [
        {
            "role": item.role,
            "result": item.result,
            "stdout_digest": item.stdout_digest,
            "stderr_digest": item.stderr_digest,
        }
        for item in execution_result.auxiliary_results
    ]
    if variant.variant_id == "rust-javascript-full-stack":
        if len(arguments) != 1 or not isinstance(arguments[0], dict):
            raise SampleFailure(
                "full-stack execution requires one release snapshot argument"
            )
        if len(auxiliary_results) != 1 or auxiliary_results[0]["role"] != "backend":
            raise SampleFailure(
                "full-stack execution did not expose exactly one backend result"
            )
        if verification_source != "generated-implementation-test":
            expected_analysis = _release_analysis_result(arguments[0])
            observed_analysis = auxiliary_results[0]["result"]
            if canonical_json_bytes(observed_analysis) != canonical_json_bytes(
                expected_analysis
            ):
                raise _IndependentBehaviorMismatch(
                    case_id=case_id,
                    verification_source=verification_source,
                    expected_result=expected_analysis,
                    observed_result=observed_analysis,
                )
            if canonical_json_bytes(
                _release_dashboard_from_analysis(expected_analysis)
            ) != canonical_json_bytes(expected_result):
                raise SampleFailure(
                    "full-stack acceptance oracle does not match its backend-to-"
                    "frontend protocol"
                )
    elif auxiliary_results:
        raise SampleFailure(
            "single-role host execution unexpectedly reported an auxiliary role"
        )
    try:
        _require_result_matches_shape(result, result_shape)
    except SampleFailure as exc:
        if verification_source == "generated-implementation-test":
            raise _GeneratedBehaviorMismatch(
                case_id=case_id,
                expected_result=expected_result,
                observed_result=result,
            ) from exc
        raise _IndependentBehaviorMismatch(
            case_id=case_id,
            verification_source=verification_source,
            expected_result=expected_result,
            observed_result=result,
        ) from exc
    if canonical_json_bytes(result) != canonical_json_bytes(expected_result):
        if verification_source == "generated-implementation-test":
            raise _GeneratedBehaviorMismatch(
                case_id=case_id,
                expected_result=expected_result,
                observed_result=result,
            )
        raise _IndependentBehaviorMismatch(
            case_id=case_id,
            verification_source=verification_source,
            expected_result=expected_result,
            observed_result=result,
        )
    return {
        "case_id": case_id,
        "passed": True,
        "verification_source": verification_source,
        "result": result,
        "artifact_digest": host_execution.artifact_digest,
        "artifact_tree_digest": host_execution.artifact_tree_digest,
        "entrypoint_digest": host_execution.entrypoint_digest,
        "harness_digest": host_execution.harness_digest,
        "observation_request_identity": canonical_identity(observation.to_dict()).uri,
        "execution_authorization_id": execution_authorization.authorization_id,
        "observation_request": observation.to_dict(),
        "execution_authorization": execution_authorization.to_dict(),
        "execution_authorization_classification_digest": (
            execution_authorization.classification_digest
        ),
        "execution_security_profile": execution_authorization.profile.value,
        "execution_mode": execution_result.execution_mode,
        "stdout_digest": execution_result.stdout_digest,
        "stderr_digest": execution_result.stderr_digest,
        "auxiliary_results": auxiliary_results,
    }


class _SampleGeneratedTestRunner:
    """Run recipe-bound generated cases before the workspace tree is accepted."""

    runner_id = "tester:sample-generated-implementation@1"

    def __init__(
        self,
        *,
        sample_root: Path,
        runtime_root: Path,
        recipe: GenerationRecipe,
        model: _SpecificationCompilerModel,
        execution_contract: Mapping[str, object],
        variant: _ExecutionVariant,
        policy: SecurityPolicy,
        effective_revision_digest: str,
        runtime_command: tuple[str, ...] | None,
        runtime_toolchain,
        expected_toolchain_identity: str,
        toolchain_output_field: str,
        composite_toolchain: RustJavaScriptToolchain | None,
        allow_host_execution: bool,
        input_closure: PinnedInputClosure,
    ) -> None:
        self.sample_root = sample_root
        self.runtime_root = runtime_root
        self.recipe = recipe
        self.model = model
        self.execution_contract = execution_contract
        self.variant = variant
        self.policy = policy
        self.effective_revision_digest = effective_revision_digest
        self.runtime_command = runtime_command
        self.runtime_toolchain = runtime_toolchain
        self.expected_toolchain_identity = expected_toolchain_identity
        self.toolchain_output_field = toolchain_output_field
        self.composite_toolchain = composite_toolchain
        self.allow_host_execution = allow_host_execution
        self.input_closure = input_closure

    def run(
        self,
        artifact,
        build,
        classification,
        dependency_resolution,
        test_suite,
    ):
        _require_sample_inputs_unchanged(self.input_closure, label="generation")
        invocations = self.execution_contract["invocations"]
        assert isinstance(invocations, list)
        suite = test_suite
        if suite.content_identity != artifact["generated_test_suite_identity"]:
            raise SampleFailure("generated-test runner received another admitted suite")
        if dependency_resolution.get("artifact_digest") != build["artifact_digest"]:
            raise SampleFailure(
                "generated-test runner received another dependency graph"
            )
        if (
            self.model.generation is None
            or self.model.generation.generated_test_suite_identity
            != suite.content_identity
        ):
            raise SampleFailure(
                "generated implementation tests differ from generation provenance"
            )
        first_invocation = invocations[0]
        assert isinstance(first_invocation, dict)
        seed_arguments = first_invocation["arguments"]
        assert isinstance(seed_arguments, list)
        compiled = _compiled_sample_artifact(
            sample_root=self.sample_root,
            runtime_root=self.runtime_root,
            execution_contract=self.execution_contract,
            variant=self.variant,
            build=build,
            seed_arguments=seed_arguments,
            runtime_command=self.runtime_command,
            runtime_toolchain=self.runtime_toolchain,
            expected_toolchain_identity=self.expected_toolchain_identity,
            toolchain_output_field=self.toolchain_output_field,
            composite_toolchain=self.composite_toolchain,
        )
        current_classification = _classification_from_output(classification)
        try:
            case_results = [
                _execute_sample_case(
                    baseline_execution=compiled.execution,
                    case_id=case.case_id,
                    arguments=list(case.arguments),
                    expected_result=case.expected_result,
                    verification_source="generated-implementation-test",
                    result_shape=self.execution_contract["result_shape"],
                    variant=self.variant,
                    runtime_toolchain=self.runtime_toolchain,
                    effective_revision_digest=self.effective_revision_digest,
                    classification=current_classification,
                    policy=self.policy,
                    allow_host_execution=self.allow_host_execution,
                )
                for case in suite.cases
            ]
        except (_GeneratedBehaviorMismatch, _GeneratedApplicationRejected) as exc:
            raise _GeneratedCandidateRejected(
                exc,
                source_bundle_digest=str(artifact["source_bundle_digest"]),
                artifact_digest=str(build["artifact_digest"]),
                generated_test_suite_identity=suite.content_identity,
            ) from exc
        _require_sample_inputs_unchanged(self.input_closure, label="generation")
        return {
            "passed": True,
            "runner_id": self.runner_id,
            "execution_profile": AUTHORIZED_EXECUTION_PROFILE,
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": suite.content_identity,
            "total": len(suite.cases),
            "passed_count": len(suite.cases),
            "failed_count": 0,
            "skipped_count": 0,
            "case_results": case_results,
            "categories": sorted(suite.categories),
        }


class _SampleIndependentAcceptanceRunner:
    """Run verifier-only cases before the candidate tree becomes workspace truth."""

    runner_id = "acceptance:sample-independent-verifier@1"

    def __init__(
        self,
        *,
        sample_id: str,
        sample_root: Path,
        runtime_root: Path,
        execution_contract: Mapping[str, object],
        oracle_results: Sequence[object],
        acceptance_suite_identity: str,
        variant: _ExecutionVariant,
        policy: SecurityPolicy,
        effective_revision_digest: str,
        runtime_command: tuple[str, ...] | None,
        runtime_toolchain,
        expected_toolchain_identity: str,
        toolchain_output_field: str,
        composite_toolchain: RustJavaScriptToolchain | None,
        allow_host_execution: bool,
        input_closure: PinnedInputClosure,
    ) -> None:
        self.sample_id = sample_id
        self.sample_root = sample_root
        self.runtime_root = runtime_root
        self.execution_contract = execution_contract
        self.oracle_results = tuple(oracle_results)
        self.acceptance_suite_identity = acceptance_suite_identity
        self.variant = variant
        self.policy = policy
        self.effective_revision_digest = effective_revision_digest
        self.runtime_command = runtime_command
        self.runtime_toolchain = runtime_toolchain
        self.expected_toolchain_identity = expected_toolchain_identity
        self.toolchain_output_field = toolchain_output_field
        self.composite_toolchain = composite_toolchain
        self.allow_host_execution = allow_host_execution
        self.input_closure = input_closure

    def run(
        self,
        artifact,
        build,
        classification,
        generated_tests,
        dependency_resolution,
    ):
        _require_sample_inputs_unchanged(self.input_closure, label="verifier")
        if dependency_resolution.get("artifact_digest") != build["artifact_digest"]:
            raise SampleFailure(
                "independent verifier received another dependency graph"
            )
        invocations = self.execution_contract["invocations"]
        assert isinstance(invocations, list)
        first_invocation = invocations[0]
        assert isinstance(first_invocation, dict)
        seed_arguments = first_invocation["arguments"]
        assert isinstance(seed_arguments, list)
        compiled = _compiled_sample_artifact(
            sample_root=self.sample_root,
            runtime_root=self.runtime_root,
            execution_contract=self.execution_contract,
            variant=self.variant,
            build=build,
            seed_arguments=seed_arguments,
            runtime_command=self.runtime_command,
            runtime_toolchain=self.runtime_toolchain,
            expected_toolchain_identity=self.expected_toolchain_identity,
            toolchain_output_field=self.toolchain_output_field,
            composite_toolchain=self.composite_toolchain,
        )
        generated_case_results = generated_tests.get("case_results")
        if not isinstance(generated_case_results, Sequence) or isinstance(
            generated_case_results, (str, bytes, bytearray)
        ):
            raise SampleFailure("generated-test evidence omitted its case results")
        reserved_case_ids = tuple(
            str(case.get("case_id"))
            for case in generated_case_results
            if isinstance(case, Mapping)
        )
        if len(reserved_case_ids) != len(generated_case_results):
            raise SampleFailure("generated-test evidence contains an invalid case")
        runtime_probe = create_post_build_probe(self.sample_id)
        cases = _independent_verification_cases(
            invocations,
            self.oracle_results,
            runtime_probe,
            reserved_case_ids=reserved_case_ids,
        )
        current_classification = _classification_from_output(classification)
        try:
            case_results = [
                _execute_sample_case(
                    baseline_execution=compiled.execution,
                    case_id=case_id,
                    arguments=arguments,
                    expected_result=expected_result,
                    verification_source=verification_source,
                    result_shape=self.execution_contract["result_shape"],
                    variant=self.variant,
                    runtime_toolchain=self.runtime_toolchain,
                    effective_revision_digest=self.effective_revision_digest,
                    classification=current_classification,
                    policy=self.policy,
                    allow_host_execution=self.allow_host_execution,
                )
                for case_id, arguments, expected_result, verification_source in cases
            ]
        except (_IndependentBehaviorMismatch, _IndependentApplicationRejected) as exc:
            raise _IndependentCandidateRejected(
                exc,
                source_bundle_digest=str(artifact["source_bundle_digest"]),
                artifact_digest=str(build["artifact_digest"]),
                independent_acceptance_suite_identity=self.acceptance_suite_identity,
            ) from exc
        _require_sample_inputs_unchanged(self.input_closure, label="verifier")
        return {
            "passed": True,
            "runner_id": self.runner_id,
            "execution_profile": AUTHORIZED_EXECUTION_PROFILE,
            "classification_digest": classification["classification_digest"],
            "effective_revision_digest": artifact["effective_revision_digest"],
            "source_bundle_digest": artifact["source_bundle_digest"],
            "artifact_digest": build["artifact_digest"],
            "dependency_resolution_identity": dependency_resolution[
                "resolution_identity"
            ],
            "verified_tree_identity": artifact["tree_identity"],
            "test_suite_identity": self.acceptance_suite_identity,
            "total": len(case_results),
            "passed_count": len(case_results),
            "failed_count": 0,
            "skipped_count": 0,
            "case_results": case_results,
            "post_build_runtime_probe_case_id": runtime_probe.case_id,
            "post_build_runtime_probe_count": 1,
        }


def _accepted_source_files(accepted_root: Path) -> dict[str, str]:
    """Read only generated source, excluding workspace/index sidecars."""

    return {
        path.relative_to(accepted_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(accepted_root.rglob("*"))
        if path.is_file()
        and path.name
        not in {
            ".literate-tree.json",
            ".literate-source-index.json",
            ".literate-source-intelligence.json",
        }
        and ".codegraph" not in path.relative_to(accepted_root).parts
    }


def _run_host_e2e(
    sample_root: Path,
    scratch: Path,
    sample_id: str,
    oracle_reference: ContentReference,
    definition: ComponentDefinition,
    loaded: LoadedSpecification,
    input_closures: _SampleInputClosures,
    *,
    variant: _ExecutionVariant,
    source_generator,
    object_root: Path | None = None,
    cpp_toolchain=None,
    allow_host_execution: bool,
    planning_only: bool = False,
    pipeline_model: str | None = None,
) -> dict[str, object]:
    rejections: list[dict[str, object]] = []
    candidate_root = scratch / "candidate-attempts" / variant.variant_id
    candidate_root.mkdir(parents=True, exist_ok=False)
    candidate_root = candidate_root.resolve(strict=True)
    for attempt in range(1, _MAX_GENERATED_CANDIDATE_ATTEMPTS + 1):
        attempt_scratch = candidate_root / f"{attempt:02d}"
        try:
            result = _run_host_e2e_once(
                sample_root,
                attempt_scratch,
                sample_id,
                oracle_reference,
                definition,
                loaded,
                input_closures,
                variant=variant,
                source_generator=source_generator,
                object_root=object_root,
                cpp_toolchain=cpp_toolchain,
                allow_host_execution=allow_host_execution,
                planning_only=planning_only,
                candidate_feedback=rejections,
                pipeline_model=pipeline_model,
            )
        except GenerationFailure as exc:
            rejection = _generated_candidate_rejection(exc, attempt=attempt)
            if rejection is None:
                raise
            rejections.append(rejection)
            envelope = _candidate_rejection_envelope(variant.variant_id, rejections)
            _write_candidate_rejection_evidence(
                candidate_root, f"rejections-{attempt:02d}.json", envelope
            )
            if attempt == _MAX_GENERATED_CANDIDATE_ATTEMPTS:
                raise GenerationFailure(
                    "generation.generated-candidate-attempts-exhausted",
                    "generated candidate attempts exhausted: "
                    + canonical_json_bytes(envelope).decode("utf-8"),
                    None,
                ) from exc
            continue
        return {
            **result,
            "generation_candidate_attempt_count": attempt,
            "rejected_generation_candidates": rejections,
        }
    raise AssertionError("generated candidate attempt loop did not terminate")


def _generated_candidate_rejection(
    failure: GenerationFailure, *, attempt: int
) -> dict[str, object] | None:
    if failure.code == "generation.model-stage-failed":
        cause = _cause_of_type(failure, CodingCliError)
        run = failure.run
        if (
            not isinstance(cause, CodingCliError)
            or cause.code not in _RETRYABLE_MODEL_STAGE_CONTRACT_FAILURE_CODES
            or run is None
            or run.status is not GenerationStatus.FAILED
            or len(run.events) < 2
            or any(
                run.step(step_id) is not None for step_id in _EXPECTED_LIFECYCLE_STEPS
            )
        ):
            return None
        failed_stage_event, terminal_event = run.events[-2:]
        if (
            failed_stage_event.event_type != "model-stage-failed"
            or failed_stage_event.stage_id != "generate"
            or terminal_event.event_type != "run-failed"
            or terminal_event.stage_id is not None
            or terminal_event.data.get("code") != failure.code
        ):
            return None
        diagnostic = {
            "contract_failure_code": cause.code,
            "message": cause.message,
            "generation_run_id": run.run_id,
        }
        rejection_kind = (
            "generated-source-contract-rejected"
            if cause.code.startswith("coding_cli.generated_")
            else "generated-test-contract-rejected"
        )
        return {
            "schema": "literate-ai/generated-candidate-rejection@1",
            "attempt": attempt,
            "failure_code": failure.code,
            "generation_run_id": run.run_id,
            "rejection_kind": rejection_kind,
            **diagnostic,
            "diagnostic_identity": canonical_identity(diagnostic).uri,
        }
    if failure.code == "generation.validation-rejected":
        run = failure.run
        if (
            run is None
            or run.status is not GenerationStatus.FAILED
            or len(run.events) < 2
        ):
            return None
        rejected_event, terminal_event = run.events[-2:]
        if (
            rejected_event.event_type != "tree-rejected"
            or rejected_event.stage_id != "validate"
            or terminal_event.event_type != "run-failed"
            or terminal_event.stage_id is not None
            or terminal_event.data.get("code") != failure.code
        ):
            return None
        stage = run.stage("generate")
        validation = run.step("validate")
        if (
            stage is None
            or validation is None
            or any(
                run.step(step_id) is not None
                for step_id in _EXPECTED_LIFECYCLE_STEPS
                if step_id != "validate"
            )
        ):
            return None
        findings = validation.output.get("findings")
        if not isinstance(findings, list) or not findings:
            return None
        failed_findings: list[dict[str, object]] = []
        for finding in findings:
            if not isinstance(finding, Mapping) or finding.get("severity") != "error":
                continue
            code = finding.get("code")
            if code not in _RETRYABLE_GENERATED_SOURCE_FINDING_CODES:
                return None
            failed_findings.append(
                {
                    key: finding[key]
                    for key in ("code", "message", "path", "line")
                    if key in finding
                }
            )
        if not failed_findings:
            return None
        source_identities = _generated_candidate_source_identities(stage.response)
        if source_identities is None:
            return None
        diagnostic = {
            "validation_finding_codes": sorted(
                {str(item["code"]) for item in failed_findings}
            ),
            "validation_findings": failed_findings,
            "validation_output_identity": validation.output_identity.uri,
            "candidate_tree_identity": source_identities["candidate_tree_identity"],
            "source_bundle_digest": source_identities["source_bundle_digest"],
        }
        return {
            "schema": "literate-ai/generated-candidate-rejection@1",
            "attempt": attempt,
            "failure_code": failure.code,
            "generation_run_id": run.run_id,
            "generated_stage_output_identity": stage.output_identity.uri,
            "rejection_kind": "generated-source-validation-rejected",
            **diagnostic,
            "diagnostic_identity": canonical_identity(diagnostic).uri,
        }
    if failure.code != "generation.lifecycle-step-failed":
        return None
    run = failure.run
    if run is None or run.status is not GenerationStatus.FAILED or len(run.events) < 2:
        return None
    failed_step_event, terminal_event = run.events[-2:]
    if (
        failed_step_event.event_type != "lifecycle-step-failed"
        or terminal_event.event_type != "run-failed"
        or terminal_event.stage_id is not None
        or terminal_event.data.get("code") != failure.code
    ):
        return None
    stage = run.stage("generate")
    if stage is None:
        return None
    common: dict[str, object] = {
        "schema": "literate-ai/generated-candidate-rejection@1",
        "attempt": attempt,
        "failure_code": failure.code,
        "generation_run_id": run.run_id,
        "generated_stage_output_identity": stage.output_identity.uri,
    }
    if failed_step_event.stage_id == "validate":
        cause = _cause_of_type(failure, DependencyObservationError)
        if not isinstance(cause, DependencyObservationError):
            return None
        validation_failure_code = cause.code
        if (
            validation_failure_code not in _GENERATED_SOURCE_VALIDATION_FAILURE_CODES
            or failed_step_event.data.get("error_code") != validation_failure_code
            or any(
                run.step(step_id) is not None for step_id in _EXPECTED_LIFECYCLE_STEPS
            )
        ):
            return None
        source_identities = _generated_candidate_source_identities(stage.response)
        if source_identities is None:
            return None
        diagnostic = {
            "validation_failure_code": validation_failure_code,
            "candidate_tree_identity": source_identities["candidate_tree_identity"],
            "source_bundle_digest": source_identities["source_bundle_digest"],
        }
        validation_failure_summary = _GENERATED_SOURCE_VALIDATION_FAILURE_SUMMARIES.get(
            validation_failure_code
        )
        if validation_failure_summary is not None:
            diagnostic["validation_failure_summary"] = validation_failure_summary
        return {
            **common,
            "rejection_kind": "generated-source-validation-rejected",
            **diagnostic,
            **source_identities,
            "diagnostic_identity": canonical_identity(diagnostic).uri,
        }
    if failed_step_event.stage_id == "test-generated":
        cause = _cause_of_type(failure, _GeneratedCandidateRejected)
        if not isinstance(cause, _GeneratedCandidateRejected) or any(
            run.step(step_id) is not None
            for step_id in ("verify-independent", "prepare-tree", "commit-tree")
        ):
            return None
        build = run.step("build")
        if build is None:
            return None
        build_output = build.output
        if (
            build_output.get("source_bundle_digest") != cause.source_bundle_digest
            or build_output.get("artifact_digest") != cause.artifact_digest
        ):
            return None
        diagnostic = dict(cause.case_evidence)
        return {
            **common,
            "rejection_kind": cause.rejection_kind,
            **diagnostic,
            "source_bundle_digest": cause.source_bundle_digest,
            "artifact_digest": cause.artifact_digest,
            "generated_test_suite_identity": cause.generated_test_suite_identity,
            "diagnostic_identity": canonical_identity(diagnostic).uri,
        }
    if failed_step_event.stage_id == "verify-independent":
        cause = _cause_of_type(failure, _IndependentCandidateRejected)
        if not isinstance(cause, _IndependentCandidateRejected) or any(
            run.step(step_id) is not None for step_id in ("prepare-tree", "commit-tree")
        ):
            return None
        build = run.step("build")
        generated_tests = run.step("test-generated")
        if build is None or generated_tests is None:
            return None
        if (
            build.output.get("source_bundle_digest") != cause.source_bundle_digest
            or build.output.get("artifact_digest") != cause.artifact_digest
            or generated_tests.output.get("source_bundle_digest")
            != cause.source_bundle_digest
            or generated_tests.output.get("artifact_digest") != cause.artifact_digest
        ):
            return None
        diagnostic = dict(cause.case_evidence)
        return {
            **common,
            "rejection_kind": cause.rejection_kind,
            **diagnostic,
            "source_bundle_digest": cause.source_bundle_digest,
            "artifact_digest": cause.artifact_digest,
            "independent_acceptance_suite_identity": (
                cause.independent_acceptance_suite_identity
            ),
            "diagnostic_identity": canonical_identity(diagnostic).uri,
        }
    if failed_step_event.stage_id != "build":
        return None
    cause = _cause_of_type(failure, BuildError)
    if not isinstance(cause, BuildError):
        return None
    build_failure_code = cause.code
    if (
        build_failure_code not in _GENERATED_SOURCE_BUILD_FAILURE_CODES
        or failed_step_event.data.get("error_code") != build_failure_code
        or run.step("build") is not None
        or any(
            run.step(step_id) is not None
            for step_id in (
                "resolve-dependencies",
                "test-generated",
                "verify-independent",
                "prepare-tree",
                "commit-tree",
            )
        )
        or any(
            run.step(step_id) is None
            for step_id in ("validate", "classify", "authorize-build")
        )
    ):
        return None
    source_identities = _generated_candidate_source_identities(stage.response)
    if source_identities is None:
        return None
    diagnostic = {
        "build_failure_code": build_failure_code,
        "candidate_tree_identity": source_identities["candidate_tree_identity"],
        "source_bundle_digest": source_identities["source_bundle_digest"],
    }
    return {
        **common,
        "rejection_kind": "generated-source-build-rejected",
        "build_failure_code": build_failure_code,
        **source_identities,
        "diagnostic_identity": canonical_identity(diagnostic).uri,
    }


def _cause_of_type(
    failure: BaseException, expected_type: type[BaseException]
) -> BaseException | None:
    cause: BaseException | None = failure.__cause__
    seen: set[int] = set()
    while cause is not None and not isinstance(cause, expected_type):
        marker = id(cause)
        if marker in seen:
            return None
        seen.add(marker)
        cause = cause.__cause__
    return cause


def _generated_candidate_source_identities(
    response: Mapping[str, object],
) -> dict[str, str] | None:
    files_value = response.get("files")
    if (
        not isinstance(files_value, Mapping)
        or not files_value
        or any(
            not isinstance(path, str) or not isinstance(content, str)
            for path, content in files_value.items()
        )
    ):
        return None
    files = {str(path): str(content) for path, content in files_value.items()}
    entries = [
        {
            "path": path,
            "size": len(content.encode("utf-8")),
            "digest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for path, content in sorted(files.items())
    ]
    suite = files.get(GENERATED_TEST_SUITE_PATH)
    if suite is None:
        return None
    tree_material = {
        "files": [
            {
                "path": path,
                "content_identity": canonical_identity(content).to_dict(),
            }
            for path, content in files.items()
        ]
    }
    return {
        "candidate_tree_identity": canonical_identity(tree_material).uri,
        "source_bundle_digest": "sha256:"
        + hashlib.sha256(canonical_json_bytes(entries)).hexdigest(),
        "generated_test_suite_identity": "sha256:"
        + hashlib.sha256(suite.encode("utf-8")).hexdigest(),
    }


def _candidate_rejection_envelope(
    variant_id: str, rejections: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    material: dict[str, object] = {
        "schema": "literate-ai/generated-candidate-attempts@1",
        "variant": variant_id,
        "maximum_attempts": _MAX_GENERATED_CANDIDATE_ATTEMPTS,
        "rejections": [dict(item) for item in rejections],
    }
    return {**material, "identity": canonical_identity(material).uri}


def _write_candidate_rejection_evidence(
    directory: Path, filename: str, value: Mapping[str, object]
) -> None:
    """Create one write-once harness record without following generated-tree links."""

    parent = Path(os.path.abspath(directory))
    destination = parent / filename
    raw = canonical_json_bytes(value) + b"\n"
    parent_descriptor: int | None = None
    descriptor: int | None = None
    written_identity: tuple[int, int] | None = None
    try:
        if parent.resolve(strict=True) != parent:
            raise OSError
        parent_named = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_named.st_mode):
            raise OSError
        parent_identity = (parent_named.st_dev, parent_named.st_ino)
        use_directory_descriptor = os.name != "nt" and all(
            operation in os.supports_dir_fd
            for operation in (os.open, os.stat, os.unlink)
        )
        if use_directory_descriptor:
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened_parent = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(opened_parent.st_mode)
                or (opened_parent.st_dev, opened_parent.st_ino) != parent_identity
            ):
                raise OSError
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = (
            os.open(filename, flags, 0o600, dir_fd=parent_descriptor)
            if parent_descriptor is not None
            else os.open(destination, flags, 0o600)
        )
        opened_file = os.fstat(descriptor)
        written_identity = (opened_file.st_dev, opened_file.st_ino)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            written = os.fstat(stream.fileno())
        named = (
            os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
            if parent_descriptor is not None
            else os.stat(destination, follow_symlinks=False)
        )
        current_parent = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_size != len(raw)
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != written_identity
            or (current_parent.st_dev, current_parent.st_ino) != parent_identity
        ):
            raise OSError
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if written_identity is not None:
            try:
                named = (
                    os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
                    if parent_descriptor is not None
                    else os.stat(destination, follow_symlinks=False)
                )
                if (named.st_dev, named.st_ino) == written_identity:
                    if parent_descriptor is not None:
                        os.unlink(filename, dir_fd=parent_descriptor)
                    else:
                        destination.unlink()
            except OSError:
                pass
        raise SampleFailure(
            "generated-candidate rejection evidence could not be written exactly once"
        ) from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _run_host_e2e_once(
    sample_root: Path,
    scratch: Path,
    sample_id: str,
    oracle_reference: ContentReference,
    definition: ComponentDefinition,
    loaded: LoadedSpecification,
    input_closures: _SampleInputClosures,
    *,
    variant: _ExecutionVariant,
    source_generator,
    object_root: Path | None = None,
    cpp_toolchain=None,
    allow_host_execution: bool,
    planning_only: bool = False,
    candidate_feedback: Sequence[Mapping[str, object]] = (),
    pipeline_model: str | None = None,
) -> dict[str, object]:
    if not allow_host_execution and not planning_only:
        raise SampleFailure("host build and execution require explicit acknowledgement")
    execution, execution_document = _execution_contract(sample_root, definition, loaded)
    invocations_value = execution["invocations"]
    assert isinstance(invocations_value, list)
    invocation_ids = tuple(
        str(invocation["case_id"])
        for invocation in invocations_value
        if isinstance(invocation, Mapping)
    )
    oracle, oracle_reference = _execution_oracle(
        sample_root,
        oracle_reference,
        ContentIdentity.parse_uri(execution_document.identity),
        invocation_ids,
        execution["result_shape"],
    )
    oracle_results = oracle["oracle_results"]
    assert isinstance(oracle_results, list)
    runtime_root = scratch / "e2e" / variant.variant_id
    runtime_root.mkdir(parents=True, exist_ok=False)
    execution_identity = ContentIdentity.parse_uri(execution_document.identity)
    acceptance_suite_identity = canonical_identity(
        {
            "profile": "sample-independent-acceptance-suite@1",
            "runner_id": _SampleIndependentAcceptanceRunner.runner_id,
            "acceptance_contract_identity": execution_identity.uri,
            "oracle_identity": oracle_reference.identity.uri,
            "runtime_probe_profile": "post-build-entropy@1",
        }
    ).uri
    (
        base,
        target,
        resolved_targets,
        authority,
        authority_catalog,
        authority_plan,
        selected_flavors,
    ) = _sample_runtime_recipe(
        sample_root,
        definition,
        loaded.specification_set,
        execution,
        variant,
        input_closures.generation,
    )
    try:
        input_closures.verifier.include(input_closures.generation)
    except PinnedInputClosureError as exc:
        raise SampleFailure("sample verifier input closure is invalid") from exc
    ordered_flavors = _ordered_selected_flavors(
        definition,
        selected_flavors,
        variant,
    )
    effective_toolchain_constraints = _effective_toolchain_constraints(
        ordered_flavors,
        _authoring_toolchain_constraints(
            sample_root,
            definition,
            boundary=project_boundary(sample_root, legacy=sample_root.parent),
        ),
    )
    flavor_specification_ids = tuple(
        item.specification_identity for item in ordered_flavors
    )
    selection = getattr(source_generator, "selection", None)
    coding_cli = getattr(selection, "name", "external-adapter")
    model_scope = None
    if pipeline_model is not None:
        model_bindings = _locked_standard_sample_model_bindings(
            _StandardSampleAuthoritySnapshot(authority, authority_catalog),
            coding_cli=coding_cli,
            pipeline_model=pipeline_model,
        )
        model_scope = model_bindings[authority.lock.root_revision.uri]
    recipe = _generation_recipe(
        sample_root=sample_root,
        definition=definition,
        loaded=loaded,
        acceptance_document=execution_document,
        selected_flavors=ordered_flavors,
        base_skills=_coding_skills(
            sample_root,
            definition,
            boundary=project_boundary(sample_root, legacy=sample_root.parent),
            source=f"Component {definition.coordinate.uri}",
        ),
        variant=variant,
        authority=authority,
        catalog=authority_catalog,
        resolved_inputs=(
            ("component_lock", authority.lock.identity.uri),
            ("root_revision", authority.lock.root_revision.uri),
            ("target_profile", authority.lock.target_profile_identity.uri),
            ("selection_policy", authority.lock.selection_policy_identity.uri),
        ),
        model_scope=model_scope,
    )
    effective = next(
        item.revision
        for item in authority.lock.nodes
        if item.revision.identity == authority.lock.root_revision
    )
    authority_evidence = runtime_root / "evidence" / "component.lock.json"
    authority_evidence.parent.mkdir(parents=True, exist_ok=False)
    authority_evidence.write_bytes(
        canonical_json_bytes(authority.lock.to_dict()) + b"\n"
    )
    execution_plan = _sample_execution_plan(
        sample_root, definition, effective, recipe, source_generator
    )
    skill_ids = tuple(item.identity for item in recipe.resolved_skills)
    model = _SpecificationCompilerModel(
        recipe=recipe,
        source_generator=source_generator,
        generation_root=runtime_root / "coding-cli",
        specification=loaded.specification_set,
        flavor_specification_ids=flavor_specification_ids,
        skill_ids=skill_ids,
        execution_contract_id=execution_identity.uri,
        requirement_id=str(execution["requirement_id"]),
        scenario_id=str(execution["scenario_id"]),
        execution_plan=execution_plan,
        candidate_feedback=candidate_feedback,
    )
    policy = SecurityPolicy(
        canonical_identity(
            {
                "policy": "sample-host-e2e@2",
                "sample": sample_id,
                "variant": variant.variant_id,
            }
        ).uri
    )
    live_build_verifier = LiveBuildAuthorizationVerifier(
        lambda: AuthorizationRevocationSet()
    )
    runtime_command: tuple[str, ...] | None = None
    runtime_toolchain = None
    composite_toolchain: RustJavaScriptToolchain | None = None
    observed_toolchains: tuple[object, ...]
    toolchain_output_field = "toolchain_identity"
    bazel_build_options: tuple[str, ...] = ()
    artifact_store = (
        runtime_root / "artifacts" if object_root is None else object_root / "artifacts"
    )
    if variant.variant_id == "python":
        python_constraint = _required_toolchain_constraint(
            effective_toolchain_constraints, "python"
        )
        python_toolchain = discover_python_toolchain(
            **_toolchain_discovery_options(
                python_constraint.constraint,
                operator_environment="PYTHON",
            )
        )
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:python-bytecode@1",
            "toolchain_digest": python_toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": ["python-bytecode"],
        }
        validators = (PythonSyntaxValidator(),)
        scanner_rules = baseline_python_rules()
        scanner_id = "scanner:sample-host-python@2"
        runtime_command = python_toolchain.command
        runtime_toolchain = python_toolchain
        observed_toolchains = (python_toolchain,)
        builder = PythonBuildAdapter(
            artifact_store,
            python_toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
    elif variant.variant_id == "cpp":
        toolchain = cpp_toolchain or discover_cpp_toolchain()
        bazel_build_options = bazel_sdk_build_options(toolchain.environment)
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:cpp-native@1",
            "toolchain_digest": toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": ["native-executable"],
        }
        validators = (
            CppSourceValidator(),
            CppPortableLifetimeValidator(),
            CppTranslationUnitIncludeValidator(),
        )
        scanner_rules = baseline_cpp_rules()
        scanner_id = "scanner:sample-host-cpp@1"
        toolchain_output_field = "compiler_identity"
        builder = CppBuildAdapter(
            artifact_store,
            toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
        observed_toolchains = (toolchain,)
    elif variant.variant_id == "rust":
        toolchain = discover_rust_toolchain()
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:rust-native@1",
            "toolchain_digest": toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": ["native-executable"],
        }
        validators = (RustSourceValidator(),)
        scanner_rules = baseline_rust_rules()
        scanner_id = "scanner:sample-host-rust@1"
        toolchain_output_field = "compiler_identity"
        builder = RustBuildAdapter(
            artifact_store,
            toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
        observed_toolchains = (toolchain,)
    elif variant.variant_id == "swift":
        swift_constraint = _required_toolchain_constraint(
            effective_toolchain_constraints, "swift"
        )
        configured = dict(os.environ)
        configured["CXX"] = " ".join(swift_constraint.constraint.command)
        toolchain = discover_cpp_toolchain(configured)
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:swift-native@1",
            "toolchain_digest": toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": ["native-executable"],
        }
        validators = (SwiftSourceValidator(),)
        scanner_rules = baseline_swift_rules()
        scanner_id = "scanner:sample-host-swift@1"
        toolchain_output_field = "compiler_identity"
        builder = SwiftBuildAdapter(
            artifact_store,
            toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
        observed_toolchains = (toolchain,)
    elif variant.variant_id == "javascript":
        node_constraint = _required_toolchain_constraint(
            effective_toolchain_constraints, "node"
        )
        toolchain = discover_node_toolchain(
            **_toolchain_discovery_options(
                node_constraint.constraint,
                operator_environment="NODE",
            )
        )
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:javascript-node-check@1",
            "toolchain_digest": toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": ["javascript-checked-bundle"],
        }
        validators = (JavaScriptSourceValidator(),)
        scanner_rules = baseline_javascript_rules()
        scanner_id = "scanner:sample-host-javascript@1"
        runtime_command = toolchain.command
        runtime_toolchain = toolchain
        observed_toolchains = (toolchain,)
        builder = JavaScriptBuildAdapter(
            artifact_store,
            toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
    elif variant.variant_id == "rust-javascript-full-stack":
        node_constraint = _required_toolchain_constraint(
            effective_toolchain_constraints, "node"
        )
        toolchain = RustJavaScriptToolchain(
            rust=discover_rust_toolchain(),
            node=discover_node_toolchain(
                **_toolchain_discovery_options(
                    node_constraint.constraint,
                    operator_environment="NODE",
                )
            ),
        )
        composite_toolchain = toolchain
        toolchain.require_unchanged()
        build_request = {
            "effective_revision_digest": effective.identity.uri,
            "builder_id": "builder:rust-javascript-full-stack@1",
            "toolchain_digest": toolchain.identity,
            "sandbox_profile": UNSANDBOXED_HOST_BUILD_PROFILE,
            "requested_privileges": list(UNSANDBOXED_HOST_BUILD_PRIVILEGES),
            "allowed_outputs": [
                "javascript-checked-bundle",
                "native-executable",
            ],
        }
        validators = (RustSourceValidator(), JavaScriptSourceValidator())
        scanner_rules = (*baseline_rust_rules(), *baseline_javascript_rules())
        scanner_id = "scanner:sample-host-rust-javascript@1"
        runtime_command = toolchain.node.command
        runtime_toolchain = toolchain.node
        observed_toolchains = (toolchain.rust, toolchain.node)
        builder = RustJavaScriptBuildAdapter(
            artifact_store,
            toolchain,
            clock=lambda: FIXED_TIME,
            authorization_verifier=live_build_verifier,
        )
    else:
        raise SampleFailure(
            f"unsupported sample execution variant: {variant.variant_id}"
        )
    if variant.variant_id != "cpp":
        validators = (*validators, CppTranslationUnitIncludeValidator())
    selected_build_systems = tuple(
        item
        for item in ordered_flavors
        if item.flavor.definition.primary_axis is FlavorAxis.BUILD_SYSTEM
    )
    if any(
        item.flavor.definition.coordinate.name == "build-bazel"
        for item in selected_build_systems
    ):
        allowed_outputs = build_request["allowed_outputs"]
        assert isinstance(allowed_outputs, list)
        allowed_outputs.append(BAZEL_DEPENDENCY_EVIDENCE_OUTPUT)
        bazel_toolchain = discover_bazel_toolchain()
        builder = BazelConformanceBuildAdapter(
            builder,
            bazel_toolchain,
            artifact_store,
            authorization_verifier=live_build_verifier,
            clock=lambda: FIXED_TIME,
            cache_root=(None if object_root is None else object_root / "bazel"),
            build_options=bazel_build_options,
        )
        observed_toolchains = (*observed_toolchains, bazel_toolchain)
    build_cache = None
    if object_root is not None and not planning_only:
        build_cache = CachedBuildAdapter(
            builder,
            object_root=object_root,
            project_root=Path(__file__).resolve().parents[3],
            namespace_material={
                "variant": variant.variant_id,
                "toolchains": [
                    getattr(item, "identity", None) for item in observed_toolchains
                ],
                "build_system_flavors": [
                    item.flavor.definition.coordinate.uri
                    for item in selected_build_systems
                ],
            },
        )
        builder = build_cache
    if recipe.managed_sbom_graph is None:
        raise SampleFailure("sample recipe omitted its managed dependency graph")
    observed_components, observed_edges = _toolchain_sbom_observations(
        recipe.managed_sbom_graph.root_ref,
        observed_toolchains,
    )
    toolchain_commands: list[tuple[str, ...]] = []
    for observed_toolchain in observed_toolchains:
        command = getattr(observed_toolchain, "command", None)
        if (
            not isinstance(command, tuple)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise SampleFailure("sample toolchain lacks an exact executable command")
        toolchain_commands.append(command)
    workspace_store = WorkspaceTreeStore(runtime_root / "workspace")
    project = discover_project(sample_root)
    if project is None:
        raise SampleFailure("sample source-intelligence policy is unavailable")
    workspace_source_intelligence = select_source_intelligence_provider(
        project.definition.source_intelligence,
        SourceIntelligenceStage.SOURCE_GENERATION,
    )
    events = LifecycleEventStoreAdapter(AppendOnlyEventStore(runtime_root / "events"))
    acceptance_arguments = tuple(
        invocation["arguments"]
        for invocation in execution["invocations"]
        if isinstance(invocation, dict)
    )
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
                {"component": effective.identity.uri, "prompt": recipe.prompt()}
            ),
            complexity_decision_identity=canonical_identity(
                {"component": effective.identity.uri, "bounded": True}
            ),
            prompt_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(recipe.prompt().encode("utf-8")).hexdigest()
            ),
            generation_recipe_identity=ContentIdentity.parse_uri(recipe.identity),
            workspace_allocation_identity=canonical_identity(
                {
                    "component": effective.identity.uri,
                    "workspace": (f"samples/{sample_id}/{variant.variant_id}"),
                }
            ),
            workspace_reference=(f"samples/{sample_id}/{variant.variant_id}"),
            prompt=recipe.prompt(),
        ),
        execution_plan=execution_plan,
        build_request_declaration=BuildRequestDeclaration.from_dict(
            {
                "schema": BuildRequestDeclaration.SCHEMA,
                **build_request,
            }
        ),
        workspace_reference=(f"samples/{sample_id}/{variant.variant_id}"),
        generated_test_suite_policy=GeneratedTestSuitePolicy(
            recipe.identity,
            recipe.non_acceptance_document_paths,
            acceptance_arguments,
        ),
        managed_sbom_graph=recipe.managed_sbom_graph,
    )
    orchestrator = GenerationOrchestrator(
        readiness_provider=_ExactSampleReadiness(
            loaded.specification_set.identity.uri,
            flavor_specification_ids,
            skill_ids,
            execution_identity.uri,
        ),
        model_provider=model,
        validator=PipelineValidatorAdapter(
            ValidationPipeline(
                validators,
                required_categories=(
                    ("syntax", "correctness")
                    if variant.variant_id == "cpp"
                    else ("syntax",)
                ),
            )
        ),
        classifier=GeneratedTreeSecurityClassifier(
            policy,
            RuleBasedSourceScanner(scanner_id, scanner_rules),
            model.provider_id,
            "sample-local-trust-root",
        ),
        build_authorizer=PolicyBuildAuthorizer(
            policy,
            "sample-conformance-runner",
            "compile exact generated sample source in an acknowledged unsandboxed "
            "host process",
            yolo_acknowledged=allow_host_execution,
            clock=lambda: FIXED_TIME,
        ),
        builder=builder,
        dependency_resolver=CycloneDxLifecycleResolver(
            managed_graph=recipe.managed_sbom_graph,
            observer=PortableHostDependencyObserver(
                toolchain_commands=tuple(toolchain_commands),
            ),
            evidence_path=runtime_root / "evidence" / "resolved-sbom.cdx.json",
            additional_components=observed_components,
            additional_edges=observed_edges,
        ),
        generated_test_runner=_SampleGeneratedTestRunner(
            sample_root=sample_root,
            runtime_root=runtime_root,
            recipe=recipe,
            model=model,
            execution_contract=execution,
            variant=variant,
            policy=policy,
            effective_revision_digest=effective.identity.uri,
            runtime_command=runtime_command,
            runtime_toolchain=runtime_toolchain,
            expected_toolchain_identity=str(build_request["toolchain_digest"]),
            toolchain_output_field=toolchain_output_field,
            composite_toolchain=composite_toolchain,
            allow_host_execution=allow_host_execution,
            input_closure=input_closures.generation,
        ),
        acceptance_runner=_SampleIndependentAcceptanceRunner(
            sample_id=sample_id,
            sample_root=sample_root,
            runtime_root=runtime_root,
            execution_contract=execution,
            oracle_results=oracle_results,
            acceptance_suite_identity=acceptance_suite_identity,
            variant=variant,
            policy=policy,
            effective_revision_digest=effective.identity.uri,
            runtime_command=runtime_command,
            runtime_toolchain=runtime_toolchain,
            expected_toolchain_identity=str(build_request["toolchain_digest"]),
            toolchain_output_field=toolchain_output_field,
            composite_toolchain=composite_toolchain,
            allow_host_execution=allow_host_execution,
            input_closure=input_closures.verifier,
        ),
        workspace=WorkspaceTreeAdapter(
            workspace_store,
            source_intelligence=workspace_source_intelligence,
        ),
        event_store=events,
    )
    _require_sample_inputs_unchanged(input_closures.generation, label="generation")
    _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    authority_catalog.require_unchanged(nodes=authority_plan.nodes)
    try:
        lifecycle_result = HostComponentLifecycleSession(orchestrator, request).run()
        run = lifecycle_result.run
    finally:
        _require_sample_inputs_unchanged(input_closures.generation, label="generation")
        _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    if run.status is not GenerationStatus.COMPLETE or run.provenance is None:
        raise SampleFailure("sample generation lifecycle did not complete")
    if model.calls != ["plan", "generate"]:
        raise SampleFailure(
            "sample did not execute specification planning and generation"
        )
    lifecycle_steps = tuple(item.step_id for item in run.lifecycle_executions)
    if lifecycle_steps != _EXPECTED_LIFECYCLE_STEPS:
        raise SampleFailure("sample skipped a concrete generation lifecycle step")
    if len(events.stream(run.run_id)) != len(run.events):
        raise SampleFailure("sample lifecycle event stream is incomplete")
    build = run.step("build")
    dependency_resolution_step = run.step("resolve-dependencies")
    build_authorization = run.step("authorize-build")
    classification_step = run.step("classify")
    generated_test_step = run.step("test-generated")
    acceptance_step = run.step("verify-independent")
    commit = run.step("commit-tree")
    assert (
        build is not None
        and dependency_resolution_step is not None
        and build_authorization is not None
        and classification_step is not None
        and generated_test_step is not None
        and acceptance_step is not None
        and commit is not None
    )
    if build_authorization.output.get(
        "profile"
    ) != SecurityProfile.YOLO.value or "MAXIMUM PRIVILEGE" not in str(
        build_authorization.output.get("warning", "")
    ):
        raise SampleFailure("unsandboxed host build lacks explicit yolo authorization")
    invocations = execution["invocations"]
    assert isinstance(invocations, list)
    first_invocation = invocations[0]
    assert isinstance(first_invocation, dict)
    first_arguments = first_invocation["arguments"]
    assert isinstance(first_arguments, list)
    compiled = _compiled_sample_artifact(
        sample_root=sample_root,
        runtime_root=runtime_root,
        execution_contract=execution,
        variant=variant,
        build=build.output,
        seed_arguments=first_arguments,
        runtime_command=runtime_command,
        runtime_toolchain=runtime_toolchain,
        expected_toolchain_identity=str(build_request["toolchain_digest"]),
        toolchain_output_field=toolchain_output_field,
        composite_toolchain=composite_toolchain,
    )
    built_toolchain = compiled.built_toolchain
    compiled_files = list(compiled.compiled_files)
    compiled_relative = compiled.compiled_entrypoint
    required_compiled_files = compiled.required_compiled_files
    baseline_execution = compiled.execution
    accepted_root = workspace_store.resolve(request.workspace_reference)
    generated_files = model.generated_files
    if generated_files is None:
        raise SampleFailure("sample compiler did not emit generated source")
    accepted_files = (
        _accepted_source_files(accepted_root) if accepted_root is not None else {}
    )
    if accepted_files != generated_files:
        raise SampleFailure("accepted sample source differs from generated source")
    suite_content = accepted_files.get(GENERATED_TEST_SUITE_PATH)
    if suite_content is None:
        raise SampleFailure("accepted source omits its generated implementation tests")
    acceptance_arguments = tuple(
        invocation["arguments"]
        for invocation in execution["invocations"]
        if isinstance(invocation, dict)
    )
    try:
        generated_test_suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=acceptance_arguments,
        )
    except GeneratedTestSuiteError as exc:
        raise SampleFailure(
            "accepted generated implementation-test suite is invalid: " + exc.message
        ) from exc
    if (
        model.generation is None
        or model.generation.generated_test_suite_identity
        != generated_test_suite.content_identity
    ):
        raise SampleFailure(
            "accepted generated implementation tests differ from generation provenance"
        )
    generated_case_results_value = generated_test_step.output.get("case_results")
    if not isinstance(generated_case_results_value, list) or any(
        not isinstance(item, dict) for item in generated_case_results_value
    ):
        raise SampleFailure("generated-test lifecycle omitted exact case results")
    generated_case_results = [dict(item) for item in generated_case_results_value]
    if (
        generated_test_step.output.get("passed") is not True
        or generated_test_step.output.get("test_suite_identity")
        != generated_test_suite.content_identity
        or generated_test_step.output.get("total") != len(generated_test_suite.cases)
        or generated_test_step.output.get("categories")
        != sorted(generated_test_suite.categories)
        or [item.get("case_id") for item in generated_case_results]
        != list(generated_test_suite.case_ids)
        or any(
            item.get("verification_source") != "generated-implementation-test"
            for item in generated_case_results
        )
    ):
        raise SampleFailure(
            "generated-test lifecycle result does not match the admitted suite"
        )
    classification = _classification_from_output(classification_step.output)
    independent_results_value = acceptance_step.output.get("case_results")
    if not isinstance(independent_results_value, list) or any(
        not isinstance(item, dict) for item in independent_results_value
    ):
        raise SampleFailure("independent acceptance omitted exact case results")
    independent_results = [dict(item) for item in independent_results_value]
    runtime_probe_case_id = acceptance_step.output.get(
        "post_build_runtime_probe_case_id"
    )
    if (
        acceptance_step.output.get("passed") is not True
        or acceptance_step.output.get("runner_id")
        != _SampleIndependentAcceptanceRunner.runner_id
        or acceptance_step.output.get("test_suite_identity")
        != acceptance_suite_identity
        or acceptance_step.output.get("total") != len(invocations) + 1
        or acceptance_step.output.get("post_build_runtime_probe_count") != 1
        or not isinstance(runtime_probe_case_id, str)
        or any(
            item.get("verification_source")
            not in {"pinned-oracle", "post-build-runtime-oracle"}
            for item in independent_results
        )
    ):
        raise SampleFailure("independent acceptance result is incomplete or unbound")
    case_results: list[dict[str, object]] = [
        *generated_case_results,
        *independent_results,
    ]
    _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    primary_case = _primary_pinned_case(
        case_results,
        str(first_invocation["case_id"]),
    )
    loaded.require_unchanged(sample_root)
    generation_provenance = _coding_cli_generation_provenance(model)
    source_sbom = dependency_resolution_step.output.get("source_bom")
    resolved_sbom = dependency_resolution_step.output.get("resolved_bom")
    resolved_sbom_path_value = dependency_resolution_step.output.get(
        "resolved_bom_path"
    )
    if (
        not isinstance(source_sbom, Mapping)
        or not isinstance(resolved_sbom, Mapping)
        or not isinstance(resolved_sbom_path_value, str)
    ):
        raise SampleFailure("dependency-resolution lifecycle evidence is incomplete")
    if (
        model.generation is None
        or source_sbom != model.generation.source_sbom.to_dict()
    ):
        raise SampleFailure(
            "dependency-resolution lifecycle did not bind the generated source BOM"
        )
    resolved_sbom_path = Path(resolved_sbom_path_value)
    if (
        resolved_sbom_path.is_symlink()
        or not resolved_sbom_path.is_file()
        or not resolved_sbom_path.resolve().is_relative_to(runtime_root.resolve())
    ):
        raise SampleFailure("resolved SBOM lifecycle evidence path is unsafe")
    _require_sample_inputs_unchanged(input_closures.generation, label="generation")
    _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    accept_cached_generation = getattr(model.source_generator, "accept", None)
    if callable(accept_cached_generation) and model.generation is not None:
        accept_cached_generation(model.generation)
    return {
        "specification_identity": loaded.specification_set.identity.uri,
        "recipe_identity": recipe.identity,
        "flavor_specification_identities": list(flavor_specification_ids),
        "resolved_target": resolved_targets,
        "selected_flavor_identities": [
            item.identity.uri for item in run.locked_authority.selected_flavors
        ],
        "component_lock_identity": authority.lock.identity.uri,
        "component_lock_evidence": authority_evidence.relative_to(
            runtime_root
        ).as_posix(),
        "generation_skills": [item.to_dict() for item in recipe.resolved_skills],
        "model_selector": source_cache_model_selector(
            recipe.model_for(coding_cli), path="sample.correctness_evidence.model"
        ),
        "model_scope_identity": (
            None if recipe.model_scope is None else recipe.model_scope.identity.uri
        ),
        "requirement_id": execution["requirement_id"],
        "scenario_id": execution["scenario_id"],
        "component_authoring_identity": effective.authoring_identity.uri,
        "execution_variant": variant.variant_id,
        "implementation_language": variant.variant_id,
        "implementation_languages": list(variant.languages),
        "source_snapshot_identity": None,
        "generated_from_specification": True,
        "generation_input_closure": _sample_input_closure_report(
            input_closures.generation
        ),
        "verifier_input_closure": _sample_input_closure_report(input_closures.verifier),
        **generation_provenance,
        "source_sbom_identity": ContentIdentity.from_dict(
            source_sbom["bom_identity"]
        ).uri,
        "resolved_sbom_identity": ContentIdentity.from_dict(
            resolved_sbom["bom_identity"]
        ).uri,
        "source_sbom": dict(source_sbom),
        "resolved_sbom": dict(resolved_sbom),
        "dependency_resolution_identity": dependency_resolution_step.output[
            "resolution_identity"
        ],
        "resolved_sbom_path": resolved_sbom_path.relative_to(runtime_root).as_posix(),
        "generation_run_id": run.run_id,
        "model_stages": list(model.calls),
        "model_routes": [
            {
                "stage_id": stage.stage_id,
                "endpoint_id": route.selected_endpoint_id,
                "route_digest": route.digest,
            }
            for stage, route in zip(
                execution_plan.model_stages,
                execution_plan.route_decisions,
                strict=True,
            )
        ],
        "lifecycle_steps": list(lifecycle_steps),
        "accepted_tree_identity": run.provenance.accepted_tree_identity.uri,
        "workspace_reference": request.workspace_reference,
        "workspace_tree_digest": commit.output["tree_digest"],
        "accepted_source_intelligence": commit.output["source_intelligence"],
        "accepted_source_intelligence_status": (
            commit.output["source_intelligence_status"]["state"]
        ),
        "accepted_source_intelligence_reason_code": (
            commit.output["source_intelligence_status"].get("reason_code")
        ),
        "build_artifact_identity": build.output["artifact_digest"],
        "build_authorization_id": build_authorization.output["authorization_id"],
        "build_security_profile": build_authorization.output["profile"],
        "build_toolchain_identity": built_toolchain,
        "build_consumed_source_files": list(
            build.output.get(
                "consumed_source_files",
                build.output.get("consumed_backend_files", ()),
            )
        ),
        "build_checked_source_files": list(
            build.output.get(
                "checked_files",
                build.output.get("checked_frontend_files", ()),
            )
        ),
        "toolchain_constraints": [
            item.to_dict() for item in effective_toolchain_constraints
        ],
        "compiled_files": compiled_files,
        "compiled_entrypoint": compiled_relative,
        "compiled_entrypoints": list(required_compiled_files),
        "acceptance_contract_identity": execution_identity.uri,
        "generation_interface_identity": execution_document.identity,
        "acceptance_oracle_identity": oracle_reference.identity.uri,
        "acceptance_oracle_excluded_from_generation_request": True,
        "post_build_runtime_probe_case_id": runtime_probe_case_id,
        "post_build_runtime_probe_count": acceptance_step.output[
            "post_build_runtime_probe_count"
        ],
        "generated_test_runner_id": generated_test_step.output["runner_id"],
        "generated_test_execution_profile": generated_test_step.output[
            "execution_profile"
        ],
        "generated_test_case_count": len(generated_test_suite.cases),
        "generated_test_categories": sorted(generated_test_suite.categories),
        "independent_acceptance_runner_id": acceptance_step.output["runner_id"],
        "independent_acceptance_suite_identity": acceptance_step.output[
            "test_suite_identity"
        ],
        "independent_acceptance_execution_profile": acceptance_step.output[
            "execution_profile"
        ],
        "execution_case_count": len(case_results),
        "execution_cases": case_results,
        "execution_artifact_tree_identity": (baseline_execution.artifact_tree_digest),
        "execution_entrypoint_identity": baseline_execution.entrypoint_digest,
        "execution_auxiliary_artifacts": [
            artifact.identity_document()
            for artifact in baseline_execution.auxiliary_artifacts
        ],
        "execution_auxiliary_results": primary_case["auxiliary_results"],
        "execution_authorization_id": primary_case["execution_authorization_id"],
        "execution_authorization_classification_digest": (
            primary_case["execution_authorization_classification_digest"]
        ),
        "source_security_classification_digest": classification.digest,
        "source_security_profile": classification.profile.value,
        "execution_security_profile": primary_case["execution_security_profile"],
        "execution_mode": primary_case["execution_mode"],
        "execution_stdout_digest": primary_case["stdout_digest"],
        "execution_stderr_digest": primary_case["stderr_digest"],
        "result": primary_case["result"],
        "_operational_build_cache": (
            None if build_cache is None else build_cache.report()
        ),
        "passed": True,
    }


def _identity(label: str):
    return canonical_identity({"fixture": label})


def _toolchain_sbom_observations(
    root_ref: str, toolchains: Sequence[object]
) -> tuple[tuple[dict[str, object], ...], tuple[tuple[str, str], ...]]:
    """Project only exact toolchain facts already verified by the builders."""

    components: list[dict[str, object]] = []
    edges: list[tuple[str, str]] = []
    for toolchain in toolchains:
        identity = getattr(toolchain, "identity", None)
        version = getattr(toolchain, "version", None)
        if not isinstance(identity, str) or not isinstance(version, str):
            raise SampleFailure("sample toolchain lacks exact SBOM facts")
        try:
            parsed_identity = ContentIdentity.parse_uri(identity)
        except ValueError as exc:
            raise SampleFailure("sample toolchain SBOM identity is invalid") from exc
        name = type(toolchain).__name__
        scopes = ["build"]
        if name in {"PythonToolchain", "NodeToolchain"}:
            scopes.append("runtime")
        ref = f"urn:literate-ai:toolchain:{parsed_identity.digest}"
        components.append(
            {
                "type": "platform",
                "bom-ref": ref,
                "name": name,
                "version": version,
                "scope": "required",
                "properties": [
                    {
                        "name": "literate-ai:dependency-kind",
                        "value": "toolchain",
                    },
                    *(
                        {
                            "name": "literate-ai:dependency-scope",
                            "value": scope,
                        }
                        for scope in scopes
                    ),
                    {
                        "name": "literate-ai:toolchain-identity",
                        "value": identity,
                    },
                ],
            }
        )
        edges.append((root_ref, ref))
    return (
        tuple(sorted(components, key=lambda item: str(item["bom-ref"]))),
        tuple(sorted(edges)),
    )


def _reference(kind: str, label: str) -> ContentReference:
    return ContentReference(kind, f"fixture://{label}", _identity(label))


def _specification(label: str = "behavior") -> SpecificationSet:
    return SpecificationSet(
        "openspec",
        "1.0.0",
        (SpecificationArtifact("openspec/spec.md", _identity(f"{label}-spec")),),
        (
            SpecificationRequirement(
                label,
                label.replace("-", " ").title(),
                f"{label.replace('-', ' ').title()} remains deterministic.",
                (
                    SpecificationScenario(
                        f"{label}.works",
                        "Works",
                        (),
                        ("the sample is executed",),
                        ("its declared behavior is verified",),
                    ),
                ),
            ),
        ),
    )


def _definition(
    name: str,
    *,
    provides: tuple[Capability, ...] = (),
    requires: tuple[CapabilityRequirement, ...] = (),
    slots: tuple[FlavorSlot, ...] = (),
) -> ComponentDefinition:
    return ComponentDefinition(
        ComponentCoordinate("samples", name),
        "1.0.0",
        name.replace("-", " ").title(),
        "Offline neutral conformance Component.",
        ("sample",),
        True,
        provides,
        requires,
        "openspec",
        ("openspec/spec.md",),
        (_reference("authoring-input", f"{name}-authoring"),),
        _reference("workflow", f"{name}-workflow"),
        _reference("routing-policy", f"{name}-routing"),
        slots,
        (Entrypoint("run", "python", "source/main.py"),),
        (_reference("acceptance-contract", f"{name}-acceptance"),),
    )


def _requirement(requirement_id: str, capability: str) -> CapabilityRequirement:
    return CapabilityRequirement(
        requirement_id,
        capability,
        ">=1,<2",
        DependencyKind.RUNTIME,
    )


def _load_markdown_component(
    component_root: Path,
) -> tuple[ComponentDefinition, LoadedSpecification, ContentIdentity]:
    component_root = component_root.resolve(strict=True)
    """Project canonical component.md through the normal authoring/provider seam."""

    boundary = project_boundary(component_root, legacy=component_root.parent).resolve()
    manifest = component_root / "component.md"
    if manifest.is_symlink() or not manifest.is_file():
        raise SampleFailure("sample requires one canonical component.md")
    try:
        content = manifest.read_text(encoding="utf-8")
        authoring = parse_component_markdown(manifest, content, project_root=boundary)
        loaded = load_specification_provider(
            authoring.specification_provider,
            component_root,
            authoring.specification_roots,
            id_prefix=(f"{authoring.coordinate.namespace}.{authoring.coordinate.name}"),
        )
    except (
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        LookupError,
        *SPECIFICATION_PROVIDER_ERRORS,
    ) as exc:
        raise SampleFailure("sample component.md authority is invalid") from exc

    global_kinds = {
        "model-selection",
        "routing-policy",
        "specification-to-source-skill",
        "toolchain-constraint",
        "workflow",
    }

    def reference(selector) -> ContentReference:
        base = boundary if selector.kind in global_kinds else component_root
        path = base.joinpath(*Path(selector.uri).parts)
        if path.is_symlink():
            raise SampleFailure("sample Component selector cannot be a symbolic link")
        try:
            resolved = path.resolve(strict=True)
            data = resolved.read_bytes()
        except OSError as exc:
            raise SampleFailure("sample Component selector is unavailable") from exc
        if not resolved.is_relative_to(boundary) or not resolved.is_file():
            raise SampleFailure("sample Component selector escapes the project")
        identity = ContentIdentity(
            HashAlgorithm.SHA256, hashlib.sha256(data).hexdigest()
        )
        if selector.pin is not None and selector.pin != identity:
            raise SampleFailure("sample Component selector pin is stale")
        relative = os.path.relpath(resolved, component_root).replace(os.sep, "/")
        return ContentReference(selector.kind, relative, identity)

    if authoring.source_dependencies:
        raise SampleFailure(
            "sample harness does not accept unresolved repository source selectors"
        )
    definition = ComponentDefinition(
        authoring.coordinate,
        authoring.version,
        authoring.display_name,
        authoring.description,
        authoring.profiles,
        authoring.sample,
        tuple(
            Capability(
                item.name,
                item.version,
                None if item.interface is None else reference(item.interface).identity,
            )
            for item in authoring.provides
        ),
        authoring.requires,
        authoring.specification_provider,
        authoring.specification_roots,
        tuple(reference(item) for item in authoring.authoring_inputs),
        reference(authoring.workflow_definition),
        reference(authoring.routing_policy),
        authoring.flavor_slots,
        authoring.entrypoints,
        (),
        (),
    )
    if loaded.specification_set.provider_kind != authoring.specification_provider:
        raise SampleFailure("sample specification provider does not match component.md")
    loaded.require_unchanged(component_root)
    return definition, loaded, authoring.identity


def _load_sample(
    sample_root: Path,
) -> tuple[
    dict[str, Any],
    ComponentDefinition,
    LoadedSpecification,
    _SampleInputClosures,
]:
    harness_root = sample_harness_root(sample_root)
    sample_manifest = sample_harness_manifest(sample_root)
    component_manifest = sample_root / "component.md"
    metadata_content = sample_manifest.read_bytes()
    component_content = component_manifest.read_bytes()
    metadata = json.loads(metadata_content)
    if metadata.get("schema") != SAMPLE_SCHEMA:
        raise SampleFailure(f"unsupported sample schema: {sample_root.name}")
    definition, loaded, authoring_identity = _load_markdown_component(sample_root)
    if definition.coordinate.name != metadata["sample_id"] or not definition.sample:
        raise SampleFailure("sample metadata and canonical Component disagree")
    references = (
        *(
            (f"authoring-input:{index}", reference)
            for index, reference in enumerate(definition.authoring_inputs)
        ),
        *(
            (f"repository-source-dependency:{index}", reference)
            for index, reference in enumerate(definition.source_dependencies)
        ),
        ("workflow", definition.workflow_definition),
        ("routing-policy", definition.routing_policy),
    )
    sample_collection = project_boundary(
        sample_root, legacy=sample_root.parent
    ).resolve()
    if loaded.specification_set.provider_kind != definition.specification_provider:
        raise SampleFailure(
            "sample specification provider does not match its Component"
        )
    loaded.require_unchanged(sample_root)
    try:
        expected_authoring = ContentIdentity.parse_uri(
            metadata["component_authoring_identity"]
        )
        expected_specification = ContentIdentity.parse_uri(
            metadata["specification_identity"]
        )
        expected_execution = ContentIdentity.parse_uri(
            metadata["execution_interface_identity"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SampleFailure("sample metadata lacks exact authoring identities") from exc
    if expected_authoring != authoring_identity:
        raise SampleFailure("sample metadata has a stale component.md identity")
    if expected_specification != loaded.specification_set.identity:
        raise SampleFailure("sample metadata has a stale specification identity")
    execution, execution_document = _execution_contract(sample_root, definition, loaded)
    del execution
    if expected_execution != ContentIdentity.parse_uri(execution_document.identity):
        raise SampleFailure("sample metadata has a stale execution interface identity")
    generation = PinnedInputClosure()
    prefix = f"component:{definition.coordinate.uri}"
    try:
        project = discover_project(sample_root)
        if project is not None:
            project_manifest = project.root / PROJECT_FILENAME
            project_content = project_manifest.read_bytes()
            generation.pin(
                project_manifest,
                boundary=project.root,
                label=f"{prefix}:project-manifest",
                expected_content=project_content,
            )
        generation.pin(
            component_manifest,
            boundary=sample_collection,
            label=f"{prefix}:manifest",
            expected_content=component_content,
        )
        for relative, content in loaded.contents:
            generation.pin(
                sample_root.joinpath(*Path(relative).parts),
                boundary=sample_collection,
                label=f"{prefix}:specification:{relative}",
                expected_content=content,
            )
        for role, reference in references:
            content = _pinned_project_content(
                sample_root,
                reference,
                boundary=sample_collection,
                source=f"{prefix}:{role}",
            )
            generation.pin(
                sample_root / reference.uri,
                boundary=sample_collection,
                label=f"{prefix}:{role}:{reference.kind}",
                expected_content=content,
                expected_identity=reference.identity,
            )
        execution_path = harness_root / "acceptance" / "execution.json"
        generation.pin(
            execution_path,
            boundary=harness_root,
            label=f"{prefix}:harness-execution-interface",
            expected_identity=(
                definition.acceptance_contracts[0].identity
                if len(definition.acceptance_contracts) == 1
                else None
            ),
        )
        verifier = generation.fork()
        verifier.pin(
            sample_manifest,
            boundary=harness_root,
            label=f"verifier:{definition.coordinate.uri}:sample-manifest",
            expected_content=metadata_content,
        )
        oracle_reference = _sample_oracle_reference(metadata)
        oracle_content = _pinned_project_content(
            harness_root,
            oracle_reference,
            boundary=harness_root,
            source="acceptance oracle",
        )
        verifier.pin(
            sample_harness_oracle(sample_root, oracle_reference),
            boundary=harness_root,
            label=f"verifier:{definition.coordinate.uri}:acceptance-oracle",
            expected_content=oracle_content,
            expected_identity=oracle_reference.identity,
        )
        if "portfolio_manifest" in metadata:
            from tests.conformance.support.durable_split_service import (
                DurableSplitPortfolioError,
                load_durable_split_portfolio,
            )

            try:
                portfolio = load_durable_split_portfolio(harness_root, metadata)
            except DurableSplitPortfolioError as exc:
                raise SampleFailure("sample portfolio authority is invalid") from exc
            verifier.pin(
                portfolio.path,
                boundary=harness_root,
                label=f"verifier:{definition.coordinate.uri}:portfolio-manifest",
                expected_identity=portfolio.reference.identity,
            )
            verifier.pin(
                portfolio.upstream_path,
                boundary=harness_root,
                label=f"verifier:{definition.coordinate.uri}:upstream-fixture",
                expected_identity=portfolio.upstream_reference.identity,
            )
            verifier.pin(
                portfolio.browser_path,
                boundary=harness_root,
                label=f"verifier:{definition.coordinate.uri}:browser-acceptance",
                expected_identity=portfolio.browser_reference.identity,
            )
            for component_harness in portfolio.component_harnesses:
                verifier.pin(
                    component_harness.execution_path,
                    boundary=harness_root.parent,
                    label=(
                        f"verifier:{definition.coordinate.uri}:"
                        f"{component_harness.role}-execution-interface"
                    ),
                    expected_identity=component_harness.execution_reference.identity,
                )
                verifier.pin(
                    component_harness.oracle_path,
                    boundary=harness_root.parent,
                    label=(
                        f"verifier:{definition.coordinate.uri}:"
                        f"{component_harness.role}-acceptance-oracle"
                    ),
                    expected_identity=component_harness.oracle_reference.identity,
                )
    except (KeyError, TypeError, ValueError, OSError, PinnedInputClosureError) as exc:
        raise SampleFailure("sample input closure is invalid") from exc
    return metadata, definition, loaded, _SampleInputClosures(generation, verifier)


def _hello(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    workflow = WorkflowDefinition(
        "hello-lifecycle",
        "1.0.0",
        (
            StageDefinition("resolve", "identity"),
            StageDefinition("generate", "identity", ("resolve",)),
            StageDefinition("validate", "validate", ("generate",), 2),
        ),
    )

    class SimulatedProcessTermination(BaseException):
        """Leave an in-flight attempt exactly as a terminated process would."""

    store = LifecycleEventStoreAdapter(
        AppendOnlyEventStore(scratch / "workflow-events")
    )
    inputs = {"message": "hello"}

    def identity(value: Mapping[str, object]) -> Mapping[str, object]:
        return {"digest": canonical_identity(value).uri}

    def terminate(_: Mapping[str, object]) -> Mapping[str, object]:
        raise SimulatedProcessTermination

    interrupted = WorkflowEngine(
        handlers={
            "identity": identity,
            "validate": terminate,
        },
        event_sink=store,
    )
    try:
        interrupted.execute(workflow, inputs=inputs)
    except SimulatedProcessTermination:
        pass
    else:
        raise SampleFailure("hello workflow did not simulate a process restart")

    resumed = WorkflowEngine(
        handlers={"identity": identity, "validate": identity}, event_sink=store
    ).execute(workflow, inputs=inputs)
    reused = [
        item.stage_id for item in resumed.events if item.event_type == "stage-reused"
    ]
    if resumed.status is not WorkflowStatus.COMPLETE:
        raise SampleFailure("hello workflow did not resume deterministically")
    if reused != ["resolve", "generate"]:
        raise SampleFailure("hello workflow did not reuse its completed stages")
    if resumed.stage_results["validate"].attempt != 2:
        raise SampleFailure("hello workflow reset its durable attempt counter")
    return ("workflow-completes", "restart-reuses-exact-stages")


def _generated_library(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    cache_root = scratch / "cache"
    cas = FileSystemCAS(cache_root)
    if tuple(cas.iter_refs()):
        raise SampleFailure("generated-library cache was not empty")
    store = BundleStore(cas)
    library_source = cas.put_bytes(
        b"def add(a, b): return a + b\n", media_type="text/x-python"
    )
    library_revision = _identity("generated-library-revision").uri
    library = BundleManifest(
        BundleKind.SOURCE,
        library_revision,
        library_revision,
        None,
        {"source": library_source},
        (),
        (),
        component_ref=ComponentRevisionRef(
            ComponentCoordinate("samples", "library"),
            "1.0.0",
            ContentIdentity.parse_uri(library_revision),
        ),
    )
    library_ref = store.put(library)
    missing_required_rejected = False
    try:
        BundleDependency(
            library_revision,
            "runtime",
            True,
            None,
            ComponentRevisionRef(
                ComponentCoordinate("samples", "library"),
                "1.0.0",
                ContentIdentity.parse_uri(library_revision),
            ),
        )
    except ValueError:
        missing_required_rejected = True
    consumer_source = cas.put_bytes(
        b"from generated import add\n", media_type="text/x-python"
    )
    consumer_revision = _identity("library-consumer-revision").uri
    consumer = BundleManifest(
        BundleKind.SOURCE,
        consumer_revision,
        consumer_revision,
        None,
        {"source": consumer_source},
        (
            BundleDependency(
                library_revision,
                "runtime",
                True,
                library_ref,
                ComponentRevisionRef(
                    ComponentCoordinate("samples", "library"),
                    "1.0.0",
                    ContentIdentity.parse_uri(library_revision),
                ),
            ),
        ),
        (),
        component_ref=ComponentRevisionRef(
            ComponentCoordinate("samples", "consumer"),
            "1.0.0",
            ContentIdentity.parse_uri(consumer_revision),
        ),
    )
    consumer_ref = store.put(consumer)
    closure = ArtifactResolver(store).closure(consumer_ref)
    restarted = BundleStore(FileSystemCAS(cache_root))
    if not missing_required_rejected:
        raise SampleFailure("required dependency accepted no exact bundle")
    if closure != (library_ref, consumer_ref):
        raise SampleFailure("generated library closure is not dependency-first")
    if ArtifactResolver(restarted).closure(consumer_ref) != closure:
        raise SampleFailure("generated library cache did not survive restart")
    return (
        "empty-cache-faults-exact-library",
        "consumer-resolves-required-source-bundle",
        "cache-restart-preserves-closure",
    )


def _critical_path_scheduler(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    harness = sample_harness_root(root) / "acceptance"
    execution = json.loads((harness / "execution.json").read_text(encoding="utf-8"))
    oracle = json.loads((harness / "oracle.json").read_text(encoding="utf-8"))
    invocations = execution["invocations"]
    expected = oracle["oracle_results"]
    if len(invocations) != len(expected):
        raise SampleFailure("scheduler invocation and oracle catalogs differ")
    for invocation, oracle_case in zip(invocations, expected, strict=True):
        request = invocation["arguments"][0]
        tasks = request["tasks"]
        if _critical_path_result(tasks) != oracle_case["expected_result"]:
            raise SampleFailure("scheduler oracle does not implement its CPM contract")

    primary_tasks = invocations[0]["arguments"][0]["tasks"]
    positions = {task["id"]: index for index, task in enumerate(primary_tasks)}
    if not any(
        positions[dependency] > positions[task["id"]]
        for task in primary_tasks
        for dependency in task["depends_on"]
    ):
        raise SampleFailure("scheduler primary case has no forward reference")
    general_path = expected[1]["expected_result"]["critical_path"]
    if "api" not in general_path or "data" in general_path:
        raise SampleFailure("scheduler tie-breaking oracle is not stable")
    return (
        "accepts-shuffled-forward-reference-dag",
        "computes-exact-forward-and-backward-passes",
        "orders-ready-tasks-by-ascii-id",
        "selects-stable-critical-path-on-ties",
    )


def _containerized_log_tally(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    _fixed_application_case(root, _log_tally_result)
    return (
        "workflow-completes",
        "report-shape-matches-boundary-contract",
        "malformed-lines-isolated-from-totals",
        "top-paths-rank-deterministically",
        "container-image-inputs-traceable-to-flavor",
    )


def _fixed_application_case(
    root: Path, calculator: Callable[[dict[str, object]], object]
) -> None:
    harness = sample_harness_root(root) / "acceptance"
    execution = json.loads((harness / "execution.json").read_text(encoding="utf-8"))
    oracle = json.loads((harness / "oracle.json").read_text(encoding="utf-8"))
    for invocation, expected in zip(
        execution["invocations"], oracle["oracle_results"], strict=True
    ):
        arguments = invocation["arguments"]
        if (
            len(arguments) != 1
            or not isinstance(arguments[0], dict)
            or calculator(arguments[0]) != expected["expected_result"]
        ):
            raise SampleFailure(f"{root.name} verifier fixture is inconsistent")


def _cuda_foundation(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch

    def calculate(argument: dict[str, object]) -> object:
        if root.name == "cuda-vector-transform-cpp":
            values = [int(item) for item in argument["values"]]
            multiplier = int(argument["multiplier"])
            bias = int(argument["bias"])
            transformed = [item * multiplier + bias for item in values]
            return {
                "backend": "cuda",
                "device_executed": True,
                "count": len(transformed),
                "values": transformed,
                "checksum": sum(transformed),
            }
        left = argument["left"]
        right = argument["right"]
        values = [
            [
                sum(
                    int(left[row][inner]) * int(right[inner][column])
                    for inner in range(len(right))
                )
                for column in range(len(right[0]))
            ]
            for row in range(len(left))
        ]
        return {
            "backend": "cupy-cuda",
            "device_executed": True,
            "rows": len(values),
            "columns": len(values[0]),
            "values": values,
            "checksum": sum(sum(row) for row in values),
        }

    _fixed_application_case(root, calculate)
    return (
        "cuda-contract-oracle-is-exact",
        "device-execution-is-required",
        "cpu-fallback-is-rejected",
    )


def _dependency_planner(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    _fixed_application_case(
        root,
        lambda argument: _dependency_plan_result(argument["tasks"]),
    )
    return (
        "task-graph-validates",
        "topological-order-is-deterministic",
        "critical-path-respects-parallelism",
    )


def _javascript_ledger_workbench(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    _fixed_application_case(root, _ledger_result)
    return (
        "ledger-reconciles",
        "budget-statuses-are-deterministic",
        "javascript-toolchain-is-exact",
    )


def _full_stack_rust_js(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    _fixed_application_case(root, _release_dashboard_result)
    _metadata, definition, loaded, closures = _load_sample(root)
    execution, _document = _execution_contract(root, definition, loaded)
    _require_sample_inputs_unchanged(closures.generation, label="generation")
    language_slots = {
        slot.slot_id
        for slot in definition.flavor_slots
        if slot.axis is FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM
    }
    raw_roles = execution["target"][FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value]
    roles = {
        slot_id: _language_target(str(value)) for slot_id, value in raw_roles.items()
    }
    if language_slots != {"backend-language", "frontend-language"} or roles != {
        "backend-language": "rust",
        "frontend-language": "javascript",
    }:
        raise SampleFailure("full-stack language roles are not explicitly pinned")
    return (
        "both-language-flavors-are-pinned",
        "rust-backend-performs-risk-analysis",
        "trusted-runner-invokes-backend-then-frontend",
        "frontend-receives-canonical-backend-json",
        "full-stack-result-is-deterministic",
    )


def _project_component_descriptor_catalog(
    root: Path, base: ComponentRevision
) -> tuple[ComponentDescriptor, ...]:
    """Load the project Component catalog without depending on CLI internals."""

    project = discover_project(root)
    if project is None:
        raise SampleFailure("service stack requires its project Component catalog")
    descriptors: list[ComponentDescriptor] = []
    for manifest in sorted(
        path
        for catalog_root in project.roots("component")
        for path in catalog_root.rglob("component.md")
        if path.is_file() and not path.is_symlink()
    ):
        if manifest.parent.resolve() == root.resolve():
            revision = base
        else:
            item_definition, item_loaded, _identity = _load_markdown_component(
                manifest.parent
            )
            revision = ComponentRevision(
                item_definition,
                item_loaded.specification_set,
                None,
                (),
                (),
            )
        descriptors.append(ComponentDescriptor(revision.identity, revision.definition))
    return tuple(descriptors)


def _project_component_root(sample_root: Path, component_name: str) -> Path:
    project = discover_project(sample_root)
    if project is None:
        raise SampleFailure("sample composition requires a declared Component catalog")
    matches = tuple(
        manifest.parent
        for catalog_root in project.roots("component")
        for manifest in catalog_root.rglob("component.md")
        if manifest.is_file()
        and not manifest.is_symlink()
        and manifest.parent.name == component_name
    )
    if len(matches) != 1:
        raise SampleFailure(
            f"sample composition requires one Component named {component_name!r}"
        )
    return matches[0]


def _service_stack(
    root: Path,
    scratch: Path,
    executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    _metadata, definition, loaded, closures = _load_sample(root)
    base = ComponentRevision(definition, loaded.specification_set, None, (), ())
    descriptor_catalog = _project_component_descriptor_catalog(root, base)
    composition = ComponentComposer(DescriptorRegistry(descriptor_catalog)).compose(
        base.ref
    )
    if len(composition.revisions) != 3 or len(composition.edges) != 4:
        raise SampleFailure(
            "service stack did not resolve its exact transitive closure"
        )
    coordinates = {item.coordinate.uri for item in composition.revision_refs}
    if (
        coordinates
        != {
            "component://samples/service-stack",
            "component://literate-ai/invoice-service",
            "component://literate-ai/money-calculation",
        }
        or {item.requirement.capability for item in composition.edges}
        != {
            "literate-ai.invoice-service",
            "literate-ai.money-calculation",
        }
        or {item.requirement.dependency_kind for item in composition.edges}
        != {
            DependencyKind.GENERATION,
            DependencyKind.RUNTIME,
        }
    ):
        raise SampleFailure(
            "service stack composition differs from its checked-in graph"
        )
    library = next(
        item
        for item in descriptor_catalog
        if item.coordinate == "component://literate-ai/money-calculation"
    )
    application = next(
        item
        for item in descriptor_catalog
        if item.coordinate == "component://samples/service-stack"
    )
    ambiguous_library = ComponentDescriptor(
        _identity("stack-library-two"),
        _definition(
            "stack-library-two",
            provides=(Capability("literate-ai.money-calculation", "1.1.0"),),
        ),
    )
    ambiguity_rejected = False
    try:
        ComponentComposer(
            DescriptorRegistry((*descriptor_catalog, ambiguous_library))
        ).compose(application.revision_identity)
    except CompositionError as exc:
        ambiguity_rejected = exc.code == "ambiguous-provider"
    if not ambiguity_rejected:
        raise SampleFailure("ambiguous service-stack provider was not rejected")
    if not any(
        item.revision_identity == library.revision_identity
        for item in descriptor_catalog
    ):
        raise SampleFailure("money calculation descriptor disappeared from the catalog")
    del scratch, closures
    standard = next(
        (
            item
            for item in executions
            if item.get("schema") == STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA
            and item.get("variant_id") == "python"
        ),
        None,
    )
    if (
        standard is None
        or standard.get("node_count") != 3
        or standard.get("sample_id") != "service-stack"
        or not isinstance(standard.get("project_build_plan_identity"), str)
        or not isinstance(standard.get("root_integration_evidence_identity"), str)
    ):
        raise SampleFailure(
            "service stack did not traverse three independent Standard lifecycle nodes"
        )
    return ("service-stack-resolves", "transitive-library-is-exact")


def _accepted_workspace_from_execution(
    scratch: Path, execution: Mapping[str, object]
) -> Path:
    variant = execution.get("execution_variant")
    attempt = execution.get("generation_candidate_attempt_count")
    reference = execution.get("workspace_reference")
    if (
        not isinstance(variant, str)
        or not variant
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
        or not isinstance(reference, str)
        or not reference
    ):
        raise SampleFailure("accepted workspace execution evidence is incomplete")
    workspace_root = (
        scratch
        / "candidate-attempts"
        / variant
        / f"{attempt:02d}"
        / "e2e"
        / variant
        / "workspace"
    )
    generated = WorkspaceTreeStore(workspace_root).resolve(reference)
    if generated is None:
        raise SampleFailure("accepted workspace execution evidence is unavailable")
    return generated


def _executions_by_language(
    executions: tuple[Mapping[str, object], ...],
) -> dict[str, Mapping[str, object]]:
    """Index the exact selected execution matrix without assuming its size."""

    by_language: dict[str, Mapping[str, object]] = {}
    for execution in executions:
        language = execution.get("implementation_language")
        if not isinstance(language, str) or not language or language in by_language:
            raise SampleFailure("execution variants do not identify unique languages")
        by_language[language] = execution
    if not by_language:
        raise SampleFailure("sample execution variants are incomplete")
    return by_language


def _multi_repository(
    root: Path,
    scratch: Path,
    executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del root
    snapshotter = SourceSnapshotter()
    by_language = _executions_by_language(executions)
    for execution in by_language.values():
        generated = _accepted_workspace_from_execution(scratch, execution)
        source = generated / "source"
        api = snapshotter.snapshot_local(source / "api")
        worker = snapshotter.snapshot_local(source / "worker")
        first = snapshotter.aggregate(
            (("api", "services/api", api), ("worker", "services/worker", worker))
        )
        second = snapshotter.aggregate(
            (("worker", "services/worker", worker), ("api", "services/api", api))
        )
        if first.snapshot.identity != second.snapshot.identity:
            raise SampleFailure("aggregate source identity depends on repository order")
        if len(first.snapshot.aggregate_members) != 2:
            raise SampleFailure("aggregate source lost a repository member")
        duplicate_rejected = False
        try:
            snapshotter.aggregate(
                (("api", "services/api", api), ("api", "services/worker", worker))
            )
        except ValueError:
            duplicate_rejected = True
        if not duplicate_rejected:
            raise SampleFailure("duplicate aggregate member was not rejected")
    return ("aggregate-identity-is-order-independent", "member-identities-are-retained")


def _model_routing(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    preferred = ModelEndpoint(
        "preferred",
        "offline",
        "large",
        "http://127.0.0.1:9001",
        Locality.LOCAL,
        ("code",),
        32768,
        available=False,
    )
    fallback = ModelEndpoint(
        "fallback",
        "offline",
        "small",
        "unix:///tmp/literate-ai-model.sock",
        Locality.LOCAL,
        ("code",),
        16384,
    )
    router = ModelRouter(
        endpoints=(preferred, fallback),
        groups=(ModelGroup("offline-code", "1.0.0", ("preferred", "fallback")),),
    )
    policy = StageModelPolicy(
        "offline-fallback",
        "generation",
        "offline-code",
        required_capabilities=("code",),
        required_locality=Locality.LOCAL,
        data_egress=DataEgress.NONE,
    )
    first = router.select(policy)
    second = router.select(policy)
    if first != second or first.selected_endpoint_id != "fallback":
        raise SampleFailure("model routing fallback is not deterministic")
    if not first.fallback_used or first.data_egress is not DataEgress.NONE:
        raise SampleFailure("model route lost fallback or egress provenance")
    fallback_rejected = False
    try:
        router.select(
            StageModelPolicy(
                "offline-no-fallback",
                "generation",
                "offline-code",
                required_capabilities=("code",),
                required_locality=Locality.LOCAL,
                fallback_allowed=False,
                data_egress=DataEgress.NONE,
            )
        )
    except RoutingError as exc:
        fallback_rejected = exc.code == "models.fallback_disallowed"
    if not fallback_rejected:
        raise SampleFailure("disallowed fallback was selected")
    return ("offline-fallback-is-deterministic", "request-egress-remains-none")


def _publication(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    source_cas = FileSystemCAS(scratch / "source-cache")
    source = source_cas.put_bytes(b"portable source\n", media_type="text/plain")
    provenance = source_cas.put_bytes(
        b'{"generator":"fixture"}\n', media_type="application/json"
    )
    component_ref = ComponentRevisionRef(
        ComponentCoordinate("samples", "publication-sample"),
        "1.0.0",
        _identity("publication-sample-revision"),
    )
    target = FilesystemPublicationTarget("offline-publication", scratch / "published")
    publication_policy = PublicationPolicy(
        _identity("publication-sample-policy").uri,
        (target.target_id,),
    )
    classification_digest = _identity("publication-sample-classification").uri
    component_lock_identity = _identity("publication-sample-component-lock")
    publication_request = PublicationRequest.create(
        component_ref=component_ref,
        component_lock_identity=component_lock_identity,
        effective_revision_digest=component_ref.revision_identity.uri,
        source_bundle=source,
        roots={"source": source, "provenance": provenance},
        blobs=(source, provenance),
        provenance=(provenance,),
        security_classification_digest=classification_digest,
        security_profile=SecurityProfile.CONSTRAINED,
        target_id=target.target_id,
        target_identity_digest=target.identity,
        policy_digest=publication_policy.policy_digest,
        actor="sample-publication-publisher",
    )
    publication_authorization = publication_policy.authorize(
        publication_request,
        reason="publish the exact sample source and provenance",
        issued_at=FIXED_TIME,
        expires_at=FIXED_TIME + timedelta(minutes=5),
    )
    manifest = PublicationManifest.create(
        request=publication_request,
        authorization=publication_authorization,
        created_at=FIXED_TIME,
    )
    publisher = PublicationService(
        source_cas,
        AppendOnlyEventStore(scratch / "publish-events"),
        publication_policy,
        clock=lambda: FIXED_TIME + timedelta(seconds=1),
    )
    published = publisher.publish(manifest, target)
    repeated = publisher.publish(manifest, target)
    destination = FileSystemCAS(scratch / "destination-cache")
    import_policy = ImportPolicy(
        _identity("publication-sample-import-policy").uri,
        (target.target_id,),
        (classification_digest,),
        (publication_policy.policy_digest,),
    )
    importer = PublicationService(
        destination,
        AppendOnlyEventStore(scratch / "import-events"),
        import_policy=import_policy,
        clock=lambda: FIXED_TIME + timedelta(seconds=1),
    )
    import_request = ImportRequest.create(
        manifest=manifest,
        publication_manifest=published.publication_manifest,
        destination_identity_digest=importer.import_destination_identity,
        policy_digest=import_policy.policy_digest,
        actor="sample-publication-importer",
    )
    import_authorization = import_policy.authorize(
        import_request,
        reason="import the exact trusted sample publication",
        issued_at=FIXED_TIME,
        expires_at=FIXED_TIME + timedelta(minutes=5),
    )
    imported = importer.import_release(
        published.publication_manifest,
        target,
        expected_component=component_ref,
        import_authorization=import_authorization,
    )
    if published != repeated or imported.direction != "import":
        raise SampleFailure("publication was not idempotent and importable")
    if destination.get_bytes(source) != b"portable source\n":
        raise SampleFailure("import changed source bytes")
    overlap_rejected = False
    overlapping = FilesystemPublicationTarget(
        "overlapping-publication", source_cas.root / "published"
    )
    overlap_policy = PublicationPolicy(
        _identity("overlapping-publication-policy").uri,
        (overlapping.target_id,),
    )
    overlap_request = PublicationRequest.create(
        component_ref=component_ref,
        component_lock_identity=component_lock_identity,
        effective_revision_digest=component_ref.revision_identity.uri,
        source_bundle=source,
        roots={"source": source, "provenance": provenance},
        blobs=(source, provenance),
        provenance=(provenance,),
        security_classification_digest=classification_digest,
        security_profile=SecurityProfile.CONSTRAINED,
        target_id=overlapping.target_id,
        target_identity_digest=overlapping.identity,
        policy_digest=overlap_policy.policy_digest,
        actor="sample-publication-publisher",
    )
    overlap_manifest = PublicationManifest.create(
        request=overlap_request,
        authorization=overlap_policy.authorize(
            overlap_request,
            reason="prove overlapping storage is rejected",
            issued_at=FIXED_TIME,
            expires_at=FIXED_TIME + timedelta(minutes=5),
        ),
        created_at=FIXED_TIME,
    )
    try:
        PublicationService(
            source_cas,
            AppendOnlyEventStore(scratch / "overlap-events"),
            overlap_policy,
            clock=lambda: FIXED_TIME + timedelta(seconds=1),
        ).publish(overlap_manifest, overlapping)
    except (PublicationError, StorageSafetyError):
        overlap_rejected = True
    if not overlap_rejected:
        raise SampleFailure("overlapping publication target was accepted")
    return ("publication-is-idempotent", "import-verifies-identical-bytes")


def _security(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    digest = _identity("security-source").uri
    revision = _identity("security-revision").uri
    policy = SecurityPolicy(_identity("security-policy").uri)
    classification = policy.classify(
        effective_revision_digest=revision,
        attestations=(OriginAttestation(digest, "fixture", "root", "sig", True),),
        findings=(
            SecurityFinding(
                "critical-fixture",
                digest,
                "unsafe-operation",
                FindingSeverity.CRITICAL,
                "fixture-scanner@1",
                "critical fixture finding",
            ),
        ),
    )
    privileges = (
        "compiler",
        "devices",
        "host-filesystem",
        "network",
        "package-manager",
        "processes",
        "sandbox-escape",
        "secrets",
    )
    request = BuildRequest(
        revision,
        digest,
        "fixture-builder@1",
        _identity("toolchain").uri,
        "isolated",
        privileges,
        ("build/**",),
    )
    blocked = False
    try:
        policy.authorize_build(
            classification,
            request,
            actor="sample-reviewer",
            reason="negative gate",
            issued_at=FIXED_TIME,
            expires_at=FIXED_TIME + timedelta(minutes=5),
        )
    except AuthorizationError as exc:
        blocked = exc.code == "security.build_blocked"
    authorization = policy.authorize_build(
        classification,
        request,
        actor="sample-reviewer",
        reason="explicit maximum privilege conformance",
        issued_at=FIXED_TIME,
        expires_at=FIXED_TIME + timedelta(minutes=5),
        yolo_acknowledged=True,
    )
    partial_request = BuildRequest(
        revision,
        digest,
        "fixture-builder@1",
        _identity("toolchain").uri,
        "isolated",
        privileges[:-1],
        ("build/**",),
    )
    partial_authorization = policy.authorize_build(
        classification,
        partial_request,
        actor="sample-reviewer",
        reason="explicit risk acceptance still grants only requested privileges",
        issued_at=FIXED_TIME,
        expires_at=FIXED_TIME + timedelta(minutes=5),
        yolo_acknowledged=True,
    )
    if not blocked or authorization.profile is not SecurityProfile.YOLO:
        raise SampleFailure("blocked source bypassed explicit yolo acknowledgement")
    if partial_authorization.privileges != tuple(sorted(privileges[:-1])):
        raise SampleFailure("yolo authorization escalated beyond requested privileges")
    if (
        authorization.warning != policy.yolo_warning
        or "MAXIMUM PRIVILEGE" not in authorization.warning
    ):
        raise SampleFailure("yolo warning is not persistent and explicit")
    return (
        "critical-source-is-blocked",
        "yolo-requires-explicit-acknowledgement",
        "yolo-warning-is-persisted",
    )


def _empty_cache_restart(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    cache_root = scratch / "cas"
    first = FileSystemCAS(cache_root)
    if tuple(first.iter_refs()):
        raise SampleFailure("cache did not start empty")
    reference = first.put_bytes(b"faulted into cache\n")
    restarted = FileSystemCAS(cache_root)
    if not restarted.contains(reference):
        raise SampleFailure("restarted cache lost exact object")
    workspace_root = scratch / "workspace"
    workspace = WorkspaceTreeStore(workspace_root)
    prepared = workspace.prepare({"output.txt": restarted.get_bytes(reference)})
    committed = workspace.commit(prepared, "samples/restart")
    after_restart = WorkspaceTreeStore(workspace_root).resolve("samples/restart")
    if after_restart != committed:
        raise SampleFailure("workspace reference did not survive restart")
    return (
        "cache-starts-empty",
        "object-faults-in-by-identity",
        "cache-and-workspace-survive-restart",
    )


def _flavor(
    name: str,
    axis: FlavorAxis,
    value: str,
    *,
    conflicts: tuple[str, ...] = (),
) -> FlavorDescriptor:
    definition = FlavorDefinition(
        FlavorCoordinate("samples", name),
        "1.0.0",
        name.title(),
        axis,
        (),
        ("sample.app",),
        (),
        (),
        (_reference("specification", f"flavor-{name}-spec"),),
        (),
        (),
        conflicts,
        (),
        (),
        (),
        (value,),
    )
    revision = FlavorRevision(definition, None, ())
    return FlavorDescriptor(revision.identity, definition, value)


def _flavor_matrix(
    _root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    slots = (
        FlavorSlot("os", FlavorAxis.PLATFORM_OS, FlavorCardinality.EXACTLY_ONE, "os"),
        FlavorSlot(
            "accelerator",
            FlavorAxis.ACCELERATOR,
            FlavorCardinality.EXACTLY_ONE,
            "accelerator",
        ),
        FlavorSlot(
            "language",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            FlavorCardinality.EXACTLY_ONE,
            "language",
        ),
    )
    base = ComponentRevision(
        _definition(
            "flavor-matrix-base",
            provides=(Capability("sample.app", "1.0.0"),),
            slots=slots,
        ),
        _specification("flavor-base"),
        None,
        (),
        (),
    )
    linux = _flavor("linux", FlavorAxis.PLATFORM_OS, "linux")
    windows = _flavor("windows", FlavorAxis.PLATFORM_OS, "windows")
    cpu = _flavor("cpu", FlavorAxis.ACCELERATOR, "cpu")
    cuda = _flavor(
        "cuda",
        FlavorAxis.ACCELERATOR,
        "cuda",
        conflicts=(windows.coordinate,),
    )
    python = _flavor("python", FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "python")
    rust = _flavor("rust", FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, "rust")
    registry = DescriptorRegistry(flavors=(linux, windows, cpu, cuda, python, rust))
    resolver = FlavorResolver(registry)

    def profile(profile_id: str, os_value: str, accelerator: str, language: str):
        return TargetProfile(
            profile_id,
            "1.0.0",
            "explicit",
            _identity(f"target-{profile_id}"),
            (
                TargetConstraint(FlavorAxis.PLATFORM_OS, os_value),
                TargetConstraint(FlavorAxis.ACCELERATOR, accelerator),
                TargetConstraint(
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM, language
                ),
            ),
        )

    linux_cpu_python = resolver.resolve(
        base, profile("linux-cpu-python", "linux", "cpu", "python")
    )
    windows_cpu_rust = resolver.resolve(
        base, profile("windows-cpu-rust", "windows", "cpu", "rust")
    )
    linux_cuda_rust = resolver.resolve(
        base, profile("linux-cuda-rust", "linux", "cuda", "rust")
    )
    identities = {
        item.effective_revision.identity.uri
        for item in (linux_cpu_python, windows_cpu_rust, linux_cuda_rust)
    }
    if len(identities) != 3:
        raise SampleFailure("Flavor effective revisions were not cache-isolated")
    conflict = False
    try:
        resolver.resolve(
            base, profile("windows-cuda-python", "windows", "cuda", "python")
        )
    except CompositionError as exc:
        conflict = exc.code == "flavor-conflict"
    if not conflict:
        raise SampleFailure("Windows/CUDA Flavor conflict was not explicit")
    ambiguity = False
    linux_two = _flavor("linux-two", FlavorAxis.PLATFORM_OS, "linux")
    try:
        FlavorResolver(
            DescriptorRegistry(
                flavors=(linux, linux_two, windows, cpu, cuda, python, rust)
            )
        ).resolve(base, profile("linux-cpu-rust", "linux", "cpu", "rust"))
    except CompositionError as exc:
        ambiguity = exc.code == "ambiguous-provider"
    if not ambiguity:
        raise SampleFailure("ambiguous Flavor candidates were not rejected")
    cardinality = False
    try:
        resolver.resolve(base, profile("linux-metal-rust", "linux", "metal", "rust"))
    except CompositionError as exc:
        cardinality = exc.code in {
            "flavor-cardinality",
            "flavor-target-unsatisfied",
        }
    if not cardinality:
        raise SampleFailure("missing exactly-one Flavor was not rejected")

    cas = FileSystemCAS(scratch / "flavor-source-cache")
    flavor_blob = cas.put_bytes(canonical_json_bytes(linux.to_dict()))
    flavor_provenance = cas.put_bytes(
        canonical_json_bytes(
            {
                "flavor_revision": linux.revision_identity.uri,
                "effective_revision": linux_cpu_python.effective_revision.identity.uri,
            }
        ),
        media_type="application/json",
    )
    component_ref = ComponentRevisionRef(
        ComponentCoordinate("samples", "linux-flavor"),
        "1.0.0",
        linux.revision_identity,
    )
    target = FilesystemPublicationTarget(
        "flavor-publication", scratch / "flavor-published"
    )
    publication_policy = PublicationPolicy(
        _identity("flavor-publication-policy").uri,
        (target.target_id,),
    )
    classification_digest = _identity("flavor-publication-classification").uri
    publication_request = PublicationRequest.create(
        component_ref=component_ref,
        component_lock_identity=_identity("flavor-sample-component-lock"),
        effective_revision_digest=linux_cpu_python.effective_revision.identity.uri,
        source_bundle=flavor_blob,
        roots={"descriptor": flavor_blob, "provenance": flavor_provenance},
        blobs=(flavor_blob, flavor_provenance),
        provenance=(flavor_provenance,),
        security_classification_digest=classification_digest,
        security_profile=SecurityProfile.CONSTRAINED,
        target_id=target.target_id,
        target_identity_digest=target.identity,
        policy_digest=publication_policy.policy_digest,
        actor="flavor-sample-publisher",
    )
    manifest = PublicationManifest.create(
        request=publication_request,
        authorization=publication_policy.authorize(
            publication_request,
            reason="publish an exact resolved Flavor descriptor",
            issued_at=FIXED_TIME,
            expires_at=FIXED_TIME + timedelta(minutes=5),
        ),
        created_at=FIXED_TIME,
    )
    published = PublicationService(
        cas,
        AppendOnlyEventStore(scratch / "flavor-publish-events"),
        publication_policy,
        clock=lambda: FIXED_TIME + timedelta(seconds=1),
    ).publish(manifest, target)
    imported_cas = FileSystemCAS(scratch / "flavor-import-cache")
    import_policy = ImportPolicy(
        _identity("flavor-import-policy").uri,
        (target.target_id,),
        (classification_digest,),
        (publication_policy.policy_digest,),
    )
    importer = PublicationService(
        imported_cas,
        AppendOnlyEventStore(scratch / "flavor-import-events"),
        import_policy=import_policy,
        clock=lambda: FIXED_TIME + timedelta(seconds=1),
    )
    import_request = ImportRequest.create(
        manifest=manifest,
        publication_manifest=published.publication_manifest,
        destination_identity_digest=importer.import_destination_identity,
        policy_digest=import_policy.policy_digest,
        actor="flavor-sample-importer",
    )
    importer.import_release(
        published.publication_manifest,
        target,
        expected_component=component_ref,
        import_authorization=import_policy.authorize(
            import_request,
            reason="import the exact trusted Flavor publication",
            issued_at=FIXED_TIME,
            expires_at=FIXED_TIME + timedelta(minutes=5),
        ),
    )
    if imported_cas.get_bytes(flavor_blob) != canonical_json_bytes(linux.to_dict()):
        raise SampleFailure("Flavor publication/import changed descriptor bytes")
    return (
        "windows-linux-resolve-independently",
        "cpu-cuda-resolve-independently",
        "python-rust-resolve-independently",
        "flavor-conflict-is-explicit",
        "effective-revision-caches-are-isolated",
        "flavor-publication-import-roundtrips",
    )


def _framework_readiness(
    root: Path,
    scratch: Path,
    _executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    del scratch
    _fixed_application_case(root, _framework_readiness_result)
    return (
        "generated-readiness-artifact-executes",
        "framework-version-compatibility-is-evaluated",
        "packaged-skill-readiness-is-evaluated",
    )


def _durable_split_service(
    _root: Path,
    _scratch: Path,
    executions: tuple[Mapping[str, object], ...],
) -> tuple[str, ...]:
    from tests.conformance.support.durable_split_service import (
        DurableSplitPortfolioError,
        verify_durable_split_service_report,
    )

    try:
        return verify_durable_split_service_report(executions)
    except DurableSplitPortfolioError as exc:
        raise SampleFailure(str(exc)) from exc


HANDLERS: dict[
    str,
    Callable[
        [Path, Path, tuple[Mapping[str, object], ...]],
        tuple[str, ...],
    ],
] = {
    "containerized-log-tally": _containerized_log_tally,
    "critical-path-scheduler": _critical_path_scheduler,
    "cuda-foundation": _cuda_foundation,
    "dependency-planner": _dependency_planner,
    "durable-split-service": _durable_split_service,
    "full-stack-rust-js": _full_stack_rust_js,
    "hello": _hello,
    "javascript-ledger-workbench": _javascript_ledger_workbench,
    "generated-library": _generated_library,
    "service-stack": _service_stack,
    "multi-repository": _multi_repository,
    "model-routing": _model_routing,
    "publication": _publication,
    "security": _security,
    "empty-cache-restart": _empty_cache_restart,
    "flavor-matrix": _flavor_matrix,
    "framework-readiness": _framework_readiness,
}


def _sample_platform_pin(sample_root: Path) -> str | None:
    """Return an explicit OS pin without loading generation authority."""

    path = sample_harness_root(sample_root) / "acceptance" / "execution.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        target = value["target"]
        configured = target[FlavorAxis.PLATFORM_OS.value]
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise SampleFailure(
            f"sample platform target is unavailable: {sample_root.name}"
        ) from exc
    if configured == "host":
        return None
    if not isinstance(configured, str):
        raise SampleFailure(
            "sample platform target must be host or one exact OS Flavor"
        )
    selected = _axis_target(FlavorAxis.PLATFORM_OS, configured)
    if selected not in _OS_FLAVOR_COORDINATES:
        raise SampleFailure(f"sample platform target is unsupported: {configured}")
    return selected


def _sample_accelerator_pin(sample_root: Path) -> str | None:
    """Return an explicit accelerator target without loading generation authority."""

    path = sample_harness_root(sample_root) / "acceptance" / "execution.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        configured = value["target"].get(FlavorAxis.ACCELERATOR.value)
    except (
        AttributeError,
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise SampleFailure(
            f"sample accelerator target is unavailable: {sample_root.name}"
        ) from exc
    if configured is None:
        return None
    if not isinstance(configured, str) or not configured:
        raise SampleFailure("sample accelerator target must be one exact Flavor target")
    return _axis_target(FlavorAxis.ACCELERATOR, configured)


@lru_cache(maxsize=1)
def _local_worker_observation():
    worker = ExecutionWorker(
        "sample-host",
        ExecutionWorkerKind.LOCAL,
        requirements=ExecutionRequirements(os_family=_host_os()),
    )
    return probe_worker_capabilities(worker)


@lru_cache(maxsize=4)
def _nvidia_stack_selection_document(
    variant: _ExecutionVariant,
) -> RecipeDocument:
    """Bind observed hardware and the exact installed CUDA closure into generation."""

    try:
        observation = _local_worker_observation()
    except Exception as exc:
        raise SampleFailure(
            "CUDA generation requires a current local worker observation"
        ) from exc
    devices = tuple(device for device in observation.gpus if device.vendor == "nvidia")
    if observation.nvidia_status is not NvidiaProbeStatus.OK or not devices:
        raise SampleFailure("CUDA generation requires one healthy observed NVIDIA GPU")
    language = variant.languages[0]
    if language == "python":
        executable = os.environ.get("PYTHON", "").strip() or shutil.which("python3")
        if executable is None:
            executable = shutil.which("python")
        if executable is None:
            raise SampleFailure("CUDA Python generation requires Python on PATH")
        probe = (
            "import hashlib,importlib.metadata as m,json,sys; import cupy as cp; "
            "ds=[]; "
            "[(lambda r,n,v,f,q: ds.append({'name':n,'version':v,'record_identity':"
            "('sha256:'+hashlib.sha256(r.encode()).hexdigest()) if r else None,"
            "'top_level_imports':sorted({str(x).split('/')[0].split('.')[0] for x in f "
            "if '/' in str(x) and not str(x).split('/')[0].endswith("
            "('.dist-info','.data'))}),"
            "'requires':sorted(q or [])}))"
            "(d.read_text('RECORD'),d.metadata['Name'],d.version,d.files or (),"
            "d.requires) for d in m.distributions() "
            "if d.metadata.get('Name')]; "
            "print(json.dumps({'interpreter':sys.executable,'python':sys.version.split()[0],"
            "'cupy':cp.__version__,'cuda_runtime':cp.cuda.runtime.runtimeGetVersion(),"
            "'device_count':cp.cuda.runtime.getDeviceCount(),'packages':sorted(ds,"
            "key=lambda x:"
            "(x['name'].casefold(),x['version']))},sort_keys=True,separators=(',',':')))"
        )
        command = (str(executable), "-c", probe)
        stack_kind = "cupy-cuda"
    elif language == "cpp":
        executable = os.environ.get("CXX", "").strip() or shutil.which("nvcc")
        if executable is None:
            raise SampleFailure("CUDA C++ generation requires nvcc on PATH")
        command = (str(executable), "--version")
        stack_kind = "cuda-cpp"
    else:
        raise SampleFailure(f"CUDA sample language is unsupported: {language}")
    resolved = Path(str(executable)).resolve()
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        executable_identity = (
            "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SampleFailure(
            "the selected CUDA stack failed its local preflight"
        ) from exc
    details: object
    if language == "python":
        try:
            details = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SampleFailure(
                "the selected CuPy stack returned invalid evidence"
            ) from exc
    else:
        details = {"version_output": completed.stdout.strip()}
    device = devices[0]
    target = (
        "sm_" + device.compute_capability.replace(".", "")
        if device.compute_capability is not None
        else None
    )
    observed_hardware = observation.to_dict()
    # Freshness and reachability are admission evidence, not source semantics.  Keep
    # timestamps, worker aliases, and transient diagnostics out of the derivation key so
    # an unchanged compatible machine can reuse its generated source across runs.
    for transient in ("worker_id", "observed_at", "diagnostic"):
        observed_hardware.pop(transient)
    record = {
        "schema": "literate-ai/nvidia-accelerated-stack-selection@1",
        "selection": stack_kind,
        "worker_observation_validated_current": True,
        "observed_hardware": observed_hardware,
        "selected_device_uuid": device.uuid,
        "compiler_target": target,
        "executable": str(resolved),
        "executable_identity": executable_identity,
        "stack": details,
        "rationale": (
            "exact installed stack passed a live preflight on the selected observed "
            "worker"
        ),
        "rejected_alternatives": ["cpu-fallback", "unobserved-cuda-stack"],
    }
    return RecipeDocument.create(
        "execution/nvidia-accelerated-stack-selection.json",
        canonical_json_bytes(record).decode("utf-8"),
    )


def _host_supports_nvidia_cuda() -> bool:
    """Probe once and reject CUDA samples before any generation work begins."""

    try:
        observation = _local_worker_observation()
    except Exception:
        return False
    return observation.nvidia_status is NvidiaProbeStatus.OK and any(
        device.vendor == "nvidia" for device in observation.gpus
    )


def discover(
    root: Path,
    patterns: Sequence[str] = ("*",),
    *,
    platform: str | None = None,
) -> tuple[Path, ...]:
    if not patterns or any(not item.strip() for item in patterns):
        raise SampleFailure("sample patterns must be non-empty glob expressions")
    harness_collection = root / SAMPLE_HARNESS_DIRECTORY
    if harness_collection.is_symlink() or not harness_collection.is_dir():
        raise SampleFailure("central sample harness directory is unavailable")
    catalog = tuple(
        sorted(
            root / path.parent.name
            for path in harness_collection.glob("*/sample.json")
            if path.parent.name != "source-to-specification"
            and (root / path.parent.name / "component.md").is_file()
            and any(
                fnmatch.fnmatchcase(path.parent.name, pattern) for pattern in patterns
            )
        )
    )
    if platform is not None and platform not in _OS_FLAVOR_COORDINATES:
        raise SampleFailure(f"sample discovery platform is unsupported: {platform}")
    discovered = tuple(
        sample
        for sample in catalog
        if platform is None
        or (pin := _sample_platform_pin(sample)) is None
        or pin == platform
    )
    discovered = tuple(
        sample
        for sample in discovered
        if _sample_accelerator_pin(sample) != "nvidia-cuda"
        or _host_supports_nvidia_cuda()
    )
    if not discovered:
        raise SampleFailure(
            "sample pattern matched no applications: " + ", ".join(patterns)
        )
    return discovered


def required_host_flavor_names(
    root: Path,
    *,
    sample_patterns: Sequence[str] = ("*",),
    flavor_selectors: Sequence[str] = (),
    platform: str | None = None,
) -> tuple[str, ...]:
    """Project selected sample variants to host-toolchain Flavor mix-ins.

    This is deliberately a local deterministic planning helper. Worker bootstrap uses
    it before generation so language and build dependencies come from the same Flavor
    selections as execution rather than from a second hardcoded worker profile.
    """

    names: set[str] = set()
    for sample_root in discover(root, sample_patterns, platform=platform):
        _metadata, definition, loaded, _closures = _load_sample(sample_root)
        contract, _document = _execution_contract(sample_root, definition, loaded)
        for variant in _execution_variants(definition, contract, flavor_selectors):
            names.update(f"lang-{language}" for language in variant.languages)
            for slot_id, value in variant.slot_values:
                slot = next(
                    item for item in definition.flavor_slots if item.slot_id == slot_id
                )
                if slot.axis is FlavorAxis.BUILD_SYSTEM:
                    names.add(f"build-{value}")
                elif slot.axis is FlavorAxis.TOOLCHAIN:
                    names.add(f"toolchain-{value}")
        target = contract.get("target")
        if isinstance(target, Mapping):
            packaging = target.get(FlavorAxis.PACKAGING.value)
            if isinstance(packaging, str):
                names.add(f"package-{_axis_target(FlavorAxis.PACKAGING, packaging)}")
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class StandardSampleExecutionSelection:
    """One sample variant proven plannable by the public Standard service."""

    sample_id: str
    variant_id: str
    execution_service: str
    component_count: int
    dependency_edge_count: int


class _StandardSampleAuthoritySnapshot:
    def __init__(self, authority, catalog) -> None:
        self.authority = authority
        self.catalog = catalog

    def component_content(self, authoring_identity, reference):
        return self.catalog.component_content(authoring_identity, reference)

    def admit_component_asset(self, authoring_identity, asset, cas):
        return self.catalog.admit_component_asset(authoring_identity, asset, cas)

    def flavor_content(self, flavor_revision, reference):
        return self.catalog.flavor_content(flavor_revision, reference)

    def require_unchanged(self) -> None:
        self.catalog.require_unchanged()


def _locked_standard_sample_model_bindings(
    snapshot: _StandardSampleAuthoritySnapshot,
    *,
    coding_cli: str,
    pipeline_model: str | None = None,
) -> dict[str, ModelScopeBinding]:
    """Resolve sample models through the same bound adapter as production."""

    return LockedComponentModelSelectionAdapter(pipeline_model=pipeline_model).bindings(
        snapshot, coding_cli=coding_cli
    )


def _locked_standard_sample_model_identities(
    snapshot: _StandardSampleAuthoritySnapshot,
    *,
    coding_cli: str,
    pipeline_model: str | None = None,
) -> dict[str, ContentIdentity]:
    return {
        revision: binding.identity
        for revision, binding in _locked_standard_sample_model_bindings(
            snapshot,
            coding_cli=coding_cli,
            pipeline_model=pipeline_model,
        ).items()
    }


class _StandardSampleAcceptanceOracle:
    def __init__(self, sample_id: str, root_revision: ContentIdentity) -> None:
        self.sample_id = sample_id
        self.root_revision = root_revision

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/standard-sample-runtime-oracle@1",
                "sample_id": self.sample_id,
                "root_revision": self.root_revision.uri,
                "algorithm": "tests.conformance.runtime-oracles@1",
            }
        )

    def cases(self, component_lock: ComponentLock):
        if component_lock.root_revision != self.root_revision:
            raise SampleFailure("Standard sample oracle received another root")
        probe = create_post_build_probe(self.sample_id)
        return (
            LocalIndependentAcceptanceCase.create(
                probe.case_id, probe.arguments, probe.expected_result
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutedStandardSampleVariant:
    lifecycle: StandardProjectLifecycleResult
    report: Mapping[str, object]


def _standard_sample_budget() -> GenerationComplexityBudget:
    return GenerationComplexityBudget(
        max_prompt_bytes=1_000_000,
        max_estimated_tokens=250_000,
        max_document_count=100,
        max_direct_interface_bytes=100_000,
        max_dependency_fan_in=32,
        max_model_attempts=3,
        max_wall_time_ms=900_000,
        max_model_tokens=250_000,
        max_cost_microunits=100_000_000,
    )


_STANDARD_SINGLE_COMPONENT_SAMPLES = frozenset(
    {
        "critical-path-scheduler",
        "cuda-vector-transform-cpp",
        "cuda-vector-transform-python",
        "dependency-planner",
        "empty-cache-restart",
        "flavor-matrix",
        "generated-library",
        "hello-component",
        "javascript-ledger-workbench",
        "model-routing",
        "publication-import",
        "security-policies",
        "loan-risk-gate",
        "playback-controller",
    }
)


def _prepare_standard_sample_project(
    *,
    snapshot: _StandardSampleAuthoritySnapshot,
    execution_plan,
    source_root: Path,
    coding_cli: str,
    pipeline_model: str | None,
    execution_documents: tuple[RecipeDocument, ...] = (),
):
    preparation = LockedComponentNodePreparationAdapter(
        model_selector=LockedComponentModelSelectionAdapter(
            pipeline_model=pipeline_model
        ),
        coding_cli=coding_cli,
        execution_documents=execution_documents,
    )
    source_root.mkdir(parents=True, exist_ok=True)
    allocator = FilesystemComponentWorkspaceAllocator(source_root)
    return StandardProjectApplicationService.prepare(
        execution_plan,
        authority=snapshot,
        authority_lock_identity=preparation.authority_lock_identity,
        authority_guard=preparation.guard,
        node_projector=preparation.project,
        workspace_allocator=allocator.allocate,
        framework_envelope=lambda projection: projection.recipe.prompt(
            include_locked_authority_documents=False
        ).encode("utf-8"),
        budget=_standard_sample_budget(),
    )


def _standard_sample_stage_request(
    *,
    sample_root: Path,
    node,
    revision: ComponentRevision,
    source_generator,
    forbidden_acceptance_arguments: list[object] | None = None,
):
    model_plan = _sample_execution_plan(
        sample_root,
        node.definition,
        revision,
        node.recipe,
        source_generator,
    )
    prior: dict[str, object] = {
        "component_revision": node.plan.component_revision.uri,
        "recipe_identity": node.recipe.identity,
        "generation_key_identity": node.plan.generation_key.identity.uri,
        "required_entrypoints": list(node.recipe.all_required_entrypoints),
        "direct_public_interface_identities": [
            item.uri
            for item in node.plan.generation_key.direct_public_interface_identities
        ],
    }
    execution_documents = tuple(
        document
        for document in node.recipe.documents
        if document.path.startswith("execution/")
    )
    if execution_documents:
        try:
            prior["execution_context"] = {
                document.path: json.loads(document.content)
                for document in execution_documents
            }
        except json.JSONDecodeError as exc:
            raise SampleFailure("execution context document is not valid JSON") from exc
    if forbidden_acceptance_arguments is not None:
        prior["generated_test_constraints"] = {
            "must_not_reuse_acceptance_arguments": True,
            "forbidden_argument_vector_identities": [
                canonical_identity(forbidden_acceptance_arguments).uri
            ],
        }
    return model_plan, {
        "stage_id": model_plan.model_stages[-1].stage_id,
        "prior_stage_outputs": {"plan": prior},
        "input_identity": canonical_identity(prior).to_dict(),
    }


def _plan_standard_sample_derivations(
    *,
    sample_root: Path,
    sample_id: str,
    definition: ComponentDefinition,
    loaded: LoadedSpecification,
    contract: Mapping[str, object],
    variant: _ExecutionVariant,
    input_closure,
    generator: CachedCodingCliSourceGenerator,
    scratch: Path,
    pipeline_model: str | None = None,
) -> tuple[tuple[SourceDerivationCacheKey, ContentIdentity], ...]:
    """Plan the exact prepared Standard nodes that the live path executes."""

    _base, _target, _resolved, authority, catalog, _plan, _selected = (
        _sample_runtime_recipe(
            sample_root,
            definition,
            loaded.specification_set,
            contract,
            variant,
            input_closure,
        )
    )
    snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)
    selection = generator.selection
    models = _locked_standard_sample_model_identities(
        snapshot,
        coding_cli=selection.name,
        pipeline_model=pipeline_model,
    )
    execution_plan = StandardProjectApplicationService.plan(
        authority.lock, model_identities=models
    )
    prepared = _prepare_standard_sample_project(
        snapshot=snapshot,
        execution_plan=execution_plan,
        source_root=scratch / "workspaces",
        coding_cli=selection.name,
        pipeline_model=pipeline_model,
    )
    invocation_arguments: dict[str, list[object]] = {}
    if sample_id == "service-stack":
        for locked_node in authority.lock.nodes:
            component_name = locked_node.revision.coordinate.name
            component_root = (
                sample_root
                if component_name == definition.coordinate.name
                else _project_component_root(sample_root, component_name)
            )
            component_definition, component_loaded, _authoring = (
                _load_markdown_component(component_root)
            )
            component_contract, _document = _execution_contract(
                component_root, component_definition, component_loaded
            )
            invocation_arguments[component_name] = component_contract["invocations"][0][
                "arguments"
            ]
    locked = {
        node.revision.identity.uri: node.revision for node in authority.lock.nodes
    }
    results = []
    for node in prepared.nodes:
        revision = locked[node.plan.component_revision.uri]
        model_plan, stage_request = _standard_sample_stage_request(
            sample_root=sample_root,
            node=node,
            revision=revision,
            source_generator=generator,
            forbidden_acceptance_arguments=(
                invocation_arguments[revision.coordinate.name]
                if sample_id == "service-stack"
                else None
            ),
        )
        results.append(
            (
                generator.derivation_cache_key(
                    node.recipe,
                    execution_plan=model_plan,
                    stage_request=stage_request,
                    bounded_prompt=node.request.prompt,
                ),
                node.recipe.component_lock_identity,
            )
        )
    return tuple(results)


def execute_standard_sample_variant(
    *,
    sample_root: Path,
    sample_id: str,
    variant: _ExecutionVariant,
    authority,
    catalog,
    source_generator,
    scratch: Path,
    object_root: Path,
    native_packages: bool = False,
    selected_flavors: Sequence[_SharedFlavor] = (),
    pipeline_model: str | None = None,
) -> ExecutedStandardSampleVariant:
    """Run one ordinary sample solely through the public production Standard path."""

    if len(authority.lock.nodes) != 1:
        raise SampleFailure(
            "generic Standard sample runner currently requires one Component"
        )
    if variant.variant_id == "rust-javascript-full-stack":
        raise SampleFailure(
            "full-stack Rust/JavaScript must be split into bounded Components"
        )
    snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)
    selection = source_generator.selection
    model_bindings = _locked_standard_sample_model_bindings(
        snapshot,
        coding_cli=selection.name,
        pipeline_model=pipeline_model,
    )
    models = {
        revision: binding.identity for revision, binding in model_bindings.items()
    }
    candidate_cas = FileSystemCAS(scratch / "source-generation-cas")
    assets = admit_locked_authored_assets(snapshot, cas=candidate_cas)
    from literate_ai.application.standard_project_services import (
        StandardProjectApplicationService,
    )

    execution_plan = StandardProjectApplicationService.plan(
        authority.lock, model_identities=models, assets=assets
    )
    locked = {
        node.revision.identity.uri: node.revision for node in authority.lock.nodes
    }

    def invocation(node):
        revision = locked[node.plan.component_revision.uri]
        model_plan, request = _standard_sample_stage_request(
            sample_root=sample_root,
            node=node,
            revision=revision,
            source_generator=source_generator,
        )
        return CodingCliSourceGenerationInvocation.create(
            model_plan,
            request,
            application_root_revision_identity=authority.lock.root_revision,
            readiness_identity=canonical_identity(
                {"standard-sample-readiness": node.plan.component_revision.uri}
            ),
        )

    source_runner = CachedCodingCliSourceGenerationRunner(
        source_generator,
        cas=candidate_cas,
        invocation_provider=invocation,
        assets=assets,
    )
    source_trees = LocalSourceTreeRegistry()
    toolchain_closure = project_locked_standard_toolchain_closure(
        snapshot, execution_plan
    )
    execution_documents: tuple[RecipeDocument, ...] = ()
    if any(
        item.definition.primary_axis is FlavorAxis.ACCELERATOR
        and item.descriptor.axis_value == "nvidia-cuda"
        for item in selected_flavors
    ):
        execution_documents = (_nvidia_stack_selection_document(variant),)
    runtime = assemble_filesystem_standard_project_runtime(
        generator=source_runner,
        object_root=object_root,
        toolchain_closure=toolchain_closure,
        source_trees=source_trees,
        indexer=DisabledGenerationIndexer(source_trees, artifact_root=None),
        source_cache_publisher=source_runner,
        independent_acceptance_oracle=_StandardSampleAcceptanceOracle(
            sample_id, authority.lock.root_revision
        ),
        node_preparation=LockedComponentNodePreparationAdapter(
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=pipeline_model
            ),
            coding_cli=selection.name,
            execution_documents=execution_documents,
        ),
    )
    revisions = tuple(
        plan.component_revision for plan in execution_plan.generation_plans
    )
    try:
        executed = runtime.execute(
            snapshot,
            StandardProjectExecutionRequest(
                PlannedStandardProject(selection, execution_plan),
                scratch / "workspaces",
                ComponentInvalidationDecision(
                    f"standard-sample-{sample_id}-{variant.variant_id}",
                    authority.lock.root_revision,
                    ComponentChangeSurface.LOCAL_AUTHORITY,
                    revisions,
                    revisions,
                    revisions,
                ),
                max_parallelism=1,
                budget=_standard_sample_budget(),
            ),
        )
    except LocalStandardLifecycleError as exc:
        raise SampleFailure(
            f"Standard sample {sample_id}/{variant.variant_id} failed: {exc}"
        ) from exc
    if not executed.lifecycle.successful:
        failures = {
            item.component_revision.uri: item.failure_code
            for item in executed.lifecycle.node_results
        }
        diagnostics = dict(runtime.lifecycle_ports.failure_diagnostics)
        for item in executed.lifecycle.node_results:
            if (failure := item.failure_evidence) is not None and failure.diagnostic:
                diagnostics[item.component_revision.uri] = failure.diagnostic
        raise SampleFailure(
            f"Standard sample {sample_id}/{variant.variant_id} lifecycle failed: "
            f"{failures!r}; diagnostics={diagnostics!r}"
        )
    report = project_standard_sample_execution_report(
        executed.lifecycle,
        component_lock=authority.lock,
        sample_id=sample_id,
        variant_id=variant.variant_id,
        model_bindings=model_bindings,
    )
    if native_packages:
        report["native_packages"] = _build_standard_sample_native_packages(
            sample_root=sample_root,
            sample_id=sample_id,
            variant_id=variant.variant_id,
            lifecycle=executed.lifecycle,
            lifecycle_ports=runtime.lifecycle_ports,
            object_root=object_root,
            selected_flavors=selected_flavors,
        )
    return ExecutedStandardSampleVariant(executed.lifecycle, report)


def _build_standard_sample_native_packages(
    *,
    sample_root: Path,
    sample_id: str,
    variant_id: str,
    lifecycle: StandardProjectLifecycleResult,
    lifecycle_ports,
    object_root: Path,
    selected_flavors: Sequence[_SharedFlavor],
) -> list[dict[str, str]]:
    """Build selected native formats from the already accepted sample closure."""

    providers = tuple(
        item.definition.coordinate.name.removeprefix("package.")
        for item in selected_flavors
        if item.definition.primary_axis is FlavorAxis.PACKAGING
    )
    if not providers:
        return []
    unsupported = sorted(set(providers) - {"pip", "conan", "npm"})
    if unsupported:
        raise SampleFailure(
            "native sample packaging is not implemented for: " + ", ".join(unsupported)
        )
    integration = lifecycle.root_integration
    if integration is None:
        raise SampleFailure("accepted sample omitted root package custody")
    custody = lifecycle_ports.project_package_custody(
        integration.package_plan, integration.package_result
    )
    root_result = next(
        item
        for item in lifecycle.node_results
        if item.component_revision == integration.package_plan.root_component_revision
    )
    if root_result.build_evidence is None:
        raise SampleFailure("accepted sample omitted resolved SBOM evidence")
    resolved_sbom = lifecycle_ports.resolved_sbom_content(root_result.build_evidence)
    source_sbom = lifecycle_ports.source_sbom_content(root_result.build_evidence)
    package_parent = object_root / "sample-native-packages" / sample_id / variant_id
    package_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="batch-", dir=package_parent))
    try:
        input_root = staging / "input-root"
        shutil.copytree(custody.root, input_root)
        specification = (sample_root / "component.md").read_bytes()
        spec_path = input_root / "specification" / "component.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_bytes(specification)
        sbom_path = input_root / "sbom" / "resolved.cdx.json"
        sbom_path.parent.mkdir(parents=True)
        sbom_path.write_bytes(resolved_sbom)
        records = []
        for provider in providers:
            if provider == "pip":
                adapter = WheelPackageAdapter(sample_id, "1.0.0")
            elif provider == "conan":
                adapter = ConanPackageAdapter(
                    sample_id.lower(), "1.0.0", ConanToolBinding.discover()
                )
            else:
                adapter = NpmPackageAdapter(
                    sample_id.lower().replace("_", "-"), "1.0.0"
                )
            plan = (
                npm_archive_package_plan(
                    integration.package_plan,
                    packager_identity=adapter.packager_identity,
                    specification=specification,
                    source_sbom=source_sbom,
                    source_sbom_identity=(
                        root_result.build_evidence.source_sbom.bom_identity
                    ),
                    resolved_sbom=resolved_sbom,
                    resolved_sbom_source_identity=root_result.build_evidence.identity,
                )
                if provider == "npm"
                else native_archive_package_plan(
                    integration.package_plan,
                    packager_identity=adapter.packager_identity,
                    specification=specification,
                    resolved_sbom=resolved_sbom,
                    resolved_sbom_source_identity=root_result.build_evidence.identity,
                )
            )
            provider_root = staging / provider
            provider_root.mkdir()
            materialized = input_root
            if provider == "npm":
                materialized = provider_root / "materialized"
                shutil.copytree(input_root, materialized)
                source_path = materialized / "sbom" / "source.cdx.json"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(source_sbom)
            if provider == "pip":
                result = adapter.package(plan, materialized_root=materialized)
                content = adapter.read_created_blob(result.artifacts[0].blob)
                artifact = provider_root / result.artifacts[0].path
                artifact.write_bytes(content)
                WheelPackageAdapter(sample_id, "1.0.0").verify_bytes(result, content)
            elif provider == "conan":
                result = adapter.package(
                    plan, materialized_root=materialized, object_root=provider_root
                )
                content = adapter.read_created_blob(result.artifacts[0].blob)
                artifact = provider_root / result.artifacts[0].path
                verification_root = provider_root / "verify"
                verification_root.mkdir()
                ConanPackageAdapter(
                    sample_id.lower(), "1.0.0", adapter.tool
                ).verify_bytes(result, content, object_root=verification_root)
            else:
                result = adapter.package(plan, materialized_root=materialized)
                content = adapter.read_created_blob(result.artifacts[0].blob)
                artifact = provider_root / result.artifacts[0].path
                artifact.write_bytes(content)
                NpmPackageAdapter(
                    sample_id.lower().replace("_", "-"), "1.0.0"
                ).verify_bytes(result, content)
            records.append(
                {
                    "provider": provider,
                    "artifact": artifact.relative_to(staging).as_posix(),
                    "package_plan_identity": plan.identity.uri,
                    "package_result_identity": result.identity.uri,
                }
            )
        batch_identity = canonical_identity(
            {
                "schema": "literate-ai/sample-native-package-batch@1",
                "sample_id": sample_id,
                "variant_id": variant_id,
                "accepted_lifecycle_identity": lifecycle.identity.uri,
                "results": records,
            }
        )
        final = package_parent / batch_identity.digest
        if final.exists():
            shutil.rmtree(staging)
        else:
            staging.rename(final)
        return [
            {**record, "artifact": str(final / record["artifact"])}
            for record in records
        ]
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def execute_standard_service_stack_variant(
    *,
    sample_root: Path,
    variant: _ExecutionVariant,
    authority,
    catalog,
    scratch: Path,
    object_root: Path,
    build_system: str,
    live_source_generator=None,
    invocation_arguments: dict[str, list[object]] | None = None,
    pipeline_model: str | None = None,
) -> ExecutedStandardSampleVariant:
    """Run the dependency-bearing sample through its one public Standard service."""

    from tests.conformance.support.standard_service_stack import (
        execute_live_standard_service_stack,
        execute_standard_service_stack,
    )

    if live_source_generator is None:
        details = execute_standard_service_stack(
            authority=authority,
            catalog=catalog,
            scratch=scratch,
            object_root=object_root,
            build_system=build_system,
            include_lifecycle_result=True,
        )
        cache = details["build_cache"]
    else:
        if invocation_arguments is None:
            raise SampleFailure(
                "live Standard service-stack requires verifier-owned invocations"
            )
        details = execute_live_standard_service_stack(
            sample_root=sample_root,
            authority=authority,
            catalog=catalog,
            scratch=scratch,
            object_root=object_root,
            source_generator=live_source_generator,
            invocation_arguments=invocation_arguments,
            pipeline_model=pipeline_model,
            include_lifecycle_result=True,
        )
        cache = details["_operational_build_cache"]
    lifecycle = details.pop("_lifecycle_result", None)
    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise SampleFailure("Standard service-stack omitted its typed lifecycle result")
    model_bindings = (
        _locked_standard_sample_model_bindings(
            _StandardSampleAuthoritySnapshot(authority, catalog),
            coding_cli=live_source_generator.selection.name,
            pipeline_model=pipeline_model,
        )
        if live_source_generator is not None
        else None
    )
    report = project_standard_sample_execution_report(
        lifecycle,
        component_lock=authority.lock,
        sample_id="service-stack",
        variant_id=variant.variant_id,
        model_bindings=model_bindings,
    )
    report["_operational_build_cache"] = cache
    return ExecutedStandardSampleVariant(lifecycle, report)


def project_standard_sample_execution_report(
    lifecycle: StandardProjectLifecycleResult,
    *,
    component_lock: ComponentLock,
    sample_id: str,
    variant_id: str,
    independent_acceptance_case_count: int = 1,
    model_bindings: Mapping[str, ModelScopeBinding] | None = None,
) -> dict[str, object]:
    """Project only native Standard evidence; never synthesize legacy runner fields."""

    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise TypeError("lifecycle must be a StandardProjectLifecycleResult")
    if not isinstance(component_lock, ComponentLock):
        raise TypeError("component_lock must be a ComponentLock")
    if not sample_id or not variant_id:
        raise ValueError("Standard sample report requires sample and variant IDs")
    if (
        isinstance(independent_acceptance_case_count, bool)
        or not isinstance(independent_acceptance_case_count, int)
        or independent_acceptance_case_count < 1
    ):
        raise ValueError("Standard sample report requires independent case evidence")
    if (
        not lifecycle.successful
        or lifecycle.aggregate_receipt is None
        or lifecycle.admission_identity is None
        or lifecycle.receipt_identity is None
        or lifecycle.root_integration_evidence_identity is None
        or lifecycle.project_build_plan is None
    ):
        raise SampleFailure(
            "Standard sample report requires a complete accepted root lifecycle"
        )
    expected_revisions = tuple(
        sorted(
            (node.revision.identity for node in component_lock.nodes),
            key=lambda identity: identity.uri,
        )
    )
    observed_revisions = tuple(
        item.component_revision for item in lifecycle.node_results
    )
    if observed_revisions != expected_revisions:
        raise SampleFailure("Standard sample result differs from its Component lock")
    nodes = []
    generated_test_count = 0
    if model_bindings is not None and set(model_bindings) != {
        identity.uri for identity in expected_revisions
    }:
        raise SampleFailure(
            "Standard sample model evidence differs from its Component lock"
        )
    for node in lifecycle.node_results:
        if (
            node.source_output is None
            or node.index_identity is None
            or node.authorization_identity is None
            or node.build_plan_identity is None
            or node.build_evidence is None
            or node.generated_test_evidence is None
            or node.execution_evidence is None
            or node.acceptance_evidence is None
        ):
            raise SampleFailure(
                "Standard sample result omitted its strict typed node evidence"
            )
        source_bom = node.source_output.candidate.source_bom_identity
        build = node.build_evidence
        if (
            build.source_sbom.bom_identity != source_bom
            or build.source_sbom.resolved_graph_identity != component_lock.identity
            or build.resolved_sbom.resolved_graph_identity != component_lock.identity
        ):
            raise SampleFailure(
                "Standard sample SBOM evidence names another Component lock"
            )
        generated_test_count += node.generated_test_evidence.executed_count
        node_report = {
            "component_revision": node.component_revision.uri,
            "source_generation_identity": node.source_output.identity.uri,
            "recipe_identity": node.source_generation.recipe_identity.uri,
            "source_candidate_identity": node.source_candidate_identity.uri,
            "source_tree_identity": node.source_output.candidate.tree_identity.uri,
            "source_index_identity": node.index_identity.uri,
            "provenance_evidence_identity": node.source_output.provenance_identity.uri,
            "workspace_admission_identity": lifecycle.admission_identity.uri,
            "build_authorization_identity": node.authorization_identity.uri,
            "build_plan_identity": node.build_plan_identity.uri,
            "build_evidence_identity": build.identity.uri,
            "source_sbom_identity": source_bom.uri,
            "resolved_sbom_identity": build.resolved_sbom.bom_identity.uri,
            "generated_test_evidence_identity": (
                node.generated_test_evidence.identity.uri
            ),
            "generated_test_count": node.generated_test_evidence.executed_count,
            "execution_evidence_identity": node.execution_evidence.identity.uri,
            "stdout_identity": node.execution_evidence.stdout_identity.uri,
            "acceptance_evidence_identity": node.acceptance_evidence.identity.uri,
            "artifact_export_identities": [item.identity.uri for item in node.exports],
        }
        if model_bindings is not None:
            binding = model_bindings[node.component_revision.uri]
            node_report.update(
                {
                    "model_selector": binding.model_selector,
                    "model_scope_identity": binding.identity.uri,
                }
            )
        nodes.append(node_report)
    report = {
        "schema": STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA,
        "sample_id": sample_id,
        "variant_id": variant_id,
        "passed": True,
        "component_lock_identity": component_lock.identity.uri,
        "execution_plan_identity": lifecycle.execution_plan_identity.uri,
        "project_build_plan_identity": lifecycle.project_build_plan_identity.uri,
        "lifecycle_result_identity": lifecycle.identity.uri,
        "lifecycle_membership_identity": lifecycle.lifecycle_membership.identity.uri,
        "admission_identity": lifecycle.admission_identity.uri,
        "aggregate_receipt_identity": lifecycle.aggregate_receipt.identity.uri,
        "root_integration_evidence_identity": (
            lifecycle.root_integration_evidence_identity.uri
        ),
        "generated_test_count": generated_test_count,
        "independent_acceptance_case_count": independent_acceptance_case_count,
        "node_count": len(nodes),
        "nodes": nodes,
    }
    return {**report, "identity": canonical_identity(report).uri}


def project_standard_sample_test_receipt(
    lifecycle: StandardProjectLifecycleResult,
    *,
    project_id: str,
    project_revision_identity: ContentIdentity,
    lifecycle_policy: StandardLifecyclePolicy,
    receipt_policy: ProjectTestReceiptPolicy,
    lifecycle_request_identity: ContentIdentity,
    lifecycle_invocation_identity: ContentIdentity,
    runner_identity: ContentIdentity,
) -> ProjectTestReceipt:
    """Use the policy-enforced Standard receipt path, not the legacy sample receipt."""

    return project_standard_project_test_receipt(
        lifecycle,
        project_id=project_id,
        project_revision_identity=project_revision_identity,
        lifecycle_policy=lifecycle_policy,
        receipt_policy=receipt_policy,
        lifecycle_request_identity=lifecycle_request_identity,
        lifecycle_invocation_identity=lifecycle_invocation_identity,
        runner_identity=runner_identity,
    )


def standard_sample_execution_selections(
    root: Path,
) -> tuple[StandardSampleExecutionSelection, ...]:
    """Resolve every sample lock and select the Standard project service."""

    from literate_ai.application.standard_project_services import (
        StandardProjectApplicationService,
    )

    selections = []
    for sample_root in discover(root, platform=_host_os()):
        metadata, definition, loaded, closures = _load_sample(sample_root)
        contract, _document = _execution_contract(sample_root, definition, loaded)
        for variant in _execution_variants(definition, contract):
            if metadata["sample_id"] == "durable-split-service":
                from tests.conformance.support.durable_split_service import (
                    resolve_durable_split_portfolio,
                )

                portfolio = resolve_durable_split_portfolio(
                    sample_root, platform=_host_os()
                )
                component_revisions = {
                    node.revision.identity.uri
                    for lock in portfolio.locks
                    for node in lock.nodes
                }
                edges = {
                    edge.identity.uri for lock in portfolio.locks for edge in lock.edges
                }
                selections.append(
                    StandardSampleExecutionSelection(
                        str(metadata["sample_id"]),
                        variant.variant_id,
                        "filesystem-standard-project-runtime",
                        len(component_revisions),
                        len(edges),
                    )
                )
                continue
            authority = _sample_runtime_recipe(
                sample_root,
                definition,
                loaded.specification_set,
                contract,
                variant,
                closures.generation.fork(),
            )[3]
            models = {
                node.revision.identity.uri: canonical_identity(
                    {
                        "schema": "literate-ai/sample-standard-selection-model@1",
                        "sample_id": metadata["sample_id"],
                        "variant_id": variant.variant_id,
                        "component_revision": node.revision.identity.uri,
                    }
                )
                for node in authority.lock.nodes
            }
            execution = StandardProjectApplicationService.plan(
                authority.lock, model_identities=models
            )
            selections.append(
                StandardSampleExecutionSelection(
                    str(metadata["sample_id"]),
                    variant.variant_id,
                    "filesystem-standard-project-runtime",
                    len(execution.generation_plans),
                    len(authority.lock.edges),
                )
            )
    return tuple(selections)


def _external_runtime_root(configured: Path) -> Path:
    if configured.is_symlink():
        raise SampleFailure("sample runtime root cannot be a symbolic link")
    resolved = configured.resolve()
    repository_root = Path(__file__).resolve().parents[3]
    if resolved == repository_root or resolved.is_relative_to(repository_root):
        raise SampleFailure("sample runtime root must be outside the repository")
    return resolved


@contextmanager
def _sample_evidence_phase(
    *,
    evidence_run,
    sample_id: str,
    variant: _ExecutionVariant,
    phase: str,
    scratch: Path,
    flavor_selectors: Sequence[str],
    pipeline_model: str | None,
    source_generator,
) -> Iterator[Path]:
    language = "-".join(variant.languages)
    pins: dict[str, object] = {
        "sample": sample_id,
        "variant": variant.variant_id,
        "phase": phase,
        "languages": list(variant.languages),
        "flavor_selectors": list(flavor_selectors),
    }
    if pipeline_model is not None:
        pins["model"] = pipeline_model
    source_cache_key = os.environ.get("LITAI_SOURCE_CACHE_CONTROL_IDENTITY")
    if source_cache_key:
        pins["source_cache_key"] = source_cache_key
    coding_cli = getattr(source_generator, "coding_cli", None)
    if isinstance(coding_cli, str) and coding_cli:
        pins["coding_cli"] = coding_cli
    context = (
        evidence_run.node(
            f"samples/{sample_id}/{language}/{phase}",
            operation="sample.conformance",
            parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
            pins=pins,
        )
        if evidence_run is not None
        else nullcontext()
    )
    with context as node:
        phase_scratch = scratch
        phase_scratch.mkdir(parents=True, exist_ok=True)
        try:
            yield phase_scratch
        except BaseException:
            if isinstance(node, EvidenceNode):
                node.add_output(
                    role="scratch",
                    path=phase_scratch,
                    media_type="inode/directory",
                )
                report_progress(f"Retained sample scratch: {phase_scratch.resolve()}")
            raise


def run_sample(
    sample_root: Path,
    scratch: Path,
    *,
    source_generator,
    standard_source_generator=None,
    cpp_toolchain,
    object_root: Path | None = None,
    allow_host_execution: bool,
    flavor_selectors: Sequence[str] = (),
    native_packages: bool = False,
    pipeline_model: str | None = None,
) -> dict[str, Any]:
    sample_root = sample_root.resolve(strict=True)
    scratch = _external_runtime_root(scratch)
    metadata, definition, loaded, input_closures = _load_sample(sample_root)
    sample_id = str(metadata["sample_id"])
    oracle_reference = _sample_oracle_reference(metadata)
    scenario = metadata["scenario"]
    handler = HANDLERS.get(scenario)
    if handler is None:
        raise SampleFailure(f"unknown sample scenario: {scenario}")
    contract, _contract_document = _execution_contract(sample_root, definition, loaded)
    _require_host_os_selection(flavor_selectors)
    variants = _execution_variants(definition, contract, flavor_selectors)
    executions_list = []
    for variant in variants:
        if sample_id == "durable-split-service":
            with _sample_evidence_phase(
                evidence_run=attach_run(),
                sample_id=sample_id,
                variant=variant,
                phase="standard-portfolio",
                scratch=scratch,
                flavor_selectors=flavor_selectors,
                pipeline_model=pipeline_model,
                source_generator=(standard_source_generator or source_generator),
            ) as phase_scratch:
                from tests.conformance.support.durable_split_service import (
                    DurableSplitPortfolioError,
                    execute_durable_split_service_portfolio,
                )

                try:
                    portfolio_execution = execute_durable_split_service_portfolio(
                        sample_root=sample_root,
                        source_generator=(
                            standard_source_generator or source_generator
                        ),
                        scratch=phase_scratch / "standard-portfolio",
                        object_root=(object_root or phase_scratch / "standard-objects")
                        / sample_id
                        / variant.variant_id,
                        allow_host_execution=allow_host_execution,
                        pipeline_model=pipeline_model,
                    )
                except DurableSplitPortfolioError as exc:
                    raise SampleFailure(str(exc)) from exc
            executions_list.append(portfolio_execution)
            continue
        if sample_id in _STANDARD_SINGLE_COMPONENT_SAMPLES:
            with _sample_evidence_phase(
                evidence_run=attach_run(),
                sample_id=sample_id,
                variant=variant,
                phase="standard",
                scratch=scratch,
                flavor_selectors=flavor_selectors,
                pipeline_model=pipeline_model,
                source_generator=(standard_source_generator or source_generator),
            ) as phase_scratch:
                _base, _target, _resolved, authority, catalog, _plan, selected = (
                    _sample_runtime_recipe(
                        sample_root,
                        definition,
                        loaded.specification_set,
                        contract,
                        variant,
                        input_closures.generation.fork(),
                    )
                )
                standard_execution = execute_standard_sample_variant(
                    sample_root=sample_root,
                    sample_id=sample_id,
                    variant=variant,
                    authority=authority,
                    catalog=catalog,
                    source_generator=(standard_source_generator or source_generator),
                    scratch=phase_scratch / "standard" / variant.variant_id,
                    object_root=(object_root or phase_scratch / "standard-objects")
                    / sample_id
                    / variant.variant_id,
                    native_packages=native_packages,
                    selected_flavors=selected,
                    pipeline_model=pipeline_model,
                )
            executions_list.append(dict(standard_execution.report))
            continue
        if sample_id == "service-stack" and variant.variant_id == "python":
            with _sample_evidence_phase(
                evidence_run=attach_run(),
                sample_id=sample_id,
                variant=variant,
                phase="standard",
                scratch=scratch,
                flavor_selectors=flavor_selectors,
                pipeline_model=pipeline_model,
                source_generator=standard_source_generator,
            ) as phase_scratch:
                _base, _target, _resolved, authority, catalog, _plan, selected = (
                    _sample_runtime_recipe(
                        sample_root,
                        definition,
                        loaded.specification_set,
                        contract,
                        variant,
                        input_closures.generation.fork(),
                    )
                )
                build_system = (
                    "bazel"
                    if any(
                        item.definition.primary_axis is FlavorAxis.BUILD_SYSTEM
                        and item.definition.coordinate.name == "build-bazel"
                        for item in selected
                    )
                    else "native"
                )
                invocation_arguments = None
                if standard_source_generator is not None:
                    invocation_arguments = {}
                    for node in authority.lock.nodes:
                        component_name = node.revision.coordinate.name
                        component_root = (
                            sample_root
                            if component_name == definition.coordinate.name
                            else _project_component_root(sample_root, component_name)
                        )
                        (
                            component_definition,
                            component_loaded,
                            _component_authoring_identity,
                        ) = _load_markdown_component(component_root)
                        component_contract, _component_document = _execution_contract(
                            component_root, component_definition, component_loaded
                        )
                        invocation_arguments[component_name] = component_contract[
                            "invocations"
                        ][0]["arguments"]
                standard_execution = execute_standard_service_stack_variant(
                    sample_root=sample_root,
                    variant=variant,
                    authority=authority,
                    catalog=catalog,
                    scratch=phase_scratch / "standard" / variant.variant_id,
                    object_root=(object_root or phase_scratch / "standard-objects")
                    / sample_id
                    / variant.variant_id,
                    build_system=build_system,
                    live_source_generator=standard_source_generator,
                    invocation_arguments=invocation_arguments,
                    pipeline_model=pipeline_model,
                )
            executions_list.append(dict(standard_execution.report))
            continue
        with _sample_evidence_phase(
            evidence_run=attach_run(),
            sample_id=sample_id,
            variant=variant,
            phase="host-e2e",
            scratch=scratch,
            flavor_selectors=flavor_selectors,
            pipeline_model=pipeline_model,
            source_generator=source_generator,
        ) as phase_scratch:
            executions_list.append(
                _run_host_e2e(
                    sample_root,
                    phase_scratch,
                    sample_id,
                    oracle_reference,
                    definition,
                    loaded,
                    _SampleInputClosures(
                        input_closures.generation.fork(),
                        input_closures.verifier.fork(),
                    ),
                    variant=variant,
                    source_generator=source_generator,
                    object_root=object_root,
                    cpp_toolchain=cpp_toolchain,
                    allow_host_execution=allow_host_execution,
                    pipeline_model=pipeline_model,
                )
            )
    executions = tuple(executions_list)
    _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    assertions = handler(sample_root, scratch, executions)
    _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    expected = tuple(metadata["expected_assertions"])
    if assertions != expected:
        raise SampleFailure(f"assertion fixture drift: {metadata['sample_id']}")
    result = {
        "sample_id": metadata["sample_id"],
        "component_identity": definition.identity.uri,
        "proof_level": metadata["proof_level"],
        "assertions": list(assertions),
        "expected_failures": metadata["expected_failures"],
        "executions": list(executions),
        "flavor_matrix": [
            {
                "languages": [
                    _LANGUAGE_FLAVOR_COORDINATES[language]
                    for language in variant.languages
                ],
                "build_system": next(
                    (
                        _BUILD_FLAVOR_COORDINATES[variant.value_for(slot.slot_id)]
                        for slot in definition.flavor_slots
                        if slot.axis is FlavorAxis.BUILD_SYSTEM
                    ),
                    _BUILD_FLAVOR_COORDINATES["make"],
                ),
                "operating_system": _OS_FLAVOR_COORDINATES[_host_os()],
            }
            for variant in variants
        ],
        "passed": True,
    }
    return result


SAMPLE_CHECKPOINT_SCHEMA = "literate-ai/samples-checkpoint@1"
SAMPLE_CHECKPOINT_FILE = "samples-checkpoint.json"
SAMPLE_CHECKPOINT_DISABLE_ENVIRONMENT = "LITAI_SAMPLES_CHECKPOINT"


def _sample_tree_identity(sample: Path) -> str:
    """Digest one sample's complete authored specification tree."""

    digest = hashlib.sha256()
    for path in sorted(item for item in sample.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(sample).as_posix()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def _sample_checkpoint_key(
    sample: Path,
    *,
    flavor_selectors: Sequence[str],
    native_packages: bool,
    project_revision: str,
    pipeline_model: str | None = None,
) -> str:
    """Bind every input whose change must invalidate a recorded pass.

    ``project_revision`` is the lifecycle driver's trusted implementation digest, so any
    change to the framework code that decides sample behavior discards the whole
    checkpoint. That is stricter than strictly necessary, and intentionally so: a
    checkpoint whose invalidation is too loose reports a stale result as a current pass,
    which is worse than regenerating.
    """

    return canonical_identity(
        {
            "schema": SAMPLE_CHECKPOINT_SCHEMA,
            "sample": sample.name,
            "tree": _sample_tree_identity(sample),
            "flavor_selectors": list(flavor_selectors),
            "native_packages": native_packages,
            "platform": _host_os(),
            "project_revision": project_revision,
            "pipeline_model": pipeline_model,
        }
    ).uri


def _sample_checkpoint_framework_revision(project_root: Path) -> str:
    """Identify the framework code that decides how a sample behaves.

    This is deliberately the lifecycle driver's own trusted implementation digest and
    not the validated project authority identity: the samples gate must not acquire a
    dependency on documentation-authority freshness, which is a separate gate's concern.
    Returns an empty string when the digest cannot be computed, which disables the
    checkpoint rather than guessing at invalidation.
    """

    try:
        project = discover_project(project_root)
        driver = project.definition.lifecycle_driver
        if driver is None:
            return ""
        return lifecycle_driver_implementation_identity(project, driver).uri
    except Exception:  # pragma: no cover - a diagnostic aid never fails the run
        return ""


def _load_sample_checkpoint(path: Path) -> dict[str, Any]:
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(recorded, dict)
        or recorded.get("schema") != SAMPLE_CHECKPOINT_SCHEMA
        or not isinstance(recorded.get("entries"), dict)
    ):
        return {}
    return {
        key: value
        for key, value in recorded["entries"].items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _store_sample_checkpoint(path: Path, entries: Mapping[str, Any]) -> None:
    """Persist recorded passes; a diagnostic aid must never fail the run."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"schema": SAMPLE_CHECKPOINT_SCHEMA, "entries": dict(entries)},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        return


def _standard_execution_reports(
    execution: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Expand a direct Standard run or the validated two-root portfolio envelope."""

    schema = execution.get("schema")
    if schema == STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA:
        return (execution,)
    if schema != DURABLE_SPLIT_SERVICE_EXECUTION_REPORT_SCHEMA:
        return ()
    if execution.get("proof_status") != "passed":
        raise SampleFailure("durable split report does not contain a passing proof")
    roots: list[Mapping[str, object]] = []
    for field in ("frontend_root", "collector_root"):
        root = execution.get(field)
        if (
            not isinstance(root, Mapping)
            or root.get("schema") != STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA
            or root.get("passed") is not True
            or not isinstance(root.get("component_lock_identity"), str)
        ):
            raise SampleFailure("durable split report omitted a passing Standard root")
        roots.append(root)
    declared = execution.get("component_lock_identities")
    nested = sorted(str(root["component_lock_identity"]) for root in roots)
    if (
        not isinstance(declared, list)
        or any(not isinstance(value, str) for value in declared)
        or sorted(declared) != nested
    ):
        raise SampleFailure("durable split report has inconsistent Component locks")
    return tuple(roots)


def run_all(
    root: Path,
    scratch_root: Path,
    *,
    source_generator=None,
    allow_host_execution: bool,
    sample_patterns: Sequence[str] = ("*",),
    force_regeneration: bool = False,
    jobs: int | None = None,
    flavor_selectors: Sequence[str] = (),
    native_packages: bool = False,
    pipeline_model: str | None = None,
) -> dict[str, Any]:
    if not allow_host_execution:
        raise SampleFailure(
            "host build and execution require the explicit "
            "--allow-host-execution acknowledgement"
        )
    evidence_run = attach_run()
    configured_scratch_root = _external_runtime_root(scratch_root)
    project_root = Path(__file__).resolve().parents[3]
    directories = resolve_cache_directories(project_root)
    object_root: Path | None = None
    standard_generator = None
    if source_generator is None:
        generator = CachedCodingCliSourceGenerator(
            CodingCliSourceGenerator(),
            cache_root=directories.build_dir,
            project_root=project_root,
            force_regeneration=force_regeneration,
        )
        standard_generator = CachedCodingCliSourceGenerator(
            CodingCliSourceGenerator(source_intelligence_mode="off"),
            cache_root=directories.build_dir,
            project_root=project_root,
            force_regeneration=force_regeneration,
        )
        object_root = directories.obj_dir
    else:
        generator = source_generator
    samples = discover(root, sample_patterns, platform=_host_os())
    worker_count = min(len(samples), 4 if jobs is None else jobs)
    if type(worker_count) is not int or worker_count < 1:
        raise SampleFailure("sample worker count must be a positive integer")

    checkpoint_enabled = (
        object_root is not None
        and not force_regeneration
        and os.environ.get(SAMPLE_CHECKPOINT_DISABLE_ENVIRONMENT, "").strip().lower()
        not in {"off", "0", "false"}
    )
    checkpoint_path = (
        (object_root / SAMPLE_CHECKPOINT_FILE)
        if checkpoint_enabled and object_root is not None
        else None
    )
    project_revision = (
        _sample_checkpoint_framework_revision(project_root)
        if checkpoint_path is not None
        else ""
    )
    if not project_revision:
        # Without an exact framework revision there is no sound invalidation rule, so
        # run every sample rather than risk reporting a stale pass as a current one.
        checkpoint_path = None
    sample_records = _load_sample_checkpoint(checkpoint_path) if checkpoint_path else {}
    checkpoint_lock = threading.Lock()

    def execute(sample: Path) -> dict[str, Any]:
        key = (
            _sample_checkpoint_key(
                sample,
                flavor_selectors=flavor_selectors,
                native_packages=native_packages,
                project_revision=project_revision,
                pipeline_model=pipeline_model,
            )
            if checkpoint_path is not None
            else None
        )
        if key is not None and key in sample_records:
            # A recorded entry carries the sample's complete case, not merely a pass
            # flag: the receipt aggregates evidence from every sample, so a skipped
            # sample must still contribute exactly what it contributed when it ran.
            print(
                f"sample: {sample.name} skipped (checkpointed pass)",
                file=sys.stderr,
                flush=True,
            )
            return json.loads(json.dumps(sample_records[key]))
        # Identify which sample is in flight to stderr regardless of how it fails:
        # some failure paths (an unwrapped GenerationFailure from the host lifecycle,
        # not this module's own SampleFailure) previously propagated with no sample
        # identity in the traceback at all, making a multi-sample run's failure
        # impossible to attribute without re-running under a debugger.
        print(f"sample: {sample.name} starting", file=sys.stderr, flush=True)
        sample_scratch = configured_scratch_root / sample.name
        sample_scratch.mkdir(parents=True, exist_ok=True)
        try:
            case = run_sample(
                sample,
                sample_scratch,
                source_generator=generator,
                standard_source_generator=standard_generator,
                cpp_toolchain=None,
                object_root=object_root,
                allow_host_execution=allow_host_execution,
                flavor_selectors=flavor_selectors,
                native_packages=native_packages,
                pipeline_model=pipeline_model,
            )
        except BaseException as exc:
            retained = sample_scratch
            if evidence_run is not None:
                try:
                    evidence_nodes = evidence_run.reduced().get("nodes", ())
                    has_sample_scratch = any(
                        isinstance(item, Mapping)
                        and str(item.get("path", "")).startswith(
                            f"samples/{sample.name}/"
                        )
                        and any(
                            isinstance(output, Mapping)
                            and output.get("role") == "scratch"
                            for output in item.get("outputs", ())
                        )
                        for item in evidence_nodes
                    )
                except Exception:
                    has_sample_scratch = False
                if not has_sample_scratch:
                    record_retained_output(
                        retained,
                        node_path=f"samples/{sample.name}/failure",
                        operation="sample.conformance.failure",
                        role="scratch",
                        failed=True,
                    )
            else:
                report_progress(f"Retained sample scratch: {retained.resolve()}")
            print(
                f"sample: {sample.name} failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            raise
        else:
            shutil.rmtree(sample_scratch, ignore_errors=True)
        print(f"sample: {sample.name} passed", file=sys.stderr, flush=True)
        if key is not None and checkpoint_path is not None:
            with checkpoint_lock:
                # Snapshot before the caller strips its private `_operational_*` keys,
                # so a resumed case is byte-identical to the one this run produced.
                sample_records[key] = json.loads(json.dumps(case))
                _store_sample_checkpoint(checkpoint_path, sample_records)
        return case

    if worker_count == 1:
        cases = tuple(execute(sample) for sample in samples)
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="litai-sample"
        ) as executor:
            cases = tuple(executor.map(execute, samples))
    if checkpoint_path is not None:
        # A complete matrix pass is the natural reset point; the next run must prove
        # every sample again rather than inheriting this run's recorded state.
        checkpoint_path.unlink(missing_ok=True)
    build_cache_reports: list[dict[str, object]] = []
    adoption_proofs: list[dict[str, object]] = []
    for item in cases:
        raw_proofs = item.pop("_operational_adoption_proofs", None)
        if isinstance(raw_proofs, list):
            adoption_proofs.extend(
                proof for proof in raw_proofs if isinstance(proof, dict)
            )
        for execution in item["executions"]:
            raw_cache = execution.pop("_operational_build_cache", None)
            if isinstance(raw_cache, dict):
                build_cache_reports.append(raw_cache)
    execution_metrics = []

    def append_standard_metrics(execution: Mapping[str, object]) -> None:
        nodes = execution.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise SampleFailure("Standard sample report omitted node evidence")
        generated_count = int(execution["generated_test_count"])
        independent_count = int(execution["independent_acceptance_case_count"])
        execution_metrics.append(
            (
                generated_count + independent_count,
                generated_count,
                tuple(str(node["source_sbom_identity"]) for node in nodes),
                tuple(str(node["resolved_sbom_identity"]) for node in nodes),
            )
        )

    for item in cases:
        for execution in item["executions"]:
            standard_reports = _standard_execution_reports(execution)
            if standard_reports:
                for standard_report in standard_reports:
                    append_standard_metrics(standard_report)
            else:
                execution_metrics.append(
                    (
                        int(execution["execution_case_count"]),
                        int(execution["generated_test_case_count"]),
                        (str(execution["source_sbom_identity"]),),
                        (str(execution["resolved_sbom_identity"]),),
                    )
                )
    invocation_count = sum(item[0] for item in execution_metrics)
    generated_test_count = sum(item[1] for item in execution_metrics)
    assertion_count = sum(len(item["assertions"]) for item in cases)
    source_sbom_members = sorted(
        identity for item in execution_metrics for identity in item[2]
    )
    resolved_sbom_members = sorted(
        identity for item in execution_metrics for identity in item[3]
    )
    source_cache_report = getattr(generator, "report", None)
    return {
        "schema": REPORT_SCHEMA,
        "passed": all(item["passed"] for item in cases),
        "recipe_count": sum(len(item["executions"]) for item in cases),
        "generated_test_count": generated_test_count,
        "verifier_test_count": invocation_count - generated_test_count,
        "assertion_count": assertion_count,
        "test_count": invocation_count + assertion_count,
        "invocation_count": invocation_count,
        "source_sbom_members": source_sbom_members,
        "resolved_sbom_members": resolved_sbom_members,
        "source_sbom_identity": canonical_identity(
            {
                "schema": "literate-ai/cyclonedx-sbom-set@1",
                "lifecycle": "pre-build",
                "members": source_sbom_members,
            }
        ).uri,
        "resolved_sbom_identity": canonical_identity(
            {
                "schema": "literate-ai/cyclonedx-sbom-set@1",
                "lifecycle": "post-build",
                "members": resolved_sbom_members,
            }
        ).uri,
        "operational_metrics": {
            "schema": "literate-ai/conformance-operational-metrics@1",
            "generated_source_cache": (
                source_cache_report() if callable(source_cache_report) else None
            ),
            "build_caches": build_cache_reports,
            "standard_adoption_proofs": adoption_proofs,
        },
        "samples": list(cases),
    }


def _execution_cache_key(
    execution: Mapping[str, object], execution_index: int
) -> SourceDerivationCacheKey:
    bound = execution.get("_receipt_cache_key")
    if isinstance(bound, SourceDerivationCacheKey):
        return bound

    def execution_identity(field: str) -> ContentIdentity:
        value = execution.get(field)
        if not isinstance(value, str):
            raise SampleFailure(
                f"conformance execution[{execution_index}] omits {field} identity"
            )
        try:
            return ContentIdentity.parse_uri(value)
        except ValueError as exc:
            raise SampleFailure(
                f"conformance execution[{execution_index}] has invalid {field} identity"
            ) from exc

    coding_cli = execution.get("coding_cli")
    coding_model = execution.get("coding_model")
    if not isinstance(coding_cli, str) or not isinstance(coding_model, str):
        raise SampleFailure(
            f"conformance execution[{execution_index}] omits its coding model binding"
        )
    return SourceDerivationCacheKey(
        recipe_identity=execution_identity("recipe_identity"),
        execution_plan_identity=execution_identity("execution_plan_identity"),
        coding_cli_tool_binding_identity=execution_identity(
            "coding_cli_tool_binding_identity"
        ),
        model_binding=SourceCacheModelBinding(coding_cli, coding_model),
        request_identity=execution_identity("request_identity"),
    )


def _outer_bound_execution_cache_key(
    execution: Mapping[str, object],
    execution_index: int,
    source_cache_control: RebuildSourceCacheControl,
) -> SourceDerivationCacheKey:
    """Bind legacy execution evidence to its immutable outer derivation key.

    Candidate repair deliberately changes the coding-agent prompt by adding the
    prior rejection chain.  That accepted-attempt request remains provenance,
    but it cannot replace the derivation key that the outer CLI fixed before any
    generation began.  Match on the stable planned axes and permit the request
    identity to differ only for an explicit, complete retry chain.
    """

    observed = _execution_cache_key(execution, execution_index)
    recipe_matches = tuple(
        key
        for key in source_cache_control.derivation_manifest.cache_keys
        if key.recipe_identity == observed.recipe_identity
    )
    matches = tuple(
        key
        for key in recipe_matches
        if key.execution_plan_identity == observed.execution_plan_identity
        and key.coding_cli_tool_binding_identity
        == observed.coding_cli_tool_binding_identity
        and key.model_binding == observed.model_binding
        and key.source_semantics_identity == observed.source_semantics_identity
    )
    if len(matches) != 1:
        if recipe_matches and not matches:
            raise SampleFailure(
                f"conformance execution[{execution_index}] changed a stable outer "
                "derivation-key input"
            )
        raise SampleFailure(
            f"conformance execution[{execution_index}] cannot uniquely bind its "
            "outer derivation key"
        )
    planned = matches[0]
    if planned.request_identity != observed.request_identity:
        attempt_count = execution.get("generation_candidate_attempt_count")
        rejections = execution.get("rejected_generation_candidates")
        if (
            type(attempt_count) is not int
            or attempt_count < 2
            or not isinstance(rejections, list)
            or len(rejections) != attempt_count - 1
            or any(
                not isinstance(rejection, Mapping)
                or rejection.get("schema")
                != "literate-ai/generated-candidate-rejection@1"
                or rejection.get("attempt") != attempt
                for attempt, rejection in enumerate(rejections, start=1)
            )
        ):
            raise SampleFailure(
                f"conformance execution[{execution_index}] changed its planned "
                "request identity without an exact candidate retry chain"
            )
    return planned


def _report_component_lock_identities(
    report: Mapping[str, object],
) -> tuple[ContentIdentity, ...]:
    values: set[ContentIdentity] = set()
    samples = report.get("samples")
    if not isinstance(samples, list):
        raise SampleFailure("conformance report omits sample results")
    for sample in samples:
        executions = sample.get("executions") if isinstance(sample, Mapping) else None
        if not isinstance(executions, list):
            raise SampleFailure("conformance sample omits execution results")
        for execution in executions:
            if not isinstance(execution, Mapping):
                raise SampleFailure("conformance execution is not an object")
            standard_reports = _standard_execution_reports(execution)
            lock_reports = standard_reports or (execution,)
            for lock_report in lock_reports:
                value = lock_report.get("component_lock_identity")
                if not isinstance(value, str):
                    raise SampleFailure(
                        "conformance execution omits its Component lock"
                    )
                try:
                    values.add(ContentIdentity.parse_uri(value))
                except ValueError as exc:
                    raise SampleFailure(
                        "conformance execution has an invalid Component lock"
                    ) from exc
    return tuple(sorted(values, key=lambda identity: identity.uri))


def _receipt_source_intelligence(
    execution: Mapping[str, object],
    *,
    artifact_field: str,
    status_field: str,
    reason_field: str,
    label: str,
) -> tuple[str, str | None, SourceIntelligenceArtifact | None]:
    status = execution.get(status_field)
    if not isinstance(status, str) or status not in {
        "current",
        "off",
        "unavailable",
    }:
        raise SampleFailure(
            f"conformance {label} observed source-intelligence status "
            f"{status!r}; required 'current', 'off', or 'unavailable'"
        )
    reason_code = execution.get(reason_field)
    if reason_code is not None and not isinstance(reason_code, str):
        raise SampleFailure(f"conformance {label} has an invalid reason code")
    raw_artifact = execution.get(artifact_field)
    if status == "current":
        if not isinstance(raw_artifact, Mapping):
            raise SampleFailure(
                f"conformance receipt requires {label} source intelligence"
            )
        try:
            artifact = SourceIntelligenceArtifact.from_dict(raw_artifact)
        except (TypeError, ValueError) as exc:
            raise SampleFailure(
                f"conformance receipt contains invalid {label} source intelligence"
            ) from exc
        return status, reason_code, artifact
    if raw_artifact is not None:
        raise SampleFailure(
            f"conformance {label} source-intelligence status '{status}' "
            "cannot carry an artifact"
        )
    if status == "unavailable" and not reason_code:
        raise SampleFailure(
            f"conformance {label} source-intelligence status "
            "'unavailable' requires a reason code"
        )
    return status, reason_code, None


def _source_intelligence_status_identity(
    status: str, reason_code: str | None
) -> ContentIdentity:
    return canonical_identity(
        {
            "schema": "literate-ai/source-intelligence-status@1",
            "status": status,
            "reason_code": reason_code,
        }
    )


def _standard_receipt_executions(
    execution: Mapping[str, object],
    *,
    source_cache_control: RebuildSourceCacheControl | None,
) -> tuple[dict[str, object], ...]:
    """Expand one typed Standard report into exact derivation receipt members."""

    if execution.get("schema") != STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA:
        return ()
    if source_cache_control is None:
        raise SampleFailure(
            "Standard sample receipts require the outer derivation manifest"
        )
    nodes = execution.get("nodes")
    component_lock_identity = execution.get("component_lock_identity")
    variant_id = execution.get("variant_id")
    if (
        not isinstance(nodes, list)
        or not nodes
        or not isinstance(component_lock_identity, str)
        or not isinstance(variant_id, str)
    ):
        raise SampleFailure("Standard sample report omits typed receipt members")
    keys_by_recipe: dict[str, list[SourceDerivationCacheKey]] = {}
    for key in source_cache_control.derivation_manifest.cache_keys:
        keys_by_recipe.setdefault(key.recipe_identity.uri, []).append(key)
    result: list[dict[str, object]] = []
    required = (
        "recipe_identity",
        "source_tree_identity",
        "source_index_identity",
        "build_evidence_identity",
        "generated_test_evidence_identity",
        "acceptance_evidence_identity",
        "workspace_admission_identity",
        "provenance_evidence_identity",
        "source_sbom_identity",
        "resolved_sbom_identity",
    )
    for index, raw_node in enumerate(nodes):
        if not isinstance(raw_node, Mapping):
            raise SampleFailure(
                f"Standard sample receipt node[{index}] is not an object"
            )
        if any(not isinstance(raw_node.get(field), str) for field in required):
            raise SampleFailure(
                f"Standard sample receipt node[{index}] omits typed evidence"
            )
        recipe_identity = str(raw_node["recipe_identity"])
        matches = keys_by_recipe.get(recipe_identity, [])
        if len(matches) != 1:
            raise SampleFailure(
                "Standard sample receipt cannot uniquely bind its derivation key"
            )
        result.append(
            {
                "passed": True,
                "_standard_receipt_member": True,
                "_receipt_cache_key": matches[0],
                "execution_variant": variant_id,
                "component_lock_identity": component_lock_identity,
                **{field: raw_node[field] for field in required},
            }
        )
    return tuple(result)


def project_test_receipt(
    report: Mapping[str, object],
    project_definition: ProjectDefinition,
    project_revision_identity: ContentIdentity,
    *,
    lifecycle_request_identity: ContentIdentity | None = None,
    lifecycle_command_identity: ContentIdentity | None = None,
    source_cache_control: RebuildSourceCacheControl | None = None,
    source_cache_decision: RebuildSourceCacheDecision | None = None,
    source_cache_lifecycle_path: Path | None = None,
) -> ProjectTestReceipt:
    """Collapse one complete passing report into a tiny content-addressed receipt."""

    policy = project_definition.test_receipt_policy
    if policy is None:
        raise SampleFailure(
            "project test receipt policy is required before a candidate can be produced"
        )
    if project_definition.source_cache is not None:
        raise SampleFailure(
            "sample runner cannot receipt a configured source cache until its exact "
            "resolver decision is wired into every execution"
        )
    runner_identity = sample_test_runner_identity()
    if runner_identity != policy.runner_identity:
        raise SampleFailure(
            "sample test runner does not match the identity authorized by "
            "project policy"
        )
    if report.get("schema") != REPORT_SCHEMA or report.get("passed") is not True:
        raise SampleFailure(
            "only a complete passing conformance report can be receipted"
        )
    samples = report.get("samples")
    if not isinstance(samples, list) or not samples:
        raise SampleFailure("conformance receipt requires non-empty sample results")
    executions: list[Mapping[str, object]] = []
    suite_members: list[dict[str, object]] = []
    subject_members: list[dict[str, object]] = []
    source_intelligence_members: list[dict[str, object]] = []
    for sample in samples:
        if not isinstance(sample, Mapping) or sample.get("passed") is not True:
            raise SampleFailure("conformance receipt cannot include a failing sample")
        sample_executions = sample.get("executions")
        if not isinstance(sample_executions, list) or not sample_executions:
            raise SampleFailure("conformance receipt requires sample executions")
        for execution in sample_executions:
            if not isinstance(execution, Mapping):
                raise SampleFailure("conformance receipt cannot include a failed run")
            standard_reports = _standard_execution_reports(execution)
            if standard_reports:
                if execution.get("schema") == STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA:
                    if execution.get("passed") is not True:
                        raise SampleFailure(
                            "conformance receipt cannot include a failed run"
                        )
                for standard_report in standard_reports:
                    standard_members = _standard_receipt_executions(
                        standard_report, source_cache_control=source_cache_control
                    )
                    standard_sample = standard_report.get("sample_id")
                    if not isinstance(standard_sample, str):
                        standard_sample = sample.get("sample_id")
                    for member in standard_members:
                        executions.append(member)
                        source_intelligence_members.append(
                            {
                                "sample": standard_sample,
                                "variant": member["execution_variant"],
                                "intelligence": member["source_index_identity"],
                                "tree": member["source_tree_identity"],
                                "lifecycle": "standard",
                            }
                        )
                        suite_members.append(
                            {
                                "sample": standard_sample,
                                "variant": member["execution_variant"],
                                "generated": member["generated_test_evidence_identity"],
                                "acceptance": member["acceptance_evidence_identity"],
                                "assertions": sample.get("assertions"),
                                "lifecycle": "standard",
                            }
                        )
                        subject_members.append(
                            {
                                "sample": standard_sample,
                                "variant": member["execution_variant"],
                                "recipe": member["recipe_identity"],
                                "tree": member["source_tree_identity"],
                                "artifact": member["build_evidence_identity"],
                                "lifecycle": "standard",
                            }
                        )
                continue
            if execution.get("passed") is not True:
                raise SampleFailure("conformance receipt cannot include a failed run")
            executions.append(execution)
            (
                generated_source_intelligence_status,
                generated_source_intelligence_reason_code,
                source_intelligence,
            ) = _receipt_source_intelligence(
                execution,
                artifact_field="generated_source_intelligence",
                status_field="source_intelligence_status",
                reason_field="source_intelligence_reason_code",
                label="generated",
            )
            workspace_tree_identity = execution.get("workspace_tree_digest")
            if source_intelligence is not None:
                if source_intelligence.source_tree_identity != workspace_tree_identity:
                    raise SampleFailure(
                        "conformance source intelligence names another workspace tree"
                    )
            (
                accepted_source_intelligence_status,
                accepted_source_intelligence_reason_code,
                accepted_source_intelligence,
            ) = _receipt_source_intelligence(
                execution,
                artifact_field="accepted_source_intelligence",
                status_field="accepted_source_intelligence_status",
                reason_field="accepted_source_intelligence_reason_code",
                label="accepted",
            )
            if accepted_source_intelligence is not None:
                if (
                    accepted_source_intelligence.source_tree_identity
                    != workspace_tree_identity
                ):
                    raise SampleFailure(
                        "accepted source intelligence names another workspace tree"
                    )
            source_intelligence_members.append(
                {
                    "sample": sample.get("sample_id"),
                    "variant": execution.get("execution_variant"),
                    "generated_status": generated_source_intelligence_status,
                    "generated_reason_code": (
                        generated_source_intelligence_reason_code
                    ),
                    "generated_intelligence": (
                        None
                        if source_intelligence is None
                        else source_intelligence.intelligence_identity
                    ),
                    "generated_binding": (
                        None
                        if source_intelligence is None
                        else source_intelligence.artifact_binding
                    ),
                    "generated_artifact": (
                        None
                        if source_intelligence is None
                        else source_intelligence.artifact_identity
                    ),
                    "accepted_status": accepted_source_intelligence_status,
                    "accepted_reason_code": accepted_source_intelligence_reason_code,
                    "accepted_intelligence": (
                        None
                        if accepted_source_intelligence is None
                        else accepted_source_intelligence.intelligence_identity
                    ),
                    "accepted_binding": (
                        None
                        if accepted_source_intelligence is None
                        else accepted_source_intelligence.artifact_binding
                    ),
                    "accepted_artifact": (
                        None
                        if accepted_source_intelligence is None
                        else accepted_source_intelligence.artifact_identity
                    ),
                    "tree": (
                        None
                        if source_intelligence is None
                        else source_intelligence.source_tree_identity
                    ),
                    "snapshot": (
                        None
                        if source_intelligence is None
                        else source_intelligence.source_snapshot_identity
                    ),
                }
            )
            suite_members.append(
                {
                    "sample": sample.get("sample_id"),
                    "variant": execution.get("execution_variant"),
                    "generated": execution.get("generated_test_suite_identity"),
                    "acceptance": execution.get("acceptance_contract_identity"),
                    "oracle": execution.get("acceptance_oracle_identity"),
                    "probe": execution.get("post_build_runtime_probe_case_id"),
                    "assertions": sample.get("assertions"),
                }
            )
            generation_inputs = execution.get("generation_input_closure")
            verifier_inputs = execution.get("verifier_input_closure")
            subject_members.append(
                {
                    "sample": sample.get("sample_id"),
                    "variant": execution.get("execution_variant"),
                    "recipe": execution.get("recipe_identity"),
                    "tree": execution.get("accepted_tree_identity"),
                    "artifact": execution.get("build_artifact_identity"),
                    "generation_inputs": (
                        generation_inputs.get("identity")
                        if isinstance(generation_inputs, Mapping)
                        else None
                    ),
                    "verifier_inputs": (
                        verifier_inputs.get("identity")
                        if isinstance(verifier_inputs, Mapping)
                        else None
                    ),
                }
            )
    total = report.get("test_count")
    if type(total) is not int or total < 1:
        raise SampleFailure("conformance report has an invalid test count")
    if total < policy.minimum_test_count:
        raise SampleFailure(
            "conformance report contains fewer tests than project policy requires"
        )
    suite_identity = canonical_identity(
        {"schema": "literate-ai/sample-test-suite@1", "members": suite_members}
    )
    subject_identity = canonical_identity(
        {
            "schema": "literate-ai/sample-test-subject@1",
            "project": project_revision_identity.uri,
            "members": subject_members,
        }
    )
    authoritative_report = dict(report)
    authoritative_report.pop("operational_metrics", None)
    result_identity = canonical_identity(authoritative_report)
    if lifecycle_request_identity is None:
        raise SampleFailure(
            "conformance receipt requires an exact lifecycle request identity"
        )
    planned_cache_keys = tuple(
        sorted(
            (
                _execution_cache_key(execution, index)
                for index, execution in enumerate(executions)
            ),
            key=lambda key: key.identity.uri,
        )
    )
    component_lock_identities = tuple(
        sorted(
            {
                ContentIdentity.parse_uri(str(item["component_lock_identity"]))
                for item in executions
            },
            key=lambda identity: identity.uri,
        )
    )
    if source_cache_control is None:
        lifecycle_plan_identity = canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                "component_lock_identities": [
                    identity.uri for identity in component_lock_identities
                ],
                "cache_key_identities": [
                    key.identity.uri for key in planned_cache_keys
                ],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=canonical_identity(
                {
                    "schema": "literate-ai/direct-receipt-planning-request@1",
                    "lifecycle_request_identity": lifecycle_request_identity.uri,
                    "lifecycle_command_identity": (
                        None
                        if lifecycle_command_identity is None
                        else lifecycle_command_identity.uri
                    ),
                }
            ),
            project_revision_identity=project_revision_identity,
            lifecycle_driver_identity=runner_identity,
            lifecycle_plan_identity=lifecycle_plan_identity,
            component_lock_identities=component_lock_identities,
            cache_keys=planned_cache_keys,
        )
        source_cache_control = RebuildSourceCacheControl(
            lifecycle_request_identity=lifecycle_request_identity,
            project_revision_identity=project_revision_identity,
            configuration=None,
            derivation_manifest=manifest,
            component_lock_identities=component_lock_identities,
        )
    if source_cache_decision is None:
        source_cache_decision = RebuildSourceCacheDecision.fresh(source_cache_control)
    if (
        source_cache_control.lifecycle_request_identity != lifecycle_request_identity
        or source_cache_control.project_revision_identity != project_revision_identity
        or source_cache_control.configuration != project_definition.source_cache
        or source_cache_control.component_lock_identities != component_lock_identities
    ):
        raise SampleFailure("source-cache control does not bind this lifecycle")
    try:
        source_cache_decision.validate_against(source_cache_control)
    except (TypeError, ValueError) as exc:
        raise SampleFailure("source-cache decision is invalid") from exc
    lifecycle_plan_identity = (
        source_cache_control.derivation_manifest.lifecycle_plan_identity
    )
    lifecycle_members: list[RebuildSourceCacheLifecycleMember] = []
    for index, item in enumerate(executions):

        def execution_identity(
            field: str,
            execution: Mapping[str, object] = item,
            execution_index: int = index,
        ) -> ContentIdentity:
            value = execution.get(field)
            if not isinstance(value, str):
                raise SampleFailure(
                    f"conformance execution[{execution_index}] omits {field} identity"
                )
            try:
                return ContentIdentity.parse_uri(value)
            except ValueError as exc:
                raise SampleFailure(
                    f"conformance execution[{execution_index}] has invalid {field} "
                    "identity"
                ) from exc

        cache_key = _outer_bound_execution_cache_key(
            item,
            index,
            source_cache_control,
        )
        if item.get("_standard_receipt_member") is True:
            lifecycle_members.append(
                RebuildSourceCacheLifecycleMember(
                    cache_key=cache_key,
                    accepted_entry_identity=None,
                    source_tree_identity=execution_identity("source_tree_identity"),
                    source_intelligence_identity=execution_identity(
                        "source_index_identity"
                    ),
                    build_evidence_identity=execution_identity(
                        "build_evidence_identity"
                    ),
                    test_evidence_identity=execution_identity(
                        "generated_test_evidence_identity"
                    ),
                    acceptance_evidence_identity=execution_identity(
                        "acceptance_evidence_identity"
                    ),
                    workspace_admission_identity=execution_identity(
                        "workspace_admission_identity"
                    ),
                    provenance_evidence_identity=execution_identity(
                        "provenance_evidence_identity"
                    ),
                    source_sbom_identity=execution_identity("source_sbom_identity"),
                    resolved_sbom_identity=execution_identity("resolved_sbom_identity"),
                )
            )
            continue
        (
            accepted_source_intelligence_status,
            accepted_source_intelligence_reason_code,
            accepted_intelligence,
        ) = _receipt_source_intelligence(
            item,
            artifact_field="accepted_source_intelligence",
            status_field="accepted_source_intelligence_status",
            reason_field="accepted_source_intelligence_reason_code",
            label=f"execution[{index}] accepted",
        )
        lifecycle_members.append(
            RebuildSourceCacheLifecycleMember(
                cache_key=cache_key,
                accepted_entry_identity=None,
                source_tree_identity=execution_identity("accepted_tree_identity"),
                source_intelligence_identity=(
                    _source_intelligence_status_identity(
                        accepted_source_intelligence_status,
                        accepted_source_intelligence_reason_code,
                    )
                    if accepted_intelligence is None
                    else ContentIdentity.parse_uri(
                        accepted_intelligence.intelligence_identity
                    )
                ),
                build_evidence_identity=canonical_identity(
                    {
                        "schema": "literate-ai/sample-build-member@1",
                        "artifact": item.get("build_artifact_identity"),
                        "toolchain": item.get("build_toolchain_identity"),
                    }
                ),
                test_evidence_identity=canonical_identity(
                    {
                        "schema": "literate-ai/sample-test-member@1",
                        "suite": item.get("generated_test_suite_identity"),
                        "cases": item.get("execution_cases"),
                    }
                ),
                acceptance_evidence_identity=canonical_identity(
                    {
                        "schema": "literate-ai/sample-acceptance-member@1",
                        "contract": item.get("acceptance_contract_identity"),
                        "oracle": item.get("acceptance_oracle_identity"),
                        "probe": item.get("post_build_runtime_probe_case_id"),
                        "cases": item.get("execution_cases"),
                    }
                ),
                workspace_admission_identity=canonical_identity(
                    {
                        "schema": "literate-ai/sample-workspace-member@1",
                        "tree": item.get("accepted_tree_identity"),
                        "workspace": item.get("workspace_tree_digest"),
                        "source_intelligence": (
                            None
                            if accepted_intelligence is None
                            else accepted_intelligence.to_dict()
                        ),
                        "source_intelligence_status": (
                            accepted_source_intelligence_status
                        ),
                        "source_intelligence_reason_code": (
                            accepted_source_intelligence_reason_code
                        ),
                    }
                ),
                provenance_evidence_identity=canonical_identity(
                    {
                        "schema": "literate-ai/sample-provenance-member@1",
                        "recipe": item.get("recipe_identity"),
                        "request": item.get("request_identity"),
                        "tree": item.get("accepted_tree_identity"),
                        "suite": item.get("generated_test_suite_identity"),
                    }
                ),
                source_sbom_identity=execution_identity("source_sbom_identity"),
                resolved_sbom_identity=execution_identity("resolved_sbom_identity"),
            )
        )
    lifecycle_members.sort(key=lambda member: member.cache_key.identity.uri)
    lifecycle = RebuildSourceCacheLifecycleBinding(
        control_identity=source_cache_control.identity,
        decision_identity=source_cache_decision.identity,
        lifecycle_request_identity=lifecycle_request_identity,
        lifecycle_plan_identity=lifecycle_plan_identity,
        receipt_subject_identity=subject_identity,
        component_lock_identities=component_lock_identities,
        derivation_key_identities=tuple(
            member.cache_key.identity for member in lifecycle_members
        ),
        members=tuple(lifecycle_members),
    )
    evidence_values = {
        "lifecycle-plan": lifecycle_plan_identity,
        "source-cache-decision": source_cache_decision.identity,
        "source-cache-lifecycle": lifecycle.identity,
        **{
            kind: lifecycle.evidence_identity(kind) for kind in lifecycle.EVIDENCE_KINDS
        },
        "observation-result": canonical_identity(
            {
                "executions": [
                    {
                        "artifact_tree": item.get("execution_artifact_tree_identity"),
                        "cases": item.get("execution_cases"),
                    }
                    for item in executions
                ],
                "source_intelligence": source_intelligence_members,
            }
        ),
        "security-scan-report": canonical_identity(
            [
                {
                    "classification": item.get("source_security_classification_digest"),
                    "build_authorization": item.get("build_authorization_id"),
                    "security_profile": item.get("build_security_profile"),
                }
                for item in executions
            ]
        ),
        PROJECT_TEST_RUNNER_EVIDENCE_KIND: runner_identity,
    }
    if lifecycle_request_identity is not None:
        evidence_values["lifecycle-request"] = lifecycle_request_identity
    if lifecycle_command_identity is not None:
        evidence_values["lifecycle-command"] = lifecycle_command_identity
    # BUILD_DIR/OBJ_DIR here are whatever the outer litai bound for this lifecycle --
    # an explicit --build-dir/--obj-dir, else the inherited environment, else the
    # portable default -- so the receipt states the cache custody actually used.
    evidence_values["cache-directory-custody"] = resolve_cache_directories(
        Path(__file__).resolve().parents[3]
    ).identity
    missing_evidence = set(policy.required_evidence_kinds) - evidence_values.keys()
    if missing_evidence:
        raise SampleFailure(
            "sample runner cannot produce project-required evidence: "
            + ", ".join(sorted(missing_evidence))
        )
    evidence = tuple(
        ProjectTestEvidence(kind, identity)
        for kind, identity in sorted(evidence_values.items())
    )
    receipt = ProjectTestReceipt(
        project_id=project_definition.project_id,
        project_revision_identity=project_revision_identity,
        subject_identity=subject_identity,
        suite=VersionedContentRef(
            "test-suite",
            policy.suite_id,
            policy.suite_version,
            suite_identity,
        ),
        outcome="passed",
        summary=ProjectTestSummary(total, total, 0, 0),
        result_identity=result_identity,
        evidence=evidence,
    )
    try:
        lifecycle.validate_against(
            source_cache_control,
            source_cache_decision,
            receipt,
        )
        if source_cache_lifecycle_path is not None:
            write_rebuild_source_cache_lifecycle(
                source_cache_lifecycle_path,
                lifecycle,
            )
    except (TypeError, ValueError) as exc:
        raise SampleFailure(
            "conformance receipt does not bind exact source-cache lifecycle membership"
        ) from exc
    return receipt


def sample_test_runner_identity() -> ContentIdentity:
    """Identify the complete runner source closure authorized to issue receipts."""

    repository = Path(__file__).resolve().parents[3]
    return sample_test_runner_source_closure_identity(repository)


def write_project_test_receipt_candidate(
    target: Path,
    provisional: ProjectTestReceiptProvisional,
    *,
    repository_root: Path,
) -> None:
    """Write one driver assertion for outer finalization, never a public candidate."""

    if target.is_symlink():
        raise SampleFailure("test receipt candidate cannot be a symbolic link")
    resolved = target.resolve()
    repository = repository_root.resolve(strict=True)
    if resolved == repository or resolved.is_relative_to(repository):
        raise SampleFailure(
            "test receipt candidate must be written outside the repository"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.parent.is_symlink() or not resolved.parent.is_dir():
        raise SampleFailure("test receipt candidate parent must be a regular directory")
    write_project_test_receipt_provisional(resolved, provisional)


def plan_recipes(root: Path) -> tuple[dict[str, object], ...]:
    """Compose every host recipe without invoking a coding CLI or generating source."""

    plans: list[dict[str, object]] = []
    for sample_root in discover(root, platform=_host_os()):
        metadata, definition, loaded, input_closures = _load_sample(sample_root)
        execution, execution_document = _execution_contract(
            sample_root, definition, loaded
        )
        variants = _execution_variants(definition, execution)
        for variant in variants:
            (
                _base,
                _target_profile,
                _resolved_targets,
                authority,
                authority_catalog,
                authority_plan,
                selected_flavors,
            ) = _sample_runtime_recipe(
                sample_root,
                definition,
                loaded.specification_set,
                execution,
                variant,
                input_closures.generation,
            )
            ordered_flavors = _ordered_selected_flavors(
                definition,
                selected_flavors,
                variant,
            )
            toolchain_constraints = _effective_toolchain_constraints(
                ordered_flavors,
                _authoring_toolchain_constraints(
                    sample_root,
                    definition,
                    boundary=project_boundary(sample_root, legacy=sample_root.parent),
                ),
            )
            plans.append(
                {
                    "sample_id": metadata["sample_id"],
                    "variant": variant.variant_id,
                    "language": variant.variant_id,
                    "languages": list(variant.languages),
                    "slot_values": variant.slot_values_dict(),
                    "toolchain_constraints": [
                        item.to_dict() for item in toolchain_constraints
                    ],
                    "recipe": _generation_recipe(
                        sample_root=sample_root,
                        definition=definition,
                        loaded=loaded,
                        acceptance_document=execution_document,
                        selected_flavors=ordered_flavors,
                        base_skills=_coding_skills(
                            sample_root,
                            definition,
                            boundary=project_boundary(
                                sample_root, legacy=sample_root.parent
                            ),
                            source=f"Component {definition.coordinate.uri}",
                        ),
                        variant=variant,
                        authority=authority,
                        catalog=authority_catalog,
                        resolved_inputs=(
                            ("component_lock", authority.lock.identity.uri),
                            ("root_revision", authority.lock.root_revision.uri),
                            (
                                "target_profile",
                                authority.lock.target_profile_identity.uri,
                            ),
                            (
                                "selection_policy",
                                authority.lock.selection_policy_identity.uri,
                            ),
                        ),
                    ),
                    "component_lock_identity": authority.lock.identity.uri,
                }
            )
            authority_catalog.require_unchanged(nodes=authority_plan.nodes)
        _require_sample_inputs_unchanged(input_closures.generation, label="generation")
        _require_sample_inputs_unchanged(input_closures.verifier, label="verifier")
    return tuple(plans)


def plan_derivation_manifest(
    root: Path,
    scratch_root: Path,
    *,
    planning_request_identity: ContentIdentity,
    project_revision_identity: ContentIdentity,
    lifecycle_driver_identity: ContentIdentity,
    pipeline_model: str | None = None,
) -> RebuildSourceCacheDerivationManifest:
    """Plan every exact cache key without generating, compiling, or running code."""

    generator = CodingCliSourceGenerator()
    planning_generator = _DerivationPlanningSourceGenerator(generator)
    standard_generator = CachedCodingCliSourceGenerator(
        CodingCliSourceGenerator(source_intelligence_mode="off"),
        cache_root=scratch_root / "standard-cache",
        project_root=root.parent,
    )
    cpp_toolchain = discover_cpp_toolchain()
    keys: list[SourceDerivationCacheKey] = []
    lock_identities: set[ContentIdentity] = set()
    for sample_root in discover(root, platform=_host_os()):
        metadata, definition, loaded, input_closures = _load_sample(sample_root)
        sample_id = str(metadata["sample_id"])
        oracle_reference = _sample_oracle_reference(metadata)
        contract, _contract_document = _execution_contract(
            sample_root, definition, loaded
        )
        for variant in _execution_variants(definition, contract):
            if sample_id == "durable-split-service":
                from tests.conformance.support.durable_split_service import (
                    plan_durable_split_service_derivations,
                )

                planned_nodes = plan_durable_split_service_derivations(
                    sample_root=sample_root,
                    source_generator=standard_generator,
                    scratch=scratch_root / sample_root.name / variant.variant_id,
                    pipeline_model=pipeline_model,
                )
                keys.extend(key for key, _lock in planned_nodes)
                lock_identities.update(lock for _key, lock in planned_nodes)
                continue
            if sample_id in _STANDARD_SINGLE_COMPONENT_SAMPLES or (
                sample_id == "service-stack" and variant.variant_id == "python"
            ):
                planned_nodes = _plan_standard_sample_derivations(
                    sample_root=sample_root,
                    sample_id=sample_id,
                    definition=definition,
                    loaded=loaded,
                    contract=contract,
                    variant=variant,
                    input_closure=input_closures.generation.fork(),
                    generator=standard_generator,
                    scratch=scratch_root / sample_root.name / variant.variant_id,
                    pipeline_model=pipeline_model,
                )
                keys.extend(key for key, _lock in planned_nodes)
                lock_identities.update(lock for _key, lock in planned_nodes)
                continue
            try:
                _run_host_e2e(
                    sample_root,
                    scratch_root / sample_root.name,
                    sample_id,
                    oracle_reference,
                    definition,
                    loaded,
                    _SampleInputClosures(
                        input_closures.generation.fork(),
                        input_closures.verifier.fork(),
                    ),
                    variant=variant,
                    source_generator=planning_generator,
                    cpp_toolchain=cpp_toolchain,
                    allow_host_execution=False,
                    planning_only=True,
                    pipeline_model=pipeline_model,
                )
            except BaseException as exc:
                planned = _planned_derivation(exc)
                if planned is None:
                    raise
                keys.append(planned.cache_key)
                lock_identities.add(planned.component_lock_identity)
            else:  # pragma: no cover - the planning generator always stops here
                raise SampleFailure(
                    "derivation planning crossed the coding-agent execution boundary"
                )
    ordered = tuple(sorted(keys, key=lambda key: key.identity.uri))
    lifecycle_plan_identity = canonical_identity(
        {
            "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
            "component_lock_identities": [
                identity.uri
                for identity in sorted(lock_identities, key=lambda item: item.uri)
            ],
            "cache_key_identities": [key.identity.uri for key in ordered],
        }
    )
    return RebuildSourceCacheDerivationManifest(
        planning_request_identity=planning_request_identity,
        project_revision_identity=rebuild_project_authority_identity(
            project_revision_identity,
            tuple(sorted(lock_identities, key=lambda item: item.uri)),
        ),
        lifecycle_driver_identity=lifecycle_driver_identity,
        lifecycle_plan_identity=lifecycle_plan_identity,
        component_lock_identities=tuple(
            sorted(lock_identities, key=lambda item: item.uri)
        ),
        cache_keys=ordered,
    )


def _receipted_driver_summary(
    report: Mapping[str, object], receipt: ProjectTestReceipt
) -> dict[str, object]:
    """Keep process diagnostics bounded; the candidate file carries the evidence."""

    return {
        "schema": "literate-ai/receipted-sample-driver-summary@1",
        "passed": report["passed"],
        "receipt_identity": receipt.identity.uri,
        "recipe_count": report["recipe_count"],
        "test_count": report["test_count"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help=(
            "retain generated source and compiled artifacts in a new or empty "
            "directory outside the checkout"
        ),
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    parser.add_argument(
        "--coding-cli",
        metavar="CLI",
        help=(
            "override the live-test coding CLI from literate.test.json "
            "(codex, claude, cursor-agent, or opencode)"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="pipeline-default coding-CLI model selector for every generated sample",
    )
    parser.add_argument(
        "--test-receipt",
        type=Path,
        help=(
            "write a provisional passing assertion for outer litai finalization; "
            "this raw driver output is deliberately not publicly promotable"
        ),
    )
    parser.add_argument(
        "--lifecycle-project",
        type=Path,
        help="exact project root supplied by a trusted litai lifecycle binding",
    )
    parser.add_argument(
        "--lifecycle-specification",
        type=Path,
        help="exact specification scope supplied by a trusted litai lifecycle binding",
    )
    parser.add_argument(
        "--lifecycle-project-revision",
        help="exact current project authority identity supplied by litai",
    )
    parser.add_argument(
        "--lifecycle-request-identity",
        help="exact rebuild request identity to bind into candidate evidence",
    )
    parser.add_argument(
        "--allow-host-execution",
        action="store_true",
        help=(
            "explicitly acknowledge that generated source is compiled and the "
            "resulting applications run as authorized host processes rather than "
            "hardened OS sandboxes"
        ),
    )
    parser.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="GLOB",
        help=(
            "run only sample directory names matching this glob; repeat for a union "
            "and use '*' for the complete matrix"
        ),
    )
    parser.add_argument(
        "--flavor",
        action="append",
        default=[],
        metavar="SELECTOR",
        help=(
            "select an exact canonical Flavor coordinate for unpinned sample axes; "
            "repeat selectors, or use lang.*, build.*, and os.* for explicit matrix "
            "expansion"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help=(
            "maximum independent samples to execute concurrently; defaults to four "
            "or the selected sample count, whichever is smaller"
        ),
    )
    parser.add_argument(
        "--expected-platform",
        choices=("linux", "macos", "windows"),
        help="fail unless host Flavor discovery resolves this exact platform",
    )
    parser.add_argument(
        "--native-package",
        action="store_true",
        help=(
            "construct and independently verify selected pip/Conan packages from "
            "accepted Standard sample artifacts beneath OBJ_DIR"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    require_live_selection: bool = False,
) -> int:
    args = _parser().parse_args(argv)
    sample_patterns = tuple(args.sample) or ("*",)
    if args.expected_platform is not None and _host_os() != args.expected_platform:
        raise SampleFailure(
            f"remote target expected {args.expected_platform} but resolved {_host_os()}"
        )
    if args.test_receipt is not None and sample_patterns != ("*",):
        raise SampleFailure(
            "project test receipts require the complete '*' sample matrix"
        )
    root = Path(__file__).resolve().parents[3] / "samples"
    try:
        resolver = (
            resolve_live_test_selection
            if require_live_selection
            else try_resolve_live_test_selection
        )
        selection = resolver(
            coding_cli=args.coding_cli,
            model=args.model,
            environment=os.environ,
            project_root=Path(__file__).resolve().parents[3],
        )
    except CodingCliError as exc:
        raise SampleFailure(f"{exc.code}: {exc.message}") from exc
    if selection is not None:
        # Planning and execution must construct one model-scoped recipe/key set.
        # This projection is pure configuration; preflight remains execution-only.
        apply_live_test_selection(os.environ, selection)
        args.model = selection.model
    if os.environ.get(SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT) == (
        SOURCE_CACHE_PLANNING_MODE
    ):
        project = discover_project(root)
        if project is None or project.definition.lifecycle_driver is None:
            raise SampleFailure(
                "derivation planning requires the exact project lifecycle driver"
            )
        required = (
            args.runtime_root,
            args.lifecycle_project,
            args.lifecycle_specification,
            args.lifecycle_project_revision,
            os.environ.get(SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT),
            os.environ.get(SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT),
        )
        if any(value is None for value in required):
            raise SampleFailure(
                "derivation planning requires the complete outer CLI binding"
            )
        assert args.runtime_root is not None
        assert args.lifecycle_project is not None
        assert args.lifecycle_specification is not None
        assert args.lifecycle_project_revision is not None
        if args.allow_host_execution:
            raise SampleFailure(
                "derivation planning must not receive host-execution authority"
            )
        if args.lifecycle_project.resolve(strict=True) != project.root or (
            args.lifecycle_specification.resolve(strict=True) != project.root
        ):
            raise SampleFailure("derivation planning identifies another project scope")
        try:
            project_revision = ContentIdentity.parse_uri(
                args.lifecycle_project_revision
            )
            planning_request = ContentIdentity.parse_uri(
                os.environ[SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT]
            )
        except (KeyError, ValueError) as exc:
            raise SampleFailure("derivation planning identities are invalid") from exc
        if _validated_project_revision(root) != project_revision:
            raise SampleFailure("derivation planning project revision is stale")
        configured_manifest = Path(
            os.environ[SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT]
        )
        expected_parent = args.runtime_root.resolve(strict=True) / ".litai"
        if (
            configured_manifest.parent.resolve(strict=True) != expected_parent
            or configured_manifest.name != "source-cache-derivations.json"
            or configured_manifest.exists()
            or configured_manifest.is_symlink()
        ):
            raise SampleFailure(
                "derivation manifest path is not the pinned protocol path"
            )
        with tempfile.TemporaryDirectory(
            prefix=".litai-plan-", dir=args.runtime_root
        ) as temporary:
            manifest = plan_derivation_manifest(
                root,
                Path(temporary),
                planning_request_identity=planning_request,
                project_revision_identity=project_revision,
                lifecycle_driver_identity=project.definition.lifecycle_driver.identity,
                pipeline_model=args.model,
            )
        if _validated_project_revision(root) != project_revision:
            raise SampleFailure("project authority changed during derivation planning")
        write_rebuild_source_cache_derivation_manifest(
            configured_manifest,
            manifest,
        )
        print(
            json.dumps(
                {
                    "schema": "literate-ai/rebuild-derivation-planning@1",
                    "manifest_identity": manifest.identity.uri,
                    "derivation_count": len(manifest.cache_keys),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    if selection is not None:
        log_live_session_models(selection)
        # Fail fast (before any expensive generation) if the pinned model does
        # not actually resolve for the selected coding CLI. Without this, a wrong
        # or mis-qualified model id in literate.test.json only surfaces deep in a
        # generation run as an opaque coding_cli error. An operator can skip the
        # preflight with LITAI_SKIP_MODEL_PREFLIGHT for a deliberate dry run.
        if not str(os.environ.get("LITAI_SKIP_MODEL_PREFLIGHT", "")).strip():
            try:
                verify_live_model_resolves(selection, environment=os.environ)
            except CodingCliError as exc:
                raise SampleFailure(f"{exc.code}: {exc.message}") from exc
    receipt_project = None
    receipt_project_revision = None
    source_cache_control = None
    source_cache_decision = None
    source_cache_lifecycle_path = None
    if args.test_receipt is not None:
        receipt_project = discover_project(root)
        if receipt_project is None:
            raise SampleFailure("sample repository is not a Literate AI project")
        receipt_project_revision = _validated_project_revision(root)
        lifecycle_values = (
            args.lifecycle_project,
            args.lifecycle_specification,
            args.lifecycle_project_revision,
            args.lifecycle_request_identity,
            os.environ.get("LITAI_LIFECYCLE_COMMAND_IDENTITY"),
        )
        if any(value is None for value in lifecycle_values):
            raise SampleFailure(
                "receipted conformance requires the complete litai lifecycle binding"
            )
        assert args.lifecycle_project is not None
        assert args.lifecycle_specification is not None
        assert args.lifecycle_project_revision is not None
        assert args.lifecycle_request_identity is not None
        if args.lifecycle_project.resolve(strict=True) != receipt_project.root:
            raise SampleFailure("lifecycle binding identifies another project")
        if args.lifecycle_specification.resolve(strict=True) != receipt_project.root:
            raise SampleFailure(
                "sample conformance lifecycle driver only accepts project scope"
            )
        try:
            supplied_project_revision = ContentIdentity.parse_uri(
                args.lifecycle_project_revision
            )
            lifecycle_request_identity = ContentIdentity.parse_uri(
                args.lifecycle_request_identity
            )
            lifecycle_command_identity = ContentIdentity.parse_uri(
                os.environ["LITAI_LIFECYCLE_COMMAND_IDENTITY"]
            )
        except (KeyError, ValueError) as exc:
            raise SampleFailure("lifecycle binding identities are invalid") from exc
        try:
            source_cache_control = read_rebuild_source_cache_control(
                Path(os.environ["LITAI_SOURCE_CACHE_CONTROL"])
            )
            source_cache_decision = read_rebuild_source_cache_decision(
                Path(os.environ["LITAI_SOURCE_CACHE_DECISION"])
            )
            source_cache_lifecycle_path = Path(
                os.environ["LITAI_SOURCE_CACHE_LIFECYCLE"]
            )
            source_cache_decision.validate_against(source_cache_control)
            if source_cache_control.identity.uri != os.environ[
                "LITAI_SOURCE_CACHE_CONTROL_IDENTITY"
            ] or source_cache_decision.identity.uri != os.environ.get(
                "LITAI_SOURCE_CACHE_DECISION_IDENTITY"
            ):
                raise ValueError
        except (
            KeyError,
            RebuildSourceCacheProtocolError,
            TypeError,
            ValueError,
        ) as exc:
            raise SampleFailure(
                "source-cache protocol does not bind this litai lifecycle"
            ) from exc
        receipt_project_revision = _receipted_project_revision(
            root, source_cache_control.component_lock_identities
        )
        if supplied_project_revision != receipt_project_revision:
            raise SampleFailure("lifecycle binding project revision is stale")
    invocation_succeeded = False

    def emit_report(report: Mapping[str, object]) -> None:
        nonlocal invocation_succeeded
        try:
            if args.test_receipt is not None:
                assert receipt_project is not None
                assert receipt_project_revision is not None
                if source_cache_control is None:
                    authority_is_current = (
                        _validated_project_revision(root) == receipt_project_revision
                    )
                else:
                    authority_is_current = (
                        source_cache_control.project_revision_identity
                        == receipt_project_revision
                    )
                if not authority_is_current:
                    raise SampleFailure(
                        "project authority changed during the receipted conformance run"
                    )
                receipt = project_test_receipt(
                    report,
                    receipt_project.definition,
                    receipt_project_revision,
                    lifecycle_request_identity=lifecycle_request_identity,
                    lifecycle_command_identity=lifecycle_command_identity,
                    source_cache_control=source_cache_control,
                    source_cache_decision=source_cache_decision,
                    source_cache_lifecycle_path=source_cache_lifecycle_path,
                )
                assert source_cache_control is not None
                provisional = ProjectTestReceiptProvisional(
                    lifecycle_request_identity=lifecycle_request_identity,
                    lifecycle_command_identity=lifecycle_command_identity,
                    source_cache_control_identity=source_cache_control.identity,
                    component_lock_identities=_report_component_lock_identities(report),
                    receipt_identity=receipt.identity,
                    receipt=receipt,
                )
                write_project_test_receipt_candidate(
                    args.test_receipt,
                    provisional,
                    repository_root=receipt_project.root,
                )
                output: Mapping[str, object] = _receipted_driver_summary(
                    report, receipt
                )
            else:
                output = report
            print(
                json.dumps(
                    output,
                    indent=2 if args.pretty else None,
                    sort_keys=True,
                    separators=None if args.pretty else (",", ":"),
                )
            )
        except BaseException:
            raise
        invocation_succeeded = bool(report["passed"])

    if args.runtime_root is None:
        owned_runtime_root = Path(tempfile.mkdtemp(prefix="literate-ai-conformance-"))
        with retained_directory(
            owned_runtime_root,
            node_path="samples/runtime",
            operation="samples.runtime",
            role="sample-runtime-root",
            success=lambda: invocation_succeeded,
        ):
            report = run_all(
                root,
                owned_runtime_root,
                allow_host_execution=args.allow_host_execution,
                sample_patterns=sample_patterns,
                force_regeneration=(
                    source_cache_control.force_regeneration
                    if source_cache_control is not None
                    else False
                ),
                jobs=args.jobs,
                flavor_selectors=tuple(args.flavor),
                native_packages=args.native_package,
                pipeline_model=args.model,
            )
            emit_report(report)
    else:
        configured = args.runtime_root
        existing = (
            tuple(configured.iterdir())
            if configured.exists() and configured.is_dir()
            else ()
        )
        if configured.is_symlink() or (
            configured.exists()
            and (
                not configured.is_dir()
                or any(path.name != ".litai" for path in existing)
            )
        ):
            raise SampleFailure("runtime root must be a new or empty directory")
        resolved = configured.resolve()
        repository_root = root.parent.resolve(strict=True)
        if resolved == repository_root or resolved.is_relative_to(repository_root):
            raise SampleFailure("runtime root must be outside the repository")
        configured.mkdir(parents=True, exist_ok=True)
        report = run_all(
            root,
            resolved,
            allow_host_execution=args.allow_host_execution,
            sample_patterns=sample_patterns,
            force_regeneration=(
                source_cache_control.force_regeneration
                if source_cache_control is not None
                else False
            ),
            jobs=args.jobs,
            flavor_selectors=tuple(args.flavor),
            native_packages=args.native_package,
            pipeline_model=args.model,
        )
        emit_report(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

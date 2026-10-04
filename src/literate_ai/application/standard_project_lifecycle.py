"""Reusable standard lifecycle over exact per-Component plans and artifact contracts."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial

from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.artifact_graph import (
    ArtifactAssemblyError,
    realize_manifest,
    validate_composite_build_request,
)
from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.component_workers import (
    ComponentNodeCancellation,
    ComponentNodeDispatcher,
    ComponentNodeDispatchRequest,
    ComponentNodeRecoveryCandidate,
    ComponentWorkerError,
    component_artifact_handoff,
    recover_component_node_result,
    validate_component_node_outcome,
    validate_component_worker_routing,
)
from literate_ai.application.source_generation_scheduling import (
    ComponentSourceGenerationExecution,
    ComponentSourceGenerationRunner,
    execute_component_source_generation_node,
    source_generation_terminal_result,
    validate_prepared_component_generation_node,
)
from literate_ai.application.standard_lifecycle_membership import (
    assemble_standard_lifecycle_membership,
    create_standard_aggregate_receipt,
)
from literate_ai.application.standard_lifecycle_ports import (
    AcceptedSourceCachePublisher,
    AdmittedBuildAuthorizer,
    AdmittedBuildIntentDispatcher,
    AdmittedBuildPlanFinalizer,
    AdmittedComponentBuilder,
    AdmittedComponentExecutor,
    AdmittedComponentTester,
    AdmittedGenerationIndexer,
    BuildAuthorizer,
    BuildProviderEvidenceReceiver,
    CompleteAcceptedSourceCachePublisher,
    ComponentAcceptor,
    ComponentBuilder,
    ComponentBuildIntentFactory,
    ComponentBuildPlanFinalizer,
    ComponentExecutor,
    ComponentTester,
    ExecutionProviderEvidenceReceiver,
    GenerationIndexer,
    IndependentProjectAcceptor,
    PackagedProjectExecutor,
    ProjectAdmitter,
    ProjectArtifactAssembler,
    ProjectPackageCreator,
    ProjectReceiptIssuer,
    ProjectValidator,
    ReservedLifecycleOperation,
    RootIntegrationTester,
    StandardCandidateRepairPort,
    StandardContextEvidenceRecorder,
    StandardLifecycleCheckpointRecorder,
)
from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphError,
    AuthorityGraphNode,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.component_workers import (
    ComponentArtifactHandoff,
    ComponentWorkerProduct,
    ComponentWorkerRouting,
)
from literate_ai.contracts.executable_components import (
    ArtifactBuildGraph,
    ArtifactExport,
    ArtifactMaterializationPlan,
    CandidateAttempt,
    CandidateAttemptChain,
    CandidateAttemptChainDisposition,
    CandidateAttemptDisposition,
    CandidateFailureClassification,
    CandidateRepairDiagnostic,
    CandidateRepairRequest,
    ComponentBuildManifest,
    ComponentContextBenchmarkRecord,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ComponentInvalidationDecision,
    CompositeBuildRequest,
    DependencyInputKind,
    ExactLinkPlan,
    ForwardGenerationContextCacheReport,
    GeneratedSourceCandidate,
    PackagePlan,
    PackageResult,
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    SourceGenerationScheduleResult,
)
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
)
from literate_ai.contracts.standard_execution_inputs import StandardExecutionInputScope
from literate_ai.contracts.standard_lifecycle import (
    StandardBuildAuthorizationDocument,
    StandardComponentBuildIntentDocument,
    StandardComponentBuildPlanDocument,
    StandardSourceCacheMembershipDocument,
)
from literate_ai.contracts.standard_lifecycle_checkpoint import (
    StandardLifecycleCheckpointOutcome,
    StandardLifecycleStage,
    StandardLifecycleStageEvidence,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardAggregateReceipt,
    StandardNodeCacheOutcome,
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
    StandardProjectLifecycleMembership,
)
from literate_ai.contracts.standard_post_source_evidence import (
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestExecutionEvidence,
)
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from literate_ai.contracts.standard_source_admission import (
    StandardSourceAdmissionMembership,
)
from literate_ai.diagnostics import trace_exception
from literate_ai.security import BuildAuthorization, BuildRequest


class StandardProjectLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_PORTABLE_FAILURE_CODE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_PRIVATE_FAILURE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:\\[^\r\n\t\"']+|/(?:[^/\s\"']+/)+[^/\s\"']*)"
)
_SECRET_FAILURE_VALUE = re.compile(
    r"(?i)(authorization|cookie|credential|password|secret|token)"
    r"([\"'\s:=]+)([^\s,\"']+)"
)


def _stable_failure_code(value: object, fallback: str) -> str:
    candidate = value if isinstance(value, str) else getattr(value, "code", None)
    if isinstance(candidate, str) and _PORTABLE_FAILURE_CODE.fullmatch(candidate):
        return candidate
    return fallback


def _portable_failure_diagnostic(value: object) -> str:
    """Retain the nested stable cause while removing secrets and worker paths."""

    parts: list[str] = []
    current: object | None = value
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        candidate = getattr(current, "code", None)
        code = (
            candidate
            if isinstance(candidate, str)
            and _PORTABLE_FAILURE_CODE.fullmatch(candidate)
            else type(current).__name__
        )
        message = getattr(current, "message", current)
        parts.append(f"{code}: {message}")
        current = getattr(current, "__cause__", None)
    text = " caused by ".join(parts).replace("\x00", "")
    text = _SECRET_FAILURE_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    text = _PRIVATE_FAILURE_PATH.sub("<private-path>", text)
    text = text.replace("/", "<path-separator>").replace("\\", "<path-separator>")
    return text[:8192] or "remote lifecycle stage failed without a public diagnostic"


def _failure(
    component_revision: ContentIdentity,
    phase: StandardNodeFailurePhase,
    subject_identity: ContentIdentity,
    cause: object,
    fallback: str,
) -> StandardNodeFailureEvidence:
    return StandardNodeFailureEvidence(
        component_revision,
        phase,
        _stable_failure_code(cause, fallback),
        subject_identity,
        _portable_failure_diagnostic(cause),
    )


@dataclass(frozen=True, slots=True)
class StandardComponentBuildPlan:
    component_revision: ContentIdentity
    manifest: ComponentBuildManifest
    materialization: ArtifactMaterializationPlan
    request: CompositeBuildRequest
    provider_artifact_identities: tuple[ContentIdentity, ...] = ()
    package_artifact_identities: tuple[ContentIdentity, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.manifest.component_revision != self.component_revision
            or self.request.component_revision != self.component_revision
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.build_plan_component_mismatch",
                "typed build plan names different Components",
            )
        validate_composite_build_request(
            self.request,
            self.manifest,
            self.materialization,
            build_system_resolver_identity=(
                self.request.build_system_resolver_identity
            ),
            language_compiler_identity=self.request.language_compiler_identity,
            language_runtime_identity=self.request.language_runtime_identity,
        )
        uris = tuple(item.uri for item in self.provider_artifact_identities)
        if uris != tuple(sorted(set(uris))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.provider_artifacts_noncanonical",
                "provider artifact identities must be unique and canonical",
            )
        package_uris = tuple(item.uri for item in self.package_artifact_identities)
        if package_uris != tuple(sorted(set(package_uris))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.package_artifacts_noncanonical",
                "package artifact identities must be unique and canonical",
            )

    @property
    def identity(self) -> ContentIdentity:
        return self.to_document().identity

    def to_document(self) -> StandardComponentBuildPlanDocument:
        return StandardComponentBuildPlanDocument(
            self.component_revision,
            self.manifest,
            self.materialization,
            self.request,
            self.provider_artifact_identities,
            self.package_artifact_identities,
        )

    def to_dict(self) -> dict[str, object]:
        return self.to_document().to_dict()

    @classmethod
    def from_dict(cls, value: object) -> StandardComponentBuildPlan:
        document = StandardComponentBuildPlanDocument.from_dict(value)
        return cls(
            document.component_revision,
            document.manifest,
            document.materialization,
            document.request,
            document.provider_artifact_identities,
            document.package_artifact_identities,
        )


_ExecutionInputResolver = Callable[
    [StandardComponentBuildPlan, tuple[ArtifactExport, ...]],
    tuple[StandardExecutionInputScope, tuple[ArtifactExport, ...]],
]


@dataclass(frozen=True, slots=True)
class StandardComponentBuildIntent:
    """Authorization-free build request derived from actual generated source."""

    component_revision: ContentIdentity
    source_tree_identity: ContentIdentity
    source_bundle_identity: ContentIdentity
    build_request: BuildRequest
    provider_artifact_identities: tuple[ContentIdentity, ...] = ()
    package_artifact_identities: tuple[ContentIdentity, ...] = ()
    native_sdk_input_identities: tuple[ContentIdentity, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.component_revision, "Component revision"),
            (self.source_tree_identity, "source tree"),
            (self.source_bundle_identity, "source bundle"),
        ):
            _require_identity(value, label)
        if not isinstance(self.build_request, BuildRequest):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.build_request_invalid",
                "build intent requires a typed post-source BuildRequest",
            )
        if (
            self.build_request.effective_revision_digest != self.component_revision.uri
            or self.build_request.source_bundle_digest
            != self.source_bundle_identity.uri
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.build_request_source_mismatch",
                "build request must bind the intent's exact Component and "
                "source bundle",
            )
        for item in self.provider_artifact_identities:
            _require_identity(item, "provider artifact")
        uris = tuple(item.uri for item in self.provider_artifact_identities)
        if uris != tuple(sorted(set(uris))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.provider_artifacts_noncanonical",
                "provider artifact identities must be unique and canonical",
            )
        for item in self.package_artifact_identities:
            _require_identity(item, "package artifact")
        package_uris = tuple(item.uri for item in self.package_artifact_identities)
        if package_uris != tuple(sorted(set(package_uris))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.package_artifacts_noncanonical",
                "package artifact identities must be unique and canonical",
            )

        for item in self.native_sdk_input_identities:
            _require_identity(item, "native SDK input")
        sdk_uris = tuple(item.uri for item in self.native_sdk_input_identities)
        if not isinstance(self.native_sdk_input_identities, tuple) or (
            sdk_uris != tuple(sorted(set(sdk_uris)))
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.native_sdk_inputs_noncanonical",
                "native SDK input identities must be a unique canonical tuple",
            )

    @property
    def identity(self) -> ContentIdentity:
        return self.to_document().identity

    def to_document(self) -> StandardComponentBuildIntentDocument:
        return StandardComponentBuildIntentDocument(
            self.component_revision,
            self.source_tree_identity,
            self.source_bundle_identity,
            self.build_request,
            self.provider_artifact_identities,
            self.package_artifact_identities,
            self.native_sdk_input_identities,
        )

    @property
    def build_request_identity(self) -> ContentIdentity:
        return self.to_document().build_request_identity

    def to_dict(self) -> dict[str, object]:
        return self.to_document().to_dict()

    @classmethod
    def from_dict(cls, value: object) -> StandardComponentBuildIntent:
        document = StandardComponentBuildIntentDocument.from_dict(value)
        return cls(
            document.component_revision,
            document.source_tree_identity,
            document.source_bundle_identity,
            document.build_request,
            document.provider_artifact_identities,
            document.package_artifact_identities,
            document.native_sdk_input_identities,
        )


@dataclass(frozen=True, slots=True)
class StandardBuildAuthorization:
    """Exact grant returned after source indexing validates one build intent."""

    build_intent_identity: ContentIdentity
    build_request_identity: ContentIdentity
    index_identity: ContentIdentity
    grant: BuildAuthorization

    def __post_init__(self) -> None:
        for value, label in (
            (self.build_intent_identity, "build intent"),
            (self.build_request_identity, "build request"),
            (self.index_identity, "source index"),
        ):
            _require_identity(value, label)
        if not isinstance(self.grant, BuildAuthorization):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.authorization_grant_invalid",
                "authorization boundary requires a typed BuildAuthorization grant",
            )

    @property
    def authorization_identity(self) -> ContentIdentity:
        return self.to_document().authorization_identity

    @property
    def identity(self) -> ContentIdentity:
        return self.to_document().identity

    def to_document(self) -> StandardBuildAuthorizationDocument:
        return StandardBuildAuthorizationDocument(
            self.build_intent_identity,
            self.build_request_identity,
            self.index_identity,
            self.grant,
        )

    def to_dict(self) -> dict[str, object]:
        return self.to_document().to_dict()

    @classmethod
    def from_dict(cls, value: object) -> StandardBuildAuthorization:
        document = StandardBuildAuthorizationDocument.from_dict(value)
        return cls(
            document.build_intent_identity,
            document.build_request_identity,
            document.index_identity,
            document.grant,
        )


@dataclass(frozen=True, slots=True)
class StandardSourceCacheMembership:
    """Previously accepted source that remains untrusted for the current lifecycle."""

    component_revision: ContentIdentity
    generation_key_identity: ContentIdentity
    generation: SourceGenerationResumeCandidate
    acceptance_identity: ContentIdentity

    @property
    def identity(self) -> ContentIdentity:
        return self.to_document().identity

    def to_document(self) -> StandardSourceCacheMembershipDocument:
        return StandardSourceCacheMembershipDocument(
            self.component_revision,
            self.generation_key_identity,
            self.generation,
            self.acceptance_identity,
        )

    def to_dict(self) -> dict[str, object]:
        return self.to_document().to_dict()

    @classmethod
    def from_dict(cls, value: object) -> StandardSourceCacheMembership:
        document = StandardSourceCacheMembershipDocument.from_dict(value)
        return cls(
            document.component_revision,
            document.generation_key_identity,
            document.generation,
            document.acceptance_identity,
        )


@dataclass(frozen=True, slots=True)
class StandardProjectBuildPlan:
    execution_plan_identity: ContentIdentity
    components: tuple[StandardComponentBuildPlan, ...]

    def __post_init__(self) -> None:
        _require_identity(self.execution_plan_identity, "project execution plan")
        if any(
            not isinstance(item, StandardComponentBuildPlan) for item in self.components
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.project_plan_invalid",
                "project build plan components must be typed",
            )
        uris = tuple(item.component_revision.uri for item in self.components)
        if not uris or uris != tuple(sorted(set(uris))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.project_plan_noncanonical",
                "project build plan must use unique canonical Component order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """Return the existing project-plan identity payload."""

        return {
            "schema": "literate-ai/standard-project-build-plan@1",
            "execution_plan_identity": self.execution_plan_identity.uri,
            "components": [item.identity.uri for item in self.components],
        }


@dataclass(frozen=True, slots=True)
class StandardNodeAcceptedCandidate:
    generation: SourceGenerationResumeCandidate
    build_plan_identity: ContentIdentity
    exports: tuple[ArtifactExport, ...]
    index_identity: ContentIdentity
    authorization_identity: ContentIdentity
    build_identity: ContentIdentity
    test_identity: ContentIdentity
    execution_identity: ContentIdentity
    acceptance_identity: ContentIdentity
    source_cache_membership: StandardSourceCacheMembership

    def __post_init__(self) -> None:
        if not isinstance(self.source_cache_membership, StandardSourceCacheMembership):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_membership_invalid",
                "accepted node requires typed source-cache membership",
            )
        membership = self.source_cache_membership
        if (
            membership.generation != self.generation
            or membership.acceptance_identity != self.acceptance_identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_membership_mismatch",
                "accepted node and source-cache membership must bind the same "
                "source and acceptance",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/standard-node-accepted-candidate@2",
                "generation": self.generation.to_dict(),
                "build_plan_identity": self.build_plan_identity.uri,
                "exports": [item.to_dict() for item in self.exports],
                "index_identity": self.index_identity.uri,
                "authorization_identity": self.authorization_identity.uri,
                "build_identity": self.build_identity.uri,
                "test_identity": self.test_identity.uri,
                "execution_identity": self.execution_identity.uri,
                "acceptance_identity": self.acceptance_identity.uri,
                "source_cache_membership_identity": (
                    self.source_cache_membership.identity.uri
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class StandardAcceptedSourcePublication:
    """Complete accepted-node custody offered to a durable source cache."""

    component_revision: ContentIdentity
    build_plan_identity: ContentIdentity
    source_output: SourceGenerationRunOutput
    exports: tuple[ArtifactExport, ...]
    index_identity: ContentIdentity
    authorization_identity: ContentIdentity
    membership: StandardSourceCacheMembership
    build_evidence: StandardBuildEvidence
    generated_test_evidence: StandardGeneratedTestExecutionEvidence
    execution_evidence: StandardExecutionEvidence
    acceptance_evidence: StandardComponentAcceptanceEvidence

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "build_plan_identity",
            "index_identity",
            "authorization_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError(f"{name} must be a ContentIdentity")
        if not isinstance(self.source_output, SourceGenerationRunOutput):
            raise TypeError("source_output must be a SourceGenerationRunOutput")
        if any(not isinstance(item, ArtifactExport) for item in self.exports):
            raise TypeError("exports must contain only ArtifactExport values")
        for name, expected_type in (
            ("membership", StandardSourceCacheMembership),
            ("build_evidence", StandardBuildEvidence),
            ("generated_test_evidence", StandardGeneratedTestExecutionEvidence),
            ("execution_evidence", StandardExecutionEvidence),
            ("acceptance_evidence", StandardComponentAcceptanceEvidence),
        ):
            if not isinstance(getattr(self, name), expected_type):
                raise TypeError(f"{name} must be {expected_type.__name__}")
        if (
            self.membership.component_revision != self.component_revision
            or self.membership.generation.output != self.source_output
            or self.membership.acceptance_identity != self.acceptance_evidence.identity
            or self.acceptance_evidence.component_revision != self.component_revision
            or self.acceptance_evidence.source_generation_identity
            != self.source_output.identity
            or self.build_evidence.component_revision != self.component_revision
            or self.build_evidence.build_plan_identity != self.build_plan_identity
            or self.acceptance_evidence.build != self.build_evidence
            or self.acceptance_evidence.generated_tests != self.generated_test_evidence
            or self.acceptance_evidence.execution != self.execution_evidence
            or self.build_evidence.exports != self.exports
            or self.generated_test_evidence.build_evidence_identity
            != self.build_evidence.identity
            or self.execution_evidence.build_evidence_identity
            != self.build_evidence.identity
            or any(
                item.authorization_identity != self.authorization_identity
                for item in self.exports
            )
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_publication_custody_mismatch",
                "durable cache publication must bind one complete accepted node",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/standard-accepted-source-publication@1",
                "component_revision": self.component_revision.uri,
                "build_plan_identity": self.build_plan_identity.uri,
                "source_output_identity": self.source_output.identity.uri,
                "exports": [item.identity.uri for item in self.exports],
                "index_identity": self.index_identity.uri,
                "authorization_identity": self.authorization_identity.uri,
                "membership_identity": self.membership.identity.uri,
                "build_evidence_identity": self.build_evidence.identity.uri,
                "generated_test_evidence_identity": (
                    self.generated_test_evidence.identity.uri
                ),
                "execution_evidence_identity": self.execution_evidence.identity.uri,
                "acceptance_evidence_identity": self.acceptance_evidence.identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class StandardBuildOutput:
    exports: tuple[ArtifactExport, ...]
    build_identity: ContentIdentity
    evidence: StandardBuildEvidence | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(item, ArtifactExport) for item in self.exports):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.build_output_invalid",
                "build exports must be ArtifactExport values",
            )
        _require_identity(self.build_identity, "build")
        if self.evidence is not None and (
            not isinstance(self.evidence, StandardBuildEvidence)
            or self.evidence.identity != self.build_identity
            or self.evidence.exports != self.exports
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.build_evidence_mismatch",
                "typed build evidence must bind the exact build identity and exports",
            )


@dataclass(frozen=True, slots=True)
class StandardNodeLifecycleResult:
    component_revision: ContentIdentity
    source_generation: SourceGenerationNodeResult
    source_output: SourceGenerationRunOutput | None
    build_plan_identity: ContentIdentity | None
    exports: tuple[ArtifactExport, ...]
    index_identity: ContentIdentity | None
    authorization_identity: ContentIdentity | None
    build_identity: ContentIdentity | None
    test_identity: ContentIdentity | None
    execution_identity: ContentIdentity | None
    acceptance_identity: ContentIdentity | None
    failure_code: str | None = None
    source_cache_membership: StandardSourceCacheMembership | None = None
    source_cache_publication_identity: ContentIdentity | None = None
    failure_evidence: StandardNodeFailureEvidence | None = None
    build_evidence: StandardBuildEvidence | None = None
    generated_test_evidence: StandardGeneratedTestExecutionEvidence | None = None
    execution_evidence: StandardExecutionEvidence | None = None
    acceptance_evidence: StandardComponentAcceptanceEvidence | None = None
    source_admission_identity: ContentIdentity | None = None

    def __post_init__(self) -> None:
        typed_evidence = (
            (self.build_evidence, self.build_identity),
            (self.generated_test_evidence, self.test_identity),
            (self.execution_evidence, self.execution_identity),
            (self.acceptance_evidence, self.acceptance_identity),
        )
        if any(
            evidence is not None
            and (
                getattr(evidence, "identity", None) != identity
                or getattr(evidence, "component_revision", None)
                != self.component_revision
            )
            for evidence, identity in typed_evidence
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.post_source_evidence_mismatch",
                "typed post-source evidence must bind its node identity and Component",
            )
        if (self.failure_code is None) != (self.failure_evidence is None):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.failure_evidence_incomplete",
                "failure code and typed failure evidence must exist together",
            )
        if self.failure_evidence is not None and (
            not isinstance(self.failure_evidence, StandardNodeFailureEvidence)
            or self.failure_evidence.component_revision != self.component_revision
            or self.failure_evidence.code != self.failure_code
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.failure_evidence_mismatch",
                "failure evidence must bind the Component and stable failure code",
            )
        if not isinstance(self.source_generation, SourceGenerationNodeResult):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.source_result_invalid",
                "node lifecycle result requires typed source-generation evidence",
            )
        if self.source_generation.component_revision != self.component_revision:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.source_result_component_mismatch",
                "source-generation evidence names another Component",
            )
        source_succeeded = self.source_generation.disposition in {
            SourceGenerationDisposition.GENERATED,
            SourceGenerationDisposition.RETAINED,
            SourceGenerationDisposition.REUSED,
        }
        if not source_succeeded and self.failure_evidence is None:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.failure_evidence_missing",
                "failed or cancelled source evidence requires typed failure evidence",
            )
        if self.failure_evidence is not None and any(
            value is not None
            for value in (
                self.acceptance_identity,
                self.source_cache_membership,
                self.source_cache_publication_identity,
            )
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.failed_node_claims_acceptance",
                "failed nodes cannot claim acceptance or cache publication",
            )
        if source_succeeded:
            if not isinstance(self.source_output, SourceGenerationRunOutput):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.source_output_missing",
                    "successful source evidence requires its complete output",
                )
            if (
                self.source_generation.candidate_identity
                != self.source_output.candidate_identity
                or self.source_generation.provenance_identity
                != self.source_output.provenance_identity
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.source_output_mismatch",
                    "source result does not bind its complete output",
                )
        elif self.source_output is not None:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.failed_source_has_output",
                "failed or cancelled source evidence cannot expose an output",
            )
        if self.source_cache_membership is not None:
            if not isinstance(
                self.source_cache_membership, StandardSourceCacheMembership
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_membership_invalid",
                    "node result cache membership must be typed",
                )
            if (
                self.source_output is None
                or self.source_cache_membership.generation.output != self.source_output
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_membership_mismatch",
                    "node result cache membership must bind its exact source",
                )
        if (self.acceptance_identity is not None) != (
            self.source_cache_membership is not None
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_membership_missing",
                "accepted nodes require exact source-cache membership and rejected "
                "nodes cannot claim it",
            )
        if self.source_cache_publication_identity is not None and (
            self.source_cache_membership is None
            or self.source_cache_publication_identity
            != self.source_cache_membership.identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_publication_mismatch",
                "cache publication must identify the exact accepted membership",
            )
        if self.source_admission_identity is not None:
            _require_identity(self.source_admission_identity, "source admission")

    @property
    def disposition(self) -> SourceGenerationDisposition:
        return self.source_generation.disposition

    @property
    def source_candidate_identity(self) -> ContentIdentity | None:
        return self.source_generation.candidate_identity

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """Return the existing identity payload for evidence retention."""

        return {
            "schema": "literate-ai/standard-node-lifecycle-result@4",
            "component_revision": self.component_revision.uri,
            "source_generation": self.source_generation.to_dict(),
            "source_output": (
                None if self.source_output is None else self.source_output.to_dict()
            ),
            "build_plan_identity": _uri(self.build_plan_identity),
            "exports": [item.to_dict() for item in self.exports],
            "index_identity": _uri(self.index_identity),
            "authorization_identity": _uri(self.authorization_identity),
            "build_identity": _uri(self.build_identity),
            "test_identity": _uri(self.test_identity),
            "execution_identity": _uri(self.execution_identity),
            "acceptance_identity": _uri(self.acceptance_identity),
            "build_evidence": (
                None if self.build_evidence is None else self.build_evidence.to_dict()
            ),
            "generated_test_evidence": (
                None
                if self.generated_test_evidence is None
                else self.generated_test_evidence.to_dict()
            ),
            "execution_evidence": (
                None
                if self.execution_evidence is None
                else self.execution_evidence.to_dict()
            ),
            "acceptance_evidence": (
                None
                if self.acceptance_evidence is None
                else self.acceptance_evidence.to_dict()
            ),
            "failure_code": self.failure_code,
            "failure_evidence": (
                None
                if self.failure_evidence is None
                else self.failure_evidence.to_dict()
            ),
            "source_cache_membership_identity": (
                None
                if self.source_cache_membership is None
                else self.source_cache_membership.identity.uri
            ),
            "source_cache_publication_identity": _uri(
                self.source_cache_publication_identity
            ),
            "source_admission_identity": _uri(self.source_admission_identity),
        }


@dataclass(frozen=True, slots=True)
class StandardProjectLifecycleResult:
    execution_plan_identity: ContentIdentity
    validation_identity: ContentIdentity
    project_build_plan_identity: ContentIdentity | None
    generation_schedule: SourceGenerationScheduleResult
    node_results: tuple[StandardNodeLifecycleResult, ...]
    lifecycle_membership: StandardProjectLifecycleMembership
    admission_identity: ContentIdentity | None
    aggregate_receipt: StandardAggregateReceipt | None
    receipt_identity: ContentIdentity | None
    project_build_plan: StandardProjectBuildPlan | None = field(
        default=None, repr=False, compare=False
    )
    root_integration_evidence_identity: ContentIdentity | None = None
    context_prompt_journal_identities: tuple[ContentIdentity, ...] = ()
    context_benchmark_records: tuple[ComponentContextBenchmarkRecord, ...] = ()
    context_cache_report: ForwardGenerationContextCacheReport | None = None
    candidate_attempt_chains: tuple[CandidateAttemptChain, ...] = ()
    root_integration: StandardRootIntegrationEvidence | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (self.root_integration is None) != (
            self.root_integration_evidence_identity is None
        ) or (
            self.root_integration is not None
            and self.root_integration.identity
            != self.root_integration_evidence_identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.root_integration_mismatch",
                "retained root integration must match its exact evidence identity",
            )
        chain_components = tuple(
            item.component_revision.uri for item in self.candidate_attempt_chains
        )
        if any(
            not isinstance(item, CandidateAttemptChain)
            for item in self.candidate_attempt_chains
        ) or chain_components != tuple(sorted(set(chain_components))):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.repair_chain_invalid",
                "candidate attempt chains must be typed, unique, and canonical",
            )
        results_by_component = {
            item.component_revision.uri: item for item in self.node_results
        }
        for chain in self.candidate_attempt_chains:
            result = results_by_component.get(chain.component_revision.uri)
            if (
                result is None
                or chain.attempts[-1].lifecycle_result_identity != result.identity
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.repair_chain_mismatch",
                    "attempt chain must end at the exact retained lifecycle result",
                )
        context_lengths = {
            len(self.context_prompt_journal_identities),
            len(self.context_benchmark_records),
            (
                0
                if self.context_cache_report is None
                else len(self.context_cache_report.entries)
            ),
        }
        if context_lengths != {0} and context_lengths != {len(self.node_results)}:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.context_evidence_incomplete",
                "context journals, benchmarks, and cache entries must cover every node",
            )
        if self.context_cache_report is not None:
            for result, journal_identity, benchmark, cache_entry in zip(
                self.node_results,
                self.context_prompt_journal_identities,
                self.context_benchmark_records,
                self.context_cache_report.entries,
                strict=True,
            ):
                source = result.source_generation
                if (
                    benchmark.component_revision != result.component_revision
                    or benchmark.node_result_identity != source.identity
                    or benchmark.prompt_journal_identity != journal_identity
                    or benchmark.context_manifest_identity
                    != source.context_manifest_identity
                    or benchmark.complexity_budget_identity
                    != source.complexity_budget_identity
                    or benchmark.complexity_decision_identity
                    != source.complexity_decision_identity
                    or cache_entry.component_revision != result.component_revision
                    or cache_entry.prompt_journal_identity != journal_identity
                    or cache_entry.context_manifest_identity
                    != source.context_manifest_identity
                    or cache_entry.complexity_budget_identity
                    != source.complexity_budget_identity
                    or cache_entry.complexity_decision_identity
                    != source.complexity_decision_identity
                ):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.context_evidence_mismatch",
                        "context evidence differs from its exact source-generation "
                        "node",
                    )
        if self.project_build_plan is not None and (
            not isinstance(self.project_build_plan, StandardProjectBuildPlan)
            or self.project_build_plan.identity != self.project_build_plan_identity
            or self.project_build_plan.execution_plan_identity
            != self.execution_plan_identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.project_plan_mismatch",
                "retained project build plan must match its exact recorded identity",
            )
        if self.project_build_plan is not None:
            planned = {
                item.component_revision: item.identity
                for item in self.project_build_plan.components
            }
            recorded = {
                item.component_revision: item.build_plan_identity
                for item in self.node_results
                if item.build_plan_identity is not None
            }
            if planned != recorded:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.project_plan_membership_mismatch",
                    "retained project build plans must match every recorded node plan",
                )
        if not isinstance(
            self.lifecycle_membership, StandardProjectLifecycleMembership
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.membership_invalid",
                "project result requires typed lifecycle membership",
            )
        if (
            self.lifecycle_membership.execution_plan_identity
            != self.execution_plan_identity
            or self.lifecycle_membership.lifecycle_result_identities
            != tuple(item.identity for item in self.node_results)
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.membership_mismatch",
                "project membership must bind every exact lifecycle result",
            )
        for decision, result in zip(
            self.lifecycle_membership.cache_decisions,
            self.node_results,
            strict=True,
        ):
            accepted_identity = (
                None
                if result.source_cache_membership is None
                else result.source_cache_membership.identity
            )
            if (
                decision.accepted_membership_identity != accepted_identity
                or decision.publication_identity
                != result.source_cache_publication_identity
                or decision.failure_identity
                != (
                    None
                    if result.failure_evidence is None
                    else result.failure_evidence.identity
                )
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.membership_evidence_mismatch",
                    "cache decision differs from its exact lifecycle result evidence",
                )
        complete_receipt = (
            self.admission_identity is not None
            and self.aggregate_receipt is not None
            and self.receipt_identity is not None
        )
        if complete_receipt:
            assert self.admission_identity is not None
            assert self.aggregate_receipt is not None
            assert self.receipt_identity is not None
            if self.context_cache_report is None:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.context_evidence_required",
                    "a successful aggregate receipt requires complete context evidence",
                )
            if any(
                decision.accepted_membership_identity is not None
                and decision.outcome is not StandardNodeCacheOutcome.HIT
                and decision.publication_identity
                != decision.accepted_membership_identity
                for decision in self.lifecycle_membership.cache_decisions
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_publication_incomplete",
                    "a successful aggregate receipt requires every accepted cache "
                    "miss to be published",
                )
            if (
                self.aggregate_receipt.execution_plan_identity
                != self.execution_plan_identity
                or self.aggregate_receipt.lifecycle_membership_identity
                != self.lifecycle_membership.identity
                or self.aggregate_receipt.lifecycle_result_identities
                != tuple(item.identity for item in self.node_results)
                or self.aggregate_receipt.admission_identity != self.admission_identity
                or self.aggregate_receipt.root_integration_evidence_identity
                != self.root_integration_evidence_identity
                or self.aggregate_receipt.context_prompt_journal_identities
                != self.context_prompt_journal_identities
                or self.aggregate_receipt.context_benchmark_record_identities
                != tuple(item.identity for item in self.context_benchmark_records)
                or self.aggregate_receipt.context_cache_report_identity
                != self.context_cache_report.identity
                or self.receipt_identity != self.aggregate_receipt.identity
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.receipt_membership_mismatch",
                    "aggregate receipt must bind the exact project membership",
                )
        elif any(
            item is not None
            for item in (
                self.admission_identity,
                self.aggregate_receipt,
                self.receipt_identity,
                self.root_integration_evidence_identity,
            )
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.receipt_state_invalid",
                "admission, aggregate receipt, and receipt identity must exist "
                "together",
            )

    @property
    def successful(self) -> bool:
        return self.admission_identity is not None and self.receipt_identity is not None

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """Return the existing identity payload for evidence retention."""

        return {
            "schema": "literate-ai/standard-project-lifecycle-result@4",
            "execution_plan_identity": self.execution_plan_identity.uri,
            "validation_identity": self.validation_identity.uri,
            "project_build_plan_identity": _uri(self.project_build_plan_identity),
            "generation_schedule_identity": self.generation_schedule.identity.uri,
            "node_results": [item.identity.uri for item in self.node_results],
            "lifecycle_membership_identity": self.lifecycle_membership.identity.uri,
            "admission_identity": _uri(self.admission_identity),
            "aggregate_receipt_identity": (
                None
                if self.aggregate_receipt is None
                else self.aggregate_receipt.identity.uri
            ),
            "receipt_identity": _uri(self.receipt_identity),
            "root_integration_evidence_identity": _uri(
                self.root_integration_evidence_identity
            ),
            "context_prompt_journal_identities": [
                item.uri for item in self.context_prompt_journal_identities
            ],
            "context_benchmark_record_identities": [
                item.identity.uri for item in self.context_benchmark_records
            ],
            "context_cache_report_identity": (
                None
                if self.context_cache_report is None
                else self.context_cache_report.identity.uri
            ),
            "candidate_attempt_chain_identities": [
                item.identity.uri for item in self.candidate_attempt_chains
            ],
        }


def _uri(value: ContentIdentity | None) -> str | None:
    return None if value is None else value.uri


def _require_identity(value: object, label: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.evidence_identity_invalid",
            f"{label} port must return a ContentIdentity",
        )
    return value


def _prepared_identity(value: object, label: str) -> ContentIdentity:
    if isinstance(value, ContentIdentity):
        return value
    if isinstance(value, str):
        try:
            return ContentIdentity.parse_uri(value)
        except ValueError as exc:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.prepared_identity_invalid",
                f"{label} must be a canonical content identity",
            ) from exc
    raise StandardProjectLifecycleError(
        "standard_lifecycle.prepared_identity_invalid",
        f"{label} must be a canonical content identity",
    )


def rebind_standard_source_cache_generation(
    execution_plan: ComponentExecutionPlan,
    prepared: PreparedComponentGenerationNode[object, object],
    membership: StandardSourceCacheMembership,
) -> SourceGenerationResumeCandidate:
    """Project immutable cached artifacts into the current authoring custody."""

    plan = prepared.plan
    if (
        membership.component_revision != plan.component_revision
        or membership.generation_key_identity != plan.generation_key.identity
    ):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.cache_membership_mismatch",
            "source-cache membership does not bind its exact planned Component",
        )
    return _rebind_standard_source_generation(
        execution_plan,
        prepared,
        membership.generation,
        input_identity=membership.identity,
        custody_schema="literate-ai/standard-source-cache-custody-rebind@1",
        require_exact_generation=False,
    )


def rebind_standard_source_admission_generation(
    execution_plan: ComponentExecutionPlan,
    prepared: PreparedComponentGenerationNode[object, object],
    membership: StandardSourceAdmissionMembership,
) -> SourceGenerationResumeCandidate:
    """Project verifier-admitted source into current worker custody."""

    plan = prepared.plan
    if (
        membership.component_revision != plan.component_revision
        or membership.generation_key_identity != plan.generation_key.identity
        or membership.evidence.generation_plan_identity != plan.identity
        or membership.evidence.component_lock_identity
        != execution_plan.component_lock_identity
    ):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.source_admission_mismatch",
            "source admission does not bind the exact worker plan and Component lock",
        )
    return _rebind_standard_source_generation(
        execution_plan,
        prepared,
        membership.generation,
        input_identity=membership.identity,
        custody_schema="literate-ai/standard-source-admission-custody-rebind@1",
        require_exact_generation=False,
    )


def rebind_standard_source_checkpoint_generation(
    execution_plan: ComponentExecutionPlan,
    prepared: PreparedComponentGenerationNode[object, object],
    generation: SourceGenerationResumeCandidate,
    *,
    checkpoint_identity: ContentIdentity,
) -> SourceGenerationResumeCandidate:
    """Rebind pre-acceptance source to current workspace custody."""

    if not isinstance(generation, SourceGenerationResumeCandidate):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.source_checkpoint_invalid",
            "source checkpoint generation must be typed",
        )
    return _rebind_standard_source_generation(
        execution_plan,
        prepared,
        generation,
        input_identity=checkpoint_identity,
        custody_schema="literate-ai/standard-source-checkpoint-custody-rebind@1",
        require_exact_generation=True,
    )


def _rebind_standard_source_generation(
    execution_plan: ComponentExecutionPlan,
    prepared: PreparedComponentGenerationNode[object, object],
    generation: SourceGenerationResumeCandidate,
    *,
    input_identity: ContentIdentity,
    custody_schema: str,
    require_exact_generation: bool,
) -> SourceGenerationResumeCandidate:
    plan = prepared.plan
    request = prepared.request.request
    workspace = prepared.workspace
    recipe_identity = _prepared_identity(
        getattr(prepared.recipe, "identity", None), "prepared recipe identity"
    )
    raw_component_lock_identity = getattr(
        prepared.recipe, "component_lock_identity", None
    )
    if raw_component_lock_identity is not None:
        prepared_component_lock_identity = _prepared_identity(
            raw_component_lock_identity,
            "prepared recipe Component lock identity",
        )
        if prepared_component_lock_identity != execution_plan.component_lock_identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_custody_lock_mismatch",
                "prepared recipe Component lock differs from the execution plan",
            )
    component_lock_identity = execution_plan.component_lock_identity
    cached = generation.output
    cached_candidate = cached.candidate
    if require_exact_generation and (
        cached_candidate.component_revision != plan.component_revision
        or cached_candidate.component_generation_plan_identity != plan.identity
        or cached_candidate.generation_key_identity != plan.generation_key.identity
        or cached_candidate.recipe_identity != recipe_identity
    ):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.source_checkpoint_mismatch",
            "reusable source does not bind the exact planned Component and recipe",
        )
    candidate = GeneratedSourceCandidate(
        plan.component_revision,
        request.identity,
        cached_candidate.planned_coding_cli_request_identity,
        plan.identity,
        plan.generation_key.identity,
        request.context_manifest_identity,
        request.prompt_identity,
        recipe_identity,
        workspace.allocation_identity,
        cached_candidate.tree_identity,
        cached_candidate.source_bundle_identity,
        cached_candidate.source_manifest_identity,
        cached_candidate.source_bom_identity,
        cached_candidate.generated_test_suite_identity,
    )
    provenance = SourceGenerationProvenance(
        request.identity,
        cached_candidate.planned_coding_cli_request_identity,
        component_lock_identity,
        execution_plan.root_revision,
        plan.component_revision,
        plan.identity,
        plan.generation_key.identity,
        request.context_manifest_identity,
        request.prompt_identity,
        recipe_identity,
        workspace.allocation_identity,
        canonical_identity(
            {
                "schema": custody_schema,
                "input_identity": input_identity.uri,
                "component_lock_identity": component_lock_identity.uri,
                "application_root_revision_identity": execution_plan.root_revision.uri,
                "component_generation_plan_identity": plan.identity.uri,
                "source_generation_request_identity": request.identity.uri,
                "planned_coding_cli_request_identity": (
                    cached_candidate.planned_coding_cli_request_identity.uri
                ),
                "recipe_identity": recipe_identity.uri,
                "workspace_allocation_identity": workspace.allocation_identity.uri,
            }
        ),
        cached.provenance.route_decision_identities,
        cached.provenance.model_stage_output_identities,
        candidate.identity,
        provider_evidence_identities=cached.provenance.provider_evidence_identities,
        retained_source_identity=cached.provenance.retained_source_identity,
    )
    output = SourceGenerationRunOutput(
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
        None,
    )
    return SourceGenerationResumeCandidate(
        output,
        output.identity,
        request.budget.identity,
        request.complexity_decision_identity,
    )


def _exports_satisfy_manifest(
    exports: tuple[ArtifactExport, ...], manifest: ComponentBuildManifest
) -> bool:
    try:
        realize_manifest(manifest, exports)
    except ArtifactAssemblyError:
        return False
    return True


def _layers(execution_plan: ComponentExecutionPlan) -> tuple[tuple[str, ...], ...]:
    revisions = {
        item.component_revision.uri: item.component_revision
        for item in execution_plan.generation_plans
    }
    try:
        graph = AuthorityGraph.create(
            "standard-lifecycle",
            (
                AuthorityGraphNode(
                    uri,
                    "component-revision",
                    revision.digest[:12],
                    "component-lock",
                    uri,
                    inheritable=False,
                    properties=(("identity", uri),),
                )
                for uri, revision in revisions.items()
            ),
            (
                AuthorityGraphEdge(
                    edge.provider_revision.uri,
                    edge.consumer_revision.uri,
                    "component-dependency",
                    edge.requirement_id,
                )
                for action in execution_plan.action_plans
                for edge in action.dependency_edges
            ),
        )
        return graph.topological_layers()
    except AuthorityGraphError as exc:
        if exc.code == "graph.cycle":
            raise StandardProjectLifecycleError(
                "standard_lifecycle.graph_cyclic",
                "combined lifecycle graph is cyclic: "
                + exc.message.removeprefix("directed cycle detected: "),
            ) from exc
        raise StandardProjectLifecycleError(exc.code, exc.message) from exc


@dataclass(frozen=True, slots=True)
class _LifecycleStep:
    """One local operation; a continuation is never a serializable worker request."""

    stage: StandardLifecycleStage
    operation: Callable[[], object]
    guard: Callable[[], None] | None = None
    reserve: Callable[[], ReservedLifecycleOperation | None] | None = None

    def execute(self) -> object:
        if self.guard is not None:
            self.guard()
        return self.operation()


class StandardProjectLifecycleService:
    def __init__(
        self,
        *,
        validator: ProjectValidator,
        build_intent_factory: ComponentBuildIntentFactory,
        build_plan_finalizer: ComponentBuildPlanFinalizer,
        generator: ComponentSourceGenerationRunner,
        indexer: GenerationIndexer,
        authorizer: BuildAuthorizer,
        builder: ComponentBuilder,
        tester: ComponentTester,
        executor: ComponentExecutor,
        acceptor: ComponentAcceptor,
        source_cache_publisher: AcceptedSourceCachePublisher,
        artifact_assembler: ProjectArtifactAssembler,
        package_creator: ProjectPackageCreator,
        root_integration_tester: RootIntegrationTester,
        packaged_project_executor: PackagedProjectExecutor,
        independent_project_acceptor: IndependentProjectAcceptor,
        admitter: ProjectAdmitter,
        receipt_issuer: ProjectReceiptIssuer,
        build_intent_dispatcher: AdmittedBuildIntentDispatcher | None = None,
        checkpoint_recorder: StandardLifecycleCheckpointRecorder | None = None,
        context_evidence_recorder: StandardContextEvidenceRecorder | None = None,
        candidate_repair_port: StandardCandidateRepairPort | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.validator = validator
        self.build_intent_factory = build_intent_factory
        self.build_intent_dispatcher = build_intent_dispatcher
        self.build_plan_finalizer = build_plan_finalizer
        self.generator = generator
        self.indexer = indexer
        self.authorizer = authorizer
        self.builder = builder
        self.tester = tester
        self.executor = executor
        self.acceptor = acceptor
        self.source_cache_publisher = source_cache_publisher
        self.artifact_assembler = artifact_assembler
        self.package_creator = package_creator
        self.root_integration_tester = root_integration_tester
        self.packaged_project_executor = packaged_project_executor
        self.independent_project_acceptor = independent_project_acceptor
        self.admitter = admitter
        self.receipt_issuer = receipt_issuer
        self.checkpoint_recorder = checkpoint_recorder
        self.context_evidence_recorder = context_evidence_recorder
        self.candidate_repair_port = candidate_repair_port
        self.clock = clock

    def _record_stage(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        prepared: PreparedComponentGenerationNode[object, object],
        output: SourceGenerationRunOutput,
        stage: StandardLifecycleStage,
        subject_identity: ContentIdentity,
        *,
        failure_code: str | None = None,
    ) -> None:
        recorder = self.checkpoint_recorder
        if recorder is None:
            return
        recipe_identity = _prepared_identity(
            getattr(prepared.recipe, "identity", None), "prepared recipe identity"
        )
        recorder.record(
            StandardLifecycleStageEvidence(
                execution_plan.identity,
                generation_plan.component_revision,
                generation_plan.identity,
                generation_plan.generation_key.identity,
                recipe_identity,
                SourceGenerationResumeCandidate(
                    output,
                    output.identity,
                    prepared.request.request.budget.identity,
                    prepared.request.request.complexity_decision_identity,
                ),
                stage,
                subject_identity,
                (
                    StandardLifecycleCheckpointOutcome.PASSED
                    if failure_code is None
                    else StandardLifecycleCheckpointOutcome.FAILED
                ),
                failure_code,
            )
        )

    def execute(
        self,
        execution_plan: ComponentExecutionPlan,
        *,
        component_lock: ComponentLock,
        invalidation: ComponentInvalidationDecision,
        prepared_nodes: Mapping[str, PreparedComponentGenerationNode[object, object]],
        source_cache_memberships: Mapping[
            str, StandardSourceCacheMembership | StandardSourceAdmissionMembership
        ]
        | None = None,
        source_generation_resume_candidates: Mapping[
            str, SourceGenerationResumeCandidate
        ]
        | None = None,
        resume_candidates: Mapping[str, StandardNodeAcceptedCandidate] | None = None,
        max_parallelism: int = 1,
        component_worker_routing: ComponentWorkerRouting | None = None,
        worker_catalog: ExecutionWorkerCatalog | None = None,
        component_node_dispatcher: ComponentNodeDispatcher | None = None,
        component_node_recovery: (
            Mapping[str, ComponentNodeRecoveryCandidate] | None
        ) = None,
        cancellation: ComponentNodeCancellation | None = None,
    ) -> StandardProjectLifecycleResult:
        if not isinstance(execution_plan, ComponentExecutionPlan):
            raise TypeError("execution_plan must be a ComponentExecutionPlan")
        if not isinstance(component_lock, ComponentLock):
            raise TypeError("component_lock must be a ComponentLock")
        lock_nodes = {item.revision.identity.uri for item in component_lock.nodes}
        planned_nodes = {
            item.component_revision.uri for item in execution_plan.generation_plans
        }
        if component_lock.identity != execution_plan.component_lock_identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.lock_identity_mismatch",
                "Component lock does not match the execution plan's exact authority",
            )
        if component_lock.root_revision != execution_plan.root_revision:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.lock_root_mismatch",
                "Component lock root does not match the execution plan root",
            )
        if lock_nodes != planned_nodes:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.lock_node_set_mismatch",
                "Component lock nodes do not match every and only planned Component",
            )
        if not isinstance(invalidation, ComponentInvalidationDecision):
            raise TypeError("invalidation must be a ComponentInvalidationDecision")
        if (
            isinstance(max_parallelism, bool)
            or not isinstance(max_parallelism, int)
            or not 1 <= max_parallelism <= 256
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.parallelism_invalid",
                "max_parallelism must be between 1 and 256",
            )
        worker_values = (
            component_worker_routing,
            worker_catalog,
            component_node_dispatcher,
        )
        if any(item is not None for item in worker_values) and any(
            item is None for item in worker_values
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.worker_configuration_incomplete",
                "worker routing, current catalog, and dispatcher are required together",
            )
        if component_worker_routing is not None:
            assert worker_catalog is not None
            try:
                validate_component_worker_routing(
                    component_worker_routing,
                    execution_plan,
                    component_lock,
                    worker_catalog,
                )
            except ComponentWorkerError as exc:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.worker_routing_invalid", str(exc)
                ) from exc
        generation_plans = {
            item.component_revision.uri: item
            for item in execution_plan.generation_plans
        }
        expected = {
            item.component_revision.uri for item in execution_plan.generation_plans
        }
        nodes = dict(prepared_nodes)
        if set(nodes) != expected:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.preparation_incomplete",
                "prepared nodes must cover every and only executable Component",
            )
        for uri, generation_plan in generation_plans.items():
            prepared = nodes[uri]
            if not isinstance(prepared, PreparedComponentGenerationNode):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.preparation_invalid",
                    "prepared node values must be typed",
                )
            try:
                validate_prepared_component_generation_node(
                    prepared, expected_plan=generation_plan
                )
            except Exception as exc:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.preparation_identity_mismatch",
                    "prepared node does not bind its exact Component plan",
                ) from exc
        supplied = {} if resume_candidates is None else dict(resume_candidates)
        cached = (
            {} if source_cache_memberships is None else dict(source_cache_memberships)
        )
        source_resumes = (
            {}
            if source_generation_resume_candidates is None
            else dict(source_generation_resume_candidates)
        )
        unknown = (set(supplied) | set(cached) | set(source_resumes)) - expected
        if unknown:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.resume_unknown",
                "resume contains an unknown Component",
            )
        worker_recovery = (
            {} if component_node_recovery is None else dict(component_node_recovery)
        )
        if set(worker_recovery) - expected:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.worker_recovery_unknown",
                "worker recovery contains an unknown Component",
            )
        if any(
            not isinstance(item, StandardNodeAcceptedCandidate)
            for item in supplied.values()
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.resume_invalid",
                "resume candidate values must be typed",
            )
        if any(
            not isinstance(
                item,
                (StandardSourceCacheMembership, StandardSourceAdmissionMembership),
            )
            for item in cached.values()
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_membership_invalid",
                "source-cache membership values must be typed",
            )
        if any(
            not isinstance(item, SourceGenerationResumeCandidate)
            for item in source_resumes.values()
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.source_resume_invalid",
                "source-generation resume candidate values must be typed",
            )
        if set(source_resumes) & (set(supplied) | set(cached)):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.source_resume_conflict",
                "source checkpoint resume cannot conflict with accepted source reuse",
            )
        for uri, membership in cached.items():
            generation_plan = generation_plans[uri]
            if (
                membership.component_revision != generation_plan.component_revision
                or membership.generation_key_identity
                != generation_plan.generation_key.identity
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_membership_mismatch",
                    "source-cache membership does not bind its exact planned Component",
                )
        for uri in set(supplied) & set(cached):
            if supplied[uri].source_cache_membership != cached[uri]:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_membership_conflict",
                    "accepted-node resume and source-cache lookup disagree",
                )
        input_membership_identities = {
            uri: membership.identity for uri, membership in cached.items()
        }
        for uri, candidate in supplied.items():
            input_membership_identities.setdefault(
                uri, candidate.source_cache_membership.identity
            )
        referenced = {
            invalidation.changed_component.uri,
            *(item.uri for item in invalidation.regenerate),
            *(item.uri for item in invalidation.rebuild),
            *(item.uri for item in invalidation.retest),
        }
        if not referenced <= expected:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.invalidation_foreign",
                "invalidation references a Component outside the exact plan",
            )
        validation = _require_identity(
            self.validator.validate(execution_plan), "validation"
        )
        scoped_execution = component_worker_routing is None and callable(
            getattr(self.executor, "execute_scoped", None)
        )
        runtime_providers = {uri: set() for uri in expected}
        dependencies = {uri: set() for uri in expected}
        build_providers = {uri: set() for uri in expected}
        package_providers = {uri: set() for uri in expected}
        for action in execution_plan.action_plans:
            for edge in action.dependency_edges:
                if edge.kind is DependencyKind.RUNTIME and scoped_execution:
                    runtime_providers[edge.consumer_revision.uri].add(
                        edge.provider_revision.uri
                    )
                    continue
                if (
                    edge.semantics.consumed_input
                    is not DependencyInputKind.PUBLIC_INTERFACE
                    and (
                        edge.semantics.consumed_input is not DependencyInputKind.PACKAGE
                        or component_worker_routing is not None
                    )
                ):
                    dependencies[edge.consumer_revision.uri].add(
                        edge.provider_revision.uri
                    )
                # Packaging edges carry exact package provenance into package
                # assembly/acceptance elsewhere (create_package_plan draws on the
                # accepted lifecycle's own artifact graph); they must never require
                # a build-time process environment binding for a Component that
                # only consumes the provider for packaging (issue #110).
                if edge.semantics.consumed_input in {
                    DependencyInputKind.ARTIFACT_EXPORT,
                    DependencyInputKind.TOOLCHAIN,
                }:
                    build_providers[edge.consumer_revision.uri].add(
                        edge.provider_revision.uri
                    )
                elif (
                    edge.semantics.consumed_input is DependencyInputKind.PACKAGE
                    and component_worker_routing is not None
                ):
                    # Complete-node transports retain their legacy input handoff.
                    # Local phases bind these inputs only at project assembly.
                    package_providers[edge.consumer_revision.uri].add(
                        edge.provider_revision.uri
                    )
        regenerate = {item.uri for item in invalidation.regenerate}
        generated: dict[str, SourceGenerationNodeResult] = {}
        planned: dict[str, StandardComponentBuildPlan] = {}
        results: dict[str, StandardNodeLifecycleResult] = {}
        evidence_nodes: dict[str, PreparedComponentGenerationNode[object, object]] = {}
        repair_chains: dict[str, CandidateAttemptChain] = {}
        worker_products: dict[str, ComponentWorkerProduct] = {}
        worker_assignments = (
            {}
            if component_worker_routing is None
            else {
                item.component_revision.uri: item
                for item in component_worker_routing.assignments
            }
        )
        _layers(execution_plan)  # Validate the entire graph before running a node.
        # Generation consumes locked interfaces and can precede provider acceptance.
        # Indexing also consumes only this Component's exact source. Build intent
        # waits for accepted providers before resolving their export identities.
        # All phases share one pool and slot bound. Explicit complete-node routing
        # keeps its transport custody until that transport supports separate phases.
        futures: dict[Future[object], tuple[str, str]] = {}
        step_runs: dict[str, Generator[_LifecycleStep, object, object]] = {}
        pending_steps: dict[str, _LifecycleStep] = {}

        def advance_step(uri: str, completed: Future[object] | None = None) -> None:
            steps = step_runs[uri]
            try:
                if completed is None:
                    step = next(steps)
                else:
                    try:
                        value = completed.result()
                    except Exception as exc:
                        step = steps.throw(exc)
                    else:
                        step = steps.send(value)
                if not isinstance(step, _LifecycleStep):
                    raise TypeError("lifecycle continuation returned an invalid step")
                pending_steps[uri] = step
            except StopIteration as finished:
                terminal: Future[object] = Future()
                terminal.set_result(finished.value)
                futures[terminal] = (uri, "lifecycle")
                del step_runs[uri]
            except Exception as exc:
                terminal = Future()
                terminal.set_exception(exc)
                futures[terminal] = (uri, "lifecycle")
                del step_runs[uri]

        def artifact_inputs(
            uri: str,
        ) -> tuple[tuple[ArtifactExport, ...], tuple[ArtifactExport, ...]]:
            # Artifact-export and toolchain providers become process
            # environment bindings for this node's build. Packaging-only
            # providers carry package result/plan provenance into package
            # assembly and acceptance separately (see create_package_plan /
            # create_standard_artifact_build_graph) and must not require a
            # runtime binding here (issue #110).
            provider_artifacts = tuple(
                sorted(
                    (
                        export
                        for parent in build_providers[uri]
                        for export in results[parent].exports
                    ),
                    key=lambda item: item.identity.uri,
                )
            )
            package_artifacts = tuple(
                sorted(
                    (
                        export
                        for parent in package_providers[uri]
                        for export in results[parent].exports
                    ),
                    key=lambda item: item.identity.uri,
                )
            )
            return provider_artifacts, package_artifacts

        def accepted_build_providers(uri):
            evidence = []
            from literate_ai.application.standard_provider_receipts import (
                select_build_provider_receipts,
            )

            for parent in sorted(results):
                result = results[parent]
                if result.acceptance_evidence is None:
                    continue
                receipt = result.acceptance_evidence
                if (
                    receipt is None
                    or receipt.identity != result.acceptance_identity
                    or receipt.component_revision != result.component_revision
                    or receipt.build.exports != result.exports
                ):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.provider_acceptance_missing",
                        "remote build intent requires exact accepted provider evidence",
                    )
                evidence.append(receipt)
            try:
                return select_build_provider_receipts(
                    artifact_inputs(uri)[0], tuple(evidence)
                )
            except ValueError as exc:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.provider_acceptance_missing",
                    "BUILD requires exact accepted provider evidence "
                    "for its complete dependency closure",
                ) from exc

        def execution_inputs(uri, plan, exports):
            from literate_ai.application.standard_execution_inputs import (
                plan_standard_execution_inputs,
                standard_execution_provider_revisions,
            )

            revisions = standard_execution_provider_revisions(
                execution_plan, plan.component_revision
            )
            providers = tuple(results[revision.uri] for revision in revisions)
            scope = plan_standard_execution_inputs(
                execution_plan, plan, exports, providers
            )
            if isinstance(self.executor, ExecutionProviderEvidenceReceiver):
                from literate_ai.application.standard_execution_inputs import (
                    plan_standard_execution_receipts,
                )

                receipts = []
                for provider in providers:
                    receipt = provider.acceptance_evidence
                    if (
                        not isinstance(receipt, StandardComponentAcceptanceEvidence)
                        or receipt.identity != provider.acceptance_identity
                        or receipt.component_revision != provider.component_revision
                        or receipt.build.exports != provider.exports
                        or receipt.build.build_plan_identity
                        != provider.build_plan_identity
                        or receipt.build.identity != provider.build_identity
                        or receipt.generated_tests.identity != provider.test_identity
                        or receipt.execution.identity != provider.execution_identity
                    ):
                        raise StandardProjectLifecycleError(
                            "standard_lifecycle.execution_provider_acceptance_missing",
                            "EXECUTE requires exact accepted runtime provider evidence",
                        )
                    receipts.append(receipt)
                receipts = tuple(receipts)
                if (
                    plan_standard_execution_receipts(
                        execution_plan, plan, exports, receipts
                    )
                    != scope
                ):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.execution_provider_scope_mismatch",
                        "runtime receipts differ from current execution scope",
                    )
                self.executor.retain_execution_provider_evidence(plan, scope, receipts)
            all_inputs = {
                item.identity.uri: item
                for provider in providers
                for item in provider.exports
            }
            return scope, tuple(all_inputs[key] for key in sorted(all_inputs))

        stage_kinds = {
            StandardLifecycleStage.SOURCE_GENERATION: LifecycleActionKind.GENERATE,
            StandardLifecycleStage.SOURCE_INDEX: LifecycleActionKind.INDEX,
            StandardLifecycleStage.BUILD_INTENT: LifecycleActionKind.BUILD_INTENT,
            StandardLifecycleStage.BUILD_AUTHORIZATION: LifecycleActionKind.AUTHORIZE,
            StandardLifecycleStage.BUILD_PLAN: LifecycleActionKind.PLAN,
            StandardLifecycleStage.BUILD: LifecycleActionKind.BUILD,
            StandardLifecycleStage.TEST: LifecycleActionKind.TEST,
            StandardLifecycleStage.EXECUTE: LifecycleActionKind.EXECUTE,
            StandardLifecycleStage.ACCEPT: LifecycleActionKind.ACCEPT,
        }
        local_action_order = (
            {
                (action.component_revision.uri, action.kind): action.identity.uri
                for action in plan_lifecycle_action_dag(
                    execution_plan, worker_ids=("local",)
                )
            }
            if component_worker_routing is None
            else {}
        )
        pending = set(expected)
        with ThreadPoolExecutor(max_workers=max_parallelism) as pool:
            while pending or futures or pending_steps:
                ready = sorted(
                    (
                        uri
                        for uri in pending
                        if (
                            dependencies[uri] <= results.keys()
                            if component_worker_routing is not None
                            else True
                        )
                    ),
                    key=lambda uri: generation_plans[uri].identity.uri,
                )
                for uri in ready:
                    failed_parent = component_worker_routing is not None and any(
                        results[parent].failure_code is not None
                        for parent in dependencies[uri]
                    )
                    if (
                        component_worker_routing is not None
                        and len(futures) >= max_parallelism
                        and not failed_parent
                    ):
                        continue
                    pending.remove(uri)
                    if failed_parent:
                        generation = self._cancelled_generation(nodes[uri])
                        generated[uri] = generation
                        results[uri] = self._cancelled(
                            generation_plans[uri].component_revision,
                            generation,
                            None,
                            None,
                            _failure(
                                generation_plans[uri].component_revision,
                                StandardNodeFailurePhase.DEPENDENCY,
                                generation_plans[uri].identity,
                                "required provider did not pass acceptance",
                                "dependency.failed",
                            ),
                        )
                        if self.context_evidence_recorder is not None:
                            self.context_evidence_recorder.record(
                                nodes[uri], generation
                            )
                        continue
                    provider_artifacts, package_artifacts = (
                        artifact_inputs(uri)
                        if component_worker_routing is not None
                        else ((), ())
                    )
                    run_arguments = (
                        execution_plan,
                        generation_plans[uri],
                        nodes[uri],
                        supplied.get(uri),
                        cached.get(uri),
                        source_resumes.get(uri),
                        provider_artifacts,
                        package_artifacts,
                        uri in regenerate,
                    )
                    if component_worker_routing is None:
                        step_runs[uri] = self._run_complete_node_steps(
                            *run_arguments,
                            None,
                            partial(artifact_inputs, uri),
                            partial(execution_inputs, uri)
                            if scoped_execution
                            else None,
                            partial(accepted_build_providers, uri),
                        )
                        advance_step(uri)
                        continue
                    else:
                        assert component_node_dispatcher is not None
                        handoff = component_artifact_handoff(
                            component_worker_routing,
                            execution_plan,
                            generation_plans[uri].component_revision,
                            worker_products,
                        )
                        reuse = (
                            supplied.get(uri)
                            or cached.get(uri)
                            or source_resumes.get(uri)
                        )
                        request = ComponentNodeDispatchRequest(
                            component_worker_routing.identity,
                            worker_assignments[uri],
                            generation_plans[uri].identity,
                            nodes[uri].request.request.identity,
                            handoff.identity,
                            tuple(item.identity for item in provider_artifacts),
                            tuple(item.identity for item in package_artifacts),
                            None if reuse is None else reuse.identity,
                            uri in regenerate,
                        )
                        if cancellation is not None and cancellation.is_set():
                            component_node_dispatcher.cancel(request)
                            generation = self._cancelled_generation(nodes[uri])
                            generated[uri] = generation
                            results[uri] = self._cancelled(
                                generation_plans[uri].component_revision,
                                generation,
                                None,
                                None,
                                _failure(
                                    generation_plans[uri].component_revision,
                                    StandardNodeFailurePhase.LIFECYCLE,
                                    request.identity,
                                    "dispatch cancelled",
                                    "dispatch.cancelled",
                                ),
                            )
                            if self.context_evidence_recorder is not None:
                                self.context_evidence_recorder.record(
                                    nodes[uri], generation
                                )
                            continue
                        future = pool.submit(
                            copy_context().run,
                            self._dispatch_complete_node,
                            component_worker_routing,
                            component_node_dispatcher,
                            request,
                            handoff,
                            worker_recovery.get(uri),
                            cancellation,
                            partial(self._run_complete_node, *run_arguments),
                        )
                    futures[future] = (uri, "lifecycle")
                waiting_for_capacity = False
                for uri in sorted(
                    pending_steps,
                    key=lambda item: local_action_order[
                        (item, stage_kinds[pending_steps[item].stage])
                    ],
                ):
                    if len(futures) >= max_parallelism:
                        break
                    step = pending_steps[uri]
                    required = (
                        dependencies[uri]
                        if step.stage is StandardLifecycleStage.BUILD_INTENT
                        else runtime_providers[uri]
                        if step.stage is StandardLifecycleStage.EXECUTE
                        else set()
                    )
                    if required:
                        if not required <= results.keys():
                            continue
                        if any(
                            results[parent].failure_code is not None
                            for parent in required
                        ):
                            del pending_steps[uri]
                            failure: Future[object] = Future()
                            failure.set_exception(
                                StandardProjectLifecycleError(
                                    "dependency.failed",
                                    "required provider did not pass acceptance",
                                )
                            )
                            advance_step(uri, failure)
                            continue
                    reservation = None
                    try:
                        if step.reserve is not None:
                            reservation = step.reserve()
                            if reservation is None:
                                waiting_for_capacity = True
                                continue
                        future = pool.submit(
                            copy_context().run,
                            self._run_reserved_step
                            if reservation is not None
                            else self._run_step,
                            step,
                            *(() if reservation is None else (reservation,)),
                        )
                    except Exception as exc:
                        if reservation is not None:
                            reservation.release()
                        del pending_steps[uri]
                        failure = Future()
                        failure.set_exception(exc)
                        advance_step(uri, failure)
                        continue
                    del pending_steps[uri]
                    futures[future] = (uri, "step")
                if not futures:
                    if pending and not ready:
                        raise StandardProjectLifecycleError(
                            "standard_lifecycle.dependency_deadlock",
                            "Component dependencies cannot make progress",
                        )
                    if pending_steps:
                        # Capacity can be held by direct port callers outside this pool.
                        # Admission rechecks its finite deadline on each bounded retry.
                        time.sleep(0.01)
                    continue
                completed, _ = wait(
                    tuple(futures),
                    timeout=0.05 if waiting_for_capacity else None,
                    return_when=FIRST_COMPLETED,
                )
                for future in sorted(completed, key=lambda item: futures[item]):
                    uri, phase = futures.pop(future)
                    if phase == "step":
                        advance_step(uri, future)
                        continue
                    try:
                        generation, plan, result, evidence_prepared, repair_chain = (
                            future.result()
                        )
                        failure = result.failure_evidence
                        if failure is not None and result.source_output is not None:
                            try:
                                failed_stage = StandardLifecycleStage(
                                    failure.phase.value
                                )
                            except ValueError:
                                failed_stage = None
                            if failed_stage is not None:
                                self._record_stage(
                                    execution_plan,
                                    generation_plans[uri],
                                    nodes[uri],
                                    result.source_output,
                                    failed_stage,
                                    failure.subject_identity,
                                    failure_code=failure.code,
                                )
                    except Exception as exc:
                        trace_exception("standard lifecycle node", exc)
                        generation = self._failed_generation(nodes[uri], exc)
                        plan = None
                        evidence_prepared = nodes[uri]
                        repair_chain = None
                        result = self._cancelled(
                            generation_plans[uri].component_revision,
                            generation,
                            None,
                            None,
                            _failure(
                                generation_plans[uri].component_revision,
                                StandardNodeFailurePhase.LIFECYCLE,
                                generation_plans[uri].identity,
                                exc,
                                "lifecycle.failed",
                            ),
                        )
                    if self.context_evidence_recorder is not None:
                        self.context_evidence_recorder.record(
                            evidence_prepared, generation
                        )
                    generated[uri] = generation
                    if plan is not None:
                        planned[uri] = plan
                    results[uri] = result
                    if (
                        component_worker_routing is not None
                        and result.acceptance_identity is not None
                        and result.exports
                    ):
                        worker_products[uri] = ComponentWorkerProduct(
                            result.component_revision,
                            worker_assignments[uri].worker_identity,
                            result.acceptance_identity,
                            result.exports,
                        )
                    evidence_nodes[uri] = evidence_prepared
                    if repair_chain is not None:
                        repair_chains[uri] = repair_chain
        schedule = SourceGenerationScheduleResult(
            execution_plan.identity,
            invalidation.identity,
            max_parallelism,
            tuple(generated[uri] for uri in sorted(generated)),
        )
        project_plan = (
            StandardProjectBuildPlan(
                execution_plan.identity,
                tuple(planned[uri] for uri in sorted(planned)),
            )
            if set(planned) == expected
            else None
        )
        ordered = tuple(results[uri] for uri in sorted(results))
        context_journals: tuple[ContentIdentity, ...] = ()
        context_benchmarks: tuple[ComponentContextBenchmarkRecord, ...] = ()
        context_cache_report: ForwardGenerationContextCacheReport | None = None
        if self.context_evidence_recorder is not None:
            context_journals = self.context_evidence_recorder.prompt_journal_identities
            context_benchmarks = self.context_evidence_recorder.benchmark_records
            context_cache_report = self.context_evidence_recorder.cache_report()
        if any(
            item.failure_code is not None or item.acceptance_identity is None
            for item in ordered
        ):
            membership = assemble_standard_lifecycle_membership(
                execution_plan,
                ordered,
                input_membership_identities=input_membership_identities,
                forced_regeneration=frozenset(regenerate),
                source_checkpoint_reuse=frozenset(source_resumes),
            )
            return StandardProjectLifecycleResult(
                execution_plan.identity,
                validation,
                None if project_plan is None else project_plan.identity,
                schedule,
                ordered,
                membership,
                None,
                None,
                None,
                project_plan,
                context_prompt_journal_identities=context_journals,
                context_benchmark_records=context_benchmarks,
                context_cache_report=context_cache_report,
                candidate_attempt_chains=tuple(
                    repair_chains[uri] for uri in sorted(repair_chains)
                ),
            )
        if project_plan is None:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.project_plan_incomplete",
                "root integration requires every exact Component build plan",
            )
        from .release_artifacts import plan_standard_assembly_dependencies

        artifact_graph, link_plan = self.artifact_assembler.assemble_project_artifacts(
            component_lock,
            execution_plan,
            project_plan,
            ordered,
        )
        if not isinstance(artifact_graph, ArtifactBuildGraph) or not isinstance(
            link_plan, ExactLinkPlan
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.artifact_assembly_invalid",
                "project artifact assembly must return a typed graph and link plan",
            )
        if link_plan not in artifact_graph.link_plans:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.artifact_link_mismatch",
                "project link plan must belong to the exact artifact graph",
            )
        if artifact_graph.assembly_dependencies != plan_standard_assembly_dependencies(
            execution_plan, ordered
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.assembly_dependencies_mismatch",
                "artifact graph must bind exact locked late dependencies "
                "and accepted providers",
            )
        package_plan, package_result = self.package_creator.create_project_package(
            component_lock,
            execution_plan,
            project_plan,
            artifact_graph,
            link_plan,
        )
        if not isinstance(package_plan, PackagePlan) or not isinstance(
            package_result, PackageResult
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.package_creation_invalid",
                "project package creation must return a typed plan and result",
            )
        if (
            package_plan.component_lock_identity != component_lock.identity
            or package_plan.artifact_graph_identity != artifact_graph.identity
            or package_plan.link_plan_identity != link_plan.identity
            or package_result.package_plan_identity != package_plan.identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.package_creation_mismatch",
                "project package must bind the exact lock, graph, link, and plan",
            )
        root_test_identity = _require_identity(
            self.root_integration_tester.test_root_integration(
                component_lock,
                execution_plan,
                project_plan,
                package_plan,
                package_result,
            ),
            "root generated integration test",
        )
        packaged_execution_identity = _require_identity(
            self.packaged_project_executor.execute_packaged_project(
                component_lock,
                execution_plan,
                project_plan,
                package_plan,
                package_result,
            ),
            "packaged project execution",
        )
        independent_acceptance_identity = _require_identity(
            self.independent_project_acceptor.accept_project_independently(
                component_lock,
                execution_plan,
                project_plan,
                package_plan,
                package_result,
                root_test_identity,
                packaged_execution_identity,
            ),
            "independent project acceptance",
        )
        root_integration = StandardRootIntegrationEvidence(
            component_lock.identity,
            execution_plan.identity,
            project_plan.identity,
            artifact_graph,
            link_plan,
            package_plan,
            package_result,
            root_test_identity,
            packaged_execution_identity,
            independent_acceptance_identity,
        )
        publication_failed = False
        for uri in sorted(results):
            if uri in input_membership_identities and uri not in regenerate:
                continue
            result = results[uri]
            membership = result.source_cache_membership
            if membership is None or result.source_output is None:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_publication_candidate_missing",
                    "project-accepted cache miss requires its exact accepted source",
                )
            try:
                publication_identity = self._publish_accepted_result(
                    result,
                    planned[uri],
                    regenerate=uri in regenerate,
                )
                self._record_stage(
                    execution_plan,
                    generation_plans[uri],
                    evidence_nodes[uri],
                    result.source_output,
                    StandardLifecycleStage.SOURCE_CACHE_PUBLICATION,
                    publication_identity,
                )
                result = replace(
                    result,
                    source_cache_publication_identity=publication_identity,
                )
            except Exception as exc:
                trace_exception("source-cache publication", exc)
                failure = _failure(
                    result.component_revision,
                    StandardNodeFailurePhase.SOURCE_CACHE_PUBLICATION,
                    membership.identity,
                    exc,
                    "source-cache-publication.failed",
                )
                result = self._cancelled(
                    result.component_revision,
                    result.source_generation,
                    result.source_output,
                    result.build_plan_identity,
                    failure,
                    exports=result.exports,
                    index_identity=result.index_identity,
                    authorization_identity=result.authorization_identity,
                    build_identity=result.build_identity,
                    test_identity=result.test_identity,
                    execution_identity=result.execution_identity,
                    source_admission_identity=result.source_admission_identity,
                )
                self._record_stage(
                    execution_plan,
                    generation_plans[uri],
                    evidence_nodes[uri],
                    result.source_output,
                    StandardLifecycleStage.SOURCE_CACHE_PUBLICATION,
                    failure.subject_identity,
                    failure_code=failure.code,
                )
                publication_failed = True
            results[uri] = result
            repair_chains[uri] = self._rebind_attempt_chain(repair_chains[uri], result)
            if publication_failed:
                break
        ordered = tuple(results[uri] for uri in sorted(results))
        membership = assemble_standard_lifecycle_membership(
            execution_plan,
            ordered,
            input_membership_identities=input_membership_identities,
            forced_regeneration=frozenset(regenerate),
            source_checkpoint_reuse=frozenset(source_resumes),
        )
        if publication_failed:
            return StandardProjectLifecycleResult(
                execution_plan.identity,
                validation,
                project_plan.identity,
                schedule,
                ordered,
                membership,
                None,
                None,
                None,
                project_plan,
                context_prompt_journal_identities=context_journals,
                context_benchmark_records=context_benchmarks,
                context_cache_report=context_cache_report,
                candidate_attempt_chains=tuple(
                    repair_chains[uri] for uri in sorted(repair_chains)
                ),
            )
        admission = _require_identity(self.admitter.admit(ordered), "admission")
        if context_cache_report is None:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.context_evidence_required",
                "successful project admission requires durable context evidence",
            )
        aggregate_receipt = create_standard_aggregate_receipt(
            execution_plan,
            membership,
            admission,
            context_journals,
            tuple(item.identity for item in context_benchmarks),
            context_cache_report.identity,
            root_integration.identity,
        )
        receipt_identity = _require_identity(
            self.receipt_issuer.issue(aggregate_receipt), "receipt"
        )
        if receipt_identity != aggregate_receipt.identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.receipt_membership_mismatch",
                "receipt issuer returned an identity outside aggregate membership",
            )
        return StandardProjectLifecycleResult(
            execution_plan.identity,
            validation,
            project_plan.identity if project_plan is not None else None,
            schedule,
            ordered,
            membership,
            admission,
            aggregate_receipt,
            receipt_identity,
            project_plan,
            root_integration.identity,
            context_journals,
            context_benchmarks,
            context_cache_report,
            tuple(repair_chains[uri] for uri in sorted(repair_chains)),
            root_integration,
        )

    def _dispatch_complete_node(
        self,
        routing: ComponentWorkerRouting,
        dispatcher: ComponentNodeDispatcher,
        request: ComponentNodeDispatchRequest,
        handoff: ComponentArtifactHandoff,
        recovery: ComponentNodeRecoveryCandidate | None,
        cancellation: ComponentNodeCancellation | None,
        execute: Callable[
            [],
            tuple[
                SourceGenerationNodeResult,
                StandardComponentBuildPlan | None,
                StandardNodeLifecycleResult,
                PreparedComponentGenerationNode[object, object],
                CandidateAttemptChain | None,
            ],
        ],
    ) -> tuple[
        SourceGenerationNodeResult,
        StandardComponentBuildPlan | None,
        StandardNodeLifecycleResult,
        PreparedComponentGenerationNode[object, object],
        CandidateAttemptChain | None,
    ]:
        """Dispatch one scheduler-owned node and admit only its correlated evidence."""

        component_revision = request.assignment.component_revision
        if recovery is None:
            outcome = dispatcher.dispatch(
                request,
                execute,
                cancellation=cancellation,
            )
        else:
            outcome = recover_component_node_result(
                routing,
                request,
                recovery,
                component_revision=component_revision,
                handoff=handoff,
            )
        return validate_component_node_outcome(
            request,
            outcome,
            component_revision=component_revision,
            handoff=handoff,
        )

    def _publish_accepted_result(
        self,
        result: StandardNodeLifecycleResult,
        plan: StandardComponentBuildPlan,
        *,
        regenerate: bool,
    ) -> ContentIdentity:
        membership = result.source_cache_membership
        if membership is None or result.source_output is None:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_publication_candidate_missing",
                "cache publication requires an accepted source membership",
            )
        complete_publisher = getattr(
            self.source_cache_publisher, "publish_accepted", None
        )
        publication = None
        if callable(complete_publisher):
            evidence = (
                result.build_evidence,
                result.generated_test_evidence,
                result.execution_evidence,
                result.acceptance_evidence,
            )
            if any(item is None for item in evidence):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_publication_evidence_missing",
                    "durable cache publication requires the complete typed "
                    "post-source evidence chain",
                )
            assert result.build_evidence is not None
            assert result.generated_test_evidence is not None
            assert result.execution_evidence is not None
            assert result.acceptance_evidence is not None
            if result.index_identity is None or result.authorization_identity is None:
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.cache_publication_evidence_missing",
                    "durable cache publication requires index and authorization "
                    "evidence",
                )
            publication = StandardAcceptedSourcePublication(
                result.component_revision,
                plan.identity,
                result.source_output,
                result.exports,
                result.index_identity,
                result.authorization_identity,
                membership,
                result.build_evidence,
                result.generated_test_evidence,
                result.execution_evidence,
                result.acceptance_evidence,
            )
            published = complete_publisher(publication)
        else:
            published = self.source_cache_publisher.publish(membership)
        publication_identity = _require_identity(
            published,
            "source-cache publication",
        )
        if publication_identity != membership.identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.cache_publication_mismatch",
                "source-cache publisher returned another membership identity",
            )
        if regenerate and publication is not None:
            retire = getattr(
                self.source_cache_publisher, "retire_prior_membership", None
            )
            if callable(retire):
                retire(publication)
        return publication_identity

    @staticmethod
    def _rebind_attempt_chain(
        chain: CandidateAttemptChain,
        result: StandardNodeLifecycleResult,
    ) -> CandidateAttemptChain:
        final = chain.attempts[-1]
        disposition = chain.disposition
        diagnostic = final.diagnostic
        attempt_disposition = final.disposition
        failure = result.failure_evidence
        if failure is not None:
            diagnostic = CandidateRepairDiagnostic(
                result.component_revision,
                failure.phase,
                failure.code,
                "project-accepted source could not be published to the cache",
                CandidateFailureClassification.TERMINAL,
                failure.identity,
            )
            attempt_disposition = CandidateAttemptDisposition.TERMINAL_REJECTED
            disposition = CandidateAttemptChainDisposition.TERMINAL
        rebound = replace(
            final,
            lifecycle_result_identity=result.identity,
            disposition=attempt_disposition,
            diagnostic=diagnostic,
        )
        return replace(
            chain,
            attempts=(*chain.attempts[:-1], rebound),
            disposition=disposition,
        )

    def _run_complete_node(self, *args, **kwargs):
        """Drive the steps inside an explicitly selected complete-node transport."""
        steps = self._run_complete_node_steps(*args, **kwargs)
        try:
            step = next(steps)
            while True:
                try:
                    value = self._run_step(step)
                except Exception as exc:
                    step = steps.throw(exc)
                else:
                    step = steps.send(value)
        except StopIteration as completed:
            return completed.value

    def _run_reserved_step(
        self, step: _LifecycleStep, reservation: ReservedLifecycleOperation
    ) -> object:
        try:
            if step.guard is not None:
                step.guard()
            return reservation.run()
        finally:
            reservation.release()

    def _run_step(self, step: _LifecycleStep) -> object:
        return step.execute()

    def _run_complete_node_steps(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        prepared: PreparedComponentGenerationNode[object, object],
        candidate: StandardNodeAcceptedCandidate | None,
        source_cache_membership: (
            StandardSourceCacheMembership | StandardSourceAdmissionMembership | None
        ),
        source_generation_resume: SourceGenerationResumeCandidate | None,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
        regenerate: bool,
        source_execution: ComponentSourceGenerationExecution | None = None,
        artifact_inputs: Callable[
            [], tuple[tuple[ArtifactExport, ...], tuple[ArtifactExport, ...]]
        ]
        | None = None,
        execution_inputs: _ExecutionInputResolver | None = None,
        accepted_build_providers: Callable[
            [], tuple[StandardComponentAcceptanceEvidence, ...]
        ]
        | None = None,
    ) -> Generator[
        _LifecycleStep,
        object,
        tuple[
            SourceGenerationNodeResult,
            StandardComponentBuildPlan | None,
            StandardNodeLifecycleResult,
            PreparedComponentGenerationNode[object, object],
            CandidateAttemptChain | None,
        ],
    ]:
        """Run one candidate plus at most two exact, fresh replacement attempts."""

        current = prepared
        attempts: list[CandidateAttempt] = []
        repair_request: CandidateRepairRequest | None = None
        original_recipe_identity = _prepared_identity(
            getattr(prepared.recipe, "identity", None), "prepared recipe identity"
        )
        for attempt_index in range(3):
            generation, plan, result = yield from self._run_complete_node_once_steps(
                execution_plan,
                generation_plan,
                current,
                candidate if attempt_index == 0 else None,
                source_cache_membership if attempt_index == 0 else None,
                source_generation_resume if attempt_index == 0 else None,
                provider_artifacts,
                package_artifacts,
                regenerate if attempt_index == 0 else True,
                source_execution if attempt_index == 0 else None,
                artifact_inputs,
                execution_inputs,
                accepted_build_providers,
            )
            if (
                result.failure_evidence is not None
                and result.failure_evidence.phase is StandardNodeFailurePhase.DEPENDENCY
            ):
                return generation, plan, result, current, None
            output = result.source_output
            if output is None:
                return generation, plan, result, current, None
            failure = result.failure_evidence
            diagnostic = None
            if failure is not None:
                if self.candidate_repair_port is None:
                    diagnostic = CandidateRepairDiagnostic(
                        result.component_revision,
                        failure.phase,
                        failure.code,
                        f"{failure.phase.value} rejected candidate with {failure.code}",
                        CandidateFailureClassification.TERMINAL,
                        failure.identity,
                    )
                else:
                    diagnostic = self.candidate_repair_port.diagnose(failure, output)
                    if (
                        not isinstance(diagnostic, CandidateRepairDiagnostic)
                        or diagnostic.component_revision != result.component_revision
                        or diagnostic.phase is not failure.phase
                        or diagnostic.code != failure.code
                        or diagnostic.failure_evidence_identity != failure.identity
                    ):
                        raise StandardProjectLifecycleError(
                            "standard_lifecycle.repair_diagnostic_mismatch",
                            "repair port diagnostic must bind the exact rejection",
                        )
            disposition = (
                CandidateAttemptDisposition.ACCEPTED
                if failure is None
                else CandidateAttemptDisposition.RETRYABLE_REJECTED
                if diagnostic is not None
                and diagnostic.classification
                is CandidateFailureClassification.RETRYABLE
                else CandidateAttemptDisposition.TERMINAL_REJECTED
            )
            source_candidate = output.candidate
            attempt = CandidateAttempt(
                result.component_revision,
                attempt_index,
                source_candidate.source_generation_request_identity,
                output.identity,
                source_candidate.tree_identity,
                source_candidate.source_bundle_identity,
                source_candidate.workspace_allocation_identity,
                result.identity,
                tuple(item.identity for item in attempts),
                disposition,
                diagnostic,
                repair_request,
            )
            attempts.append(attempt)
            if disposition is CandidateAttemptDisposition.ACCEPTED:
                chain_disposition = CandidateAttemptChainDisposition.ACCEPTED
            elif disposition is CandidateAttemptDisposition.TERMINAL_REJECTED:
                chain_disposition = CandidateAttemptChainDisposition.TERMINAL
            elif attempt_index == 2:
                chain_disposition = CandidateAttemptChainDisposition.EXHAUSTED
            else:
                assert diagnostic is not None
                assert self.candidate_repair_port is not None
                replacement = self.candidate_repair_port.prepare_repair(
                    prepared,
                    diagnostic,
                    tuple(item.identity for item in attempts),
                )
                if not isinstance(replacement, PreparedComponentGenerationNode):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.repair_preparation_invalid",
                        "repair port must return a typed prepared Component",
                    )
                validate_prepared_component_generation_node(
                    replacement, expected_plan=generation_plan
                )
                replacement_recipe_identity = _prepared_identity(
                    getattr(replacement.recipe, "identity", None),
                    "replacement recipe identity",
                )
                if (
                    replacement_recipe_identity != original_recipe_identity
                    or replacement.workspace.allocation_identity
                    in {item.workspace_allocation_identity for item in attempts}
                    or replacement.request.request.identity
                    == current.request.request.identity
                ):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.repair_preparation_mismatch",
                        "repair must retain the recipe, bind a changed request, "
                        "and use a fresh workspace",
                    )
                repair_request = CandidateRepairRequest(
                    generation_plan.component_revision,
                    attempt_index + 1,
                    original_recipe_identity,
                    tuple(item.identity for item in attempts),
                    diagnostic.identity,
                    replacement.request.request.identity,
                    replacement.workspace.allocation_identity,
                )
                current = replacement
                continue
            return (
                generation,
                plan,
                result,
                current,
                CandidateAttemptChain(
                    generation_plan.component_revision,
                    tuple(attempts),
                    chain_disposition,
                ),
            )
        raise AssertionError("bounded repair loop must return")

    def _generate_component_source(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        prepared: PreparedComponentGenerationNode[object, object],
        candidate: StandardNodeAcceptedCandidate | None,
        source_cache_membership: (
            StandardSourceCacheMembership | StandardSourceAdmissionMembership | None
        ),
        source_generation_resume: SourceGenerationResumeCandidate | None,
        regenerate: bool,
    ) -> ComponentSourceGenerationExecution:
        if source_cache_membership is None and candidate is not None:
            source_cache_membership = candidate.source_cache_membership
        if regenerate:
            candidate = None
            source_cache_membership = None
            source_generation_resume = None
        generation_candidate = None
        if candidate is not None:
            generation_candidate = candidate.generation
        elif source_cache_membership is not None:
            generation_candidate = (
                rebind_standard_source_admission_generation(
                    execution_plan, prepared, source_cache_membership
                )
                if isinstance(
                    source_cache_membership, StandardSourceAdmissionMembership
                )
                else rebind_standard_source_cache_generation(
                    execution_plan, prepared, source_cache_membership
                )
            )
        elif source_generation_resume is not None:
            generation_candidate = source_generation_resume
        execution = execute_component_source_generation_node(
            prepared,
            candidate=generation_candidate,
            explicitly_invalid=regenerate,
            runner=self.generator,
        )
        if execution.output is not None:
            self._record_stage(
                execution_plan,
                generation_plan,
                prepared,
                execution.output,
                StandardLifecycleStage.SOURCE_GENERATION,
                execution.output.identity,
            )
        return execution

    def _run_complete_node_once_steps(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        prepared: PreparedComponentGenerationNode[object, object],
        candidate: StandardNodeAcceptedCandidate | None,
        source_cache_membership: (
            StandardSourceCacheMembership | StandardSourceAdmissionMembership | None
        ),
        source_generation_resume: SourceGenerationResumeCandidate | None,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
        regenerate: bool,
        source_execution: ComponentSourceGenerationExecution | None = None,
        artifact_inputs: Callable[
            [], tuple[tuple[ArtifactExport, ...], tuple[ArtifactExport, ...]]
        ]
        | None = None,
        execution_inputs: _ExecutionInputResolver | None = None,
        accepted_build_providers: Callable[
            [], tuple[StandardComponentAcceptanceEvidence, ...]
        ]
        | None = None,
    ) -> Generator[
        _LifecycleStep,
        object,
        tuple[
            SourceGenerationNodeResult,
            StandardComponentBuildPlan | None,
            StandardNodeLifecycleResult,
        ],
    ]:
        if source_cache_membership is None and candidate is not None:
            source_cache_membership = candidate.source_cache_membership
        if regenerate:
            candidate = None
            source_cache_membership = None
            source_generation_resume = None
        if source_execution is None:
            try:
                source_execution = yield _LifecycleStep(
                    StandardLifecycleStage.SOURCE_GENERATION,
                    lambda: self._generate_component_source(
                        execution_plan,
                        generation_plan,
                        prepared,
                        candidate,
                        source_cache_membership,
                        source_generation_resume,
                        regenerate,
                    ),
                )
            except Exception as exc:
                source_execution = ComponentSourceGenerationExecution(
                    self._failed_generation(prepared, exc), None, exc
                )
        generation = source_execution.result
        if generation.disposition in {
            SourceGenerationDisposition.FAILED,
            SourceGenerationDisposition.CANCELLED,
        }:
            failure_cause = (
                source_execution.failure_cause
                if source_execution.failure_cause is not None
                else generation.failure_code
            )
            return (
                generation,
                None,
                self._cancelled(
                    generation_plan.component_revision,
                    generation,
                    None,
                    None,
                    _failure(
                        generation_plan.component_revision,
                        StandardNodeFailurePhase.SOURCE_GENERATION,
                        generation_plan.identity,
                        failure_cause,
                        generation.failure_code or "source-generation.failed",
                    ),
                ),
            )
        output = source_execution.output
        assert output is not None
        if generation.disposition in {
            SourceGenerationDisposition.GENERATED,
            SourceGenerationDisposition.RETAINED,
        }:
            candidate = None
            source_cache_membership = None
        source_candidate = output.candidate
        source = source_candidate.tree_identity
        try:
            index = _require_identity(
                (
                    yield _LifecycleStep(
                        StandardLifecycleStage.SOURCE_INDEX,
                        lambda: self.indexer.index(
                            generation_plan.component_revision, source
                        ),
                        reserve=partial(
                            self.indexer.try_reserve_index,
                            generation_plan.component_revision,
                            source,
                        )
                        if isinstance(self.indexer, AdmittedGenerationIndexer)
                        else None,
                    )
                ),
                "source index",
            )
        except Exception as exc:
            return (
                generation,
                None,
                self._cancelled(
                    generation_plan.component_revision,
                    generation,
                    output,
                    None,
                    _failure(
                        generation_plan.component_revision,
                        StandardNodeFailurePhase.SOURCE_INDEX,
                        source_candidate.identity,
                        exc,
                        "source-index.failed",
                    ),
                ),
            )
        self._record_stage(
            execution_plan,
            generation_plan,
            prepared,
            output,
            StandardLifecycleStage.SOURCE_INDEX,
            index,
        )

        def create_intent() -> StandardComponentBuildIntent:
            nonlocal provider_artifacts, package_artifacts
            if artifact_inputs is not None:
                provider_artifacts, package_artifacts = artifact_inputs()
            return self.build_intent_factory.create(
                execution_plan,
                generation_plan,
                source_candidate,
                provider_artifacts,
                package_artifacts,
            )

        def reserve_intent():
            nonlocal provider_artifacts, package_artifacts
            if artifact_inputs is not None:
                provider_artifacts, package_artifacts = artifact_inputs()
            assert self.build_intent_dispatcher is not None
            assert accepted_build_providers is not None
            return self.build_intent_dispatcher.try_reserve_intent(
                execution_plan,
                generation_plan,
                source_candidate,
                index,
                provider_artifacts,
                package_artifacts,
                tuple(
                    receipt
                    for receipt in accepted_build_providers()
                    if receipt.component_revision
                    in {item.component_revision for item in provider_artifacts}
                ),
            )

        try:
            intent = yield _LifecycleStep(
                StandardLifecycleStage.BUILD_INTENT,
                create_intent,
                reserve=reserve_intent
                if self.build_intent_dispatcher is not None
                and accepted_build_providers is not None
                else None,
            )
            if not isinstance(intent, StandardComponentBuildIntent):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.intent_invalid",
                    "build-intent factory must return a StandardComponentBuildIntent",
                )
            if (
                intent.component_revision != generation_plan.component_revision
                or intent.source_tree_identity != source
                or intent.source_bundle_identity
                != source_candidate.source_bundle_identity
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.intent_identity_mismatch",
                    "build intent does not bind the generated Component source",
                )
            if intent.provider_artifact_identities != tuple(
                item.identity for item in provider_artifacts
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.provider_artifacts_mismatch",
                    "build intent does not bind every exact provider artifact",
                )
            if intent.package_artifact_identities != tuple(
                item.identity for item in package_artifacts
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.package_artifacts_mismatch",
                    "build intent does not bind every exact package artifact",
                )
        except Exception as exc:
            return (
                generation,
                None,
                self._cancelled(
                    generation_plan.component_revision,
                    generation,
                    output,
                    None,
                    _failure(
                        generation_plan.component_revision,
                        StandardNodeFailurePhase.DEPENDENCY
                        if isinstance(exc, StandardProjectLifecycleError)
                        and exc.code == "dependency.failed"
                        else StandardNodeFailurePhase.BUILD_INTENT,
                        source_candidate.identity,
                        exc,
                        "build-intent.failed",
                    ),
                    index_identity=index,
                ),
            )
        self._record_stage(
            execution_plan,
            generation_plan,
            prepared,
            output,
            StandardLifecycleStage.BUILD_INTENT,
            intent.identity,
        )

        try:
            authorization = yield _LifecycleStep(
                StandardLifecycleStage.BUILD_AUTHORIZATION,
                lambda: self.authorizer.authorize(intent, index),
                reserve=partial(
                    self.authorizer.try_reserve_authorization, intent, index
                )
                if isinstance(self.authorizer, AdmittedBuildAuthorizer)
                else None,
            )
            if not isinstance(authorization, StandardBuildAuthorization):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.authorization_invalid",
                    "authorizer must return a StandardBuildAuthorization",
                )
            if (
                authorization.build_intent_identity != intent.identity
                or authorization.build_request_identity != intent.build_request_identity
                or authorization.index_identity != index
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.authorization_binding_mismatch",
                    "authorization does not bind the exact intent, request, and index",
                )
            authorization.grant.require_valid(
                intent.build_request,
                now=self.clock(),
            )
        except Exception as exc:
            return (
                generation,
                None,
                self._cancelled(
                    generation_plan.component_revision,
                    generation,
                    output,
                    None,
                    _failure(
                        generation_plan.component_revision,
                        StandardNodeFailurePhase.BUILD_AUTHORIZATION,
                        intent.identity,
                        exc,
                        "build-authorization.failed",
                    ),
                    index_identity=index,
                ),
            )
        self._record_stage(
            execution_plan,
            generation_plan,
            prepared,
            output,
            StandardLifecycleStage.BUILD_AUTHORIZATION,
            authorization.identity,
        )

        def require_current_authorization() -> None:
            authorization.grant.require_valid(intent.build_request, now=self.clock())

        try:
            plan = yield _LifecycleStep(
                StandardLifecycleStage.BUILD_PLAN,
                lambda: self._finalize_build_plan(intent, authorization),
                require_current_authorization,
                reserve=partial(
                    self.build_plan_finalizer.try_reserve_plan, intent, authorization
                )
                if isinstance(self.build_plan_finalizer, AdmittedBuildPlanFinalizer)
                else None,
            )
            plan = self._validate_build_plan(intent, authorization, plan)
        except Exception as exc:
            return (
                generation,
                None,
                self._cancelled(
                    generation_plan.component_revision,
                    generation,
                    output,
                    None,
                    _failure(
                        generation_plan.component_revision,
                        StandardNodeFailurePhase.BUILD_PLAN,
                        authorization.identity,
                        exc,
                        "build-plan.failed",
                    ),
                    index_identity=index,
                    authorization_identity=authorization.authorization_identity,
                ),
            )
        self._record_stage(
            execution_plan,
            generation_plan,
            prepared,
            output,
            StandardLifecycleStage.BUILD_PLAN,
            plan.identity,
        )
        if (
            execution_inputs is None
            and candidate is not None
            and self._resume_matches(candidate, plan)
        ):
            return generation, plan, self._reused(plan, candidate, generation, output)
        return (
            generation,
            plan,
            (
                yield from self._run_node_steps(
                    plan,
                    source,
                    provider_artifacts,
                    generation,
                    output,
                    index,
                    authorization.authorization_identity,
                    source_cache_membership,
                    require_current_authorization,
                    lambda stage, subject: self._record_stage(
                        execution_plan,
                        generation_plan,
                        prepared,
                        output,
                        stage,
                        subject,
                    ),
                    execution_inputs,
                    accepted_build_providers,
                )
            ),
        )

    def _finalize_build_plan(
        self,
        intent: StandardComponentBuildIntent,
        authorization: StandardBuildAuthorization,
    ) -> StandardComponentBuildPlan:
        plan = self.build_plan_finalizer.finalize(intent, authorization)
        return self._validate_build_plan(intent, authorization, plan)

    @staticmethod
    def _validate_build_plan(intent, authorization, plan) -> StandardComponentBuildPlan:
        if not isinstance(plan, StandardComponentBuildPlan):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.plan_invalid",
                "build-plan finalizer must return a StandardComponentBuildPlan",
            )
        if (
            plan.component_revision != intent.component_revision
            or plan.request.source_tree_identity != intent.source_tree_identity
            or plan.provider_artifact_identities != intent.provider_artifact_identities
            or plan.package_artifact_identities != intent.package_artifact_identities
            or plan.request.authorization_identity
            != authorization.authorization_identity
        ):
            raise StandardProjectLifecycleError(
                "standard_lifecycle.plan_identity_mismatch",
                "finalized build plan does not bind its exact intent and grant",
            )
        return plan

    @staticmethod
    def _cancelled_generation(
        prepared: PreparedComponentGenerationNode[object, object],
    ) -> SourceGenerationNodeResult:
        return source_generation_terminal_result(
            prepared,
            disposition=SourceGenerationDisposition.CANCELLED,
            failure_code="dependency-failed",
        )

    @staticmethod
    def _failed_generation(
        prepared: PreparedComponentGenerationNode[object, object],
        cause: Exception,
    ) -> SourceGenerationNodeResult:
        return source_generation_terminal_result(
            prepared,
            disposition=SourceGenerationDisposition.FAILED,
            failure_code=_stable_failure_code(cause, "runner-failed"),
        )

    @staticmethod
    def _resume_matches(
        candidate: StandardNodeAcceptedCandidate, plan: StandardComponentBuildPlan
    ) -> bool:
        return (
            isinstance(candidate, StandardNodeAcceptedCandidate)
            and candidate.build_plan_identity == plan.identity
            and candidate.generation.output.candidate.tree_identity
            == plan.request.source_tree_identity
            and candidate.authorization_identity == plan.request.authorization_identity
            and _exports_satisfy_manifest(candidate.exports, plan.manifest)
            and all(
                isinstance(value, ContentIdentity)
                for value in (
                    candidate.index_identity,
                    candidate.authorization_identity,
                    candidate.build_identity,
                    candidate.test_identity,
                    candidate.execution_identity,
                    candidate.acceptance_identity,
                )
            )
        )

    @staticmethod
    def _reused(
        plan: StandardComponentBuildPlan,
        candidate: StandardNodeAcceptedCandidate,
        generation: SourceGenerationNodeResult,
        source_output: SourceGenerationRunOutput,
    ) -> StandardNodeLifecycleResult:
        return StandardNodeLifecycleResult(
            plan.component_revision,
            generation,
            source_output,
            plan.identity,
            candidate.exports,
            candidate.index_identity,
            candidate.authorization_identity,
            candidate.build_identity,
            candidate.test_identity,
            candidate.execution_identity,
            candidate.acceptance_identity,
            source_cache_membership=candidate.source_cache_membership,
        )

    @staticmethod
    def _cancelled(
        component_revision: ContentIdentity,
        generation: SourceGenerationNodeResult,
        source_output: SourceGenerationRunOutput | None,
        build_plan_identity: ContentIdentity | None,
        failure: StandardNodeFailureEvidence,
        *,
        exports: tuple[ArtifactExport, ...] = (),
        index_identity: ContentIdentity | None = None,
        authorization_identity: ContentIdentity | None = None,
        build_identity: ContentIdentity | None = None,
        test_identity: ContentIdentity | None = None,
        execution_identity: ContentIdentity | None = None,
        source_admission_identity: ContentIdentity | None = None,
    ) -> StandardNodeLifecycleResult:
        return StandardNodeLifecycleResult(
            component_revision,
            generation,
            source_output,
            build_plan_identity,
            exports,
            index_identity,
            authorization_identity,
            build_identity,
            test_identity,
            execution_identity,
            None,
            failure.code,
            failure_evidence=failure,
            source_admission_identity=source_admission_identity,
        )

    def _run_node_steps(
        self,
        plan: StandardComponentBuildPlan,
        source: ContentIdentity | None,
        provider_artifacts: tuple[ArtifactExport, ...],
        source_generation: SourceGenerationNodeResult,
        source_output: SourceGenerationRunOutput,
        index: ContentIdentity,
        authorization: ContentIdentity,
        source_cache_membership: (
            StandardSourceCacheMembership | StandardSourceAdmissionMembership | None
        ),
        require_current_authorization: Callable[[], None],
        record_stage: Callable[[StandardLifecycleStage, ContentIdentity], None],
        execution_inputs: _ExecutionInputResolver | None = None,
        accepted_build_providers: Callable[
            [], tuple[StandardComponentAcceptanceEvidence, ...]
        ]
        | None = None,
    ) -> Generator[_LifecycleStep, object, StandardNodeLifecycleResult]:
        source_admission_identity = (
            source_cache_membership.identity
            if isinstance(source_cache_membership, StandardSourceAdmissionMembership)
            else None
        )
        if source != plan.request.source_tree_identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.source_identity_mismatch",
                "generated source does not match the typed composite build request",
            )
        if authorization != plan.request.authorization_identity:
            raise StandardProjectLifecycleError(
                "standard_lifecycle.authorization_identity_mismatch",
                "authorization does not match the composite build request",
            )
        try:
            if isinstance(self.builder, BuildProviderEvidenceReceiver):
                require_current_authorization()
                self.builder.retain_build_provider_evidence(
                    plan, accepted_build_providers() if accepted_build_providers else ()
                )
            build = yield _LifecycleStep(
                StandardLifecycleStage.BUILD,
                lambda: self.builder.build(plan, provider_artifacts),
                require_current_authorization,
                reserve=partial(
                    self.builder.try_reserve_build, plan, provider_artifacts
                )
                if isinstance(self.builder, AdmittedComponentBuilder)
                else None,
            )
            if not isinstance(build, StandardBuildOutput):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.build_output_invalid",
                    "builder must return a StandardBuildOutput",
                )
            if not _exports_satisfy_manifest(build.exports, plan.manifest):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.exports_mismatch",
                    "realized builder outputs differ from declared Component exports",
                )
            _require_identity(build.build_identity, "build")
        except Exception as exc:
            trace_exception("component build", exc)
            return self._cancelled(
                plan.component_revision,
                source_generation,
                source_output,
                plan.identity,
                _failure(
                    plan.component_revision,
                    StandardNodeFailurePhase.BUILD,
                    plan.identity,
                    exc,
                    "build.failed",
                ),
                index_identity=index,
                authorization_identity=authorization,
                source_admission_identity=source_admission_identity,
            )
        record_stage(StandardLifecycleStage.BUILD, build.build_identity)
        try:
            raw_test = yield _LifecycleStep(
                StandardLifecycleStage.TEST,
                lambda: self.tester.test(plan, build.exports),
                require_current_authorization,
                reserve=partial(self.tester.try_reserve_test, plan, build.exports)
                if isinstance(self.tester, AdmittedComponentTester)
                else None,
            )
            generated_test_evidence = (
                raw_test
                if isinstance(raw_test, StandardGeneratedTestExecutionEvidence)
                else None
            )
            if build.evidence is not None and (
                generated_test_evidence is None
                or generated_test_evidence.build_evidence_identity
                != build.evidence.identity
                or generated_test_evidence.export_identities
                != build.evidence.export_identities
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.generated_test_evidence_mismatch",
                    "strict builds require generated-test evidence for the exact build",
                )
            test = _require_identity(
                raw_test.identity if generated_test_evidence is not None else raw_test,
                "test",
            )
        except Exception as exc:
            return self._cancelled(
                plan.component_revision,
                source_generation,
                source_output,
                plan.identity,
                _failure(
                    plan.component_revision,
                    StandardNodeFailurePhase.TEST,
                    plan.identity,
                    exc,
                    "test.failed",
                ),
                exports=build.exports,
                index_identity=index,
                authorization_identity=authorization,
                build_identity=build.build_identity,
                source_admission_identity=source_admission_identity,
            )
        record_stage(StandardLifecycleStage.TEST, test)
        execution_providers = provider_artifacts

        def current_execution_scope():
            nonlocal execution_providers
            if execution_inputs is None:
                return None
            scope, execution_providers = execution_inputs(plan, build.exports)
            return scope

        def execute_current():
            scope = current_execution_scope()
            if scope is None:
                return self.executor.execute(plan, build.exports)
            return self.executor.execute_scoped(
                plan, build.exports, scope, execution_providers
            )

        def reserve_execution():
            scope = current_execution_scope()
            return self.executor.try_reserve_execute(
                plan, build.exports, scope, execution_providers
            )

        try:
            raw_execution = yield _LifecycleStep(
                StandardLifecycleStage.EXECUTE,
                execute_current,
                require_current_authorization,
                reserve=reserve_execution
                if isinstance(self.executor, AdmittedComponentExecutor)
                else None,
            )
            # Reservations replace operation(), so admission must apply equally
            # to local and reserved execution, against the current runtime scope.
            if execution_inputs is not None:
                scope = current_execution_scope()
                if (
                    not isinstance(raw_execution, StandardExecutionEvidence)
                    or raw_execution.execution_authority is None
                    or raw_execution.execution_authority.input_scope != scope
                    or raw_execution.execution_authority.source_tree_identity
                    != plan.request.source_tree_identity
                ):
                    raise StandardProjectLifecycleError(
                        "standard_lifecycle.execution_scope_mismatch",
                        "execution must bind the current admitted runtime scope",
                    )
                raw_execution.execution_authority.require_valid(now=self.clock())
            execution_evidence = (
                raw_execution
                if isinstance(raw_execution, StandardExecutionEvidence)
                else None
            )
            if build.evidence is not None and (
                execution_evidence is None
                or execution_evidence.build_evidence_identity != build.evidence.identity
                or execution_evidence.export_identities
                != build.evidence.export_identities
                or execution_evidence.provider_artifact_identities
                != tuple(item.identity for item in execution_providers)
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.execution_evidence_mismatch",
                    "strict builds require execution evidence for the exact build "
                    "and provider inputs",
                )
            execution = _require_identity(
                (
                    raw_execution.identity
                    if execution_evidence is not None
                    else raw_execution
                ),
                "execution",
            )
        except Exception as exc:
            return self._cancelled(
                plan.component_revision,
                source_generation,
                source_output,
                plan.identity,
                _failure(
                    plan.component_revision,
                    StandardNodeFailurePhase.EXECUTE,
                    plan.identity,
                    exc,
                    "execute.failed",
                ),
                exports=build.exports,
                index_identity=index,
                authorization_identity=authorization,
                build_identity=build.build_identity,
                test_identity=test,
                source_admission_identity=source_admission_identity,
            )
        record_stage(StandardLifecycleStage.EXECUTE, execution)
        try:
            raw_acceptance = yield _LifecycleStep(
                StandardLifecycleStage.ACCEPT,
                lambda: self.acceptor.accept(plan, test, execution),
                require_current_authorization,
            )
            acceptance_evidence = (
                raw_acceptance
                if isinstance(raw_acceptance, StandardComponentAcceptanceEvidence)
                else None
            )
            if build.evidence is not None and (
                acceptance_evidence is None
                or acceptance_evidence.source_generation_identity
                != source_output.identity
                or acceptance_evidence.build != build.evidence
                or acceptance_evidence.generated_tests != generated_test_evidence
                or acceptance_evidence.execution != execution_evidence
            ):
                raise StandardProjectLifecycleError(
                    "standard_lifecycle.acceptance_evidence_mismatch",
                    "strict builds require acceptance of the exact source and stages",
                )
            acceptance = _require_identity(
                (
                    raw_acceptance.identity
                    if acceptance_evidence is not None
                    else raw_acceptance
                ),
                "acceptance",
            )
        except Exception as exc:
            return self._cancelled(
                plan.component_revision,
                source_generation,
                source_output,
                plan.identity,
                _failure(
                    plan.component_revision,
                    StandardNodeFailurePhase.ACCEPT,
                    plan.identity,
                    exc,
                    "accept.failed",
                ),
                exports=build.exports,
                index_identity=index,
                authorization_identity=authorization,
                build_identity=build.build_identity,
                test_identity=test,
                execution_identity=execution,
                source_admission_identity=source_admission_identity,
            )
        record_stage(StandardLifecycleStage.ACCEPT, acceptance)
        current_membership = StandardSourceCacheMembership(
            plan.component_revision,
            source_generation.generation_key_identity,
            SourceGenerationResumeCandidate(
                source_output,
                source_output.identity,
                source_generation.complexity_budget_identity,
                source_generation.complexity_decision_identity,
            ),
            acceptance,
        )
        return StandardNodeLifecycleResult(
            plan.component_revision,
            source_generation,
            source_output,
            plan.identity,
            build.exports,
            index,
            authorization,
            build.build_identity,
            test,
            execution,
            acceptance,
            source_cache_membership=current_membership,
            build_evidence=build.evidence,
            generated_test_evidence=generated_test_evidence,
            execution_evidence=execution_evidence,
            acceptance_evidence=acceptance_evidence,
            source_admission_identity=source_admission_identity,
        )


__all__ = [
    "AcceptedSourceCachePublisher",
    "CompleteAcceptedSourceCachePublisher",
    "StandardLifecycleCheckpointRecorder",
    "BuildAuthorizer",
    "ComponentBuildIntentFactory",
    "ComponentBuildPlanFinalizer",
    "ComponentAcceptor",
    "ComponentBuilder",
    "ComponentExecutor",
    "ComponentTester",
    "GenerationIndexer",
    "ProjectAdmitter",
    "ProjectReceiptIssuer",
    "ProjectValidator",
    "StandardComponentBuildPlan",
    "StandardBuildOutput",
    "StandardBuildAuthorization",
    "StandardComponentBuildIntent",
    "StandardNodeAcceptedCandidate",
    "StandardAcceptedSourcePublication",
    "StandardNodeLifecycleResult",
    "StandardProjectBuildPlan",
    "StandardProjectLifecycleError",
    "StandardProjectLifecycleResult",
    "StandardProjectLifecycleService",
    "StandardSourceCacheMembership",
    "rebind_standard_source_admission_generation",
    "rebind_standard_source_cache_generation",
]

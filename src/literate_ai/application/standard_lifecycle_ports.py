"""Typed Standard lifecycle ports.

These protocols are the stable injection boundary for
`StandardProjectLifecycleService`. Result and plan dataclasses stay with the
orchestration module so adapters can keep importing one service surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.source_generation_scheduling import (
    ComponentContextEvidenceRecorder,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import (
    ArtifactBuildGraph,
    ArtifactExport,
    CandidateRepairDiagnostic,
    ComponentContextBenchmarkRecord,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ExactLinkPlan,
    ForwardGenerationContextCacheReport,
    GeneratedSourceCandidate,
    PackagePlan,
    PackageResult,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.standard_execution_inputs import StandardExecutionInputScope
from literate_ai.contracts.standard_lifecycle_checkpoint import (
    StandardLifecycleStageEvidence,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardAggregateReceipt,
    StandardNodeFailureEvidence,
)
from literate_ai.contracts.standard_post_source_evidence import (
    StandardComponentAcceptanceEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestExecutionEvidence,
)

if TYPE_CHECKING:
    from literate_ai.application.standard_project_lifecycle import (
        StandardAcceptedSourcePublication,
        StandardBuildAuthorization,
        StandardBuildOutput,
        StandardComponentBuildIntent,
        StandardComponentBuildPlan,
        StandardNodeLifecycleResult,
        StandardProjectBuildPlan,
        StandardSourceCacheMembership,
    )


class ProjectValidator(Protocol):
    def validate(self, execution_plan: ComponentExecutionPlan) -> ContentIdentity: ...


class ComponentBuildIntentFactory(Protocol):
    """Create intent and derive its distinct bundle identity from accepted source."""

    def create(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        source_candidate: GeneratedSourceCandidate,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardComponentBuildIntent: ...


class ComponentBuildPlanFinalizer(Protocol):
    """Bind one indexed intent to its exact issued authorization."""

    def finalize(
        self,
        intent: StandardComponentBuildIntent,
        authorization: StandardBuildAuthorization,
    ) -> StandardComponentBuildPlan: ...


class GenerationIndexer(Protocol):
    def index(
        self, component_revision: ContentIdentity, source: ContentIdentity
    ) -> ContentIdentity: ...


class ReservedLifecycleOperation(Protocol):
    """One owned worker slot; release is idempotent and execution is single-use."""

    def run(self) -> object: ...

    def release(self) -> None: ...


@runtime_checkable
class AdmittedGenerationIndexer(Protocol):
    """Reserve capacity without blocking the lifecycle's shared executor."""

    def try_reserve_index(
        self, component_revision: ContentIdentity, source: ContentIdentity
    ) -> ReservedLifecycleOperation | None: ...


class AdmittedBuildIntentDispatcher(Protocol):
    def try_reserve_intent(
        self,
        execution_plan: ComponentExecutionPlan,
        generation_plan: ComponentGenerationPlan,
        candidate: GeneratedSourceCandidate,
        index_identity: ContentIdentity,
        provider_artifacts: tuple[ArtifactExport, ...],
        package_artifacts: tuple[ArtifactExport, ...],
        accepted_providers: tuple[StandardComponentAcceptanceEvidence, ...],
    ) -> ReservedLifecycleOperation | None: ...


@runtime_checkable
class AdmittedBuildPlanFinalizer(Protocol):
    def try_reserve_plan(
        self,
        intent: StandardComponentBuildIntent,
        authorization: StandardBuildAuthorization,
    ) -> ReservedLifecycleOperation | None: ...


@runtime_checkable
class AdmittedBuildAuthorizer(Protocol):
    def try_reserve_authorization(
        self, intent: StandardComponentBuildIntent, index: ContentIdentity
    ) -> ReservedLifecycleOperation | None: ...


class BuildAuthorizer(Protocol):
    def authorize(
        self, intent: StandardComponentBuildIntent, index: ContentIdentity
    ) -> StandardBuildAuthorization: ...


class ComponentBuilder(Protocol):
    def build(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardBuildOutput: ...


@runtime_checkable
class BuildProviderEvidenceReceiver(Protocol):
    """Retain accepted dependency receipts before BUILD capacity is reserved."""

    def retain_build_provider_evidence(
        self,
        plan: StandardComponentBuildPlan,
        receipts: tuple[StandardComponentAcceptanceEvidence, ...],
    ) -> None: ...


@runtime_checkable
class AdmittedComponentBuilder(Protocol):
    """Reserve BUILD capacity before occupying the lifecycle executor."""

    def try_reserve_build(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> ReservedLifecycleOperation | None: ...


@runtime_checkable
class AdmittedComponentTester(Protocol):
    """Reserve TEST capacity before occupying the lifecycle executor."""

    def try_reserve_test(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
    ) -> ReservedLifecycleOperation | None: ...


class ComponentTester(Protocol):
    def test(
        self, plan: StandardComponentBuildPlan, exports: tuple[ArtifactExport, ...]
    ) -> ContentIdentity | StandardGeneratedTestExecutionEvidence: ...


@runtime_checkable
class ExecutionProviderEvidenceReceiver(Protocol):
    """Retain the full accepted runtime closure before EXECUTE reservation."""

    def retain_execution_provider_evidence(
        self,
        plan: StandardComponentBuildPlan,
        scope: StandardExecutionInputScope,
        receipts: tuple[StandardComponentAcceptanceEvidence, ...],
    ) -> None: ...


@runtime_checkable
class AdmittedComponentExecutor(Protocol):
    """Reserve EXECUTE capacity before occupying a lifecycle executor thread."""

    def try_reserve_execute(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        scope: StandardExecutionInputScope | None,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> ReservedLifecycleOperation | None: ...


class ComponentExecutor(Protocol):
    def execute(
        self, plan: StandardComponentBuildPlan, exports: tuple[ArtifactExport, ...]
    ) -> ContentIdentity | StandardExecutionEvidence: ...


class ScopedComponentExecutor(Protocol):
    def execute_scoped(
        self,
        plan: StandardComponentBuildPlan,
        exports: tuple[ArtifactExport, ...],
        scope: StandardExecutionInputScope,
        provider_artifacts: tuple[ArtifactExport, ...],
    ) -> StandardExecutionEvidence: ...


class ComponentAcceptor(Protocol):
    def accept(
        self,
        plan: StandardComponentBuildPlan,
        test_identity: ContentIdentity,
        execution_identity: ContentIdentity,
    ) -> ContentIdentity | StandardComponentAcceptanceEvidence: ...


class AcceptedSourceCachePublisher(Protocol):
    """Publish one accepted Component after enclosing project acceptance."""

    def publish(self, membership: StandardSourceCacheMembership) -> ContentIdentity: ...


class StandardLifecycleCheckpointRecorder(Protocol):
    """Persist a typed stage boundary without granting authority to skip replay."""

    def record(self, evidence: StandardLifecycleStageEvidence) -> None: ...


class StandardContextEvidenceRecorder(ComponentContextEvidenceRecorder, Protocol):
    @property
    def prompt_journal_identities(self) -> tuple[ContentIdentity, ...]: ...

    @property
    def benchmark_records(self) -> tuple[ComponentContextBenchmarkRecord, ...]: ...

    def cache_report(self) -> ForwardGenerationContextCacheReport: ...


class StandardCandidateRepairPort(Protocol):
    """Classify one rejection and prepare a diagnostic-bound fresh replacement."""

    def diagnose(
        self,
        failure: StandardNodeFailureEvidence,
        output: SourceGenerationRunOutput,
    ) -> CandidateRepairDiagnostic: ...

    def prepare_repair(
        self,
        original: PreparedComponentGenerationNode[object, object],
        diagnostic: CandidateRepairDiagnostic,
        predecessor_attempt_identities: tuple[ContentIdentity, ...],
    ) -> PreparedComponentGenerationNode[object, object]: ...


class CompleteAcceptedSourceCachePublisher(Protocol):
    """Persist the complete typed custody needed for a Standard cache round trip."""

    def publish_accepted(
        self, publication: StandardAcceptedSourcePublication
    ) -> ContentIdentity: ...


class ProjectAdmitter(Protocol):
    """Atomically admit one complete canonical project result set."""

    def admit(
        self, results: tuple[StandardNodeLifecycleResult, ...]
    ) -> ContentIdentity: ...


class ProjectReceiptIssuer(Protocol):
    def issue(self, receipt: StandardAggregateReceipt) -> ContentIdentity: ...


class ProjectArtifactAssembler(Protocol):
    """Assemble the exact root artifact graph after all Components succeed."""

    def assemble_project_artifacts(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        results: tuple[StandardNodeLifecycleResult, ...],
    ) -> tuple[ArtifactBuildGraph, ExactLinkPlan]: ...


class ProjectPackageCreator(Protocol):
    """Create and realize one exact root package from the retained graph."""

    def create_project_package(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        artifact_graph: ArtifactBuildGraph,
        link_plan: ExactLinkPlan,
    ) -> tuple[PackagePlan, PackageResult]: ...


class RootIntegrationTester(Protocol):
    def test_root_integration(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
    ) -> ContentIdentity: ...


class PackagedProjectExecutor(Protocol):
    def execute_packaged_project(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
    ) -> ContentIdentity: ...


class IndependentProjectAcceptor(Protocol):
    def accept_project_independently(
        self,
        component_lock: ComponentLock,
        execution_plan: ComponentExecutionPlan,
        project_build_plan: StandardProjectBuildPlan,
        package_plan: PackagePlan,
        package_result: PackageResult,
        root_integration_test_identity: ContentIdentity,
        packaged_execution_identity: ContentIdentity,
    ) -> ContentIdentity: ...

"""Public plan, prepare, and rebuild services for executable Component projects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Protocol, TypeVar

from literate_ai.application.component_execution_planning import (
    authored_assets_from_lock,
    plan_component_execution,
)
from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    LockedNodeGenerationProjection,
    PreparedComponentGenerationNode,
    prepare_component_generation_nodes,
)
from literate_ai.application.source_generation_scheduling import (
    ComponentContextEvidenceRecorder,
    ComponentSourceGenerationRunner,
    schedule_source_generation_executions,
    validate_prepared_component_generation_node,
)
from literate_ai.application.standard_lifecycle_ports import (
    AdmittedBuildIntentDispatcher,
)
from literate_ai.application.standard_project_lifecycle import (
    AcceptedSourceCachePublisher,
    BuildAuthorizer,
    ComponentAcceptor,
    ComponentBuilder,
    ComponentBuildIntentFactory,
    ComponentBuildPlanFinalizer,
    ComponentExecutor,
    ComponentTester,
    GenerationIndexer,
    IndependentProjectAcceptor,
    PackagedProjectExecutor,
    ProjectAdmitter,
    ProjectArtifactAssembler,
    ProjectPackageCreator,
    ProjectReceiptIssuer,
    ProjectValidator,
    RootIntegrationTester,
    StandardCandidateRepairPort,
    StandardContextEvidenceRecorder,
    StandardLifecycleCheckpointRecorder,
    StandardNodeAcceptedCandidate,
    StandardProjectLifecycleResult,
    StandardProjectLifecycleService,
    StandardSourceCacheMembership,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import (
    AuthoredBinaryAsset,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ComponentInvalidationDecision,
    GenerationComplexityBudget,
    SourceGenerationResumeCandidate,
)
from literate_ai.contracts.executable_components.source_custody import (
    ComponentSourceWorkspaceCustody,
    ProjectSourceGenerationCustody,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.standard_source_admission import (
    StandardSourceAdmissionMembership,
)

AuthorityT = TypeVar("AuthorityT")
DefinitionT = TypeVar("DefinitionT")
RecipeT = TypeVar("RecipeT")


class StandardProjectLifecyclePorts(
    ProjectValidator,
    ComponentBuildIntentFactory,
    ComponentBuildPlanFinalizer,
    GenerationIndexer,
    BuildAuthorizer,
    ComponentBuilder,
    ComponentTester,
    ComponentExecutor,
    ComponentAcceptor,
    AcceptedSourceCachePublisher,
    ProjectArtifactAssembler,
    ProjectPackageCreator,
    RootIntegrationTester,
    PackagedProjectExecutor,
    IndependentProjectAcceptor,
    ProjectAdmitter,
    ProjectReceiptIssuer,
    Protocol,
):
    """One cohesive provider implementing the ordinary Standard lifecycle ports."""


class StandardProjectApplicationServiceError(ValueError):
    """A public project-service request was incomplete or internally inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _recipe_identity(recipe: object) -> ContentIdentity:
    value = getattr(recipe, "identity", None)
    if isinstance(value, ContentIdentity):
        return value
    if isinstance(value, str):
        try:
            return ContentIdentity.parse_uri(value)
        except ValueError as exc:
            raise StandardProjectApplicationServiceError(
                "project_service.recipe_identity_invalid",
                "prepared recipe identity is not canonical",
            ) from exc
    raise StandardProjectApplicationServiceError(
        "project_service.recipe_identity_missing",
        "prepared recipe must expose a content identity",
    )


@dataclass(frozen=True, slots=True)
class PreparedExecutableProject(Generic[DefinitionT, RecipeT]):
    """Complete application-layer input for one Standard project rebuild."""

    execution_plan: ComponentExecutionPlan
    nodes: tuple[PreparedComponentGenerationNode[DefinitionT, RecipeT], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.execution_plan, ComponentExecutionPlan):
            raise TypeError("execution_plan must be a ComponentExecutionPlan")
        if not isinstance(self.nodes, tuple):
            raise TypeError("nodes must be a tuple")
        if any(
            not isinstance(node, PreparedComponentGenerationNode) for node in self.nodes
        ):
            raise TypeError(
                "nodes must contain only PreparedComponentGenerationNode values"
            )
        expected = tuple(
            item.component_revision.uri for item in self.execution_plan.generation_plans
        )
        actual = tuple(item.plan.component_revision.uri for item in self.nodes)
        if actual != expected:
            raise StandardProjectApplicationServiceError(
                "project_service.preparation_incomplete",
                "prepared nodes must exactly match canonical execution-plan order",
            )
        for plan, node in zip(
            self.execution_plan.generation_plans, self.nodes, strict=True
        ):
            validate_prepared_component_generation_node(node, expected_plan=plan)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/prepared-executable-project@1",
                "execution_plan_identity": self.execution_plan.identity.uri,
                "nodes": [
                    {
                        "component_revision": node.plan.component_revision.uri,
                        "request_identity": node.request.request.identity.uri,
                        "recipe_identity": _recipe_identity(node.recipe).uri,
                        "workspace_identity": node.workspace.identity.uri,
                    }
                    for node in self.nodes
                ],
            }
        )

    @property
    def nodes_by_revision(
        self,
    ) -> Mapping[str, PreparedComponentGenerationNode[DefinitionT, RecipeT]]:
        return {node.plan.component_revision.uri: node for node in self.nodes}


@dataclass(slots=True)
class StandardProjectApplicationService:
    """Provider-neutral public facade over exact planning and Standard execution."""

    lifecycle: StandardProjectLifecycleService

    @staticmethod
    def plan(
        lock: ComponentLock,
        *,
        model_identities: Mapping[str, ContentIdentity],
        assets: tuple[AuthoredBinaryAsset, ...] | None = None,
        native_sdk_input_identities: Mapping[str, tuple[ContentIdentity, ...]]
        | None = None,
    ) -> ComponentExecutionPlan:
        if assets is None:
            assets = authored_assets_from_lock(lock)
        return plan_component_execution(
            lock,
            model_identities=model_identities,
            assets=assets,
            native_sdk_input_identities=native_sdk_input_identities,
        )

    @staticmethod
    def prepare(
        execution_plan: ComponentExecutionPlan,
        *,
        authority: AuthorityT,
        authority_lock_identity: Callable[[AuthorityT], ContentIdentity],
        authority_guard: Callable[[AuthorityT], None],
        node_projector: Callable[
            [AuthorityT, ComponentGenerationPlan],
            LockedNodeGenerationProjection[DefinitionT, RecipeT],
        ],
        workspace_allocator: Callable[
            [ComponentGenerationPlan], ComponentGenerationWorkspaceDescriptor
        ],
        framework_envelope: bytes
        | Callable[[LockedNodeGenerationProjection[DefinitionT, RecipeT]], bytes],
        budget: GenerationComplexityBudget,
    ) -> PreparedExecutableProject[DefinitionT, RecipeT]:
        nodes = prepare_component_generation_nodes(
            execution_plan,
            authority=authority,
            authority_lock_identity=authority_lock_identity,
            authority_guard=authority_guard,
            node_projector=node_projector,
            workspace_allocator=workspace_allocator,
            framework_envelope=framework_envelope,
            budget=budget,
        )
        return PreparedExecutableProject(execution_plan, nodes)

    def rebuild(
        self,
        prepared: PreparedExecutableProject[object, object],
        *,
        component_lock: ComponentLock,
        invalidation: ComponentInvalidationDecision,
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
    ) -> StandardProjectLifecycleResult:
        if not isinstance(prepared, PreparedExecutableProject):
            raise TypeError("prepared must be a PreparedExecutableProject")
        return self.lifecycle.execute(
            prepared.execution_plan,
            component_lock=component_lock,
            invalidation=invalidation,
            prepared_nodes=prepared.nodes_by_revision,
            source_cache_memberships=source_cache_memberships,
            source_generation_resume_candidates=source_generation_resume_candidates,
            resume_candidates=resume_candidates,
            max_parallelism=max_parallelism,
        )

    @staticmethod
    def generate_sources(
        prepared: PreparedExecutableProject[object, object],
        *,
        invalidation: ComponentInvalidationDecision,
        runner: ComponentSourceGenerationRunner,
        resume_candidates: Mapping[str, SourceGenerationResumeCandidate] | None = None,
        max_parallelism: int = 1,
        context_evidence_recorder: ComponentContextEvidenceRecorder | None = None,
    ) -> ProjectSourceGenerationCustody:
        """Generate all planned source trees and retain their complete custody."""

        if not isinstance(prepared, PreparedExecutableProject):
            raise TypeError("prepared must be a PreparedExecutableProject")
        executions = schedule_source_generation_executions(
            prepared.execution_plan,
            invalidation=invalidation,
            prepared_nodes=prepared.nodes_by_revision,
            resume_candidates=resume_candidates,
            runner=runner,
            max_parallelism=max_parallelism,
            context_evidence_recorder=context_evidence_recorder,
        )
        nodes = prepared.nodes_by_revision
        components = tuple(
            ComponentSourceWorkspaceCustody(
                execution.result.component_revision,
                execution.result.generation_plan_identity,
                execution.result.generation_key_identity,
                nodes[execution.result.component_revision.uri].workspace.identity,
                nodes[execution.result.component_revision.uri].workspace.locator,
                execution.result,
                execution.output,
            )
            for execution in executions
        )
        return ProjectSourceGenerationCustody(
            prepared.execution_plan.identity,
            invalidation.identity,
            prepared.execution_plan.root_revision,
            components,
        )


def assemble_standard_project_application_service(
    *,
    ports: StandardProjectLifecyclePorts,
    generator: ComponentSourceGenerationRunner,
    indexer: GenerationIndexer | None = None,
    authorizer: BuildAuthorizer | None = None,
    build_plan_finalizer: ComponentBuildPlanFinalizer | None = None,
    build_intent_dispatcher: AdmittedBuildIntentDispatcher | None = None,
    source_cache_publisher: AcceptedSourceCachePublisher | None = None,
    checkpoint_recorder: StandardLifecycleCheckpointRecorder | None = None,
    context_evidence_recorder: StandardContextEvidenceRecorder | None = None,
    candidate_repair_port: StandardCandidateRepairPort | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StandardProjectApplicationService:
    """Assemble the public Standard service without a CLI-private dependency."""

    return StandardProjectApplicationService(
        StandardProjectLifecycleService(
            validator=ports,
            build_intent_factory=ports,
            build_intent_dispatcher=build_intent_dispatcher,
            build_plan_finalizer=ports
            if build_plan_finalizer is None
            else build_plan_finalizer,
            generator=generator,
            indexer=ports if indexer is None else indexer,
            authorizer=ports if authorizer is None else authorizer,
            builder=ports,
            tester=ports,
            executor=ports,
            acceptor=ports,
            source_cache_publisher=(
                ports if source_cache_publisher is None else source_cache_publisher
            ),
            artifact_assembler=ports,
            package_creator=ports,
            root_integration_tester=ports,
            packaged_project_executor=ports,
            independent_project_acceptor=ports,
            admitter=ports,
            receipt_issuer=ports,
            checkpoint_recorder=checkpoint_recorder,
            context_evidence_recorder=context_evidence_recorder,
            candidate_repair_port=candidate_repair_port,
            clock=clock,
        )
    )


__all__ = [
    "PreparedExecutableProject",
    "StandardProjectApplicationService",
    "StandardProjectApplicationServiceError",
    "StandardProjectLifecyclePorts",
    "assemble_standard_project_application_service",
]

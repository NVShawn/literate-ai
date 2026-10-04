"""Exact node routing and scoped artifact custody, without a second scheduler."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.component_workers import (
    ComponentArtifactHandoff,
    ComponentArtifactImportReceipt,
    ComponentWorkerAssignment,
    ComponentWorkerProduct,
    ComponentWorkerRouting,
)
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    DependencyInputKind,
)
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

NodeResultT = TypeVar("NodeResultT")


class ComponentNodeCancellation(Protocol):
    """Caller-owned cancellation signal shared with an executing transport."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ComponentNodeDispatchRequest:
    """Public, exact identity envelope for one complete Standard node execution."""

    routing_identity: ContentIdentity
    assignment: ComponentWorkerAssignment
    generation_plan_identity: ContentIdentity
    generation_request_identity: ContentIdentity
    handoff_identity: ContentIdentity
    provider_artifact_identities: tuple[ContentIdentity, ...]
    package_artifact_identities: tuple[ContentIdentity, ...]
    reuse_identity: ContentIdentity | None
    regenerate: bool

    def __post_init__(self) -> None:
        identities = (
            self.routing_identity,
            self.generation_plan_identity,
            self.generation_request_identity,
            self.handoff_identity,
        )
        if (
            not isinstance(self.assignment, ComponentWorkerAssignment)
            or any(not isinstance(item, ContentIdentity) for item in identities)
            or (
                self.reuse_identity is not None
                and not isinstance(self.reuse_identity, ContentIdentity)
            )
            or not isinstance(self.regenerate, bool)
        ):
            raise ComponentWorkerError("component node request is not typed")
        for values in (
            self.provider_artifact_identities,
            self.package_artifact_identities,
        ):
            if any(not isinstance(item, ContentIdentity) for item in values) or tuple(
                item.uri for item in values
            ) != tuple(sorted({item.uri for item in values})):
                raise ComponentWorkerError(
                    "component node artifact identities must be unique and ordered"
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/component-node-dispatch-request@1",
                "routing_identity": self.routing_identity.uri,
                "assignment_identity": self.assignment.identity.uri,
                "generation_plan_identity": self.generation_plan_identity.uri,
                "generation_request_identity": self.generation_request_identity.uri,
                "handoff_identity": self.handoff_identity.uri,
                "provider_artifact_identities": [
                    item.uri for item in self.provider_artifact_identities
                ],
                "package_artifact_identities": [
                    item.uri for item in self.package_artifact_identities
                ],
                "reuse_identity": (
                    None if self.reuse_identity is None else self.reuse_identity.uri
                ),
                "regenerate": self.regenerate,
            }
        )


@dataclass(frozen=True, slots=True)
class ComponentNodeDispatchOutcome(Generic[NodeResultT]):
    """Transport-correlated node result retained by the canonical scheduler."""

    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    result_identity: ContentIdentity
    import_receipt: ComponentArtifactImportReceipt
    result: NodeResultT
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class ComponentNodeRecoveryCandidate(Generic[NodeResultT]):
    """Previously accepted result that may be reused only for the exact request."""

    routing_identity: ContentIdentity
    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    result_identity: ContentIdentity
    import_receipt: ComponentArtifactImportReceipt
    result: NodeResultT


class ComponentNodeDispatcher(Protocol):
    """Local, command, or SSH transport behind the single DAG scheduler."""

    def dispatch(
        self,
        request: ComponentNodeDispatchRequest,
        execute: Callable[[], NodeResultT],
        *,
        cancellation: ComponentNodeCancellation | None,
    ) -> ComponentNodeDispatchOutcome[NodeResultT]: ...

    def cancel(self, request: ComponentNodeDispatchRequest) -> None: ...


def component_node_payload_result(payload: object) -> object:
    """Return the identity-bearing node result from a Standard execution payload."""

    if hasattr(payload, "identity") and hasattr(payload, "component_revision"):
        return payload
    if isinstance(payload, tuple) and len(payload) == 5:
        return payload[2]
    raise ComponentWorkerError("component node transport returned an invalid payload")


class ComponentWorkerError(ValueError):
    """A worker route or predecessor custody cannot satisfy its exact request."""


def plan_component_worker_routing(
    execution_plan: ComponentExecutionPlan,
    component_lock: ComponentLock,
    catalog: ExecutionWorkerCatalog,
    worker_ids: Mapping[str, str],
) -> ComponentWorkerRouting:
    """Bind explicit revision-URI assignments; eligibility remains a dispatch gate."""
    nodes = {node.revision.identity.uri: node for node in component_lock.nodes}
    if (
        execution_plan.component_lock_identity != component_lock.identity
        or execution_plan.root_revision != component_lock.root_revision
        or {item.component_revision.uri for item in execution_plan.generation_plans}
        != set(nodes)
        or {
            edge.identity.uri
            for action in execution_plan.action_plans
            for edge in action.dependency_edges
        }
        != {edge.identity.uri for edge in component_lock.edges}
    ):
        raise ComponentWorkerError("worker routing requires the exact locked plan")
    if set(worker_ids) != set(nodes):
        raise ComponentWorkerError("assign every and only planned Component revision")
    assignments = []
    for uri, node in sorted(nodes.items()):
        worker = catalog.worker(worker_ids[uri])
        if worker.target_profile != component_lock.target_name:
            raise ComponentWorkerError("worker target profile differs from the lock")
        assignments.append(
            ComponentWorkerAssignment(
                node.revision.identity,
                worker.worker_id,
                worker.identity,
                worker.target_profile,
                node.target_flavor_selection.identity,
            )
        )
    return ComponentWorkerRouting(
        execution_plan.identity,
        component_lock.identity,
        catalog.identity,
        tuple(assignments),
    )


def validate_component_worker_routing(
    routing: ComponentWorkerRouting,
    execution_plan: ComponentExecutionPlan,
    component_lock: ComponentLock,
    catalog: ExecutionWorkerCatalog,
) -> None:
    """Revalidate against current private authority immediately before dispatch."""
    expected = plan_component_worker_routing(
        execution_plan,
        component_lock,
        catalog,
        {item.component_revision.uri: item.worker_id for item in routing.assignments},
    )
    if routing != expected:
        raise ComponentWorkerError("worker routing is stale or mismatched")


def component_artifact_handoff(
    routing: ComponentWorkerRouting,
    execution_plan: ComponentExecutionPlan,
    consumer_revision: ContentIdentity,
    accepted_products: Mapping[str, ComponentWorkerProduct],
) -> ComponentArtifactHandoff:
    """Select direct artifact, toolchain and package inputs from accepted results.

    This does not authenticate acceptance evidence. The lifecycle owns admission of
    ``accepted_products`` and must validate routing against the current catalog.
    Generation-only edges never authorize transfer of a provider implementation.
    """
    if (
        execution_plan.identity != routing.execution_plan_identity
        or execution_plan.component_lock_identity != routing.component_lock_identity
        or {item.component_revision for item in execution_plan.generation_plans}
        != {item.component_revision for item in routing.assignments}
    ):
        raise ComponentWorkerError("artifact handoff requires the routed plan")
    assignments = {item.component_revision.uri: item for item in routing.assignments}
    if consumer_revision.uri not in assignments:
        raise ComponentWorkerError("consumer has no exact worker assignment")
    providers = {
        edge.provider_revision.uri
        for action in execution_plan.action_plans
        for edge in action.dependency_edges
        if edge.consumer_revision == consumer_revision
        and edge.semantics.consumed_input
        in {
            DependencyInputKind.ARTIFACT_EXPORT,
            DependencyInputKind.TOOLCHAIN,
            DependencyInputKind.PACKAGE,
        }
    }
    products = []
    for uri in sorted(providers):
        product = accepted_products.get(uri)
        assignment = assignments.get(uri)
        if (
            not isinstance(product, ComponentWorkerProduct)
            or assignment is None
            or product.component_revision != assignment.component_revision
            or product.worker_identity != assignment.worker_identity
        ):
            raise ComponentWorkerError(
                "required predecessor has no exact accepted product"
            )
        products.append(product)
    return ComponentArtifactHandoff(
        routing.identity,
        assignments[consumer_revision.uri],
        tuple(products),
    )


def validate_component_artifact_import(
    handoff: ComponentArtifactHandoff,
    receipt: ComponentArtifactImportReceipt,
) -> None:
    """Reject receipts replayed across workers, consumers, routes, or export sets."""
    if (
        receipt.handoff_identity != handoff.identity
        or receipt.worker_identity != handoff.consumer.worker_identity
        or receipt.export_identities != handoff.export_identities
    ):
        raise ComponentWorkerError(
            "artifact import receipt does not bind the exact handoff"
        )


def validate_component_node_outcome(
    request: ComponentNodeDispatchRequest,
    outcome: ComponentNodeDispatchOutcome[NodeResultT],
    *,
    component_revision: ContentIdentity,
    handoff: ComponentArtifactHandoff,
) -> NodeResultT:
    """Admit only a result correlated to this route, worker, request, and node."""

    if not isinstance(outcome, ComponentNodeDispatchOutcome):
        raise ComponentWorkerError("component node transport returned no typed outcome")
    result = component_node_payload_result(outcome.result)
    if (
        request.handoff_identity != handoff.identity
        or outcome.request_identity != request.identity
        or outcome.worker_identity != request.assignment.worker_identity
        or not isinstance(outcome.import_receipt, ComponentArtifactImportReceipt)
        or getattr(result, "identity", None) != outcome.result_identity
        or getattr(result, "component_revision", None) != component_revision
    ):
        raise ComponentWorkerError(
            "component node result does not bind the exact request and worker"
        )
    validate_component_artifact_import(handoff, outcome.import_receipt)
    return outcome.result


def recover_component_node_result(
    routing: ComponentWorkerRouting,
    request: ComponentNodeDispatchRequest,
    candidate: ComponentNodeRecoveryCandidate[NodeResultT],
    *,
    component_revision: ContentIdentity,
    handoff: ComponentArtifactHandoff,
) -> ComponentNodeDispatchOutcome[NodeResultT]:
    """Re-admit a retained result only under unchanged current routing authority."""

    if not isinstance(candidate, ComponentNodeRecoveryCandidate) or (
        candidate.routing_identity != routing.identity
        or candidate.request_identity != request.identity
        or candidate.worker_identity != request.assignment.worker_identity
        or candidate.result_identity
        != getattr(component_node_payload_result(candidate.result), "identity", None)
        or getattr(
            component_node_payload_result(candidate.result),
            "component_revision",
            None,
        )
        != component_revision
    ):
        raise ComponentWorkerError("component node recovery is stale or mismatched")
    validate_component_artifact_import(handoff, candidate.import_receipt)
    return ComponentNodeDispatchOutcome(
        request.identity,
        request.assignment.worker_identity,
        candidate.result_identity,
        candidate.import_receipt,
        candidate.result,
        recovered=True,
    )

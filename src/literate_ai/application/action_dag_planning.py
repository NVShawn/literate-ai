"""Project an exact Component execution plan into lifecycle-action nodes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import ComponentExecutionPlan
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog

from .action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)


class ActionDagPlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


_COMPONENT_ACTIONS = (
    LifecycleActionKind.GENERATE,
    LifecycleActionKind.INDEX,
    LifecycleActionKind.BUILD_INTENT,
    LifecycleActionKind.AUTHORIZE,
    LifecycleActionKind.PLAN,
    LifecycleActionKind.BUILD,
    LifecycleActionKind.TEST,
    LifecycleActionKind.EXECUTE,
    LifecycleActionKind.ACCEPT,
    LifecycleActionKind.LINK,
)


def admit_lifecycle_action_workers(
    catalog: ExecutionWorkerCatalog,
    observations: WorkerHardwareObservationCatalog,
    *,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(hours=24),
    target_profile: str | None = None,
) -> tuple[LifecycleActionWorker, ...]:
    """Admit only current, compatible, identity-bound configured worker slots."""

    if not isinstance(catalog, ExecutionWorkerCatalog) or not isinstance(
        observations, WorkerHardwareObservationCatalog
    ):
        raise TypeError("worker admission requires typed catalogs")
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or maximum_age <= timedelta(0):
        raise ActionDagPlanningError(
            "action_dag_plan.observation_time_invalid",
            "worker admission requires an aware time and positive maximum age",
        )
    observation_map = {item.worker_id: item for item in observations.workers}
    admitted: list[LifecycleActionWorker] = []
    for worker in catalog.workers:
        observed = observation_map.get(worker.worker_id)
        if observed is None:
            continue
        observed_at = datetime.fromisoformat(
            observed.observed_at.replace("Z", "+00:00")
        )
        age = current - observed_at
        if (
            age < timedelta(0)
            or age > maximum_age
            or (target_profile is not None and worker.target_profile != target_profile)
            or not observed.satisfies(worker.requirements)
        ):
            continue
        admitted.append(
            LifecycleActionWorker(
                worker.worker_id,
                worker.identity,
                catalog.identity,
                observed.identity,
                worker.slots,
            )
        )
    admitted.sort(key=lambda item: item.worker_id)
    if not admitted:
        raise ActionDagPlanningError(
            "action_dag_plan.no_eligible_workers",
            "no configured worker has a current compatible observation",
        )
    if sum(item.slots for item in admitted) > 256:
        raise ActionDagPlanningError(
            "action_dag_plan.capacity_excessive",
            "admitted worker capacity exceeds the bounded global slot limit",
        )
    return tuple(admitted)


def lifecycle_action_id(
    component_revision: ContentIdentity, kind: LifecycleActionKind
) -> str:
    if not isinstance(component_revision, ContentIdentity) or not isinstance(
        kind, LifecycleActionKind
    ):
        raise ActionDagPlanningError(
            "action_dag_plan.action_invalid",
            "action identity requires a typed Component revision and kind",
        )
    return f"{component_revision.uri}/{kind.value}"


def _canonical_workers(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise ActionDagPlanningError(
            "action_dag_plan.workers_invalid", f"{label} has no eligible workers"
        )
    return result


def lifecycle_action_payload(
    execution_plan_identity: ContentIdentity,
    component_revision: ContentIdentity,
    kind: LifecycleActionKind,
    generation_plan_identity: ContentIdentity | None = None,
) -> dict[str, str]:
    """Return the static record whose identity the production DAG binds."""
    if (
        not isinstance(execution_plan_identity, ContentIdentity)
        or not isinstance(component_revision, ContentIdentity)
        or not isinstance(kind, LifecycleActionKind)
        or (
            kind in _COMPONENT_ACTIONS
            and not isinstance(generation_plan_identity, ContentIdentity)
        )
        or (kind not in _COMPONENT_ACTIONS and generation_plan_identity is not None)
    ):
        raise ActionDagPlanningError(
            "action_dag_plan.payload_invalid", "action payload authority is not typed"
        )
    result = {
        "schema": "literate-ai/lifecycle-action-payload@1",
        "execution_plan_identity": execution_plan_identity.uri,
        "component_revision": component_revision.uri,
        "kind": kind.value,
    }
    if generation_plan_identity is not None:
        result["generation_plan_identity"] = generation_plan_identity.uri
    return result


def plan_lifecycle_action_dag(
    execution_plan: ComponentExecutionPlan,
    *,
    worker_ids: Sequence[str],
    eligibility: Mapping[str, Sequence[str]] | None = None,
    cache_affinity: Mapping[str, Sequence[str]] | None = None,
) -> tuple[LifecycleActionNode, ...]:
    """Return the canonical fine-grained action graph for one exact plan.

    ``eligibility`` and ``cache_affinity`` are keyed by ``lifecycle_action_id``.
    Omitted eligibility means every configured worker remains a candidate; a caller
    that has live capability/health observations supplies the narrowed exact sets.
    """

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("action DAG planning requires a ComponentExecutionPlan")
    all_workers = _canonical_workers(worker_ids, label="worker catalog")
    eligible = {} if eligibility is None else dict(eligibility)
    affinity = {} if cache_affinity is None else dict(cache_affinity)
    revisions = tuple(
        item.component_revision for item in execution_plan.generation_plans
    )
    generation_plans = {
        item.component_revision.uri: item for item in execution_plan.generation_plans
    }
    component_action_ids = {
        lifecycle_action_id(revision, kind)
        for revision in revisions
        for kind in _COMPONENT_ACTIONS
    }
    package_id = lifecycle_action_id(
        execution_plan.root_revision, LifecycleActionKind.PACKAGE
    )
    finalize_id = lifecycle_action_id(
        execution_plan.root_revision, LifecycleActionKind.FINALIZE
    )
    expected_ids = {*component_action_ids, package_id, finalize_id}
    if set(eligible) - expected_ids or set(affinity) - expected_ids:
        raise ActionDagPlanningError(
            "action_dag_plan.action_unknown",
            "worker eligibility or affinity names an unknown lifecycle action",
        )

    predecessors: dict[str, set[str]] = {key: set() for key in expected_ids}
    for revision in revisions:
        previous: str | None = None
        for kind in _COMPONENT_ACTIONS:
            action_id = lifecycle_action_id(revision, kind)
            if previous is not None:
                predecessors[action_id].add(previous)
            previous = action_id

    edges = {
        edge.identity.uri: edge
        for action_plan in execution_plan.action_plans
        for edge in action_plan.dependency_edges
    }.values()
    for edge in edges:
        provider = edge.provider_revision
        consumer = edge.consumer_revision
        if edge.kind in {DependencyKind.BUILD, DependencyKind.TOOLCHAIN}:
            consumer_kind = LifecycleActionKind.BUILD_INTENT
            provider_kind = LifecycleActionKind.ACCEPT
        elif edge.kind is DependencyKind.RUNTIME:
            consumer_kind = LifecycleActionKind.EXECUTE
            provider_kind = LifecycleActionKind.ACCEPT
        elif edge.kind is DependencyKind.VALIDATION:
            consumer_kind = LifecycleActionKind.TEST
            provider_kind = LifecycleActionKind.TEST
        elif edge.kind in {DependencyKind.PACKAGING, DependencyKind.DEPLOYMENT}:
            consumer_kind = LifecycleActionKind.LINK
            provider_kind = LifecycleActionKind.LINK
        else:
            # Generation consumes an already locked public-interface identity, not
            # provider source or execution evidence, so it adds no scheduling edge.
            continue
        predecessors[lifecycle_action_id(consumer, consumer_kind)].add(
            lifecycle_action_id(provider, provider_kind)
        )

    for revision in revisions:
        predecessors[package_id].add(
            lifecycle_action_id(revision, LifecycleActionKind.LINK)
        )
    predecessors[finalize_id].add(package_id)

    nodes: list[LifecycleActionNode] = []
    for revision in revisions:
        generation_plan = generation_plans[revision.uri]
        for kind in _COMPONENT_ACTIONS:
            action_id = lifecycle_action_id(revision, kind)
            action_workers = _canonical_workers(
                eligible.get(action_id, all_workers), label=action_id
            )
            action_affinity = tuple(sorted(set(affinity.get(action_id, ()))))
            if not set(action_workers) <= set(all_workers):
                raise ActionDagPlanningError(
                    "action_dag_plan.worker_unknown",
                    f"{action_id} eligibility names an unknown worker",
                )
            if not set(action_affinity) <= set(action_workers):
                raise ActionDagPlanningError(
                    "action_dag_plan.affinity_ineligible",
                    f"{action_id} affinity names an ineligible worker",
                )
            nodes.append(
                LifecycleActionNode(
                    action_id,
                    revision,
                    kind,
                    canonical_identity(
                        lifecycle_action_payload(
                            execution_plan.identity,
                            revision,
                            kind,
                            generation_plan.identity,
                        )
                    ),
                    tuple(sorted(predecessors[action_id])),
                    action_workers,
                    action_affinity,
                )
            )
    for action_id, kind in (
        (package_id, LifecycleActionKind.PACKAGE),
        (finalize_id, LifecycleActionKind.FINALIZE),
    ):
        action_workers = _canonical_workers(
            eligible.get(action_id, all_workers), label=action_id
        )
        action_affinity = tuple(sorted(set(affinity.get(action_id, ()))))
        if not set(action_workers) <= set(all_workers):
            raise ActionDagPlanningError(
                "action_dag_plan.worker_unknown",
                f"{action_id} eligibility names an unknown worker",
            )
        if not set(action_affinity) <= set(action_workers):
            raise ActionDagPlanningError(
                "action_dag_plan.affinity_ineligible",
                f"{action_id} affinity names an ineligible worker",
            )
        nodes.append(
            LifecycleActionNode(
                action_id,
                execution_plan.root_revision,
                kind,
                canonical_identity(
                    lifecycle_action_payload(
                        execution_plan.identity, execution_plan.root_revision, kind
                    )
                ),
                tuple(sorted(predecessors[action_id])),
                action_workers,
                action_affinity,
            )
        )
    return tuple(sorted(nodes, key=lambda item: item.action_id))


__all__ = [
    "ActionDagPlanningError",
    "admit_lifecycle_action_workers",
    "lifecycle_action_id",
    "plan_lifecycle_action_dag",
]

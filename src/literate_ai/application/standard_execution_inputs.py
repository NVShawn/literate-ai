"""Derive runtime input scope from the current plan and accepted provider results."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from literate_ai.contracts import StandardComponentAcceptanceEvidence
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import (
    ArtifactAssemblyDependency,
    ArtifactExport,
    ComponentExecutionPlan,
    DependencyInputKind,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.standard_execution_inputs import StandardExecutionInputScope

from .artifact_graph import realize_manifest
from .standard_project_lifecycle import (
    StandardComponentBuildPlan,
    StandardNodeLifecycleResult,
)


def _execution_edges(execution_plan):
    return tuple(
        {
            edge.identity: edge
            for action in execution_plan.action_plans
            for edge in action.dependency_edges
            if edge.semantics.consumed_input
            in {
                DependencyInputKind.ARTIFACT_EXPORT,
                DependencyInputKind.TOOLCHAIN,
            }
        }.values()
    )


def standard_execution_provider_revisions(
    execution_plan: ComponentExecutionPlan,
    component_revision: ContentIdentity,
) -> tuple[ContentIdentity, ...]:
    """Resolve the complete artifact/runtime/toolchain closure, excluding packaging."""
    if not isinstance(execution_plan, ComponentExecutionPlan) or not isinstance(
        component_revision, ContentIdentity
    ):
        raise ValueError("execution provider closure requires typed plan and consumer")
    known = {item.component_revision for item in execution_plan.generation_plans}
    if component_revision not in known:
        raise ValueError("execution consumer is outside the current plan")
    adjacency = {revision: set() for revision in known}
    for edge in _execution_edges(execution_plan):
        if edge.consumer_revision not in known or edge.provider_revision not in known:
            raise ValueError("execution dependency is outside the current plan")
        adjacency[edge.consumer_revision].add(edge.provider_revision)
    reached = {component_revision}
    pending = [component_revision]
    while pending:
        for provider in adjacency[pending.pop()]:
            if provider not in reached:
                if len(reached) >= 16385:
                    raise ValueError(
                        "execution provider closure exceeds 16384 Components"
                    )
                reached.add(provider)
                pending.append(provider)
    indegree = {node: 0 for node in reached}
    for consumer in reached:
        for provider in adjacency[consumer]:
            indegree[provider] += 1
    ready = deque(node for node in reached if not indegree[node])
    visited = 0
    while ready:
        consumer = ready.popleft()
        visited += 1
        for provider in adjacency[consumer]:
            indegree[provider] -= 1
            if not indegree[provider]:
                ready.append(provider)
    if visited != len(reached):
        raise ValueError("execution provider dependencies must be acyclic")
    return tuple(sorted(reached - {component_revision}, key=lambda item: item.uri))


def plan_standard_execution_inputs(
    execution_plan: ComponentExecutionPlan,
    build_plan: StandardComponentBuildPlan,
    exports: tuple[ArtifactExport, ...],
    runtime_providers: tuple[StandardNodeLifecycleResult, ...],
) -> StandardExecutionInputScope:
    """Bind the full provider closure needed by one already-built consumer.

    The caller owns acceptance admission. Structural validation here cannot replace
    verifying provider evidence, nor does this scope authorize a process launch.
    """
    if (
        not isinstance(execution_plan, ComponentExecutionPlan)
        or not isinstance(build_plan, StandardComponentBuildPlan)
        or not isinstance(exports, tuple)
        or not isinstance(runtime_providers, tuple)
        or any(
            not isinstance(item, StandardNodeLifecycleResult)
            for item in runtime_providers
        )
        or any(not isinstance(item, ArtifactExport) for item in exports)
    ):
        raise ValueError(
            "execution input scope requires typed plan, exports and providers"
        )
    if any(item.failure_code is not None for item in runtime_providers):
        raise ValueError("execution requires exactly its accepted runtime providers")
    return _plan_scope(
        execution_plan,
        build_plan,
        exports,
        tuple(
            _Provider(item.component_revision, item.exports, item.acceptance_identity)
            for item in runtime_providers
        ),
    )


@dataclass(frozen=True)
class _Provider:
    component_revision: ContentIdentity
    exports: tuple[ArtifactExport, ...]
    acceptance_identity: ContentIdentity | None


def plan_standard_execution_receipts(
    execution_plan: ComponentExecutionPlan,
    build_plan: StandardComponentBuildPlan,
    exports: tuple[ArtifactExport, ...],
    receipts: tuple[StandardComponentAcceptanceEvidence, ...],
) -> StandardExecutionInputScope:
    """Derive the same scope from receipts; callers must reopen their proof bytes."""
    if (
        not isinstance(execution_plan, ComponentExecutionPlan)
        or not isinstance(build_plan, StandardComponentBuildPlan)
        or not isinstance(exports, tuple)
        or any(not isinstance(item, ArtifactExport) for item in exports)
        or not isinstance(receipts, tuple)
        or len(receipts) > 4096
        or any(
            not isinstance(item, StandardComponentAcceptanceEvidence)
            for item in receipts
        )
    ):
        raise ValueError(
            "execution input scope requires typed plan, exports and receipts"
        )
    return _plan_scope(
        execution_plan,
        build_plan,
        exports,
        tuple(
            _Provider(item.component_revision, item.build.exports, item.identity)
            for item in receipts
        ),
    )


def _plan_scope(execution_plan, build_plan, exports, runtime_providers):
    required = set(
        standard_execution_provider_revisions(
            execution_plan, build_plan.component_revision
        )
    )
    realized = realize_manifest(build_plan.manifest, exports)
    providers = {item.component_revision: item for item in runtime_providers}
    if (
        len(providers) != len(runtime_providers)
        or set(providers) != required
        or any(
            item.acceptance_identity is None
            or not item.exports
            or any(
                export.component_revision != item.component_revision
                for export in item.exports
            )
            or len({export.identity for export in item.exports}) != len(item.exports)
            for item in runtime_providers
        )
    ):
        raise ValueError("execution requires exactly its accepted runtime providers")
    provider_ids = {
        export.identity for item in runtime_providers for export in item.exports
    }
    if (
        len(provider_ids) > 16384
        or not set(build_plan.provider_artifact_identities) <= provider_ids
    ):
        raise ValueError("execution provider closure differs from compilation inputs")
    outputs = {revision: result.exports for revision, result in providers.items()}
    outputs[build_plan.component_revision] = realized.exports
    if any(
        not set(export.dependency_artifact_identities) <= provider_ids
        for values in outputs.values()
        for export in values
    ):
        raise ValueError("execution provider artifact provenance is incomplete")
    edges = tuple(
        edge
        for edge in _execution_edges(execution_plan)
        if edge.consumer_revision in outputs and edge.kind is DependencyKind.RUNTIME
    )
    count = sum(
        len(outputs[edge.consumer_revision]) * len(outputs[edge.provider_revision])
        for edge in edges
    )
    if count > 16384:
        raise ValueError("execution runtime bindings exceed 16384 inputs")
    bindings = tuple(
        sorted(
            (
                ArtifactAssemblyDependency(
                    output.identity,
                    supplied.identity,
                    DependencyKind.RUNTIME,
                    edge.identity,
                    providers[edge.provider_revision].acceptance_identity,
                )
                for edge in edges
                for output in outputs[edge.consumer_revision]
                for supplied in outputs[edge.provider_revision]
            ),
            key=lambda item: item.identity.uri,
        )
    )
    bound = set(build_plan.provider_artifact_identities) | {
        item.provider_artifact_identity for item in bindings
    }
    return StandardExecutionInputScope(
        execution_plan.identity,
        build_plan.component_revision,
        build_plan.identity,
        tuple(
            sorted(
                (item.identity for item in realized.exports), key=lambda item: item.uri
            )
        ),
        build_plan.provider_artifact_identities,
        bindings,
        tuple(sorted(provider_ids - bound, key=lambda item: item.uri)),
    )

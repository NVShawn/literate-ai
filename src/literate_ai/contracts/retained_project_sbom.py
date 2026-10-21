"""Project original-source plan and locked Component authority into managed SBOMs.

This graph records reviewed authority. It does not observe installed dependencies,
qualify a toolchain, authorize a build or issue lifecycle acceptance.
"""

from __future__ import annotations

from dataclasses import replace

from .capabilities import DependencyKind
from .component_locking import ComponentLock
from .identity import canonical_identity
from .retained_project_lifecycle import RetainedProjectPlan
from .sbom import (
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    project_component_lock_managed_graph,
    retained_project_bom_ref,
)


def project_retained_project_managed_graph(
    plan: RetainedProjectPlan, locks: tuple[ComponentLock, ...], *, version: str
) -> CycloneDxManagedGraph:
    if not isinstance(plan, RetainedProjectPlan) or not isinstance(locks, tuple):
        raise TypeError("retained SBOM requires a typed plan and exact lock tuple")
    if any(not isinstance(lock, ComponentLock) for lock in locks):
        raise TypeError("retained SBOM locks must be typed Component locks")
    if tuple(lock.identity for lock in locks) != plan.component_locks:
        raise ValueError("retained.sbom.component-lock-closure-mismatch")
    root = CycloneDxManagedComponent(
        retained_project_bom_ref(plan.identity),
        ManagedComponentKind.RETAINED_PROJECT,
        plan.identity,
        plan.project_id,
        version,
        (),
    )
    components = {root.bom_ref: root}
    edges: set[CycloneDxManagedEdge] = set()
    for lock in locks:
        graph = project_component_lock_managed_graph(lock, lock.root_revision)
        for component in graph.components:
            if component.kind is ManagedComponentKind.ROOT:
                component = replace(
                    component, kind=ManagedComponentKind.COMPONENT, scopes=("build",)
                )
            existing = components.get(component.bom_ref)
            if existing is not None:
                if any(
                    getattr(existing, field) != getattr(component, field)
                    for field in (
                        "kind",
                        "identity",
                        "name",
                        "version",
                        "version_range",
                    )
                ):
                    raise ValueError("retained.sbom.conflicting-managed-component")
                component = replace(
                    component,
                    scopes=tuple(sorted(set(existing.scopes) | set(component.scopes))),
                )
            components[component.bom_ref] = component
        edges.update(graph.edges)
        edges.add(
            CycloneDxManagedEdge(
                root.bom_ref,
                graph.root_ref,
                DependencyKind.BUILD,
                False,
                canonical_identity(
                    {
                        "schema": "literate-ai/retained-project-lock-edge@1",
                        "plan_identity": plan.identity.uri,
                        "component_lock_identity": lock.identity.uri,
                        "root_ref": graph.root_ref,
                    }
                ),
            )
        )
    return CycloneDxManagedGraph(
        root.bom_ref,
        tuple(components[ref] for ref in sorted(components)),
        tuple(sorted(edges)),
        plan.identity,
    )

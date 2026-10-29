"""Shared Component lock preparation, currentness checks, and pair publication.

CLI commands and read-only observability use the same input revalidation and
artifact stability checks. Check/diff operations never acquire a writing mutex.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .legacy_generation_catalog import LegacyFlavorGraphObservation

from literate_ai.adapters.component_lock_planning import (
    ComponentCatalogSnapshot,
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.application.component_lock_resolution import (
    ComponentLockResolutionPlan,
    ComponentLockResolutionResult,
    ComponentLockResolver,
)


@dataclass(frozen=True, slots=True)
class PreparedComponentLock:
    component_root: Path
    plan: ComponentLockResolutionPlan
    result: ComponentLockResolutionResult
    catalog_snapshot: ComponentCatalogSnapshot | None = None
    requested_flavor_roots: tuple[Path, ...] | None = None
    lineage_flavors: LegacyFlavorGraphObservation | None = None


def prepare_component_lock(
    component: Path,
    *,
    target: str,
    flavors: tuple[str, ...],
    flavor_roots: tuple[Path, ...],
    planner: FilesystemComponentLockPlanner,
    reuse_lineage_catalog: bool = False,
) -> PreparedComponentLock:
    lineage_flavors = None
    if reuse_lineage_catalog:
        from literate_ai.projects import discover_project

        from .legacy_generation_catalog import (
            LegacyFlavorGraphObservation,
            scoped_flavor_graph_observation,
        )

        project = discover_project(component)
        if project is not None:
            roots = project.roots("flavor")
            lineage_flavors = scoped_flavor_graph_observation(roots)
            if lineage_flavors is None:
                lineage_flavors = LegacyFlavorGraphObservation.capture(roots)
    plan, snapshot = planner.plan_with_snapshot(
        component,
        target_name=target,
        flavor_selectors=flavors,
        flavor_roots=flavor_roots,
        **({"lineage_flavors": lineage_flavors} if lineage_flavors is not None else {}),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    return PreparedComponentLock(
        component.resolve(strict=True),
        plan,
        result,
        snapshot,
        flavor_roots,
        lineage_flavors,
    )


def operate_component_lock(
    prepared: PreparedComponentLock,
    *,
    target: str,
    flavors: tuple[str, ...],
    flavor_roots: tuple[Path, ...],
    planner: FilesystemComponentLockPlanner,
    check: bool,
    difference: bool,
) -> tuple[dict[str, object], int]:
    component_root = prepared.component_root
    result = prepared.result
    lock_store = ComponentLockStore(component_root)
    audit_store = ComponentResolutionAuditStore(component_root, target)

    def require_inputs_unchanged() -> None:
        snapshot = (
            prepared.catalog_snapshot
            if (check or difference) and prepared.requested_flavor_roots == flavor_roots
            else None
        )
        if snapshot is None:
            current = prepare_component_lock(
                component_root,
                target=target,
                flavors=flavors,
                flavor_roots=flavor_roots,
                planner=planner,
            )
        else:
            # Reuse parsed immutable catalog values only within this read-only
            # transaction. Reopen all bound bytes and membership before planning;
            # plan_snapshot still observes current graph authority and revalidates
            # selected inputs afterwards. This is private prepared-operation state,
            # not a shared cache of trusted currentness.
            snapshot.require_unchanged()
            plan = planner.plan_snapshot(
                snapshot,
                target_name=target,
                flavor_selectors=flavors,
                **(
                    {"lineage_flavors": prepared.lineage_flavors}
                    if prepared.lineage_flavors is not None
                    else {}
                ),
            )
            current = PreparedComponentLock(
                component_root,
                plan,
                ComponentLockResolver().resolve(
                    plan, expected_input_evidence_identity=plan.identity
                ),
                snapshot,
                flavor_roots,
            )
        if (
            current.plan.identity != prepared.plan.identity
            or current.result.lock.identity != result.lock.identity
            or current.result.catalog_audit.identity != result.catalog_audit.identity
        ):
            raise ComponentLockPlanningError(
                "component_lock.inputs_changed",
                "Component or catalog inputs changed during lock materialization",
            )

    with nullcontext() if check or difference else lock_store.operation():
        require_inputs_unchanged()
        first_checks = component_artifact_checks(lock_store, audit_store, result)
        require_inputs_unchanged()
        second_checks = component_artifact_checks(lock_store, audit_store, result)
        require_component_checks_stable(first_checks, second_checks)
        if check or difference:
            require_inputs_unchanged()
            lock_check, audit_check = second_checks
            current = lock_check.current and audit_check.current
            return {
                **_base_report(prepared, target, lock_check, audit_check),
                "mode": "diff" if difference else "check",
                "current": current,
            }, (0 if current else 1)

        lock_snapshot = lock_store.snapshot()
        audit_snapshot = audit_store.snapshot()
        try:
            audit_updated = audit_store.update(
                result.catalog_audit, revalidate=require_inputs_unchanged
            )
            lock_updated = lock_store.update(
                result.lock, revalidate=require_inputs_unchanged
            )
            require_inputs_unchanged()
            first_final = component_artifact_checks(lock_store, audit_store, result)
            require_inputs_unchanged()
            final_checks = component_artifact_checks(lock_store, audit_store, result)
            require_component_checks_stable(
                first_final, final_checks, require_current=True
            )
            require_inputs_unchanged()
        except Exception:
            rollback_errors: list[Exception] = []
            for restore in (
                lambda: lock_store.restore_if_current(
                    lock_snapshot, expected_current=result.lock
                ),
                lambda: audit_store.restore_if_current(
                    audit_snapshot, expected_current=result.catalog_audit
                ),
            ):
                try:
                    restore()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise ComponentLockStoreError(
                    "component_lock.rollback_failed",
                    "Component lock/audit publication failed and could not be "
                    "rolled back",
                ) from rollback_errors[0]
            raise
        lock_check, audit_check = final_checks
        return {
            **_base_report(prepared, target, lock_check, audit_check),
            "mode": "update",
            "lock_updated": lock_updated,
            "catalog_audit_updated": audit_updated,
        }, 0


def component_artifact_checks(lock_store, audit_store, result):
    return (
        lock_store.check(result.lock, authorings=result.lock.authorings),
        audit_store.check(result.catalog_audit),
    )


def require_component_checks_stable(
    first, second, *, require_current: bool = False
) -> None:
    if tuple(item.to_dict() for item in first) != tuple(
        item.to_dict() for item in second
    ):
        raise ComponentLockStoreError(
            "component_lock.concurrent_change",
            "Component lock or resolution audit changed during final verification",
        )
    if require_current and not all(item.current for item in second):
        raise ComponentLockStoreError(
            "component_lock.publication_inconsistent",
            "Component lock and resolution audit were not published as one exact pair",
        )


def _base_report(prepared, target, lock_check, audit_check) -> dict[str, object]:
    return {
        "schema": "literate-ai/component-lock-command@1",
        "component": str(prepared.component_root),
        "target": target,
        "resolution_plan_identity": prepared.plan.identity.uri,
        "component_lock_identity": prepared.result.lock.identity.uri,
        "catalog_audit_identity": prepared.result.catalog_audit.identity.uri,
        "provider_resolutions": [
            {
                "resolution_id": item.request.resolution_id,
                "selected_provider": item.selected_provider,
                "identity": item.identity.uri,
                "fallback_reason": item.fallback_reason,
                "override_declaration_identity": (
                    None
                    if item.override_declaration_identity is None
                    else item.override_declaration_identity.uri
                ),
                "override_provenance_identity": (
                    None
                    if item.override_provenance_identity is None
                    else item.override_provenance_identity.uri
                ),
            }
            for item in prepared.result.lock.provider_resolutions
        ],
        "lock": lock_check.to_dict(),
        "catalog_audit": audit_check.to_dict(),
    }

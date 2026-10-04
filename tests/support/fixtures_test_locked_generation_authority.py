from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_locked_generation_authority``."""






from pathlib import Path



from literate_ai.adapters.component_lock_planning import (
    FilesystemComponentLockPlanner,
)

from literate_ai.adapters.component_locks import ComponentLockStore

from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)


from literate_ai.application.component_lock_resolution import ComponentLockResolver



_SELECTORS = ("+macos", "+python")

_TARGET = "macos-host"

def _write_lock(
    component: Path,
    flavors: Path,
    *,
    target: str = _TARGET,
    selectors: tuple[str, ...] = _SELECTORS,
):
    plan = FilesystemComponentLockPlanner().plan(
        component,
        target_name=target,
        flavor_selectors=selectors,
        flavor_roots=(flavors,),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    ComponentResolutionAuditStore(component, target).update(result.catalog_audit)
    ComponentLockStore(component).update(result.lock)
    return result.lock


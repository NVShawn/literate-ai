"""Filesystem application adapter for atomic Component lock publication."""

from __future__ import annotations

from pathlib import Path

from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts import ContentIdentity, rebuild_project_authority_identity
from literate_ai.projects import LoadedProject, PinnedInputClosureError

from .component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from .component_locks import ComponentLockStore, ComponentLockStoreError
from .component_resolution_audits import ComponentResolutionAuditStore
from .locked_generation_authority import FilesystemLockedGenerationAuthorityReader


class ProjectComponentLockSetError(RuntimeError):
    """The current project lock set cannot form exact rebuild authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def project_component_roots(project: LoadedProject) -> tuple[Path, ...]:
    """Discover canonical authored Component roots from declared catalogs."""

    if not isinstance(project, LoadedProject):
        raise TypeError("project must be a LoadedProject")
    roots: list[Path] = []
    for catalog in project.roots("component"):
        for manifest in catalog.rglob("component.md"):
            if manifest.is_symlink() or not manifest.is_file():
                raise ProjectComponentLockSetError(
                    "component_lock.project_catalog_unsafe",
                    "project Component catalogs cannot contain redirected manifests",
                )
            roots.append(manifest.parent.resolve(strict=True))
    canonical = tuple(sorted(set(roots)))
    if not canonical:
        raise ProjectComponentLockSetError(
            "component_lock.component_missing",
            "project has no readable component.md authoring documents",
        )
    return canonical


def current_project_component_lock_identities(
    project: LoadedProject,
) -> tuple[ContentIdentity, ...]:
    """Read every persisted current lock without treating the catalog as selection.

    Component roots are a catalog of available authoring authority.  A lock file is
    the explicit selection of one of those Components for a target.  In particular,
    inherited Components deliberately arrive without their parent's target-local lock
    evidence, so an absent lock is not itself an invalid catalog entry.
    """

    identities: list[ContentIdentity] = []
    try:
        for component in project_component_roots(project):
            lock_path = component / "component.lock.json"
            try:
                lock_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProjectComponentLockSetError(
                    "component_lock.project_lock_set_unavailable",
                    "project Component lock set could not be inspected",
                ) from exc
            identities.append(current_component_lock_identity(component))
    except ProjectComponentLockSetError:
        raise
    except (
        ComponentLockPlanningError,
        ComponentLockStoreError,
        PinnedInputClosureError,
    ) as exc:
        raise ProjectComponentLockSetError(
            getattr(exc, "code", "component_lock.project_lock_set_invalid"),
            getattr(exc, "message", str(exc)),
        ) from exc
    result = tuple(sorted(identities, key=lambda item: item.uri))
    if not result:
        raise ProjectComponentLockSetError(
            "component_lock.missing",
            "project has no persisted Component locks",
        )
    if len({item.uri for item in result}) != len(result):
        raise ProjectComponentLockSetError(
            "component_lock.project_lock_set_ambiguous",
            "project Component lock identities must be unique",
        )
    return result


def current_component_lock_identity(component_root: Path) -> ContentIdentity:
    """Read one exact current lock and its complete pinned input closure."""

    reader = FilesystemLockedGenerationAuthorityReader()
    try:
        component = Path(component_root).resolve(strict=True)
        catalog = reader.load_catalog(component)
        lock_store = ComponentLockStore(component)
        lock = lock_store.read_from_catalog(authorings=catalog.authorings)
        closure = catalog.locked_input_closure(
            lock,
            lock_path=lock_store.path,
            lock_boundary=lock_store.storage_root,
        )
        catalog.require_unchanged()
        lock_store.require_current(lock)
        closure.require_unchanged()
        return lock.identity
    except ProjectComponentLockSetError:
        raise
    except (
        ComponentLockPlanningError,
        ComponentLockStoreError,
        PinnedInputClosureError,
        OSError,
    ) as exc:
        raise ProjectComponentLockSetError(
            getattr(exc, "code", "component_lock.project_lock_set_invalid"),
            getattr(exc, "message", str(exc)),
        ) from exc


def current_rebuild_project_authority_identity(
    project: LoadedProject,
    validated_project_authority_identity: ContentIdentity,
) -> ContentIdentity:
    """Bind validated project authority to every current canonical Component lock."""

    return rebuild_project_authority_identity(
        validated_project_authority_identity,
        current_project_component_lock_identities(project),
    )


def update_component_lock(
    component_root: Path,
    *,
    target_name: str,
    flavor_selectors: tuple[str, ...] = (),
    flavor_roots: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Resolve and atomically publish one exact lock/audit pair."""

    component = component_root.resolve(strict=True)
    planner = FilesystemComponentLockPlanner()

    def resolve():
        plan = planner.plan(
            component,
            target_name=target_name,
            flavor_selectors=flavor_selectors,
            flavor_roots=flavor_roots,
        )
        result = ComponentLockResolver().resolve(
            plan,
            expected_input_evidence_identity=plan.identity,
        )
        return plan, result

    plan, result = resolve()
    lock_store = ComponentLockStore(component)
    audit_store = ComponentResolutionAuditStore(component, target_name)

    def require_unchanged() -> None:
        current_plan, current_result = resolve()
        if (
            current_plan.identity != plan.identity
            or current_result.lock.identity != result.lock.identity
            or current_result.catalog_audit.identity != result.catalog_audit.identity
        ):
            raise ComponentLockStoreError(
                "component_lock.inputs_changed",
                "Component or catalog inputs changed during lock materialization",
            )

    def checks():
        return (
            lock_store.check(result.lock, authorings=result.lock.authorings),
            audit_store.check(result.catalog_audit),
        )

    with lock_store.operation():
        require_unchanged()
        first = checks()
        require_unchanged()
        second = checks()
        if tuple(item.to_dict() for item in first) != tuple(
            item.to_dict() for item in second
        ):
            raise ComponentLockStoreError(
                "component_lock.concurrent_change",
                "Component lock or resolution audit changed during verification",
            )
        lock_snapshot = lock_store.snapshot()
        audit_snapshot = audit_store.snapshot()
        try:
            audit_updated = audit_store.update(
                result.catalog_audit,
                revalidate=require_unchanged,
            )
            lock_updated = lock_store.update(
                result.lock,
                revalidate=require_unchanged,
            )
            require_unchanged()
            final = checks()
            require_unchanged()
            repeated = checks()
            if tuple(item.to_dict() for item in final) != tuple(
                item.to_dict() for item in repeated
            ) or not all(item.current for item in repeated):
                raise ComponentLockStoreError(
                    "component_lock.publication_inconsistent",
                    "Component lock and resolution audit were not published as one "
                    "exact pair",
                )
        except Exception:
            rollback_errors = []
            for restore in (
                lambda: lock_store.restore_if_current(
                    lock_snapshot,
                    expected_current=result.lock,
                ),
                lambda: audit_store.restore_if_current(
                    audit_snapshot,
                    expected_current=result.catalog_audit,
                ),
            ):
                try:
                    restore()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise ComponentLockStoreError(
                    "component_lock.rollback_failed",
                    "Component lock/audit publication failed and rollback failed",
                ) from rollback_errors[0]
            raise
    lock_check, audit_check = repeated
    return {
        "schema": "literate-ai/component-lock-command@1",
        "component": str(component),
        "target": target_name,
        "mode": "update",
        "resolution_plan_identity": plan.identity.uri,
        "component_lock_identity": result.lock.identity.uri,
        "catalog_audit_identity": result.catalog_audit.identity.uri,
        "lock": lock_check.to_dict(),
        "catalog_audit": audit_check.to_dict(),
        "lock_updated": lock_updated,
        "catalog_audit_updated": audit_updated,
    }


__all__ = [
    "ProjectComponentLockSetError",
    "current_component_lock_identity",
    "current_project_component_lock_identities",
    "current_rebuild_project_authority_identity",
    "project_component_roots",
    "update_component_lock",
]

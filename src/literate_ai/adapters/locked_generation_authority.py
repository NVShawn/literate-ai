"""Filesystem admission adapter for canonical locked generation authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    ComponentResolutionAuditStoreError,
)
from literate_ai.application.component_lock_resolution import (
    ComponentLockResolutionError,
    ComponentLockResolver,
)
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
    LockedGenerationAuthorityError,
    project_locked_generation_authority,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.component_locking import (
    ComponentLock,
    ResolvedComponentAsset,
)
from literate_ai.contracts.identity import ContentIdentity, ContentReference
from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError
from literate_ai.storage import FileSystemCAS


class LockedGenerationAuthorityReaderError(RuntimeError):
    """A persisted lock or its current exact filesystem authority is unusable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LockedGenerationAuthoritySnapshot:
    """Typed generation authority with its still-live exact-byte closure."""

    authority: LockedGenerationAuthority
    _input_closure: PinnedInputClosure = field(repr=False, compare=False)
    _catalog: ComponentCatalogSnapshot = field(repr=False, compare=False)
    _planner: FilesystemComponentLockPlanner = field(repr=False, compare=False)
    _resolution_plan_identity: ContentIdentity = field(repr=False, compare=False)
    _resolution_audit_store: ComponentResolutionAuditStore = field(
        repr=False, compare=False
    )
    _resolution_audit_identity: ContentIdentity = field(repr=False, compare=False)
    _target_name: str = field(repr=False, compare=False)
    _flavor_selectors: tuple[str, ...] = field(repr=False, compare=False)
    _lock_store: ComponentLockStore | None = field(
        default=None, repr=False, compare=False
    )
    _component_lock: ComponentLock | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (self._lock_store is None) != (self._component_lock is None):
            raise TypeError("lock store and Component lock must be supplied together")

    @property
    def input_closure_identity(self) -> str:
        return self._input_closure.identity

    @property
    def input_closure(self) -> PinnedInputClosure:
        return self._input_closure.fork()

    @property
    def catalog_audit_identity(self) -> ContentIdentity:
        return self._catalog.catalog_audit_identity

    def component_authoring_content(self, authoring_identity: ContentIdentity) -> bytes:
        return self._read(self._catalog.component_authoring_content, authoring_identity)

    def component_content(
        self, authoring_identity: ContentIdentity, reference: ContentReference
    ) -> bytes:
        return self._read(
            self._catalog.component_content, authoring_identity, reference
        )

    def admit_component_asset(
        self,
        authoring_identity: ContentIdentity,
        asset: ResolvedComponentAsset,
        cas: FileSystemCAS,
    ) -> BlobRef:
        return self._read(
            self._catalog.admit_component_asset, authoring_identity, asset, cas
        )

    def flavor_content(
        self, flavor_revision: ContentIdentity, reference: ContentReference
    ) -> bytes:
        return self._read(self._catalog.flavor_content, flavor_revision, reference)

    @staticmethod
    def _read(operation, *args):
        try:
            return operation(*args)
        except ComponentLockPlanningError as exc:
            raise LockedGenerationAuthorityReaderError(exc.code, exc.message) from exc

    def require_unchanged(self) -> None:
        """Fail if any exact byte admitted by the reader has changed."""

        try:
            if self._lock_store is not None and self._component_lock is not None:
                self._lock_store.require_current(self._component_lock)
            self._input_closure.require_unchanged()
            sources = tuple(
                source
                for node in self.authority.lock.nodes
                for source in node.revision.repository_sources
            )
            current = self._planner.plan_snapshot(
                self._catalog,
                target_name=self._target_name,
                flavor_selectors=self._flavor_selectors,
                **({"locked_repository_sources": sources} if sources else {}),
            )
            if current.identity != self._resolution_plan_identity:
                raise LockedGenerationAuthorityReaderError(
                    "component_lock.stale",
                    "effective Component resolution authority changed after admission",
                )
            audit = self._resolution_audit_store.read()
            if audit.identity != self._resolution_audit_identity:
                raise LockedGenerationAuthorityReaderError(
                    "component_lock.stale",
                    "Component resolution audit changed after admission",
                )
        except ComponentLockStoreError as exc:
            raise LockedGenerationAuthorityReaderError(exc.code, exc.message) from exc
        except (
            ComponentLockPlanningError,
            ComponentResolutionAuditStoreError,
            PinnedInputClosureError,
        ) as exc:
            raise LockedGenerationAuthorityReaderError(
                "component_lock.stale", getattr(exc, "message", str(exc))
            ) from exc


class FilesystemLockedGenerationAuthorityReader:
    """Read one canonical lock against a safely snapshotted current catalog."""

    def __init__(self, planner: FilesystemComponentLockPlanner | None = None) -> None:
        self._planner = planner or FilesystemComponentLockPlanner()

    def read(
        self,
        component_root: Path,
        *,
        target_name: str,
        flavor_selectors: tuple[str, ...] = (),
        flavor_roots: tuple[Path, ...] = (),
    ) -> LockedGenerationAuthoritySnapshot:
        catalog = self.load_catalog(component_root, flavor_roots=flavor_roots)
        return self.resolve(
            catalog,
            target_name=target_name,
            flavor_selectors=flavor_selectors,
        )

    def load_catalog(
        self,
        component_root: Path,
        *,
        flavor_roots: tuple[Path, ...] = (),
    ) -> ComponentCatalogSnapshot:
        """Capture one exact filesystem catalog without resolving policy."""

        try:
            return self._planner.snapshot(component_root, flavor_roots=flavor_roots)
        except (ComponentLockPlanningError, PinnedInputClosureError) as exc:
            code = getattr(exc, "code", "component_lock.stale")
            message = getattr(exc, "message", str(exc))
            raise LockedGenerationAuthorityReaderError(code, message) from exc

    def resolve(
        self,
        catalog: ComponentCatalogSnapshot,
        *,
        target_name: str,
        flavor_selectors: tuple[str, ...] = (),
    ) -> LockedGenerationAuthoritySnapshot:
        """Resolve one accepted lock solely against an exact captured catalog."""

        try:
            lock_store = ComponentLockStore(catalog.root)
            lock = lock_store.read_from_catalog(authorings=catalog.authorings)
            authority = project_locked_generation_authority(
                lock,
                root_authoring=catalog.root_authoring,
                authorings=lock.authorings,
                flavor_catalog=catalog.flavor_revisions,
                target_name=target_name,
                flavor_selectors=flavor_selectors,
            )
            plan = self._planner.plan_snapshot(
                catalog,
                target_name=target_name,
                flavor_selectors=flavor_selectors,
            )
            expected = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            audit_store = ComponentResolutionAuditStore(catalog.root, target_name)
            audit = audit_store.read()
            if (
                lock.identity != expected.lock.identity
                or audit.component_lock_identity != lock.identity
                or audit.catalog_identity != plan.catalog_identity
                or audit.resolver_identity != plan.resolver_identity
                or audit.identity != expected.catalog_audit.identity
            ):
                raise LockedGenerationAuthorityReaderError(
                    "component_lock.stale",
                    "Component lock and resolution audit do not bind the current "
                    "effective authority graph",
                )
            closure = catalog.locked_input_closure(
                lock,
                lock_path=lock_store.path,
                lock_boundary=lock_store.storage_root,
            )
            catalog.require_unchanged()
            closure.require_unchanged()
            sources = tuple(
                source for node in plan.nodes for source in node.repository_sources
            )
            current_plan = self._planner.plan_snapshot(
                catalog,
                target_name=target_name,
                flavor_selectors=flavor_selectors,
                **({"locked_repository_sources": sources} if sources else {}),
            )
            if current_plan.identity != plan.identity:
                raise LockedGenerationAuthorityReaderError(
                    "component_lock.stale",
                    "effective Component resolution authority changed during admission",
                )
        except (
            ComponentLockPlanningError,
            ComponentLockStoreError,
            ComponentResolutionAuditStoreError,
            ComponentLockResolutionError,
            LockedGenerationAuthorityError,
            PinnedInputClosureError,
        ) as exc:
            code = getattr(exc, "code", "component_lock.stale")
            message = getattr(exc, "message", str(exc))
            raise LockedGenerationAuthorityReaderError(code, message) from exc
        return LockedGenerationAuthoritySnapshot(
            authority,
            closure,
            catalog,
            self._planner,
            plan.identity,
            audit_store,
            audit.identity,
            target_name,
            flavor_selectors,
            lock_store,
            lock,
        )


__all__ = [
    "FilesystemLockedGenerationAuthorityReader",
    "LockedGenerationAuthorityReaderError",
    "LockedGenerationAuthoritySnapshot",
]

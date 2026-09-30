"""Shared orchestration for deterministic target-specific Component locks."""

from __future__ import annotations

import hashlib
from pathlib import Path

from literate_ai.adapters.component_lock_operations import (
    PreparedComponentLock as _PreparedComponentLock,
)
from literate_ai.adapters.component_lock_operations import (
    component_artifact_checks as _artifact_checks,
)
from literate_ai.adapters.component_lock_operations import (
    operate_component_lock as _one_component_lock,
)
from literate_ai.adapters.component_lock_operations import (
    prepare_component_lock as _prepare_component_lock,
)
from literate_ai.adapters.component_lock_operations import (
    require_component_checks_stable as _require_checks_stable,
)
from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_lock_reviews import (
    ComponentLockReviewError,
    ComponentLockReviewStore,
)
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
    ComponentResolutionAuditStoreError,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
)
from literate_ai.application.component_lock_resolution import (
    ComponentLockResolutionError,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PROJECT_FILENAME, discover_project

from .lock_command_errors import LockCommandError

_LARGE_REVIEW_ALGORITHM_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/component-lock-diff-algorithm@1",
        "ordering": "json-pointer-lexicographic",
        "sequence_comparison": "index",
    }
)
_LARGE_REVIEW_POLICY_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/component-lock-review-policy@1",
        "page_entries": 512,
        "maximum_pages": 64,
        "maximum_differences": 32768,
        "atomic_replacement": True,
    }
)


def component_lock_from_args(args) -> tuple[dict[str, object], int]:
    """Pre-plan the complete selection, then update or verify lock artifacts."""
    from .repository_lock_commands import (
        repository_lock_from_args,
        selected_repository_root,
    )

    root = selected_repository_root(args, Path(args.component))
    if root is not None:
        return repository_lock_from_args(args, root)
    return _component_locks_from_args(args)


def _component_locks_from_args(args, *, allow_empty: bool = False):
    """Resolve Components; allow_empty selects declared repository catalogs only."""

    selected = Path(args.component)
    selected_root = selected.resolve(strict=True)
    large_review = getattr(args, "large_review", None)
    if large_review is not None:
        return _component_lock_review_from_args(args, selected_root, large_review)
    project_mode = allow_empty or not (selected_root / "component.md").is_file()
    flavor_roots = tuple(Path(item) for item in args.flavor_root)
    explicit_flavors = tuple(args.flavor)
    try:
        components = _component_roots(selected, allow_empty=allow_empty)
        project = discover_project(selected_root)

        def flavors_for(component: Path) -> tuple[str, ...]:
            return (
                explicit_flavors
                if project is None
                else project.flavor_selectors_for(component, explicit_flavors)
            )

        planner = FilesystemComponentLockPlanner()
        prepared = tuple(
            (
                _prepare_component_lock(
                    component,
                    target=args.target,
                    flavors=flavors_for(component),
                    flavor_roots=flavor_roots,
                    planner=planner,
                ),
                flavors_for(component),
            )
            for component in components
        )
        completed = tuple(
            _one_component_lock(
                item,
                target=args.target,
                flavors=flavors,
                flavor_roots=flavor_roots,
                planner=planner,
                check=args.check,
                difference=args.diff,
            )
            for item, flavors in prepared
        )
    except (
        ComponentLockPlanningError,
        ComponentLockResolutionError,
        ComponentLockStoreError,
        ComponentLockReviewError,
        ComponentResolutionAuditStoreError,
        StandardLifecycleBindingError,
    ) as exc:
        raise LockCommandError(exc.code, str(exc)) from exc
    reports = [item[0] for item in completed]
    statuses = [item[1] for item in completed]
    if len(reports) == 1 and not project_mode:
        return reports[0], statuses[0]
    return {
        "schema": "literate-ai/project-component-lock-command@1",
        "project": str(selected_root),
        "target": args.target,
        "mode": "diff" if args.diff else "check" if args.check else "update",
        "current": all(status == 0 for status in statuses),
        "components": reports,
    }, max(statuses, default=0)


def _component_lock_review_from_args(
    args, selected_root: Path, operation: str
) -> tuple[dict[str, object], int]:
    try:
        if not (selected_root / "component.md").is_file():
            raise ComponentLockReviewError(
                "component_lock.review_component_required",
                "Large review requires one exact Component directory",
            )
        project = discover_project(selected_root)
        if project is None:
            raise ComponentLockReviewError(
                "component_lock.review_project_required",
                "Large review requires a canonical Literate AI project",
            )
        transaction_id = getattr(args, "transaction_id", None)
        page_identity = getattr(args, "page_identity", None)
        if operation == "start":
            if transaction_id is not None or page_identity is not None:
                raise ComponentLockReviewError(
                    "component_lock.review_argument_invalid",
                    "Large-review start does not accept transaction or page identities",
                )
        elif transaction_id is None:
            raise ComponentLockReviewError(
                "component_lock.review_identity_required",
                "This large-review operation requires --transaction-id",
            )
        if operation == "acknowledge" and page_identity is None:
            raise ComponentLockReviewError(
                "component_lock.review_page_identity_required",
                "Large-review acknowledge requires --page-identity",
            )
        if operation != "acknowledge" and page_identity is not None:
            raise ComponentLockReviewError(
                "component_lock.review_argument_invalid",
                "--page-identity is valid only for large-review acknowledge",
            )

        review_store = ComponentLockReviewStore(project.root)
        if operation == "cleanup":
            assert transaction_id is not None
            component = selected_root.relative_to(project.root).as_posix()
            return review_store.cleanup(transaction_id, component=component), 0

        flavor_roots = tuple(Path(item) for item in args.flavor_root)
        flavors = project.flavor_selectors_for(selected_root, tuple(args.flavor))
        planner = FilesystemComponentLockPlanner()
        prepared = _prepare_component_lock(
            selected_root,
            target=args.target,
            flavors=flavors,
            flavor_roots=flavor_roots,
            planner=planner,
        )
        lock_store = ComponentLockStore(selected_root)

        def current_review():
            current = _prepare_component_lock(
                selected_root,
                target=args.target,
                flavors=flavors,
                flavor_roots=flavor_roots,
                planner=planner,
            )
            if (
                current.plan.identity != prepared.plan.identity
                or current.result.lock.identity != prepared.result.lock.identity
                or current.result.catalog_audit.identity
                != prepared.result.catalog_audit.identity
            ):
                raise ComponentLockPlanningError(
                    "component_lock.inputs_changed",
                    "Component or catalog inputs changed during large lock review",
                )
            first_check = lock_store.review(
                prepared.result.lock,
                authorings=prepared.result.lock.authorings,
            )
            first_snapshot = lock_store.snapshot()
            check = lock_store.review(
                prepared.result.lock,
                authorings=prepared.result.lock.authorings,
            )
            snapshot = lock_store.snapshot()
            if (
                first_check.to_dict() != check.to_dict()
                or first_snapshot.content != snapshot.content
            ):
                raise ComponentLockReviewError(
                    "component_lock.concurrent_change",
                    "Component lock changed while its large review was prepared",
                )
            if check.state != "stale":
                raise ComponentLockReviewError(
                    "component_lock.review_stale_lock_required",
                    "Large review requires one existing stale Component lock",
                )
            assert snapshot.content is not None
            binding = _large_review_binding(
                prepared,
                project_root=project.root,
                project_definition_identity=project.definition.identity.uri,
                target=args.target,
                flavors=flavors,
                flavor_roots=flavor_roots,
                current_identity=check.current_identity,
                current_bytes=snapshot.content,
            )
            return check, binding

        with lock_store.operation():
            check, binding = current_review()
            if operation == "start":
                report = review_store.start(
                    binding=binding,
                    differences=check.differences,
                )
            elif operation == "status":
                assert transaction_id is not None
                report = review_store.status(
                    transaction_id,
                    binding=binding,
                    differences=check.differences,
                )
            elif operation == "acknowledge":
                assert transaction_id is not None and page_identity is not None
                report = review_store.acknowledge(
                    transaction_id,
                    page_identity,
                    binding=binding,
                    differences=check.differences,
                )
            elif operation == "apply":
                assert transaction_id is not None
                review_store.require_ready(
                    transaction_id,
                    binding=binding,
                    differences=check.differences,
                )
                report = _apply_large_review(
                    prepared,
                    target=args.target,
                    lock_store=lock_store,
                    planner=planner,
                    flavors=flavors,
                    flavor_roots=flavor_roots,
                    review_store=review_store,
                    transaction_id=transaction_id,
                    binding=binding,
                    differences=check.differences,
                )
            else:
                raise ComponentLockReviewError(
                    "component_lock.review_operation_invalid",
                    "Unknown Component lock large-review operation",
                )
        return {
            **report,
            "component": str(selected_root),
            "target": args.target,
        }, 0
    except (
        ComponentLockPlanningError,
        ComponentLockResolutionError,
        ComponentLockStoreError,
        ComponentLockReviewError,
        ComponentResolutionAuditStoreError,
        StandardLifecycleBindingError,
    ) as exc:
        raise LockCommandError(exc.code, str(exc)) from exc


def _large_review_binding(
    prepared: _PreparedComponentLock,
    *,
    project_root: Path,
    project_definition_identity: str,
    target: str,
    flavors: tuple[str, ...],
    flavor_roots: tuple[Path, ...],
    current_identity: str | None,
    current_bytes: bytes,
) -> dict[str, object]:
    distribution = observe_installed_framework_distribution()
    return {
        "schema": "literate-ai/component-lock-review-binding@1",
        "project_definition_identity": project_definition_identity,
        "component": prepared.component_root.relative_to(project_root).as_posix(),
        "target": target,
        "flavors": list(flavors),
        "flavor_roots": [
            str(path.expanduser().resolve(strict=True)) for path in flavor_roots
        ],
        "current_lock_identity": current_identity,
        "current_lock_bytes_identity": (
            "sha256:" + hashlib.sha256(current_bytes).hexdigest()
        ),
        "proposed_lock_identity": prepared.result.lock.identity.uri,
        "resolution_plan_identity": prepared.plan.identity.uri,
        "catalog_audit_identity": prepared.result.catalog_audit.identity.uri,
        "diff_algorithm_identity": _LARGE_REVIEW_ALGORITHM_IDENTITY.uri,
        "review_policy_identity": _LARGE_REVIEW_POLICY_IDENTITY.uri,
        "framework_distribution_identity": distribution.identity.uri,
    }


def _apply_large_review(
    prepared: _PreparedComponentLock,
    *,
    target: str,
    lock_store: ComponentLockStore,
    planner: FilesystemComponentLockPlanner,
    flavors: tuple[str, ...],
    flavor_roots: tuple[Path, ...],
    review_store: ComponentLockReviewStore,
    transaction_id: str,
    binding: dict[str, object],
    differences,
) -> dict[str, object]:
    result = prepared.result
    audit_store = ComponentResolutionAuditStore(prepared.component_root, target)

    def require_source_inputs_unchanged() -> None:
        current = _prepare_component_lock(
            prepared.component_root,
            target=target,
            flavors=flavors,
            flavor_roots=flavor_roots,
            planner=planner,
        )
        if (
            current.plan.identity != prepared.plan.identity
            or current.result.lock.identity != result.lock.identity
            or current.result.catalog_audit.identity != result.catalog_audit.identity
        ):
            raise ComponentLockPlanningError(
                "component_lock.inputs_changed",
                "Component or catalog inputs changed during reviewed lock apply",
            )

    def require_review_inputs_unchanged() -> None:
        require_source_inputs_unchanged()
        content = lock_store.snapshot().content
        observed = (
            None if content is None else "sha256:" + hashlib.sha256(content).hexdigest()
        )
        if observed != binding["current_lock_bytes_identity"]:
            raise ComponentLockReviewError(
                "component_lock.review_transaction_stale",
                "Component lock changed after its large review was acknowledged",
            )

    lock_snapshot = lock_store.snapshot()
    audit_snapshot = audit_store.snapshot()
    try:
        require_review_inputs_unchanged()
        review_store.require_ready(
            transaction_id,
            binding=binding,
            differences=differences,
        )
        audit_updated = audit_store.update(
            result.catalog_audit,
            revalidate=require_review_inputs_unchanged,
        )
        lock_updated = lock_store.update(
            result.lock,
            revalidate=require_review_inputs_unchanged,
        )
        require_source_inputs_unchanged()
        final_checks = _artifact_checks(lock_store, audit_store, result)
        _require_checks_stable(final_checks, final_checks, require_current=True)
        review_store.complete(transaction_id)
    except Exception:
        rollback_errors: list[Exception] = []
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
                "Reviewed Component lock/audit publication failed and could not be "
                "rolled back",
            ) from rollback_errors[0]
        raise
    return {
        "schema": "literate-ai/component-lock-review-command@1",
        "operation": "apply",
        "transaction_identity": transaction_id,
        "lock_updated": lock_updated,
        "catalog_audit_updated": audit_updated,
        "transaction_removed": True,
        "component_lock_identity": result.lock.identity.uri,
        "catalog_audit_identity": result.catalog_audit.identity.uri,
    }


def _component_roots(selected: Path, *, allow_empty: bool = False) -> tuple[Path, ...]:
    resolved = selected.resolve(strict=True)
    if not allow_empty and (resolved / "component.md").is_file():
        return (resolved,)
    project = discover_project(resolved)
    if (
        project is None
        or resolved != project.root
        or not (resolved / PROJECT_FILENAME).is_file()
    ):
        raise LockCommandError(
            "component_lock.component_missing",
            "lock target must be a Component directory or canonical project root",
        )
    roots = tuple(
        sorted(
            manifest.parent
            for catalog in project.roots("component")
            for manifest in catalog.rglob("component.md")
            if manifest.is_file() and not manifest.is_symlink()
        )
    )
    if not roots and not allow_empty:
        raise LockCommandError(
            "component_lock.component_missing",
            "project has no readable component.md authoring documents",
        )
    return roots


__all__ = ["component_lock_from_args"]

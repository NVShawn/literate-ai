"""Repository-root commands compose independent pins and root Component authority."""

from __future__ import annotations

import os
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.repository_lock_planning import (
    prepare_repository_lock,
    require_repository_lock_inputs_unchanged,
)
from literate_ai.adapters.repository_locks import RepositoryLockStore
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
)

from .lock_command_errors import LockCommandError


@contextmanager
def _errors():
    try:
        yield
    except LockCommandError:
        raise
    except (OrchestrationInventoryError, ProjectError) as exc:
        raise LockCommandError(exc.code, exc.message) from exc
    except (OSError, TypeError, ValueError, UnsafeFilesystemPathError) as exc:
        raise LockCommandError(
            "orchestration.inputs_invalid",
            "repository command inputs are unavailable or invalid",
        ) from exc


def repository_root(selected: Path) -> Path | None:
    """Classify only the selected directory, never infer an ancestor or child scope."""
    root = Path(selected).absolute()
    manifest = root / PROJECT_FILENAME
    if not os.path.lexists(manifest):
        return None
    with _errors():
        snapshot = ProjectConfigurationStore(root).read()
        return (
            root if snapshot.definition.repository_orchestration is not None else None
        )


def selected_repository_root(args, selected: Path) -> Path | None:
    # Main pins classification before any optional host side effects. Direct adapter
    # callers still use the same classifier, without requiring a parser namespace.
    if hasattr(args, "_repository_root"):
        return args._repository_root
    return repository_root(selected)


def _options(args, roots: tuple[Path, ...]) -> None:
    if os.environ.get("LITAI_MATRIX_CELL_ROOT"):
        raise LockCommandError(
            "orchestration.component_options",
            "matrix-cell storage requires a Component invocation, "
            "not a repository root",
        )
    if any(
        getattr(args, name, None) is not None
        for name in (
            "model",
            "recipe_id",
            "large_review",
            "transaction_id",
            "page_identity",
        )
    ):
        raise LockCommandError(
            "orchestration.component_options",
            "model, recipe and large-review options require a Component invocation",
        )
    if not roots and (
        getattr(args, "target", "host") != "host"
        or getattr(args, "flavor", ())
        or getattr(args, "flavor_root", ())
    ):
        raise LockCommandError(
            "orchestration.component_options",
            "Flavor and target selectors require root-owned Components",
        )


def _components(args, root: Path, *, readonly: bool):
    from .component_lock_commands import _component_locks_from_args

    return _component_locks_from_args(
        Namespace(
            component=str(root),
            target=getattr(args, "target", "host"),
            flavor=getattr(args, "flavor", []),
            flavor_root=getattr(args, "flavor_root", []),
            check=readonly,
            diff=False,
        ),
        allow_empty=True,
    )


def _component_observations(report: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "component": Path(item["component"]).relative_to(root).as_posix(),
            "target": item["target"],
            "component_lock_identity": item["component_lock_identity"],
            "catalog_audit_identity": item["catalog_audit_identity"],
            "resolution_plan_identity": item["resolution_plan_identity"],
            "lock": {
                key: item["lock"][key]
                for key in ("state", "current_identity", "expected_identity")
            },
            "catalog_audit": {
                key: item["catalog_audit"][key]
                for key in ("state", "current_identity", "expected_identity")
            },
        }
        for item in report["components"]
    ]


def _prepared(args, root: Path):
    from .component_lock_commands import _component_roots

    # Reject provider/override options before any Component resolver can consult them.
    _options(args, (root,))
    prepared = prepare_repository_lock(root)
    roots = _component_roots(root, allow_empty=True)
    _options(args, roots)
    return prepared, RepositoryLockStore(root)


def repository_lock_from_args(args, root: Path) -> tuple[dict[str, Any], int]:
    with _errors():
        prepared, store = _prepared(args, root)
        readonly = bool(getattr(args, "check", False) or getattr(args, "diff", False))
        previous = store.read()
        components, status = _components(args, root, readonly=readonly)
        updated = False if readonly else store.update(prepared)
        check = store.check(prepared.lock)
        observed = _component_observations(components, root)
        final_components, final_status = _components(args, root, readonly=True)
        require_repository_lock_inputs_unchanged(prepared)
        if (
            observed != _component_observations(final_components, root)
            or status != final_status
            or store.check(prepared.lock) != check
        ):
            raise LockCommandError(
                "orchestration.inputs_changed",
                "repository or root Component locks changed during verification",
            )
        current = check["state"] == "current" and status == 0
        result = {
            "schema": "literate-ai/repository-lock-command@1",
            "mode": "diff"
            if getattr(args, "diff", False)
            else "check"
            if readonly
            else "update",
            "read_only": readonly,
            "current": current,
            "repository_lock": prepared.lock.to_dict(),
            "repository_lock_identity": prepared.lock.identity,
            "repository_lock_updated": updated,
            "lock": check,
            "components": observed,
            "inventory": prepared.inventory.to_dict(),
            "child_authority": "independent",
            "execution": False,
            "publication": "not-checked",
        }
        if getattr(args, "diff", False):
            result["previous_repository_lock"] = (
                None if previous is None else previous.to_dict()
            )
        return result, 0 if current else 1


def repository_plan_from_args(args, root: Path) -> dict[str, Any]:
    with _errors():
        prepared, store = _prepared(args, root)
        check = store.check(prepared.lock)
        components, status = _components(args, root, readonly=True)
        if check["state"] != "current" or status != 0:
            raise LockCommandError(
                "orchestration.lock_not_current",
                "repository planning requires current repository and root Component "
                "locks; run litai lock on this root",
            )
        observed = _component_observations(components, root)
        final_components, final_status = _components(args, root, readonly=True)
        require_repository_lock_inputs_unchanged(prepared)
        if (
            final_status != 0
            or observed != _component_observations(final_components, root)
            or store.check(prepared.lock) != check
        ):
            raise LockCommandError(
                "orchestration.inputs_changed",
                "repository planning inputs changed during verification",
            )
        plan = {
            key: value
            for key, value in prepared.to_plan().items()
            if key != "plan_identity"
        }
        plan.update(
            schema="literate-ai/repository-plan@1",
            components=observed,
            lock_state="current",
        )
        return {**plan, "plan_identity": canonical_identity(plan).uri}


def repository_lock_check(root: Path) -> dict[str, Any]:
    """Check root inputs and lock bytes without executing Components or children."""
    with _errors():
        _options(Namespace(), (root,))
        prepared = prepare_repository_lock(root)
        store = RepositoryLockStore(root)
        check = store.check(prepared.lock)
        require_repository_lock_inputs_unchanged(prepared)
        if store.check(prepared.lock) != check:
            raise LockCommandError(
                "orchestration.inputs_changed",
                "repository lock changed during verification",
            )
        return check

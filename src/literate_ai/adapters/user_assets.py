"""Platform-neutral resolution of durable user-owned asset paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .user_paths import UserPathError, UserPaths, resolve_user_paths

WORKER_CONFIG_ENVIRONMENT = "LITAI_WORKER_CONFIG"
WORKER_OBSERVATIONS_ENVIRONMENT = "LITAI_WORKER_OBSERVATIONS"
TEST_CONFIG_ENVIRONMENT = "LITAI_TEST_CONFIG"
SHARED_CACHE_CONFIG_ENVIRONMENT = "LITAI_SHARED_CACHE_CONFIG"
ACTION_EXECUTION_CONFIG_ENVIRONMENT = "LITAI_ACTION_EXECUTION_CONFIG"

LEGACY_WORKER_CONFIG = "literate.workers.json"
LEGACY_WORKER_OBSERVATIONS = "literate.worker-observations.json"
LEGACY_TEST_CONFIG = "literate.test.json"


class UserAssetPathError(ValueError):
    """A durable user asset path cannot be selected safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def resolve_shared_cache_config_path(
    *,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    paths: UserPaths | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    override = _override(explicit, configured, SHARED_CACHE_CONFIG_ENVIRONMENT)
    return (
        override
        if override is not None
        else Path(_paths(paths, configured).shared_cache_config)
    )


def resolve_worker_config_path(
    *,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    paths: UserPaths | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    override = _override(explicit, configured, WORKER_CONFIG_ENVIRONMENT)
    if override is not None:
        return override
    resolved = _paths(paths, configured)
    destination = Path(resolved.worker_config)
    _require_migrated(destination, _legacy(project_root, LEGACY_WORKER_CONFIG))
    return destination


def resolve_action_execution_config_path(
    *,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    paths: UserPaths | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    override = _override(explicit, configured, ACTION_EXECUTION_CONFIG_ENVIRONMENT)
    return (
        override
        if override is not None
        else Path(_paths(paths, configured).action_execution_config)
    )


def resolve_worker_observations_path(
    *,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    paths: UserPaths | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    override = _override(explicit, configured, WORKER_OBSERVATIONS_ENVIRONMENT)
    if override is not None:
        return override
    resolved = _paths(paths, configured)
    destination = Path(resolved.worker_observations)
    _require_migrated(destination, _legacy(project_root, LEGACY_WORKER_OBSERVATIONS))
    return destination


def resolve_test_config_path(
    *,
    project_root: Path | None = None,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    paths: UserPaths | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    override = _override(explicit, configured, TEST_CONFIG_ENVIRONMENT)
    if override is not None:
        return override
    start = Path.cwd() if project_root is None else Path(project_root)
    try:
        project = ProjectConfigurationStore.discover(start)
    except ProjectError as exc:
        raise UserAssetPathError(
            "user_assets.project_invalid",
            "the test configuration requires a valid canonical project",
        ) from exc
    if project is None:
        raise UserAssetPathError(
            "user_assets.project_required",
            "the test configuration requires a canonical project",
        )
    resolved = _paths(paths, configured)
    try:
        destination = Path(resolved.project_test_config(project.definition.project_id))
    except UserPathError as exc:
        raise UserAssetPathError(exc.code, exc.message) from exc
    _require_migrated(destination, project.root / LEGACY_TEST_CONFIG)
    return destination


def _override(
    explicit: str | Path | None,
    environment: Mapping[str, str],
    name: str,
) -> Path | None:
    value: str | Path | None = explicit
    if value is None:
        raw = str(environment.get(name, "")).strip()
        value = raw or None
    if value is None:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        raise UserAssetPathError(
            "user_assets.override_relative", f"{name} path must be absolute"
        )
    return candidate


def _paths(paths: UserPaths | None, environment: Mapping[str, str]) -> UserPaths:
    try:
        return paths or resolve_user_paths(environment=environment)
    except UserPathError as exc:
        raise UserAssetPathError(exc.code, exc.message) from exc


def _legacy(project_root: Path | None, name: str) -> Path:
    root = Path.cwd() if project_root is None else Path(project_root)
    try:
        return root.resolve(strict=True) / name
    except OSError:
        return root.absolute() / name


def _require_migrated(destination: Path, legacy: Path) -> None:
    if _lexists(destination) or not _lexists(legacy):
        return
    raise UserAssetPathError(
        "user_assets.migration_required",
        f"legacy private asset detected at {legacy}; "
        "run `litai config migrate --apply`",
    )


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


__all__ = [
    "ACTION_EXECUTION_CONFIG_ENVIRONMENT",
    "resolve_action_execution_config_path",
    "LEGACY_TEST_CONFIG",
    "LEGACY_WORKER_CONFIG",
    "LEGACY_WORKER_OBSERVATIONS",
    "TEST_CONFIG_ENVIRONMENT",
    "UserAssetPathError",
    "WORKER_CONFIG_ENVIRONMENT",
    "WORKER_OBSERVATIONS_ENVIRONMENT",
    "resolve_test_config_path",
    "resolve_worker_config_path",
    "resolve_worker_observations_path",
]

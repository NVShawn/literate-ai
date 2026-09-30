"""Explicit migration of private 0.8.x assets into user-owned custody."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from literate_ai.contracts.channel_events import ChannelEvent
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.user_mcp import UserMcpCatalog
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .test_matrix_config import GlobalTestMatrix
from .user_paths import (
    UserPathError,
    UserPaths,
    prepare_user_directory,
    resolve_user_home,
    resolve_user_paths,
)

MIGRATION_RESULT_SCHEMA = "literate-ai/user-config-migration-result@1"
MAX_MIGRATION_ASSET_BYTES = 1024 * 1024
LEGACY_WORKER_CONFIG = "literate.workers.json"
LEGACY_TEST_CONFIG = "literate.test.json"
LEGACY_WORKER_OBSERVATIONS = "literate.worker-observations.json"

_Validator = Callable[[bytes], None]


class UserConfigMigrationError(ValueError):
    """The migration could not resolve or safely manipulate its custody roots."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MigrationEntry:
    kind: str
    source: Path
    destination: Path
    status: str
    diagnostic: str = ""

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "source": str(self.source),
            "destination": str(self.destination),
            "status": self.status,
        }
        if self.diagnostic:
            result["diagnostic"] = self.diagnostic
        return result


@dataclass(frozen=True, slots=True)
class _PreparedEntry:
    entry: MigrationEntry
    content: bytes
    validator: _Validator = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class UserConfigMigrationPlan:
    project_root: Path
    config_root: Path
    state_root: Path
    entries: tuple[MigrationEntry, ...]
    _prepared: tuple[_PreparedEntry, ...] = field(repr=False, compare=False)

    @property
    def blocked(self) -> bool:
        return any(item.status in {"conflict", "invalid"} for item in self.entries)

    def to_dict(
        self, *, applied: bool, entries: tuple[MigrationEntry, ...] | None = None
    ) -> dict[str, object]:
        selected = self.entries if entries is None else entries
        return {
            "schema": MIGRATION_RESULT_SCHEMA,
            "project": str(self.project_root),
            "config_root": str(self.config_root),
            "state_root": str(self.state_root),
            "mode": "apply" if applied else "dry-run",
            "applied": applied,
            "ok": not any(
                item.status in {"conflict", "invalid", "failed"} for item in selected
            ),
            "detected": len(selected),
            "entries": [item.to_dict() for item in selected],
        }


def plan_user_config_migration(
    project_root: str | Path,
    *,
    paths: UserPaths | None = None,
    environment: Mapping[str, str] | None = None,
    legacy_home: Path | None = None,
) -> UserConfigMigrationPlan:
    """Discover and validate recognized legacy assets without writing anything."""

    root = _project_root(project_root)
    try:
        resolved_paths = paths or resolve_user_paths(environment=environment)
    except UserPathError as exc:
        raise UserConfigMigrationError(exc.code, exc.message) from exc
    config_root = Path(resolved_paths.config_root)
    state_root = Path(resolved_paths.state_root)
    home = _home(legacy_home, environment=environment)
    old_user_root = home / ".config" / "litai"

    candidates: list[tuple[str, Path, Path, _Validator]] = [
        (
            "worker-catalog",
            root / LEGACY_WORKER_CONFIG,
            Path(resolved_paths.worker_config),
            _validate_worker_catalog,
        ),
        (
            "worker-observations",
            root / LEGACY_WORKER_OBSERVATIONS,
            Path(resolved_paths.worker_observations),
            _validate_worker_observations,
        ),
        (
            "mcp-catalog",
            old_user_root / "mcps.json",
            Path(resolved_paths.mcp_catalog),
            _validate_mcp_catalog,
        ),
    ]
    legacy_test = root / LEGACY_TEST_CONFIG
    if _lexists(legacy_test):
        try:
            project_id = ProjectConfigurationStore(root).read().definition.project_id
            test_destination = Path(resolved_paths.project_test_config(project_id))
        except (ProjectError, UserPathError) as exc:
            raise UserConfigMigrationError(
                "user_config.migration_project_invalid",
                "the legacy test matrix requires a valid canonical project ID",
            ) from exc
        candidates.append(
            ("test-matrix", legacy_test, test_destination, _validate_test_matrix)
        )

    entries: list[MigrationEntry] = []
    prepared: list[_PreparedEntry] = []
    for kind, source, destination, validator in candidates:
        selected = _prepare_file(kind, source, destination, validator)
        if selected is not None:
            entries.append(selected.entry)
            prepared.append(selected)
    event_entries, event_prepared = _prepare_events(
        old_user_root / "events",
        Path(resolved_paths.channel_events),
    )
    entries.extend(event_entries)
    prepared.extend(event_prepared)
    return UserConfigMigrationPlan(
        root,
        config_root,
        state_root,
        tuple(entries),
        tuple(prepared),
    )


def apply_user_config_migration(
    plan: UserConfigMigrationPlan,
) -> tuple[dict[str, object], int]:
    """Apply a fully preflighted plan; never overwrite a differing destination."""

    if not isinstance(plan, UserConfigMigrationPlan):
        raise TypeError("user config migration requires a migration plan")
    if plan.blocked:
        return plan.to_dict(applied=False), 1

    results: list[MigrationEntry] = []
    by_source = {item.entry.source: item for item in plan._prepared}
    failed = False
    for entry in plan.entries:
        prepared = by_source[entry.source]
        if failed:
            results.append(entry)
            continue
        try:
            status = _apply_prepared(
                prepared,
                config_root=plan.config_root,
                state_root=plan.state_root,
            )
            results.append(replace(entry, status=status, diagnostic=""))
        except UserConfigMigrationError as exc:
            failed = True
            results.append(replace(entry, status="failed", diagnostic=exc.message))
    _remove_empty_legacy_event_directories(plan, results)
    selected = tuple(results)
    return plan.to_dict(applied=True, entries=selected), 1 if failed else 0


def _prepare_events(
    source: Path, destination: Path
) -> tuple[list[MigrationEntry], list[_PreparedEntry]]:
    if not _lexists(source):
        return [], []
    if source.is_symlink() or not source.is_dir():
        return [
            MigrationEntry(
                "channel-events",
                source,
                destination,
                "invalid",
                "legacy event path must be a real directory",
            )
        ], []
    entries: list[MigrationEntry] = []
    prepared: list[_PreparedEntry] = []
    try:
        children = tuple(sorted(source.iterdir(), key=lambda item: item.name))
    except OSError:
        return [
            MigrationEntry(
                "channel-events",
                source,
                destination,
                "invalid",
                "legacy event directory cannot be read",
            )
        ], []
    for child in children:
        selected = _prepare_file(
            "channel-event", child, destination / child.name, _validate_channel_event
        )
        if selected is not None:
            entries.append(selected.entry)
            prepared.append(selected)
    return entries, prepared


def _prepare_file(
    kind: str,
    source: Path,
    destination: Path,
    validator: _Validator,
) -> _PreparedEntry | None:
    if not _lexists(source):
        return None
    try:
        content = _read_regular_file(source)
        validator(content)
    except UserConfigMigrationError as exc:
        return _PreparedEntry(
            MigrationEntry(kind, source, destination, "invalid", exc.message),
            b"",
            validator,
        )
    status = "ready"
    diagnostic = ""
    if _lexists(destination):
        try:
            destination_content = _read_regular_file(destination)
        except UserConfigMigrationError as exc:
            status = "conflict"
            diagnostic = exc.message
        else:
            if destination_content == content:
                status = "identical"
            else:
                status = "conflict"
                diagnostic = "destination already contains different bytes"
    return _PreparedEntry(
        MigrationEntry(kind, source, destination, status, diagnostic),
        content,
        validator,
    )


def _apply_prepared(
    prepared: _PreparedEntry,
    *,
    config_root: Path,
    state_root: Path,
) -> str:
    entry = prepared.entry
    try:
        current = _read_regular_file(entry.source)
        prepared.validator(current)
    except UserConfigMigrationError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_source_changed",
            "source changed or became invalid after migration preflight",
        ) from exc
    if current != prepared.content:
        raise UserConfigMigrationError(
            "user_config.migration_source_changed",
            "source changed after migration preflight",
        )
    boundary = _custody_boundary(entry.destination, config_root, state_root)
    try:
        prepare_user_directory(boundary, entry.destination.parent)
    except UserPathError as exc:
        raise UserConfigMigrationError(exc.code, exc.message) from exc

    if _lexists(entry.destination):
        destination = _read_regular_file(entry.destination)
        if destination != current:
            raise UserConfigMigrationError(
                "user_config.migration_conflict",
                "destination appeared with different bytes during migration",
            )
        _make_private_file(entry.destination)
        _remove_source(entry.source)
        return "removed-identical"

    descriptor = -1
    staging: Path | None = None
    try:
        descriptor, raw_staging = tempfile.mkstemp(
            prefix=f".{entry.destination.name}.",
            suffix=".tmp",
            dir=entry.destination.parent,
        )
        staging = Path(raw_staging)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
        _make_private_file(staging)
        prepared.validator(staging.read_bytes())
        try:
            os.link(staging, entry.destination, follow_symlinks=False)
        except FileExistsError:
            destination = _read_regular_file(entry.destination)
            if destination != current:
                raise UserConfigMigrationError(
                    "user_config.migration_conflict",
                    "destination appeared with different bytes during migration",
                ) from None
        _fsync_directory(entry.destination.parent)
    except UserConfigMigrationError:
        raise
    except OSError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_write_failed",
            "destination could not be published durably",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
    _remove_source(entry.source)
    return "moved"


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise UserConfigMigrationError(
                "user_config.migration_asset_unsafe",
                "asset must be a regular non-symlink file",
            )
        if metadata.st_size > MAX_MIGRATION_ASSET_BYTES:
            raise UserConfigMigrationError(
                "user_config.migration_asset_too_large",
                "asset exceeds the one MiB migration limit",
            )
        content = path.read_bytes()
    except UserConfigMigrationError:
        raise
    except OSError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_asset_unavailable", "asset cannot be read"
        ) from exc
    if len(content) > MAX_MIGRATION_ASSET_BYTES:
        raise UserConfigMigrationError(
            "user_config.migration_asset_too_large",
            "asset exceeds the one MiB migration limit",
        )
    return content


def _decode_json(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UserConfigMigrationError(
            "user_config.migration_asset_invalid", "asset must be UTF-8 JSON"
        ) from exc


def _validate_worker_catalog(content: bytes) -> None:
    _validate_contract(ExecutionWorkerCatalog.from_dict, content, "worker catalog")


def _validate_test_matrix(content: bytes) -> None:
    _validate_contract(GlobalTestMatrix.from_dict, content, "test matrix")


def _validate_worker_observations(content: bytes) -> None:
    _validate_contract(
        WorkerHardwareObservationCatalog.from_dict, content, "worker observations"
    )


def _validate_mcp_catalog(content: bytes) -> None:
    _validate_contract(UserMcpCatalog.from_dict, content, "MCP catalog")


def _validate_channel_event(content: bytes) -> None:
    _validate_contract(ChannelEvent.from_dict, content, "channel event")


def _validate_contract(
    parser: Callable[[Any], object], content: bytes, label: str
) -> None:
    try:
        parser(_decode_json(content))
    except UserConfigMigrationError:
        raise
    except (TypeError, ValueError) as exc:
        raise UserConfigMigrationError(
            "user_config.migration_asset_invalid",
            f"{label} does not satisfy its schema",
        ) from exc


def _custody_boundary(destination: Path, config_root: Path, state_root: Path) -> Path:
    for root in (config_root, state_root):
        try:
            destination.relative_to(root)
            return root
        except ValueError:
            continue
    raise UserConfigMigrationError(
        "user_config.migration_destination_unsafe",
        "destination is outside resolved user custody",
    )


def _make_private_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_permission_failed",
            "destination file permissions could not be restricted",
        ) from exc


def _remove_source(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_source_remove_failed",
            "destination is durable but the legacy source could not be removed",
        ) from exc


def _remove_empty_legacy_event_directories(
    plan: UserConfigMigrationPlan, entries: list[MigrationEntry]
) -> None:
    event_sources = [
        item.source.parent for item in entries if item.kind == "channel-event"
    ]
    for directory in sorted(
        set(event_sources), key=lambda item: len(item.parts), reverse=True
    ):
        try:
            directory.rmdir()
            directory.parent.rmdir()
        except OSError:
            continue


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _project_root(value: str | Path) -> Path:
    configured = Path(value).expanduser()
    try:
        root = configured.resolve(strict=True)
    except OSError as exc:
        raise UserConfigMigrationError(
            "user_config.migration_project_unavailable",
            "migration project root is unavailable",
        ) from exc
    if configured.is_symlink() or not root.is_dir():
        raise UserConfigMigrationError(
            "user_config.migration_project_unsafe",
            "migration project root must be a real directory",
        )
    return root


def _home(value: Path | None, *, environment: Mapping[str, str] | None = None) -> Path:
    try:
        home = Path(resolve_user_home(environment=environment, home=value))
    except UserPathError as exc:
        if exc.code == "user_paths.home_relative":
            raise UserConfigMigrationError(
                "user_config.migration_home_invalid",
                "legacy user home must be absolute",
            ) from exc
        raise UserConfigMigrationError(
            "user_config.migration_home_unavailable",
            "legacy user home is unavailable",
        ) from exc
    if not home.is_absolute():
        raise UserConfigMigrationError(
            "user_config.migration_home_invalid",
            "legacy user home must be absolute",
        )
    return home


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


__all__ = [
    "LEGACY_TEST_CONFIG",
    "LEGACY_WORKER_CONFIG",
    "LEGACY_WORKER_OBSERVATIONS",
    "MIGRATION_RESULT_SCHEMA",
    "MigrationEntry",
    "UserConfigMigrationError",
    "UserConfigMigrationPlan",
    "apply_user_config_migration",
    "plan_user_config_migration",
]

"""Operator-local MCP catalog path, first-launch offer, and event journal."""

from __future__ import annotations

import json
import os
import subprocess
import time
from getpass import getuser
from pathlib import Path
from typing import TextIO

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.channel_events import (
    ChannelEvent,
    ChannelKind,
    ChannelRole,
    is_mutagenic_command,
)
from literate_ai.contracts.user_mcp import (
    EMPTY_USER_MCP_CATALOG,
    UserMcpCatalog,
    UserMcpServer,
)
from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .user_paths import UserPathError, prepare_user_directory, resolve_user_paths

CATALOG_FILENAME = "mcps.json"


class UserConfigError(ValueError):
    """Operator-local config could not be resolved or admitted."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def user_config_dir() -> Path:
    try:
        return Path(resolve_user_paths().config_root)
    except UserPathError as exc:
        raise UserConfigError(
            exc.code.replace("user_paths.", "user_config.", 1), exc.message
        ) from exc


def catalog_path(root: Path | None = None) -> Path:
    return (root or user_config_dir()) / CATALOG_FILENAME


def catalog_setup_needed(root: Path) -> bool:
    if not root.exists():
        return True
    if root.is_symlink() or not root.is_dir():
        raise UserConfigError(
            "user_config.dir_invalid",
            f"user config path must be a real directory: {root}",
        )
    source = catalog_path(root)
    if source.is_symlink():
        raise UserConfigError(
            "user_config.mcp_catalog_invalid",
            f"user MCP catalog must not be a symbolic link: {source}",
        )
    return not source.exists()


def _interactive(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def load_user_mcp_catalog(root: Path | None = None) -> UserMcpCatalog:
    directory = root or user_config_dir()
    source = catalog_path(directory)
    if catalog_setup_needed(directory):
        return EMPTY_USER_MCP_CATALOG
    if source.is_symlink() or not source.is_file():
        raise UserConfigError(
            "user_config.mcp_catalog_missing",
            f"user MCP catalog is not a regular file: {source}",
        )
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UserConfigError(
            "user_config.mcp_catalog_invalid",
            f"could not read user MCP catalog: {source}",
        ) from exc
    try:
        return UserMcpCatalog.from_dict(document)
    except ContractValidationError as exc:
        raise UserConfigError(
            "user_config.mcp_catalog_invalid",
            str(exc),
        ) from exc


def write_user_mcp_catalog(catalog: UserMcpCatalog, root: Path | None = None) -> Path:
    directory = root or user_config_dir()
    try:
        prepare_user_directory(directory, directory)
    except UserPathError as exc:
        raise UserConfigError(
            exc.code.replace("user_paths.", "user_config.", 1), exc.message
        ) from exc
    target = catalog_path(directory)
    staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    payload = json.dumps(catalog.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        staging.write_text(payload, encoding="utf-8", newline="\n")
        if os.name != "nt":
            os.chmod(staging, 0o600)
        os.replace(staging, target)
    except OSError as exc:
        if staging.exists():
            staging.unlink()
        raise UserConfigError(
            "user_config.mcp_catalog_write_failed",
            f"could not write user MCP catalog: {target}",
        ) from exc
    return target


def ensure_user_mcp_catalog(
    *,
    json_mode: bool,
    stdin: TextIO,
    stderr: TextIO,
    root: Path | None = None,
) -> UserMcpCatalog:
    directory = root or user_config_dir()
    if not catalog_setup_needed(directory):
        return load_user_mcp_catalog(directory)
    if json_mode or not _interactive(stdin) or not _interactive(stderr):
        return EMPTY_USER_MCP_CATALOG
    stderr.write(
        f"No Literate AI user MCP catalog at {directory}.\n"
        "This directory lists MCP servers this user wants litai to use "
        "(Jira, Slack, Outlook). It is operator-local, not project authority.\n"
        "litai posts mutagenic events through listed command hints. "
        "A coding session can discover connected MCPs via "
        "skills/agent/configure-operator-mcp.\n"
        "Set it up now with ids only? [y/N] "
    )
    stderr.flush()
    try:
        answer = stdin.readline()
    except OSError:
        return EMPTY_USER_MCP_CATALOG
    if answer.strip().casefold() not in {"y", "yes"}:
        write_user_mcp_catalog(EMPTY_USER_MCP_CATALOG, directory)
        stderr.write(f"Wrote empty catalog {catalog_path(directory)}.\n")
        return EMPTY_USER_MCP_CATALOG
    stderr.write("MCP ids (comma-separated, blank for none): ")
    stderr.flush()
    try:
        line = stdin.readline()
    except OSError:
        line = ""
    servers = []
    for raw in line.split(","):
        mcp_id = raw.strip().casefold()
        if not mcp_id:
            continue
        try:
            servers.append(UserMcpServer.from_dict({"id": mcp_id}))
        except ContractValidationError as exc:
            raise UserConfigError(
                "user_config.mcp_catalog_invalid",
                str(exc),
            ) from exc
    catalog = UserMcpCatalog(mcps=tuple(servers))
    write_user_mcp_catalog(catalog, directory)
    stderr.write(f"Wrote {catalog_path(directory)}.\n")
    return catalog


def _project_id_from_cwd() -> str:
    try:
        return ProjectConfigurationStore(Path.cwd()).read().definition.project_id
    except ProjectError:
        return ""


def _revision_from_cwd() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _operator_name() -> str:
    try:
        return getuser()
    except OSError:
        return "operator"


def journal_mutagenic_event(
    command: str,
    *,
    outcome: str,
    lock_check: bool = False,
    root: Path | None = None,
    stderr: TextIO | None = None,
) -> Path | None:
    if not is_mutagenic_command(command, lock_check=lock_check):
        return None
    directory = root or user_config_dir()
    if not catalog_path(directory).is_file():
        return None
    events = (
        directory / "events"
        if root is not None
        else Path(resolve_user_paths().channel_events)
    )
    event = ChannelEvent(
        role=ChannelRole.AUTHOR,
        kind=(
            ChannelKind.PUBLISH_SUMMARY
            if command == "release.publish"
            else ChannelKind.MUTAGENIC_CLI
        ),
        project_id=_project_id_from_cwd(),
        command=command,
        revision=_revision_from_cwd(),
        outcome=outcome,
        user=_operator_name(),
    )
    name = f"{time.time_ns()}-{os.getpid()}.json"
    target = events / name
    staging: Path | None = None
    try:
        custody_root = (
            directory if root is not None else Path(resolve_user_paths().state_root)
        )
        prepare_user_directory(custody_root, events)
        staging = target.with_name(f".{target.name}.tmp")
        with staging.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event.to_dict(), indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(staging, 0o600)
        os.replace(staging, target)
        if os.name != "nt":
            descriptor = os.open(events, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, UserPathError) as exc:
        try:
            if staging is not None:
                staging.unlink(missing_ok=True)
        except OSError:
            pass
        if stderr is not None:
            stderr.write(
                f"Literate AI skipped channel journal ({exc.__class__.__name__}): "
                "institutional notify was not recorded.\n"
            )
        return None
    return target

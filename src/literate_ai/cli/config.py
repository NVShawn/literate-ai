"""User configuration CLI boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from literate_ai.adapters.user_config import (
    UserConfigError,
    load_user_mcp_catalog,
    write_user_mcp_catalog,
)
from literate_ai.adapters.user_config_migration import (
    UserConfigMigrationError,
    apply_user_config_migration,
    plan_user_config_migration,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_user_paths
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.channel_events import ChannelEvent, ChannelKind, ChannelRole
from literate_ai.contracts.user_mcp import UserMcpCatalog, UserMcpServer
from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .errors import CliFailure


def config_from_args(args: Any) -> tuple[dict[str, object], int]:
    if args.config_command == "paths":
        return _paths_from_args(args), 0
    if args.config_command == "mcp":
        return _mcp_from_args(args), 0
    if args.config_command == "channel-parse":
        return _channel_from_args(args), 0
    if args.config_command != "migrate":
        raise CliFailure("cli.usage", "a config command is required")
    try:
        plan = plan_user_config_migration(args.project)
        if args.apply:
            return apply_user_config_migration(plan)
        return plan.to_dict(applied=False), 0
    except UserConfigMigrationError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _paths_from_args(args: Any) -> dict[str, object]:
    try:
        project = ProjectConfigurationStore.discover(Path(args.project))
        if project is None:
            raise CliFailure(
                "user_assets.project_required",
                "user configuration paths require a canonical project",
            )
        paths = resolve_user_paths()
        return {
            "schema": "literate-ai/user-config-paths@1",
            "project": str(project.root),
            "project_id": project.definition.project_id,
            "config_root": str(paths.config_root),
            "state_root": str(paths.state_root),
            "worker_config": str(paths.worker_config),
            "worker_provisioner": str(paths.worker_provisioner),
            "worker_provisioning": str(paths.worker_provisioning),
            "shared_cache_config": str(paths.shared_cache_config),
            "test_config": str(
                paths.project_test_config(project.definition.project_id)
            ),
            "mcp_catalog": str(paths.mcp_catalog),
            "worker_observations": str(paths.worker_observations),
            "channel_events": str(paths.channel_events),
        }
    except (ProjectError, UserPathError) as exc:
        raise CliFailure(
            getattr(exc, "code", "user_assets.path_unavailable"),
            getattr(exc, "message", str(exc)),
        ) from exc


def _mcp_from_args(args: Any) -> dict[str, object]:
    try:
        if args.mcp_command == "show":
            catalog = load_user_mcp_catalog()
            return {
                "schema": "literate-ai/operator-mcp-result@1",
                "operation": "show",
                "catalog": catalog.to_dict(),
            }
        servers = []
        for configured in args.server:
            mcp_id, separator, uses = configured.partition(":")
            document: dict[str, object] = {"id": mcp_id}
            if separator:
                document["uses"] = uses
            servers.append(UserMcpServer.from_dict(document))
        catalog = UserMcpCatalog(mcps=tuple(servers))
        write_user_mcp_catalog(catalog)
        return {
            "schema": "literate-ai/operator-mcp-result@1",
            "operation": "write",
            "catalog": catalog.to_dict(),
        }
    except (ContractValidationError, UserConfigError) as exc:
        raise CliFailure(
            getattr(exc, "code", "user_config.mcp_catalog_invalid"), str(exc)
        ) from exc


def _channel_from_args(args: Any) -> dict[str, object]:
    try:
        project = ProjectConfigurationStore.discover(Path(args.project))
        if project is None:
            raise CliFailure("project.not_found", "no Literate AI project was found")
        text = Path(args.message).read_text(encoding="utf-8")
        event = ChannelEvent.parse_trailer(text)
        if event.role is not ChannelRole.RECIPIENT:
            raise CliFailure(
                "channel_event.role_invalid", "inbound work must name recipient role"
            )
        if event.kind is not ChannelKind.INBOUND_TASK:
            raise CliFailure(
                "channel_event.kind_invalid", "inbound work must use inbound-task kind"
            )
        if event.project_id != project.definition.project_id:
            raise CliFailure(
                "channel_event.project_mismatch",
                "inbound work names another project",
            )
        first_line = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("literate-ai-event:1")
            ),
            "",
        )
        return {
            "schema": "literate-ai/channel-ingest-candidate@1",
            "project_id": event.project_id,
            "summary": first_line[:512],
            "event": event.to_dict(),
        }
    except (OSError, UnicodeError, ContractValidationError) as exc:
        raise CliFailure("channel_event.invalid", str(exc)) from exc


__all__ = ["config_from_args"]

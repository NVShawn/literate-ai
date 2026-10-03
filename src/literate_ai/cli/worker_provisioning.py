"""Explicit opt-in provisioning through an organization-owned local command."""

from __future__ import annotations

import os
from pathlib import Path

from literate_ai._cache_lock import CacheLockError
from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_user_paths
from literate_ai.adapters.worker_provisioning import (
    configure,
    discover_help,
    operation_path,
    provision,
    read_private,
    read_record,
    recover,
    remove_configuration,
)
from literate_ai.contracts import ContractValidationError, ExecutionRequirements
from literate_ai.contracts.worker_provisioning import WorkerProvisioner

from .errors import CliFailure


def add_provisioning_arguments(commands):
    settings = commands.add_parser(
        "provisioner", help="manage the optional local dynamic worker command"
    )
    actions = settings.add_subparsers(dest="provisioner_command", required=True)
    for name in (
        "show",
        "configure",
        "enable",
        "disable",
        "remove",
        "command-help",
        "status",
        "recover",
    ):
        parser = actions.add_parser(name)
        if name == "configure":
            parser.add_argument(
                "--file",
                required=True,
                help="versioned local configuration; starts disabled",
            )
        if name in ("status", "recover"):
            parser.add_argument("worker_id")
    launch = commands.add_parser(
        "provision", help="explicitly request and register a dynamic SSH worker"
    )
    launch.add_argument("worker_id")
    launch.add_argument(
        "--request-id", required=True, help="stable idempotency key for this allocation"
    )
    launch.add_argument("--worker-config")
    launch.add_argument(
        "--requirements", help="versioned execution requirements JSON file"
    )
    launch.add_argument("--target-profile", default="host")
    launch.add_argument(
        "--parameter",
        action="append",
        default=[],
        help="organization-specific KEY=VALUE; never credentials",
    )


def provisioning_from_args(args):
    try:
        paths = resolve_user_paths()
        config_path = Path(paths.worker_provisioner)
        state = Path(paths.worker_provisioning)
        command = args.worker_command
        operation = "provision" if command == "provision" else args.provisioner_command
        result = {
            "schema": "literate-ai/worker-provisioning-result@1",
            "operation": operation,
        }
        if operation == "show":
            result["configuration"] = (
                WorkerProvisioner.from_dict(read_private(config_path)).to_dict()
                if config_path.exists()
                else None
            )
            result["path"] = str(config_path)
        elif operation == "configure":
            config = WorkerProvisioner.from_dict(read_private(Path(args.file)))
            result["configuration"] = configure(
                config_path, config, enabled=False
            ).to_dict()
        elif operation in ("enable", "disable"):
            result["configuration"] = configure(
                config_path, enabled=operation == "enable"
            ).to_dict()
        elif operation == "remove":
            remove_configuration(config_path)
            result["removed"] = True
        elif operation == "command-help":
            result["help"] = discover_help(config_path, os.environ)
        elif operation in ("status", "recover"):
            result["request"] = (
                read_record(operation_path(state, args.worker_id))
                if operation == "status"
                else recover(state, args.worker_id)
            )
        else:
            requirements = (
                ExecutionRequirements.from_dict(read_private(Path(args.requirements)))
                if args.requirements
                else ExecutionRequirements()
            )
            parameters = {}
            for pair in args.parameter:
                key, separator, value = pair.partition("=")
                if not separator or not key or key in parameters:
                    raise CliFailure(
                        "cli.usage", "--parameter requires unique KEY=VALUE pairs."
                    )
                parameters[key] = value
            result["request"] = provision(
                config_path,
                state,
                resolve_worker_config_path(explicit=args.worker_config),
                args.worker_id,
                args.request_id,
                requirements,
                args.target_profile,
                parameters,
                os.environ,
            )
        return result, 0
    except (ExecutionDispatchAdapterError, UserPathError, UserAssetPathError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except (
        ContractValidationError,
        OSError,
        UnicodeError,
        ValueError,
        CacheLockError,
    ) as exc:
        if isinstance(exc, CliFailure):
            raise
        # Provider output may contain credentials; never render untrusted exceptions.
        raise CliFailure(
            "worker.provisioner_invalid",
            "Provisioner configuration or response is invalid; check "
            "the versioned contract and local request status.",
        ) from exc

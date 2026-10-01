"""CLI operations on private registrations and connectivity, not VM lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

from literate_ai._cache_lock import CacheLockError
from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
)
from literate_ai.adapters.worker_connectivity import test_worker_connections
from literate_ai.adapters.worker_registry import change_registry, read_registry
from literate_ai.contracts import (
    ContractValidationError,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.diagnostics import redact_secrets

from .errors import CliFailure


def add_registry_arguments(commands):
    for name in ("list", "show", "add", "update", "remove", "test"):
        parser = commands.add_parser(
            name,
            help={
                "list": "list private worker registrations",
                "show": "show one private worker registration",
                "add": "register a worker without provisioning a VM",
                "update": "update a private worker registration",
                "remove": "remove a registration without deleting its VM",
                "test": "test SSH connectivity independently for each selected worker",
            }[name],
        )
        parser.add_argument(
            "--worker-config", help="private worker catalog (or LITAI_WORKER_CONFIG)"
        )
        if name in ("show", "add", "update", "remove"):
            parser.add_argument("worker_id")
        if name in ("add", "update", "remove"):
            parser.add_argument(
                "--if-identity", help="require this exact current catalog identity"
            )
        if name in ("add", "update"):
            parser.add_argument(
                "--file",
                help="complete versioned descriptor; replaces the selected entry",
            )
            parser.add_argument("--kind", choices=("local", "ssh", "command"))
            parser.add_argument("--endpoint")
            parser.add_argument("--workspace")
            parser.add_argument("--os", choices=("linux", "windows", "macos"))
            parser.add_argument("--os-version")
            parser.add_argument(
                "--cpu-architecture", choices=("x86_64", "aarch64", "arm64")
            )
            parser.add_argument("--target-profile")
            parser.add_argument("--transport")
            parser.add_argument("--slots", type=int)
            parser.add_argument("--lifecycle-executable")
        if name == "test":
            selection = parser.add_mutually_exclusive_group(required=True)
            selection.add_argument("--worker-id", action="append")
            selection.add_argument("--all", action="store_true")
            parser.add_argument(
                "--timeout-seconds", type=int, choices=range(1, 301), default=10
            )


def _worker(args, previous=None):
    flags = (
        "kind",
        "endpoint",
        "workspace",
        "os",
        "os_version",
        "cpu_architecture",
        "target_profile",
        "transport",
        "slots",
        "lifecycle_executable",
    )
    if args.file:
        if any(getattr(args, key) is not None for key in flags):
            raise CliFailure(
                "cli.usage", "--file cannot be combined with worker field flags."
            )
        path = Path(args.file).expanduser()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise CliFailure(
                "worker.descriptor_invalid",
                "Descriptor must be a regular non-symlink file of at most one MiB.",
            )
        worker = ExecutionWorker.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if worker.worker_id != args.worker_id:
            raise CliFailure(
                "worker.id_mismatch",
                "Descriptor worker_id must match the selected worker.",
            )
        return worker
    if previous is None:
        kind = ExecutionWorkerKind(args.kind or "ssh")
        value = {
            "schema": ExecutionWorker.SCHEMA,
            "worker_id": args.worker_id,
            "kind": kind.value,
            "target_profile": "host",
            "requirements": ExecutionRequirements().to_dict(),
            "parameters": [],
            "endpoint": None,
            "workspace": None,
            "command": [],
            "environment": [],
        }
    else:
        value = previous.to_dict()
    for key in flags:
        val = getattr(args, key)
        if val is None:
            continue
        if key in ("os", "os_version", "cpu_architecture"):
            value["requirements"]["os_family" if key == "os" else key] = val
        else:
            value[key] = val
    return ExecutionWorker.from_dict(value)


def registry_from_args(args):
    try:
        path = resolve_worker_config_path(explicit=args.worker_config)
        command = args.worker_command
        if command in ("list", "show", "test"):
            catalog = read_registry(path)
            if command == "test":
                results = test_worker_connections(
                    catalog,
                    worker_ids=None if args.all else args.worker_id,
                    timeout_seconds=args.timeout_seconds,
                )
                return {
                    "schema": "literate-ai/worker-connectivity-result@1",
                    "worker_catalog_identity": catalog.identity.uri,
                    "workers": results,
                    "ok": all(row["status"] == "passed" for row in results),
                }, int(any(row["status"] != "passed" for row in results))
            selected = (
                catalog.workers
                if command == "list"
                else (catalog.worker(args.worker_id),)
            )
            return {
                "schema": "literate-ai/worker-registry-result@1",
                "operation": command,
                "path": str(path),
                "catalog_identity": catalog.identity.uri,
                "workers": [worker.to_dict() for worker in selected],
            }, 0

        def transform(catalog):
            entries = {worker.worker_id: worker for worker in catalog.workers}
            exists = args.worker_id in entries
            if command == "add" and exists:
                raise CliFailure(
                    "worker.already_registered", "Worker already exists; use update."
                )
            if command != "add" and not exists:
                raise CliFailure(
                    "worker.not_found", "Worker is not registered; use list."
                )
            if command == "remove":
                del entries[args.worker_id]
            else:
                entries[args.worker_id] = _worker(args, entries.get(args.worker_id))
            return ExecutionWorkerCatalog(
                tuple(entries[key] for key in sorted(entries))
            )

        catalog, changed = change_registry(
            path, transform, expected_identity=args.if_identity
        )
        return {
            "schema": "literate-ai/worker-registry-result@1",
            "operation": command,
            "path": str(path),
            "catalog_identity": catalog.identity.uri,
            "worker_id": args.worker_id,
            "changed": changed,
            "count": len(catalog.workers),
        }, 0
    except (ExecutionDispatchAdapterError, UserAssetPathError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except ContractValidationError as exc:
        raise CliFailure(
            "worker.declaration_invalid", redact_secrets(f"{exc.path}: {exc.message}")
        ) from exc
    except json.JSONDecodeError as exc:
        raise CliFailure(
            "worker.descriptor_invalid",
            f"Descriptor JSON is invalid at line {exc.lineno}, column {exc.colno}.",
        ) from exc
    except (OSError, UnicodeError, ValueError, CacheLockError) as exc:
        if isinstance(exc, CliFailure):
            raise
        raise CliFailure(
            "worker.registry_failed",
            "Worker configuration could not be read or updated; "
            "check JSON, path permissions and concurrent writers.",
        ) from exc


def human_registry_result(result):
    rows = result.get("workers")
    if rows is None:
        return (
            f"{result['operation']}: {result['worker_id']} "
            f"({'changed' if result['changed'] else 'unchanged'}); "
            f"{result['count']} registrations\n"
        )
    lines = []
    if result["schema"] == "literate-ai/worker-connectivity-result@1":
        for row in rows:
            lines.append(f"{row['worker_id']}: {row['status']}")
            if row["cause"]:
                lines.append(f"  {row['cause']}: {row['diagnostic']}")
                lines.append(f"  {row['remedy']}")
    else:
        lines.append("WORKER  KIND  OS  ENDPOINT  SLOTS")
        for row in rows:
            lines.append(
                f"{row['worker_id']}  {row['kind']}  "
                f"{row['requirements']['os_family'] or '-'}  "
                f"{row['endpoint'] or '-'}  {row.get('slots', 1)}"
            )
    return "\n".join(lines or ["No workers selected."]) + "\n"

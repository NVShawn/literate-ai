"""Opt-in local command execution with durable protection against repeat allocation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from literate_ai._cache_lock import exclusive_cache_lock
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts._validation import contract_fields, string_value
from literate_ai.contracts.execution_dispatch import _identifier
from literate_ai.contracts.worker_provisioning import WorkerProvisioner
from literate_ai.diagnostics import redact_secrets

from .builders._process import run_bounded_process
from .builders.python import BuildError
from .execution_dispatch import ExecutionDispatchAdapterError
from .worker_registry import change_registry, read_registry

REQUEST_SCHEMA = "urn:literate-ai:schema:v1:worker-provision-request"
RESPONSE_SCHEMA = "urn:literate-ai:schema:v1:worker-provision-response"
LIMIT = 64 * 1024


def refuse(code, message):
    raise ExecutionDispatchAdapterError("worker.provisioner_" + code, message)


def read_private(path, *, limit=LIMIT):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        refuse("invalid_file", "Expected a bounded regular private JSON file.")
    return json.loads(path.read_text(encoding="utf-8"))


def write_private(path, value, *, limit=LIMIT):
    payload = canonical_json_bytes(value) + b"\n"
    if len(payload) > limit:
        refuse("too_large", "Private provisioning record exceeds its size limit.")
    if path.is_symlink():
        refuse("invalid_file", "Private provisioning files must not be symlinks.")
    fd, temporary = tempfile.mkstemp(prefix=".provision-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_configuration(path):
    with exclusive_cache_lock(path.with_suffix(".lock")):
        if path.is_symlink():
            refuse("invalid_file", "Configuration must not be a symlink.")
        path.unlink(missing_ok=True)


def configure(path, config=None, *, enabled=None):
    with exclusive_cache_lock(path.with_suffix(".lock")):
        if config is None:
            config = WorkerProvisioner.from_dict(read_private(path))
        if enabled is not None:
            config = replace(config, enabled=enabled)
        write_private(path, config.to_dict())
    return config


def admitted(path, environment):
    if not path.exists():
        refuse(
            "not_configured",
            "Configure a local provisioner before enabling dynamic workers.",
        )
    config = WorkerProvisioner.from_dict(read_private(path))
    if not config.enabled:
        refuse(
            "disabled",
            "Dynamic workers are disabled; explicitly enable the "
            "local provisioner first.",
        )
    # Only platform runtime variables and explicitly bound credentials reach the child.
    inherited = {
        key: value
        for key, value in environment.items()
        if key.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
        }
    }
    secrets = []
    for binding in config.environment:
        value = environment.get(binding.source_variable)
        if not value and binding.required:
            refuse(
                "credential_missing",
                "A required provisioner credential binding is unavailable.",
            )
        if value:
            inherited[binding.name] = value
            secrets.append(value)
    return config, inherited, secrets


def invoke(config, environment, *, help_mode=False, request=None):
    try:
        result = run_bounded_process(
            (*config.command, config.help_argument) if help_mode else config.command,
            cwd=None,
            environment=environment,
            timeout_seconds=min(config.timeout_seconds, 30)
            if help_mode
            else config.timeout_seconds,
            stdout_limit_bytes=LIMIT,
            stderr_limit_bytes=LIMIT,
            error_prefix="worker.provisioner",
            trace=False,
            input_bytes=None if help_mode else canonical_json_bytes(request),
        )
    except BuildError:
        refuse(
            "execution_failed",
            "Provisioner could not complete within its process "
            "limits; inspect the local request status before "
            "retrying.",
        )
    if result.returncode:
        refuse(
            "command_failed",
            "Provisioner returned a nonzero exit status; inspect the "
            "local request status and provider before retrying.",
        )
    return result.stdout


def discover_help(path, environment):
    config, child, secrets = admitted(path, environment)
    output = invoke(config, child, help_mode=True).decode("utf-8")
    for secret in secrets:
        output = output.replace(secret, "[REDACTED]")
    return redact_secrets(output)


def operation_path(state, worker_id):
    return state / (_identifier(worker_id, "worker_id") + ".json")


def register(path, worker):
    def transform(catalog):
        workers = {v.worker_id: v for v in catalog.workers}
        if worker.worker_id in workers and workers[worker.worker_id] != worker:
            refuse(
                "registration_conflict",
                "Worker registration differs; saved response is retained for recovery.",
            )
        workers[worker.worker_id] = worker
        return ExecutionWorkerCatalog(tuple(workers[key] for key in sorted(workers)))

    return change_registry(path, transform)[0]


def provision(
    config_path,
    state,
    catalog_path,
    worker_id,
    request_id,
    requirements,
    target_profile,
    parameters,
    environment,
):
    config, child, _ = admitted(config_path, environment)
    _identifier(request_id, "request_id")
    # Validate requested worker fields before any provider invocation.
    template = ExecutionWorker(
        worker_id,
        ExecutionWorkerKind.SSH,
        target_profile=target_profile,
        requirements=requirements,
        endpoint="user@placeholder.invalid",
        workspace="~/worker",
    )
    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "worker_id": worker_id,
        "target_profile": template.target_profile,
        "requirements": requirements.to_dict(),
        "parameters": parameters,
    }
    if len(canonical_json_bytes(request)) > LIMIT:
        refuse("too_large", "Provisioning request exceeds 64 KiB.")
    identity = canonical_identity(request).uri
    path = operation_path(state, worker_id)
    with exclusive_cache_lock(path.with_suffix(".lock")):
        # A queued request must observe disable/configuration changes before launch.
        config, child, _ = admitted(config_path, environment)
        if path.exists():
            previous = read_record(path)
            if previous.get("request_identity") != identity:
                refuse(
                    "request_pending",
                    "This worker already has a provisioning record; "
                    "inspect status and recover it before another "
                    "allocation.",
                )
            if previous.get("status") == "registered":
                saved = ExecutionWorker.from_dict(previous["response"]["worker"])
                if saved not in read_registry(catalog_path).workers:
                    refuse(
                        "registration_changed",
                        "Registration changed; inspect it and recover explicitly.",
                    )
                return previous
            refuse(
                "request_pending",
                "Request was already attempted; inspect status and "
                "recover without allocating again.",
            )
        if any(v.worker_id == worker_id for v in read_registry(catalog_path).workers):
            refuse(
                "already_registered",
                "Worker is already registered; select a new worker ID.",
            )
        # The organization command must implement side-effect-free help.
        invoke(config, child, help_mode=True)
        if WorkerProvisioner.from_dict(read_private(config_path)) != config:
            refuse(
                "config_changed",
                "Configuration changed during help discovery; retry after review.",
            )
        record = {
            "schema": "literate-ai/worker-provision-state@1",
            "request": request,
            "request_identity": identity,
            "config_identity": canonical_identity(config.to_dict()).uri,
            "catalog_path": str(catalog_path),
            "status": "uncertain",
            "response": None,
        }
        write_private(path, record, limit=3 * LIMIT)
        # Failures, invalid output and termination leave uncertain state.
        output = invoke(config, child, request=request)
        response = json.loads(output)
        worker = validate_response(response, request)
        record.update(status="ready", response=dict(response))
        write_private(path, record, limit=3 * LIMIT)
        if WorkerProvisioner.from_dict(read_private(config_path)) != config:
            refuse(
                "config_changed",
                "Provisioner configuration changed; the validated "
                "response is retained for explicit recovery.",
            )
        register(catalog_path, worker)
        record["status"] = "registered"
        write_private(path, record, limit=3 * LIMIT)
        return record


def validate_response(value, request):
    response = contract_fields(
        value,
        path="WorkerProvisionResponse",
        schema_uri=RESPONSE_SCHEMA,
        required=frozenset({"request_identity", "worker", "lease_id"}),
    )
    string_value(
        response["lease_id"], "WorkerProvisionResponse.lease_id", max_length=1024
    )
    worker = ExecutionWorker.from_dict(response["worker"])
    if (
        response["request_identity"] != canonical_identity(request).uri
        or worker.worker_id != request["worker_id"]
        or worker.kind != ExecutionWorkerKind.SSH
        or worker.target_profile != request["target_profile"]
        or worker.requirements.to_dict() != request["requirements"]
    ):
        refuse(
            "response_mismatch",
            "Response differs from the requested worker and requirements.",
        )
    return worker


def read_record(path):
    record = dict(
        contract_fields(
            read_private(path, limit=3 * LIMIT),
            path="WorkerProvisionState",
            schema_uri="literate-ai/worker-provision-state@1",
            required=frozenset(
                {
                    "request",
                    "request_identity",
                    "config_identity",
                    "catalog_path",
                    "status",
                    "response",
                }
            ),
        )
    )
    request = contract_fields(
        record["request"],
        path="WorkerProvisionRequest",
        schema_uri=REQUEST_SCHEMA,
        required=frozenset(
            {"request_id", "worker_id", "target_profile", "requirements", "parameters"}
        ),
    )
    _identifier(request["request_id"], "request_id")
    _identifier(request["worker_id"], "worker_id")
    ExecutionRequirements.from_dict(request["requirements"])
    if (
        record["request_identity"] != canonical_identity(request).uri
        or record["status"] not in ("uncertain", "ready", "registered")
        or request["worker_id"] != path.stem
        or not Path(string_value(record["catalog_path"], "catalog_path")).is_absolute()
    ):
        refuse("invalid_state", "Saved provisioning state is inconsistent.")
    if record["status"] != "uncertain":
        validate_response(record["response"], request)
    return record


def recover(state, worker_id):
    path = operation_path(state, worker_id)
    with exclusive_cache_lock(path.with_suffix(".lock")):
        record = read_record(path)
        if record.get("status") not in ("ready", "registered") or not record.get(
            "response"
        ):
            refuse(
                "uncertain",
                "No validated response is available. Inspect the "
                "provider allocation using the saved request ID; no "
                "automatic retry is safe.",
            )
        worker = validate_response(record["response"], record["request"])
        if worker.worker_id != worker_id:
            refuse(
                "response_mismatch",
                "Saved worker ID differs from the selected request.",
            )
        register(Path(record["catalog_path"]), worker)
        record["status"] = "registered"
        write_private(path, record, limit=3 * LIMIT)
        return record

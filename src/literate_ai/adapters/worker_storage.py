"""Private storage bindings and bounded local/SSH worker collection."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol

from literate_ai import worker_storage_probe
from literate_ai.adapters.builders._process import (
    BoundedProcessResult,
    run_bounded_process,
)
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.ssh_transport import SshTransportError, ssh_arguments
from literate_ai.contracts import (
    CapacityMetric,
    CapacityProbeStatus,
    ContentIdentity,
    ContractValidationError,
    ExecutionWorker,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
    QuotaCapacitySample,
    StorageCapacitySample,
    WorkerCapacityObservation,
    WorkerCapacityPolicy,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts._validation import fields, string_value
from literate_ai.contracts.worker_capacity import capacity_alias


class WorkerStorageProbeError(ValueError):
    """Invalid private configuration or an unrepresentable observation window."""


@dataclass(frozen=True, slots=True)
class WorkerStorageCommand:
    """An explicit stdin health receiver, separate from lifecycle dispatch."""

    command: tuple[str, ...]
    environment: tuple[ExecutionWorkerEnvironment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 128:
            raise WorkerStorageProbeError("worker.storage_command_invalid")
        for argument in self.command:
            string_value(argument, "WorkerStorageCommand.argument", max_length=4096)
            if "\x00" in argument or argument == "{request_file}":
                raise WorkerStorageProbeError("worker.storage_command_invalid")
        if sum(len(v.encode("utf-8")) + 1 for v in self.command) > 8192:
            raise WorkerStorageProbeError("worker.storage_command_oversized")
        if (
            not isinstance(self.environment, tuple)
            or len(self.environment) > 32
            or any(
                not isinstance(v, ExecutionWorkerEnvironment) for v in self.environment
            )
        ):
            raise WorkerStorageProbeError("worker.storage_environment_invalid")
        names = [v.name for v in self.environment]
        if (
            names != sorted(set(names))
            or worker_storage_probe.REQUEST_ENVIRONMENT in names
        ):
            raise WorkerStorageProbeError("worker.storage_environment_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/private-worker-storage-command@1",
            "command": list(self.command),
            "environment": [v.to_dict() for v in self.environment],
        }


@dataclass(frozen=True, slots=True)
class WorkerStorageBindings:
    worker: ExecutionWorker
    os_family: str
    paths: tuple[tuple[str, str], ...]
    python_executable: str | None = None
    health_command: WorkerStorageCommand | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.worker, ExecutionWorker):
            raise WorkerStorageProbeError("worker.storage_worker_invalid")
        if self.os_family not in ("linux", "macos", "windows"):
            raise WorkerStorageProbeError("worker.storage_platform_invalid")
        if self.worker.requirements.os_family not in (None, self.os_family):
            raise WorkerStorageProbeError("worker.storage_platform_mismatch")
        if not isinstance(self.paths, tuple) or not 4 <= len(self.paths) <= 16:
            raise WorkerStorageProbeError("worker.storage_paths_invalid")
        if self.health_command is not None and (
            self.worker.kind is not ExecutionWorkerKind.COMMAND
            or not isinstance(self.health_command, WorkerStorageCommand)
        ):
            raise WorkerStorageProbeError("worker.storage_command_invalid")
        if self.python_executable is not None:
            string_value(
                self.python_executable,
                "WorkerStorageBindings.python_executable",
                max_length=4096,
            )
            if (
                "\x00" in self.python_executable
                or self.worker.kind is not ExecutionWorkerKind.SSH
                or (
                    self.os_family == "windows"
                    and not re.fullmatch(
                        r"[A-Za-z0-9_.:/\\ -]+", self.python_executable
                    )
                )
            ):
                raise WorkerStorageProbeError("worker.storage_python_invalid")
        names = []
        for entry in self.paths:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise WorkerStorageProbeError("worker.storage_paths_invalid")
            role, path = entry
            capacity_alias(role, "WorkerStorageBindings.role")
            string_value(path, "WorkerStorageBindings.path", max_length=4096)
            pure = (
                PureWindowsPath(path)
                if self.os_family == "windows"
                else PurePosixPath(path)
            )
            if "\x00" in path or not pure.is_absolute():
                raise WorkerStorageProbeError("worker.storage_path_not_absolute")
            names.append(role)
        if names != sorted(set(names)):
            raise WorkerStorageProbeError("worker.storage_roles_not_unique_sorted")

    @property
    def identity(self) -> ContentIdentity:
        # The worker configuration and raw paths stay private; only their digest
        # enters the policy/observation. Never resolve a remote path here.
        return canonical_identity(
            {
                "schema": "literate-ai/private-worker-storage-bindings@1",
                "worker": self.worker.to_dict(),
                "os_family": self.os_family,
                "paths": [list(item) for item in self.paths],
                "python_executable": self.python_executable,
                "health_command": None
                if self.health_command is None
                else self.health_command.to_dict(),
            }
        )


class StorageProbeRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        environment: dict[str, str],
        timeout_seconds: float,
        input_bytes: bytes | None = None,
    ) -> BoundedProcessResult: ...


def _run_probe(command, *, environment, timeout_seconds, input_bytes=None):
    return run_bounded_process(
        command,
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=worker_storage_probe.MAX_RESPONSE_BYTES,
        stderr_limit_bytes=4096,
        error_prefix="worker_storage_probe",
        # The bounded runner owns spawning and process-tree teardown. Raw pipes
        # avoid buffered-reader locks while a native filesystem probe stalls.
        process_factory=partial(subprocess.Popen, bufsize=0),
        trace=False,
        input_bytes=input_bytes,
    )


def _probe_command(*, executable=None, timeout_ms=None, os_family=None):
    family = os_family or {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
        sys.platform
    )
    paths = [Path(worker_storage_probe.__file__)]
    if family != "windows":
        paths.append(paths[0].with_name("worker_quota_probe.py"))
    sources = []
    for path in paths:
        with path.open("rb") as stream:
            sources.append(stream.read(64 * 1024 + 1))
    if sum(map(len, sources)) > 64 * 1024:
        raise ValueError("worker probe source exceeds its staging bound")

    def compiled(source):
        encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
        return (
            "compile(zlib.decompress(base64.b64decode("
            + repr(encoded)
            + ")), '<worker-storage-probe>', 'exec')"
        )

    script = "import base64,zlib;s={'__name__':'worker_storage_receiver'};"
    if len(sources) == 2:
        script += (
            "q={};exec("
            + compiled(sources[1])
            + ",q);s['_native_quota_probe']=q['probe'];"
        )
    script += "exec(" + compiled(sources[0]) + ",s);"
    if timeout_ms is None:
        script += "raise SystemExit(s['entrypoint']())"
    else:
        script += "raise SystemExit(s['receive'](" + str(timeout_ms) + "))"
    return (executable or sys.executable, "-I", "-S", "-B", "-c", script)


def _unobserved(roles, status):
    metric = CapacityMetric(status)
    return tuple(
        StorageCapacitySample(
            role,
            None,
            None,
            metric,
            metric,
            (QuotaCapacitySample("unobserved", metric, metric),),
        )
        for role in roles
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _command_environment(command: WorkerStorageCommand) -> dict[str, str]:
    selected = {}
    for binding in command.environment:
        value = os.environ.get(binding.source_variable)
        if not value:
            if binding.required:
                raise PermissionError("worker.storage_environment_unavailable")
            continue
        if "\x00" in value:
            raise ValueError("worker.storage_environment_invalid")
        selected[binding.name] = value
    if sum(len(k.encode()) + len(v.encode()) + 2 for k, v in selected.items()) > 16384:
        raise ValueError("worker.storage_environment_oversized")
    return selected


def observe_worker_storage(
    bindings: WorkerStorageBindings,
    policy: WorkerCapacityPolicy,
    *,
    job_identity: ContentIdentity | None,
    runner: StorageProbeRunner = _run_probe,
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> WorkerCapacityObservation:
    """Collect without allocating directories or treating a probe failure as capacity.

    Observation times are UTC epoch milliseconds on the supervisor. The window
    conservatively contains the child measurement. Failed attempts may expire
    before completion; they remain failed observations rather than fresh evidence.
    """
    if not isinstance(bindings, WorkerStorageBindings) or not isinstance(
        policy, WorkerCapacityPolicy
    ):
        raise WorkerStorageProbeError("worker.storage_configuration_invalid")
    if job_identity is not None and not isinstance(job_identity, ContentIdentity):
        raise WorkerStorageProbeError("worker.storage_job_identity_invalid")
    roles = tuple(item.role for item in policy.roles)
    if (
        tuple(role for role, _path in bindings.paths) != roles
        or bindings.identity != policy.storage_bindings_identity
    ):
        raise WorkerStorageProbeError("worker.storage_binding_mismatch")
    monotonic_started = time.monotonic()
    started = clock_ms()
    if (
        bindings.worker.kind is ExecutionWorkerKind.COMMAND
        and bindings.health_command is None
    ):
        # Remote transports require their own deadline/response custody. Never
        # let a controller-local stat masquerade as a remote-worker observation.
        samples = _unobserved(roles, CapacityProbeStatus.UNSUPPORTED)
    else:
        payload = canonical_json_bytes(
            {
                "schema": worker_storage_probe.REQUEST_PROTOCOL,
                "worker_id": bindings.worker.worker_id,
                "worker_identity": bindings.worker.identity.uri,
                "policy_identity": policy.identity.uri,
                "job_identity": None if job_identity is None else job_identity.uri,
                "os_family": bindings.os_family,
                "timeout_ms": policy.probe_timeout_ms,
                "paths": [list(item) for item in bindings.paths],
                "nonce": secrets.token_hex(16),
            }
        )
        if len(payload) > worker_storage_probe.MAX_REQUEST_BYTES:
            raise WorkerStorageProbeError("worker.storage_request_oversized")
        expected_identity = "sha256:" + hashlib.sha256(payload).hexdigest()
        # Role paths arrive in a bounded private environment value locally or
        # standard input remotely. This invocation disables raw tracing.
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
            if key in os.environ
        }
        remote = bindings.worker.kind is ExecutionWorkerKind.SSH
        external = bindings.worker.kind is ExecutionWorkerKind.COMMAND
        if not remote and not external:
            environment[worker_storage_probe.REQUEST_ENVIRONMENT] = payload.decode(
                "utf-8"
            )
        elif remote:
            # SSH may need the already-configured agent socket; do not forward
            # the controller's general environment or private request to argv.
            if "SSH_AUTH_SOCK" in os.environ:
                environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        try:
            if external:
                assert bindings.health_command is not None
                environment.update(_command_environment(bindings.health_command))
                command = (
                    *bindings.health_command.command,
                    "--receive",
                    "--timeout-ms",
                    str(policy.probe_timeout_ms),
                )
            elif remote:
                windows = bindings.os_family == "windows"
                receiver = _probe_command(
                    executable=bindings.python_executable
                    or ("python" if windows else "python3"),
                    timeout_ms=policy.probe_timeout_ms,
                    os_family=bindings.os_family,
                )
                remote_command = (
                    subprocess.list2cmdline(receiver)
                    if windows
                    else shlex.join(receiver)
                )
                command = ssh_arguments(
                    bindings.worker.endpoint,
                    remote_command,
                    policy.probe_timeout_ms / 1000,
                    transport=bindings.worker.transport,
                    login_shell=not windows,
                )
            else:
                command = _probe_command()
            remaining = policy.probe_timeout_ms / 1000 - (
                time.monotonic() - monotonic_started
            )
            if remaining <= 0:
                raise BuildError(
                    "worker_storage_probe_timeout",
                    "Probe preparation exceeded its deadline",
                )
            result = runner(
                command,
                environment=environment,
                timeout_seconds=remaining,
                **({"input_bytes": payload} if remote or external else {}),
            )
            if (
                not isinstance(result, BoundedProcessResult)
                or not isinstance(result.stdout, bytes)
                or not isinstance(result.stderr, bytes)
                or type(result.returncode) is not int
            ):
                raise ValueError("invalid process result")
            if (
                len(result.stdout) > worker_storage_probe.MAX_RESPONSE_BYTES
                or len(result.stderr) > 4096
            ):
                raise ValueError("invalid probe output")
            if remote and result.returncode in {124, 255, 127, 9009}:
                # SSH reserves 255 for transport/authentication failures. The
                # trusted receiver uses 124 only for its own finite watchdog.
                status = {
                    124: CapacityProbeStatus.TIMED_OUT,
                    255: CapacityProbeStatus.UNREACHABLE,
                    127: CapacityProbeStatus.UNAVAILABLE,
                    9009: CapacityProbeStatus.UNAVAILABLE,
                }[result.returncode]
                samples = _unobserved(roles, status)
            elif result.returncode != 0:
                raise ValueError("invalid probe output")
            else:
                samples = _parse_response(
                    result.stdout, expected_identity, bindings.os_family, roles
                )
        except BuildError as exc:
            status = (
                CapacityProbeStatus.TIMED_OUT
                if exc.code.endswith("_timeout")
                else CapacityProbeStatus.MALFORMED
            )
            if exc.code.endswith("_launch_failed"):
                status = CapacityProbeStatus.UNAVAILABLE
            samples = _unobserved(roles, status)
        except (
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
            ContractValidationError,
        ):
            samples = _unobserved(roles, CapacityProbeStatus.MALFORMED)
        except SshTransportError:
            samples = _unobserved(roles, CapacityProbeStatus.UNSUPPORTED)
        except PermissionError:
            samples = _unobserved(roles, CapacityProbeStatus.DENIED)
        except OSError:
            samples = _unobserved(roles, CapacityProbeStatus.UNAVAILABLE)
    completed = clock_ms()
    if completed < started:
        raise WorkerStorageProbeError("worker.storage_clock_reversed")
    return WorkerCapacityObservation(
        bindings.worker.worker_id,
        policy.identity,
        job_identity,
        bindings.os_family,
        started,
        completed,
        started + policy.maximum_age_ms,
        samples,
    )


def _parse_response(raw, expected_identity, os_family, roles):
    value = json.loads(raw, object_pairs_hook=_unique_object)
    data = fields(
        value,
        path="WorkerStorageProbe",
        required=frozenset({"schema", "request_identity", "os_family", "samples"}),
    )
    if (
        data["schema"] != worker_storage_probe.PROTOCOL
        or data["request_identity"] != expected_identity
        or data["os_family"] != os_family
    ):
        raise ValueError("probe custody mismatch")
    if not isinstance(data["samples"], list) or len(data["samples"]) != len(roles):
        raise ValueError("probe role mismatch")
    samples = tuple(StorageCapacitySample.from_dict(item) for item in data["samples"])
    if tuple(item.role for item in samples) != roles:
        raise ValueError("probe role mismatch")
    return samples

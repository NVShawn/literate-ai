"""Private automatic INDEX selection with pinned configuration and health policy."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_hardware import (
    HARDWARE_PROBE_TIMEOUT_SECONDS,
    probe_command_hardware,
)
from literate_ai.adapters.action_transport import supports_action_transport
from literate_ai.adapters.user_assets import (
    ACTION_EXECUTION_CONFIG_ENVIRONMENT,
    resolve_action_execution_config_path,
    resolve_worker_config_path,
    resolve_worker_observations_path,
)
from literate_ai.adapters.worker_health import (
    inspect_worker_storage,
    load_worker_health_inputs,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorkerCatalog,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from literate_ai.storage import FileSystemCAS

_SCHEMA = "literate-ai/private-action-execution@1"


class ActionExecutionConfigurationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _read(path: Path, maximum: int = 65536) -> bytes:
    try:
        require_safe_directory(path.parent)
        if path.is_symlink():
            raise ValueError("symbolic input")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError("invalid input size or type")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(maximum + 1)
            after = path.lstat()
            if len(content) > maximum or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("input changed")
            return content
        finally:
            os.close(descriptor)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ActionExecutionConfigurationError(
            "action_execution.input_unavailable",
            "private action execution input is unavailable or unsafe",
        ) from exc


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _document(content: bytes):
    return json.loads(content, object_pairs_hook=_unique)


def _path(value) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise ValueError("invalid private path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("private path must be absolute and bounded")
    return path


def _result_sources(document, environment):
    if not isinstance(document, dict) or len(document) > 256:
        raise ValueError("invalid result source map")
    result = {}
    for worker_id, value in document.items():
        if re.fullmatch(
            r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", worker_id
        ) is None or not isinstance(value, dict):
            raise ValueError("invalid result source")
        if value == {"kind": "shared-cas"}:
            result[worker_id] = None
            continue
        if (
            set(value) - {"token_env", "allow_http"} != {"kind", "endpoint"}
            or value["kind"] != "http-cas"
        ):
            raise ValueError("invalid result source fields")
        token = None
        if "token_env" in value:
            name = value["token_env"]
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            ):
                raise ValueError("invalid result credential binding")
            token = environment.get(name, "")
        result[worker_id] = HttpActionBlobSource(
            value["endpoint"],
            ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=1)),
            bearer_token=token,
            allow_http=value.get("allow_http", False),
        )
    return MappingProxyType(result)


@dataclass(frozen=True)
class BoundActionExecution:
    path: Path
    original: bytes = field(repr=False)
    source_cas_root: Path
    source_handoff: str
    duration_seconds: int
    maximum_hardware_age_seconds: int
    health_configurations: Mapping[str, Path] = field(repr=False)
    catalog_path: Path = field(repr=False)
    observations_path: Path = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    result_sources: Mapping[str, HttpActionBlobSource | None] = field(
        default_factory=dict, repr=False
    )

    finalize_profile: ContentIdentity | None = None
    # Operator-owned file where a refused FINALIZE writes the exact grant request.
    finalize_grant_request_path: Path | None = field(default=None, repr=False)
    # Bounded wait for that grant inside the same rebuild; 0 means fail at once.
    finalize_grant_wait_seconds: int = 0

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/bound-action-execution@1",
                "configuration": _document(self.original),
            }
        )

    def require_unchanged(self):
        if _read(self.path) != self.original:
            raise ActionExecutionConfigurationError(
                "action_execution.configuration_changed",
                "private action execution configuration changed",
            )

    def build_result_source(self, pool, cas, *, phases=(LifecycleActionKind.BUILD,)):
        """Bind explicit result reads to admitted identities and configuration."""
        if (
            not isinstance(phases, tuple)
            or not phases
            or len(set(phases)) != len(phases)
            or any(
                not isinstance(phase, LifecycleActionKind)
                or phase
                not in (
                    LifecycleActionKind.GENERATE,
                    LifecycleActionKind.BUILD,
                    LifecycleActionKind.TEST,
                    LifecycleActionKind.EXECUTE,
                    LifecycleActionKind.ACCEPT,
                    LifecycleActionKind.LINK,
                    LifecycleActionKind.PACKAGE,
                    LifecycleActionKind.FINALIZE,
                )
                for phase in phases
            )
        ):
            raise ValueError(
                "result transport requires "
                "GENERATE/BUILD/TEST/EXECUTE/ACCEPT/LINK/PACKAGE/FINALIZE phases"
            )
        self.require_unchanged()
        admitted = {
            worker.worker_id: pool.catalog.worker(worker.worker_id)
            for worker in pool.workers
            if any(pool.supports_phase(worker, phase) for phase in phases)
        }
        if any(worker_id not in self.result_sources for worker_id in admitted):
            raise ActionExecutionConfigurationError(
                "action_execution.result_source_missing",
                "/".join(phase.value.upper() for phase in phases)
                + " result transport is not configured",
            )

        def fetch(worker, reference):
            self.require_unchanged()
            pool.deadline.remaining()
            if admitted.get(worker.worker_id) != worker:
                raise ActionWireError(
                    "action_build.result_worker_mismatch", "result worker differs"
                )
            configured = self.result_sources[worker.worker_id]
            if configured is None:
                content = cas.get_bytes(reference)
            else:
                source = HttpActionBlobSource(
                    configured.endpoint,
                    pool.deadline,
                    bearer_token=configured.bearer_token,
                    allow_http=configured.endpoint.startswith("http://"),
                )
                content = source.fetch(reference)
            self.require_unchanged()
            pool.deadline.remaining()
            return content

        return fetch

    def admit(
        self, *, project_root: Path, target_profile: str, job_identity: ContentIdentity
    ):
        if not isinstance(job_identity, ContentIdentity):
            raise TypeError("action execution requires a typed job identity")
        root = project_root.resolve(strict=True)
        cas_root = self.source_cas_root.resolve(strict=False)
        if (
            cas_root == root
            or cas_root.is_relative_to(root)
            or root.is_relative_to(cas_root)
        ):
            raise ActionExecutionConfigurationError(
                "action_execution.authority_overlap",
                "action source CAS must be outside project authority",
            )
        self.require_unchanged()
        deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=self.duration_seconds)
        )
        health_inputs = {}
        hardware = {}
        hardware_lock = threading.Lock()

        def catalog():
            self.require_unchanged()
            return ExecutionWorkerCatalog.from_dict(
                _document(_read(self.catalog_path, 1024 * 1024))
            )

        def observations():
            self.require_unchanged()
            if not hardware_lock.acquire(timeout=deadline.remaining()):
                raise ActionWireError(
                    "action_execution.hardware_timeout",
                    "hardware observation wait expired",
                )
            try:
                candidates = tuple(
                    worker
                    for worker in catalog().workers
                    if supports_action_transport(worker)
                    and worker.target_profile == target_profile
                    and worker.worker_id in self.health_configurations
                )
                if len(candidates) > 256:
                    raise ActionWireError(
                        "action_execution.hardware_capacity",
                        "hardware probe candidate limit exceeded",
                    )
                identities = {worker.identity for worker in candidates}
                for identity in tuple(hardware):
                    if identity not in identities:
                        del hardware[identity]
                result = []
                failures = set()
                for worker in candidates:
                    deadline.remaining()
                    observed = hardware.get(worker.identity)
                    now = datetime.now(UTC)
                    if observed is not None:
                        age = now - datetime.fromisoformat(
                            observed.observed_at.replace("Z", "+00:00")
                        )
                        if (
                            not timedelta(0)
                            <= age
                            <= timedelta(seconds=self.maximum_hardware_age_seconds)
                        ):
                            observed = None
                    if observed is None:
                        hardware.pop(worker.identity, None)
                        try:
                            observed = probe_command_hardware(
                                worker,
                                timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
                                cwd=project_root,
                                environment=self.environment,
                                deadline=deadline,
                            )
                        except (ActionWireError, OSError) as exc:
                            failures.add(
                                exc.code
                                if isinstance(exc, ActionWireError)
                                else "action_execution.hardware_os_error"
                            )
                            continue
                        hardware[worker.identity] = observed
                    result.append(observed)
                deadline.remaining()
                if not result and failures:
                    raise ActionWireError(
                        "action_execution.hardware_probe_failed",
                        "all candidate hardware probes failed: "
                        + ", ".join(sorted(failures)),
                    )
                return WorkerHardwareObservationCatalog(
                    tuple(sorted(result, key=lambda item: item.worker_id))
                )
            finally:
                hardware_lock.release()

        def health(worker):
            try:
                self.require_unchanged()
                deadline.remaining()
                if worker.worker_id not in self.health_configurations:
                    raise ValueError("missing health policy")
                if worker.worker_id not in health_inputs:
                    health_inputs[worker.worker_id] = load_worker_health_inputs(
                        self.health_configurations[worker.worker_id],
                        self.catalog_path,
                        worker_id=worker.worker_id,
                    )
                inputs = health_inputs[worker.worker_id]
                if inputs.bindings.worker != worker:
                    raise ValueError("health worker mismatch")
                for attempt in range(inputs.policy.maximum_retries + 1):
                    reserved_ms = inputs.policy.probe_timeout_ms
                    if inputs.pressure is not None:
                        reserved_ms += 60000
                    if inputs.cleanup is not None:
                        reserved_ms += inputs.cleanup.deadline_ms
                    if deadline.remaining() * 1000 < reserved_ms:
                        raise ValueError("insufficient action budget for health probes")
                    result, status = inspect_worker_storage(
                        inputs, job_identity=job_identity, attempt=attempt
                    )
                    deadline.remaining()
                    if status == 0:
                        return canonical_identity(result)
                    if status != 2:
                        break
                raise ValueError("health holds new work")
            except (ValueError, OSError, RuntimeError) as exc:
                raise ActionWireError(
                    "action_execution.health_unavailable",
                    "worker lacks current health admission",
                ) from exc

        pool = CommandActionWorkerPool(
            catalog,
            observations,
            health,
            deadline,
            phase=LifecycleActionKind.INDEX,
            source_handoff=self.source_handoff,
            target_profile=target_profile,
            cwd=project_root,
            environment=self.environment,
            maximum_hardware_age=timedelta(seconds=self.maximum_hardware_age_seconds),
        )
        self.require_unchanged()
        return pool, FileSystemCAS(self.source_cas_root)


def load_action_execution(
    *, project_root: Path, environment: Mapping[str, str] | None = None
) -> BoundActionExecution | None:
    configured = dict(os.environ if environment is None else environment)
    try:
        path = resolve_action_execution_config_path(environment=configured)
        if not path.exists() and not path.is_symlink():
            if configured.get(ACTION_EXECUTION_CONFIG_ENVIRONMENT):
                raise ActionExecutionConfigurationError(
                    "action_execution.configuration_missing",
                    "selected action execution configuration is missing",
                )
            return None
        original = _read(path)
        document = _document(original)
        if (
            not isinstance(document, dict)
            or set(document) - {"result_sources", "finalize"}
            != {
                "schema",
                "source_cas_root",
                "source_handoff",
                "duration_seconds",
                "maximum_hardware_age_seconds",
                "health_configurations",
            }
            or document["schema"] != _SCHEMA
        ):
            raise ValueError("invalid fields")
        for key in ("duration_seconds", "maximum_hardware_age_seconds"):
            if type(document[key]) is not int or not 1 <= document[key] <= 86400:
                raise ValueError("invalid time bound")
        if document["source_handoff"] not in ("filesystem-cas", "http-cas"):
            raise ValueError("unsupported source handoff")
        health = document["health_configurations"]
        if (
            not isinstance(health, dict)
            or not 1 <= len(health) <= 256
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", key) is None
                for key in health
            )
        ):
            raise ValueError("invalid health configuration map")
        finalize = document.get("finalize")
        finalize_profile = None
        grant_request_path = None
        grant_wait_seconds = 0
        if "finalize" in document:
            if (
                not isinstance(finalize, dict)
                or not {"profile_identity", "verifier"}
                <= set(finalize)
                <= {
                    "profile_identity",
                    "verifier",
                    "grant_request_path",
                    "grant_wait_seconds",
                }
                or finalize["verifier"] != "portable-application@1"
            ):
                raise ValueError("invalid FINALIZE policy")
            finalize_profile = ContentIdentity.parse_uri(finalize["profile_identity"])
            if "grant_request_path" in finalize:
                grant_request_path = _path(finalize["grant_request_path"])
            grant_wait_seconds = finalize.get("grant_wait_seconds", 0)
            if (
                type(grant_wait_seconds) is not int
                or not 0 <= grant_wait_seconds <= 3600
                or (grant_wait_seconds and grant_request_path is None)
            ):
                raise ValueError("invalid FINALIZE grant wait")
        return BoundActionExecution(
            path,
            original,
            _path(document["source_cas_root"]),
            document["source_handoff"],
            document["duration_seconds"],
            document["maximum_hardware_age_seconds"],
            MappingProxyType({key: _path(value) for key, value in health.items()}),
            resolve_worker_config_path(
                environment=configured, project_root=project_root
            ),
            resolve_worker_observations_path(
                environment=configured, project_root=project_root
            ),
            MappingProxyType(configured),
            _result_sources(document.get("result_sources", {}), configured),
            finalize_profile,
            grant_request_path,
            grant_wait_seconds,
        )
    except ActionExecutionConfigurationError:
        raise
    except (ValueError, TypeError, OSError, RecursionError, ActionWireError) as exc:
        raise ActionExecutionConfigurationError(
            "action_execution.configuration_invalid",
            "invalid private action execution configuration",
        ) from exc

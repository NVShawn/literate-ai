"""Live allocation custody through an explicitly configured provisioning inspector.

Only the trusted operator configuration selects the inspector. Its implementation
must inspect the actual allocation, not echo configuration or cached claims. This
owner runs that command for every check and never calls the allocation command.
Filesystem delegation and lifecycle admission remain separate owners.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from dataclasses import replace
from pathlib import Path

from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectPlan,
    _fields,
)
from literate_ai.contracts.worker_provisioning import WorkerProvisioner

from .execution_dispatch import ExecutionDispatchAdapterError
from .worker_provisioning import LIMIT, admitted, invoke

REQUEST_SCHEMA = "urn:literate-ai:schema:v1:retained-allocation-inspection-request"
RESPONSE_SCHEMA = "urn:literate-ai:schema:v1:retained-allocation-inspection-response"


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("retained.allocation.duplicate-json-field")
        value[key] = item
    return value


def _stamp(metadata):
    # Reading a private configuration may update atime; it does not change its
    # identity, contents or permissions. Keep mutation-sensitive fields only.
    return tuple(
        getattr(metadata, name)
        for name in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _private_configuration(path):
    if os.name != "posix":
        raise ValueError("retained.allocation.private-config-platform-unsupported")
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("retained.allocation.canonical-config-path-required")
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError("retained.allocation.private-config-parent-required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > LIMIT
        ):
            raise ValueError("retained.allocation.private-config-file-required")
        payload = stream.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("retained.allocation.config-too-large")
        after = os.fstat(stream.fileno())
    if (
        _stamp(before) != _stamp(after)
        or _stamp(path.stat(follow_symlinks=False)) != _stamp(after)
        or _stamp(path.parent.stat()) != _stamp(parent)
    ):
        raise ValueError("retained.allocation.config-changed")
    config = WorkerProvisioner.from_dict(
        json.loads(payload, object_pairs_hook=_unique_object)
    )
    if not config.enabled or not config.inspection_command:
        raise ValueError("retained.allocation.inspector-not-enabled")
    return config


class RetainedAllocationOwner:
    """A non-deserializable live owner; a policy record cannot instantiate access."""

    def __init__(
        self,
        config_path: Path,
        *,
        plan: RetainedProjectPlan,
        environment,
        minimum_remaining_seconds: int,
    ):
        if not isinstance(config_path, Path):
            raise TypeError("retained.allocation.typed-config-path-required")
        if not isinstance(plan, RetainedProjectPlan):
            raise TypeError("retained.allocation.typed-plan-required")
        if plan.profile.secondary_access is None:
            raise ValueError("retained.allocation.reviewed-secondary-policy-required")
        self._path = config_path
        self._config = _private_configuration(config_path)
        self._environment = dict(environment)
        self._reviewed_plan = plan
        self._worker = plan.profile.worker_identity
        self._plan = plan.identity
        self._policy = plan.profile.secondary_access.identity
        self._allocation = None
        self._prefix = None
        self._observations = []
        self._failed = False
        self.require_current(minimum_remaining_seconds=minimum_remaining_seconds)

    @property
    def workspace_prefix(self):
        if self._failed:
            raise ValueError("retained.allocation.owner-revoked")
        return self._prefix

    def require_current(self, *, minimum_remaining_seconds: int):
        if self._failed:
            raise ValueError("retained.allocation.owner-revoked")
        try:
            return self._inspect(minimum_remaining_seconds=minimum_remaining_seconds)
        except ExecutionDispatchAdapterError as exc:
            self._failed = True
            raise ValueError("retained.allocation.inspector-failed") from exc
        except BaseException:
            self._failed = True
            raise

    def require_plan(
        self, plan: RetainedProjectPlan, *, minimum_remaining_seconds: int
    ):
        if plan != self._reviewed_plan:
            self._failed = True
            raise ValueError("retained.allocation.plan-changed")
        return self.require_current(minimum_remaining_seconds=minimum_remaining_seconds)

    def _inspect(self, *, minimum_remaining_seconds: int):
        if (
            type(minimum_remaining_seconds) is not int
            or not 1 <= minimum_remaining_seconds <= 86400
        ):
            raise ValueError("retained.allocation.bounded-lifetime-required")
        if len(self._observations) >= 8192:
            raise ValueError("retained.allocation.observation-budget-exceeded")
        if _private_configuration(self._path) != self._config:
            raise ValueError("retained.allocation.config-changed")
        config, child, _ = admitted(self._path, self._environment)
        if config != self._config:
            raise ValueError("retained.allocation.config-changed")
        request = {
            "schema": REQUEST_SCHEMA,
            "nonce": secrets.token_hex(32),
            "worker_identity": self._worker.uri,
            "plan_identity": self._plan.uri,
            "policy_identity": self._policy.uri,
            "minimum_remaining_seconds": minimum_remaining_seconds,
        }
        started = time.monotonic()
        output = invoke(
            replace(
                config,
                command=config.inspection_command,
                timeout_seconds=min(config.timeout_seconds, 30),
            ),
            child,
            request=request,
        )
        elapsed = time.monotonic() - started
        if _private_configuration(self._path) != self._config:
            raise ValueError("retained.allocation.config-changed")
        response = _fields(
            json.loads(output, object_pairs_hook=_unique_object),
            {
                "schema",
                "request_identity",
                "allocation_identity",
                "workspace_prefix",
                "remaining_seconds",
                "disposable",
            },
        )
        remaining = response["remaining_seconds"]
        if (
            response["schema"] != RESPONSE_SCHEMA
            or response["request_identity"] != canonical_identity(request).uri
            or response["disposable"] is not True
            or type(remaining) is not int
            or not minimum_remaining_seconds + elapsed <= remaining <= 86400
        ):
            raise ValueError("retained.allocation.inspection-mismatch-or-expired")
        allocation = ContentIdentity.parse_uri(response["allocation_identity"])
        prefix = response["workspace_prefix"]
        if (
            not isinstance(prefix, str)
            or not prefix
            or len(prefix) > 4096
            or any(ord(c) < 32 for c in prefix)
            or "\\" in prefix
            or not Path(prefix).is_absolute()
            or Path(prefix).as_posix() != prefix
            or ".." in Path(prefix).parts
            or prefix == "/"
        ):
            raise ValueError("retained.allocation.workspace-prefix-invalid")
        prefix = Path(prefix)
        if self._allocation is not None and (
            allocation != self._allocation or prefix != self._prefix
        ):
            raise ValueError("retained.allocation.instance-changed")
        self._allocation = allocation
        self._prefix = prefix
        observation = {
            "schema": "literate-ai/retained-allocation-observation@1",
            "request_identity": canonical_identity(request).uri,
            "response_identity": canonical_identity(response).uri,
            "configuration_identity": canonical_identity(config.to_dict()).uri,
            "allocation_identity": allocation.uri,
            "worker_identity": self._worker.uri,
            "plan_identity": self._plan.uri,
            "policy_identity": self._policy.uri,
            "remaining_seconds_at_response": remaining,
            "acceptance": False,
        }
        self._observations.append(observation)
        return canonical_identity(observation)

    def evidence(self):
        return tuple(dict(value) for value in self._observations)

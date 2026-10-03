"""Challenge-bound hardware observations collected on opted-in command workers."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_capabilities import (
    decode_capability_request,
    receiver_code_identity,
    run_command_observation,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_transport import supports_action_transport
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservation

HARDWARE_PROTOCOL = "literate-ai/action-hardware@1"
# Bound one hardware observation, including receiver start-up on slow hosts.
HARDWARE_PROBE_TIMEOUT_SECONDS = 180
MAX_HARDWARE_BYTES = 64 * 1024
_REQUEST = "literate-ai/action-hardware-request@1"
_RESPONSE = "literate-ai/action-hardware-response@1"


def _invalid():
    raise ActionWireError(
        "action_hardware.invalid", "invalid hardware observation document"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load(content: bytes, limit: int):
    if not isinstance(content, bytes) or len(content) > limit:
        _invalid()
    try:
        value = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_hardware.invalid", "invalid hardware JSON"
        ) from exc
    if not isinstance(value, dict):
        _invalid()
    return value


def decode_hardware_request(content: bytes):
    value = _load(content, 16 * 1024)
    if (
        set(value) != {"schema", "worker_id", "challenge"}
        or value["schema"] != _REQUEST
        or not isinstance(value["worker_id"], str)
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", value["worker_id"])
        is None
    ):
        _invalid()
    _, deadline = decode_capability_request(canonical_json_bytes(value["challenge"]))
    return value, deadline


def encode_hardware_response(
    request, deadline, expected_worker: ContentIdentity
) -> bytes:
    from literate_ai.adapters.worker_capabilities import (
        MAX_PROBE_OUTPUT_BYTES,
        probe_worker_capabilities,
    )

    content = canonical_json_bytes(request)
    request, request_deadline = decode_hardware_request(content)
    if request["challenge"]["worker_identity"] != expected_worker.uri:
        raise ActionWireError(
            "action_hardware.mismatch", "hardware worker binding differs"
        )
    deadline = ActionDispatchDeadline(
        min(deadline.expires_at, request_deadline.expires_at)
    )

    def run(argv, timeout_seconds):
        completed = run_bounded_process(
            argv,
            cwd=Path.cwd(),
            environment=os.environ,
            timeout_seconds=min(timeout_seconds, deadline.remaining()),
            stdout_limit_bytes=MAX_PROBE_OUTPUT_BYTES,
            stderr_limit_bytes=4096,
            interrupt_guard=deadline.remaining,
            terminate_descendants=True,
            error_prefix="action_hardware",
            trace=False,
        )
        return subprocess.CompletedProcess(
            argv, completed.returncode, completed.stdout, completed.stderr
        )

    observation = probe_worker_capabilities(
        ExecutionWorker(request["worker_id"], ExecutionWorkerKind.LOCAL),
        runner=run,
        timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
    )
    response = canonical_json_bytes(
        {
            "schema": _RESPONSE,
            "request_identity": record_identity(content).uri,
            "worker_identity": expected_worker.uri,
            "receiver_identity": receiver_code_identity(deadline=deadline).uri,
            "observation": observation.to_dict(),
        }
    )
    deadline.remaining()
    if len(response) > MAX_HARDWARE_BYTES:
        _invalid()
    return response


def decode_hardware_response(
    content: bytes,
    request_content: bytes,
    expected_receiver: ContentIdentity,
    observed_at: datetime,
) -> WorkerHardwareObservation:
    request, deadline = decode_hardware_request(request_content)
    value = _load(content, MAX_HARDWARE_BYTES)
    try:
        if (
            set(value)
            != {
                "schema",
                "request_identity",
                "worker_identity",
                "receiver_identity",
                "observation",
            }
            or value["schema"] != _RESPONSE
        ):
            _invalid()
        if (
            value["request_identity"]
            != record_identity(canonical_json_bytes(request)).uri
            or value["worker_identity"] != request["challenge"]["worker_identity"]
            or value["receiver_identity"] != expected_receiver.uri
        ):
            raise ActionWireError(
                "action_hardware.mismatch",
                "hardware response differs from request or receiver",
            )
        observation = WorkerHardwareObservation.from_dict(value["observation"])
        if (
            observation.worker_id != request["worker_id"]
            or observed_at.tzinfo is None
            or observed_at > datetime.now(UTC)
        ):
            _invalid()
        deadline.remaining()
        return replace(observation, observed_at=observed_at.isoformat())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ActionWireError(
            "action_hardware.invalid", "invalid hardware response"
        ) from exc


def probe_command_hardware(
    worker: ExecutionWorker,
    *,
    timeout_seconds: int,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    deadline: ActionDispatchDeadline | None = None,
) -> WorkerHardwareObservation:
    if not supports_action_transport(worker):
        raise ActionWireError(
            "action_hardware.not_declared", "worker has not opted into hardware probing"
        )
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        _invalid()
    started = datetime.now(UTC)
    expires = started + timedelta(
        seconds=min(timeout_seconds, HARDWARE_PROBE_TIMEOUT_SECONDS)
    )
    deadline = ActionDispatchDeadline(
        expires if deadline is None else min(expires, deadline.expires_at)
    )
    deadline.remaining()
    content = canonical_json_bytes(
        {
            "schema": _REQUEST,
            "worker_id": worker.worker_id,
            "challenge": {
                "schema": "literate-ai/action-capability-request@1",
                "worker_identity": worker.identity.uri,
                "nonce": secrets.token_hex(16),
                "deadline": deadline.to_dict(),
            },
        }
    )
    stage = "code_identity"
    try:
        expected = receiver_code_identity(deadline=deadline)
        stage = "transport"
        response = run_command_observation(
            worker,
            deadline,
            content,
            cwd=cwd,
            environment=environment,
            mode="--describe-hardware",
            protocol=HARDWARE_PROTOCOL,
            stdout_limit_bytes=MAX_HARDWARE_BYTES,
        )
        stage = "response"
        return decode_hardware_response(response, content, expected, started)
    except ActionWireError as exc:
        if exc.code != "action_wire.expired":
            raise
        raise ActionWireError(
            f"action_hardware.{stage}_expired",
            f"hardware observation expired during {stage}",
        ) from exc

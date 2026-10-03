"""Challenged Standard tool inventory transport bound to admitted worker facts."""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_capabilities import (
    ActionWorkerCapabilities,
    decode_capability_request,
    decode_capability_response,
    encode_capability_response,
    receiver_code_identity,
    run_command_observation,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.standard_toolchain_observations import (
    StandardToolObservations,
)
from literate_ai.contracts import canonical_json_bytes

MAX_TOOL_RESPONSE_BYTES = 64 * 1024
TOOL_OBSERVATION_PROTOCOL = "literate-ai/worker-tool-observations@1"
_RESPONSE = "literate-ai/worker-tool-observation-response@1"


def encode_tool_observation_response(
    request,
    deadline,
    expected_worker,
    *,
    build_worker,
    http_source,
    test_worker=None,
    execute_worker=None,
    accept_worker=None,
):
    deadline.remaining()
    if request.get("worker_identity") != expected_worker.uri:
        raise ActionWireError(
            "action_tools.worker_mismatch", "request binds another worker"
        )
    if build_worker is None:
        raise ActionWireError(
            "action_tools.not_configured", "Standard tools are not configured"
        )
    observations = build_worker.observe_tools(require_current=deadline.remaining)
    profile = build_worker.identity
    test_profile = None if test_worker is None else test_worker.identity
    execute_profile = None if execute_worker is None else execute_worker.identity
    accept_profile = None if accept_worker is None else accept_worker.identity
    capability = encode_capability_response(
        request,
        deadline,
        expected_worker,
        http_source=http_source,
        test_profile=test_profile,
        test_toolchains=test_worker.tools.identities if test_worker else (),
        test_standard_tools=test_worker.standard_tools_identity
        if test_worker
        else None,
        execute_profile=execute_profile,
        accept_profile=accept_profile,
        execute_toolchains=execute_worker.tools.identities if execute_worker else (),
        execute_standard_tools=execute_worker.standard_tools_identity
        if execute_worker
        else None,
        build_profile=profile,
        build_toolchains=build_worker.tools.identities,
        build_standard_tools=observations.identity,
    )
    content = canonical_json_bytes(
        dict(
            schema=_RESPONSE,
            capability=json.loads(capability),
            observations=json.loads(observations.to_bytes()),
        )
    )
    if len(content) > MAX_TOOL_RESPONSE_BYTES:
        raise ActionWireError(
            "action_tools.response_oversized", "tool response exceeds bound"
        )
    if (
        build_worker.observe_tools(require_current=deadline.remaining) != observations
        or build_worker.identity != profile
        or (test_worker is not None and test_worker.identity != test_profile)
        or (execute_worker is not None and execute_worker.identity != execute_profile)
        or (accept_worker is not None and accept_worker.identity != accept_profile)
    ):
        raise ActionWireError(
            "action_tools.observation_changed",
            "worker profile changed during observation",
        )
    deadline.remaining()
    return content


@dataclass(frozen=True)
class WorkerToolObservation:
    capability: ActionWorkerCapabilities
    tools: StandardToolObservations


def decode_tool_observation_response(
    content, request_content, expected_receiver, admitted
):
    if (
        not isinstance(admitted, ActionWorkerCapabilities)
        or admitted.build_standard_tools is None
    ):
        raise ActionWireError(
            "action_tools.not_admitted", "worker tool observations are not admitted"
        )
    age = datetime.now(UTC) - admitted.observed_at
    if not timedelta(0) <= age <= timedelta(minutes=5):
        raise ActionWireError(
            "action_tools.not_admitted", "tool capability admission is stale"
        )
    if not isinstance(content, bytes) or len(content) > MAX_TOOL_RESPONSE_BYTES:
        raise ActionWireError(
            "action_tools.response_invalid", "invalid bounded tool response"
        )

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=pairs)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "capability", "observations"}
            or value["schema"] != _RESPONSE
        ):
            raise ValueError("invalid tool response envelope")
        if canonical_json_bytes(value) != content:
            raise ValueError("noncanonical response")
        capability = decode_capability_response(
            canonical_json_bytes(value["capability"]),
            request_content,
            expected_receiver,
        )
        tools = StandardToolObservations.from_bytes(
            canonical_json_bytes(value["observations"])
        )
        if (
            capability.capability_identity != admitted.capability_identity
            or tools.identity != admitted.build_standard_tools
        ):
            raise ValueError("tool response differs from admitted capability")
        if (
            tuple(
                sorted(
                    {tool.toolchain_identity for tool in tools.tools},
                    key=lambda item: item.uri,
                )
            )
            != capability.build_toolchains
        ):
            raise ValueError("tool response differs from admitted registry")
        _, deadline = decode_capability_request(request_content)
        deadline.remaining()
        return WorkerToolObservation(capability, tools)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_tools.response_invalid", "tool observation response refused"
        ) from exc


def probe_command_tool_observations(
    worker, admitted, deadline, *, cwd, environment=None
):
    deadline.remaining()
    deadline = ActionDispatchDeadline(
        min(deadline.expires_at, datetime.now(UTC) + timedelta(seconds=60))
    )
    expected = receiver_code_identity(deadline=deadline)
    admitted.require_current(worker, expected)
    if admitted.build_standard_tools is None:
        raise ActionWireError(
            "action_tools.not_admitted",
            "worker did not advertise Standard observations",
        )
    request = canonical_json_bytes(
        dict(
            schema="literate-ai/action-capability-request@1",
            worker_identity=worker.identity.uri,
            nonce=secrets.token_hex(16),
            deadline=deadline.to_dict(),
        )
    )
    response = run_command_observation(
        worker,
        deadline,
        request,
        cwd=cwd,
        environment=environment,
        mode="--describe-tools",
        protocol=TOOL_OBSERVATION_PROTOCOL,
        stdout_limit_bytes=MAX_TOOL_RESPONSE_BYTES,
    )
    admitted.require_current(worker, expected)
    return decode_tool_observation_response(response, request, expected, admitted)

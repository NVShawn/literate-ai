"""Challenged verification of authored commands against a private worker inventory."""

import json
import secrets
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_capabilities import (
    ACTION_OBSERVATION_TIMEOUT_SECONDS,
    MAX_CAPABILITY_BYTES,
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
from literate_ai.adapters.action_tool_observation import WorkerToolObservation
from literate_ai.contracts import canonical_identity, canonical_json_bytes

SELECTOR_PROTOCOL = "literate-ai/worker-tool-selectors@1"
_REQUEST = "literate-ai/worker-tool-selector-request@1"
_RESPONSE = "literate-ai/worker-tool-selector-response@1"


def _load(content, fields, schema):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        if not isinstance(content, bytes) or len(content) > MAX_CAPABILITY_BYTES:
            raise ValueError("invalid bounded document")
        value = json.loads(content, object_pairs_hook=pairs)
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != schema
            or canonical_json_bytes(value) != content
        ):
            raise ValueError("invalid canonical envelope")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_selectors.invalid", "selector envelope refused"
        ) from exc


def decode_selector_request(content):
    value = _load(content, {"schema", "capability", "selectors"}, _REQUEST)
    try:
        _, deadline = decode_capability_request(
            canonical_json_bytes(value["capability"])
        )
        selectors = value["selectors"]
        if not isinstance(selectors, list) or not 1 <= len(selectors) <= 14:
            raise ValueError("invalid selector count")
        roles = []
        for item in selectors:
            if not isinstance(item, dict) or set(item) != {"role", "command"}:
                raise ValueError("invalid selector")
            role, command = item["role"], item["command"]
            if not isinstance(role, str) or not 1 <= len(role) <= 64:
                raise ValueError("invalid role")
            if (
                not isinstance(command, list)
                or not 1 <= len(command) <= 64
                or any(
                    not isinstance(token, str)
                    or not token
                    or len(token) > 4096
                    or "\0" in token
                    for token in command
                )
            ):
                raise ValueError("invalid command")
            roles.append(role)
        if roles != sorted(set(roles)):
            raise ValueError("roles must be unique and canonical")
        return value, deadline
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_selectors.invalid", "selector request refused"
        ) from exc


def encode_selector_response(
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
    request, request_deadline = decode_selector_request(canonical_json_bytes(request))
    deadline = ActionDispatchDeadline(
        min(deadline.expires_at, request_deadline.expires_at)
    )
    deadline.remaining()
    if (
        build_worker is None
        or request["capability"]["worker_identity"] != expected_worker.uri
    ):
        raise ActionWireError(
            "action_selectors.not_configured", "selector worker authority unavailable"
        )
    inventory = build_worker.observe_tools(require_current=deadline.remaining)
    profile = build_worker.identity
    test_profile = None if test_worker is None else test_worker.identity
    execute_profile = None if execute_worker is None else execute_worker.identity
    accept_profile = None if accept_worker is None else accept_worker.identity
    selected = []
    for item in request["selectors"]:
        tool = build_worker.verify_tool_selector(
            item["role"], tuple(item["command"]), require_current=deadline.remaining
        )
        selected.append(
            {"role": item["role"], "tool_identity": tool.toolchain_identity.uri}
        )
    capability = encode_capability_response(
        request["capability"],
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
        build_standard_tools=inventory.identity,
    )
    if (
        build_worker.identity != profile
        or (test_worker is not None and test_worker.identity != test_profile)
        or (execute_worker is not None and execute_worker.identity != execute_profile)
        or (accept_worker is not None and accept_worker.identity != accept_profile)
        or build_worker.observe_tools(require_current=deadline.remaining) != inventory
    ):
        raise ActionWireError(
            "action_selectors.changed", "selector worker changed during verification"
        )
    content = canonical_json_bytes(
        dict(
            schema=_RESPONSE,
            request_identity=canonical_identity(request).uri,
            capability=json.loads(capability),
            selected=selected,
        )
    )
    if len(content) > MAX_CAPABILITY_BYTES:
        raise ActionWireError(
            "action_selectors.oversized", "selector response exceeds bound"
        )
    deadline.remaining()
    return content


def decode_selector_response(content, request_content, expected_receiver, observed):
    request, deadline = decode_selector_request(request_content)
    if not isinstance(observed, WorkerToolObservation):
        raise ActionWireError(
            "action_selectors.not_admitted", "selector inventory required"
        )
    admitted = observed.capability
    if (
        not timedelta(0)
        <= datetime.now(UTC) - admitted.observed_at
        <= timedelta(minutes=5)
        or admitted.build_standard_tools != observed.tools.identity
    ):
        raise ActionWireError(
            "action_selectors.not_admitted", "current selector inventory required"
        )
    value = _load(
        content, {"schema", "request_identity", "capability", "selected"}, _RESPONSE
    )
    try:
        capability = decode_capability_response(
            canonical_json_bytes(value["capability"]),
            canonical_json_bytes(request["capability"]),
            expected_receiver,
        )
        tools = {
            tool.role: tool.toolchain_identity.uri for tool in observed.tools.tools
        }
        expected = [
            {"role": item["role"], "tool_identity": tools[item["role"]]}
            for item in request["selectors"]
        ]
        if (
            value["request_identity"] != canonical_identity(request).uri
            or capability.capability_identity != admitted.capability_identity
            or value["selected"] != expected
        ):
            raise ValueError("selector response differs from admitted request")
        deadline.remaining()
        return canonical_identity(
            dict(
                schema=SELECTOR_PROTOCOL,
                capability=capability.capability_identity.uri,
                inventory=observed.tools.identity.uri,
                selectors=request["selectors"],
                selected=expected,
            )
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_selectors.invalid", "selector response refused"
        ) from exc


def probe_command_tool_selectors(
    worker, observed, deadline, selectors, *, cwd, environment=None
):
    deadline.remaining()
    deadline = ActionDispatchDeadline(
        min(
            deadline.expires_at,
            datetime.now(UTC) + timedelta(seconds=ACTION_OBSERVATION_TIMEOUT_SECONDS),
        )
    )
    expected = receiver_code_identity(deadline=deadline)
    observed.capability.require_current(worker, expected)
    request = canonical_json_bytes(
        dict(
            schema=_REQUEST,
            capability=dict(
                schema="literate-ai/action-capability-request@1",
                worker_identity=worker.identity.uri,
                nonce=secrets.token_hex(16),
                deadline=deadline.to_dict(),
            ),
            selectors=[
                dict(role=role, command=list(command))
                for role, command in sorted(selectors.items())
            ],
        )
    )
    parsed, _ = decode_selector_request(request)
    if not {item["role"] for item in parsed["selectors"]}.issubset(
        {tool.role for tool in observed.tools.tools}
    ):
        raise ActionWireError(
            "action_selectors.not_admitted", "requested role is not admitted"
        )
    content = run_command_observation(
        worker,
        deadline,
        request,
        cwd=cwd,
        environment=environment,
        mode="--verify-tool-selectors",
        protocol=SELECTOR_PROTOCOL,
    )
    observed.capability.require_current(worker, expected)
    return decode_selector_response(content, request, expected, observed)

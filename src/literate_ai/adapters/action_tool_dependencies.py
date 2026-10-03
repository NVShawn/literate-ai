"""Challenged native dependency evidence from a privately configured worker."""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_capabilities import (
    MAX_CAPABILITY_BYTES,
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
from literate_ai.adapters.worker_tool_dependencies import (
    MAX_DEPENDENCY_BYTES,
    WorkerToolDependencies,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

MAX_DEPENDENCY_RESPONSE_BYTES = MAX_DEPENDENCY_BYTES + 65536
DEPENDENCY_PROTOCOL = "literate-ai/worker-tool-dependencies@1"
_REQUEST = "literate-ai/worker-tool-dependency-request@1"
_RESPONSE = "literate-ai/worker-tool-dependency-response@1"


def _load(content, maximum, fields, schema):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if not isinstance(content, bytes) or len(content) > maximum:
            raise ValueError("document exceeds bound")
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
            "action_dependencies.invalid", "dependency envelope refused"
        ) from exc


def decode_dependency_request(content):
    value = _load(
        content,
        MAX_CAPABILITY_BYTES,
        {"schema", "capability", "tools", "root", "expected"},
        _REQUEST,
    )
    try:
        _, deadline = decode_capability_request(
            canonical_json_bytes(value["capability"])
        )
        tools = value["tools"]
        if (
            not isinstance(tools, list)
            or not 1 <= len(tools) <= 256
            or tools != sorted(set(tools))
        ):
            raise ValueError("invalid selected tools")
        for tool in tools:
            ContentIdentity.parse_uri(tool)
        if (
            not isinstance(value["root"], str)
            or not value["root"]
            or len(value["root"]) > 4096
        ):
            raise ValueError("invalid graph root")
        if value["expected"] is not None:
            ContentIdentity.parse_uri(value["expected"])
        return value, deadline
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_dependencies.invalid", "dependency request refused"
        ) from exc


def encode_dependency_response(
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
    request, deadline = decode_dependency_request(canonical_json_bytes(request))
    if (
        request["capability"]["worker_identity"] != expected_worker.uri
        or build_worker is None
    ):
        raise ActionWireError(
            "action_dependencies.not_configured",
            "worker dependency authority is unavailable",
        )
    profile = build_worker.identity
    test_profile = None if test_worker is None else test_worker.identity
    execute_profile = None if execute_worker is None else execute_worker.identity
    accept_profile = None if accept_worker is None else accept_worker.identity
    inventory = build_worker.observe_tools(require_current=deadline.remaining)
    graph = build_worker.observe_tool_dependencies(
        tuple(ContentIdentity.parse_uri(item) for item in request["tools"]),
        root_ref=request["root"],
        require_current=deadline.remaining,
        expected_identity=None
        if request["expected"] is None
        else ContentIdentity.parse_uri(request["expected"]),
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
    ):
        raise ActionWireError(
            "action_dependencies.changed", "worker profile changed during capture"
        )
    content = canonical_json_bytes(
        dict(
            schema=_RESPONSE,
            request_identity=canonical_identity(request).uri,
            capability=json.loads(capability),
            dependencies=json.loads(graph.document),
        )
    )
    if len(content) > MAX_DEPENDENCY_RESPONSE_BYTES:
        raise ActionWireError(
            "action_dependencies.oversized", "dependency response exceeds bound"
        )
    deadline.remaining()
    return content


@dataclass(frozen=True)
class WorkerDependencyObservation:
    capability: ActionWorkerCapabilities
    dependencies: WorkerToolDependencies


def decode_dependency_response(content, request_content, expected_receiver, admitted):
    request, deadline = decode_dependency_request(request_content)
    if (
        not isinstance(admitted, ActionWorkerCapabilities)
        or admitted.build_standard_tools is None
        or not timedelta(0)
        <= datetime.now(UTC) - admitted.observed_at
        <= timedelta(minutes=5)
    ):
        raise ActionWireError(
            "action_dependencies.not_admitted",
            "current Standard tool admission required",
        )
    value = _load(
        content,
        MAX_DEPENDENCY_RESPONSE_BYTES,
        {"schema", "request_identity", "capability", "dependencies"},
        _RESPONSE,
    )
    try:
        capability = decode_capability_response(
            canonical_json_bytes(value["capability"]),
            canonical_json_bytes(request["capability"]),
            expected_receiver,
        )
        graph = WorkerToolDependencies(canonical_json_bytes(value["dependencies"]))
        if (
            value["request_identity"] != canonical_identity(request).uri
            or capability.capability_identity != admitted.capability_identity
            or value["dependencies"]["tools"] != request["tools"]
            or value["dependencies"]["root"] != request["root"]
            or not set(request["tools"]).issubset(
                {item.uri for item in admitted.build_toolchains}
            )
            or (
                request["expected"] is not None
                and graph.identity.uri != request["expected"]
            )
        ):
            raise ValueError("dependency response differs from selected authority")
        deadline.remaining()
        return WorkerDependencyObservation(capability, graph)
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_dependencies.invalid", "dependency response refused"
        ) from exc


def probe_command_tool_dependencies(
    worker,
    admitted,
    deadline,
    identities,
    *,
    root_ref,
    cwd,
    environment=None,
    expected_identity=None,
):
    deadline.remaining()
    deadline = ActionDispatchDeadline(
        min(deadline.expires_at, datetime.now(UTC) + timedelta(seconds=60))
    )
    expected = receiver_code_identity(deadline=deadline)
    admitted.require_current(worker, expected)
    if admitted.build_standard_tools is None or not set(identities).issubset(
        set(admitted.build_toolchains)
    ):
        raise ActionWireError(
            "action_dependencies.not_admitted", "selected tools lack Standard admission"
        )
    request = canonical_json_bytes(
        dict(
            schema=_REQUEST,
            capability=dict(
                schema="literate-ai/action-capability-request@1",
                worker_identity=worker.identity.uri,
                nonce=secrets.token_hex(16),
                deadline=deadline.to_dict(),
            ),
            tools=[item.uri for item in identities],
            root=root_ref,
            expected=None if expected_identity is None else expected_identity.uri,
        )
    )
    decode_dependency_request(request)
    response = run_command_observation(
        worker,
        deadline,
        request,
        cwd=cwd,
        environment=environment,
        mode="--describe-tool-dependencies",
        protocol=DEPENDENCY_PROTOCOL,
        stdout_limit_bytes=MAX_DEPENDENCY_RESPONSE_BYTES,
    )
    admitted.require_current(worker, expected)
    return decode_dependency_response(response, request, expected, admitted)

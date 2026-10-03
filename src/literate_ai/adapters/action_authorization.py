"""Bounded authorization construction from controller-selected policy inputs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.application.standard_authorization import StandardAuthorizationInputs
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildIntent,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes

AUTHORIZATION_INPUTS_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-authorization-action-inputs"
)
_FIELDS = frozenset(
    {
        "schema",
        "execution_plan_identity",
        "generation_plan_identity",
        "intent",
        "index_result",
        "issued_at",
    }
)


def _invalid():
    raise ActionWireError(
        "action_authorization.invalid", "authorization inputs are invalid"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load(content):
    if not isinstance(content, bytes) or len(content) > MAX_ACTION_RECORD_BYTES:
        _invalid()
    value = json.loads(content, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        _invalid()
    return value


def _inputs(document):
    if set(document) != _FIELDS or document["schema"] != AUTHORIZATION_INPUTS_SCHEMA:
        _invalid()
    execution = ContentIdentity.parse_uri(document["execution_plan_identity"])
    generation = ContentIdentity.parse_uri(document["generation_plan_identity"])
    intent = StandardComponentBuildIntent.from_dict(document["intent"])
    expected_index = {
        "schema": "literate-ai/disabled-source-index@1",
        "component_revision": intent.component_revision.uri,
        "source": intent.source_tree_identity.uri,
    }
    if document["index_result"] != expected_index:
        _invalid()
    issued = datetime.fromisoformat(document["issued_at"])
    if issued.tzinfo is None:
        _invalid()
    return (
        execution,
        generation,
        StandardAuthorizationInputs(
            intent, record_identity(canonical_json_bytes(expected_index)), issued
        ),
    )


def authorization_action_inputs(execution, generation, intent, index_result, issued_at):
    document = {
        "schema": AUTHORIZATION_INPUTS_SCHEMA,
        "execution_plan_identity": execution.uri,
        "generation_plan_identity": generation.uri,
        "intent": intent.to_dict(),
        "index_result": _load(index_result),
        "issued_at": issued_at.isoformat(),
    }
    content = canonical_json_bytes(document)
    _inputs(_load(content))
    return content


def execute_authorization_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
) -> bytes:
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_authorization.worker_mismatch", "receiver binds another worker"
        )
    if request.action.kind is not LifecycleActionKind.AUTHORIZE:
        raise ActionWireError(
            "action_authorization.unsupported_phase", "receiver requires AUTHORIZE"
        )
    if (
        request.action.action_id
        != lifecycle_action_id(
            request.action.component_revision, LifecycleActionKind.AUTHORIZE
        )
        or request.action.predecessor_ids
        != (
            lifecycle_action_id(
                request.action.component_revision, LifecycleActionKind.BUILD_INTENT
            ),
        )
        or request.deadline_identity != deadline.identity
        or len(request.predecessor_result_identities) != 1
    ):
        _invalid()
    deadline.remaining()
    required = {request.action.payload_identity, *request.predecessor_result_identities}
    if set(records) != required or any(
        not isinstance(value, bytes) for value in records.values()
    ):
        _invalid()
    if sum(map(len, records.values())) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(value) != key for key, value in records.items()
    ):
        _invalid()
    try:
        execution, generation, inputs = _inputs(
            _load(records[request.predecessor_result_identities[0]])
        )
        expected_payload = lifecycle_action_payload(
            execution,
            inputs.intent.component_revision,
            LifecycleActionKind.AUTHORIZE,
            generation,
        )
        if (
            _load(records[request.action.payload_identity]) != expected_payload
            or inputs.intent.component_revision != request.action.component_revision
        ):
            _invalid()
        authorization = inputs.authorize()
        authorization.grant.require_valid(
            inputs.intent.build_request, now=datetime.now(UTC)
        )
        result = canonical_json_bytes(authorization.to_dict())
        if len(result) > MAX_ACTION_RECORD_BYTES:
            _invalid()
        deadline.remaining()
        authorization.grant.require_valid(
            inputs.intent.build_request, now=datetime.now(UTC)
        )
        return result
    except ActionWireError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
        RecursionError,
    ) as exc:
        raise ActionWireError(
            "action_authorization.invalid", "authorization input authority was refused"
        ) from exc

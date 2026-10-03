"""Bounded build-intent execution from indexed source and accepted providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    MAX_ACTION_RECORDS,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.application.standard_build_intent import StandardBuildIntentInputs
from literate_ai.contracts import StandardComponentAcceptanceEvidence
from literate_ai.contracts.executable_components import (
    ComponentCommandContract,
    ComponentExecutionPlan,
    GeneratedSourceCandidate,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes

INTENT_INPUTS_SCHEMA = "urn:literate-ai:schema:v2:standard-build-intent-action-inputs"
_FIELDS = frozenset(
    {
        "schema",
        "execution_plan",
        "candidate",
        "contract",
        "native_sdk_inputs",
        "dependency_resolution",
        "index_result",
    }
)


def _invalid():
    raise ActionWireError(
        "action_intent.invalid", "build-intent input custody is invalid"
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
    try:
        value = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_intent.invalid", "invalid build-intent JSON"
        ) from exc
    if not isinstance(value, dict):
        _invalid()
    return value


def _index_inputs(content):
    value = _load(content)
    if set(value) != _FIELDS or value["schema"] != INTENT_INPUTS_SCHEMA:
        _invalid()
    if (
        not isinstance(value["native_sdk_inputs"], list)
        or len(value["native_sdk_inputs"]) > 4096
    ):
        _invalid()
    execution = ComponentExecutionPlan.from_dict(value["execution_plan"])
    candidate = GeneratedSourceCandidate.from_dict(value["candidate"])
    contract = ComponentCommandContract.from_dict(value["contract"])
    generations = {item.component_revision: item for item in execution.generation_plans}
    generation = generations.get(candidate.component_revision)
    if generation is None or (
        candidate.component_generation_plan_identity != generation.identity
        or candidate.generation_key_identity != generation.generation_key.identity
        or contract.component_revision != candidate.component_revision
        or value["index_result"]
        != {
            "schema": "literate-ai/disabled-source-index@1",
            "component_revision": candidate.component_revision.uri,
            "source": candidate.tree_identity.uri,
        }
    ):
        _invalid()
    mode = value["dependency_resolution"]
    if not isinstance(mode, str) or mode not in {"none", "npm", "python"}:
        _invalid()
    inputs = StandardBuildIntentInputs(
        generation.identity,
        candidate,
        contract,
        (),
        (),
        tuple(ContentIdentity.parse_uri(item) for item in value["native_sdk_inputs"]),
        mode,
    )
    return execution, inputs


def _providers(action, handoffs):
    index_id = lifecycle_action_id(action.component_revision, LifecycleActionKind.INDEX)
    if set(handoffs) != set(action.predecessor_ids) or index_id not in handoffs:
        _invalid()
    exports = []
    for predecessor in action.predecessor_ids:
        if predecessor == index_id:
            continue
        acceptance = StandardComponentAcceptanceEvidence.from_dict(
            _load(handoffs[predecessor])
        )
        if (
            lifecycle_action_id(
                acceptance.component_revision, LifecycleActionKind.ACCEPT
            )
            != predecessor
        ):
            _invalid()
        exports.extend(acceptance.build.exports)
    if len(exports) > 4096:
        _invalid()
    return tuple(sorted(exports, key=lambda item: item.identity.uri))


def build_intent_action_predecessors(
    execution: ComponentExecutionPlan,
    inputs: StandardBuildIntentInputs,
    index_result: bytes,
    accepted_providers: tuple[StandardComponentAcceptanceEvidence, ...],
) -> dict[str, bytes]:
    """Bind each actual predecessor to its canonical action; never infer acceptance."""
    index_id = lifecycle_action_id(
        inputs.candidate.component_revision, LifecycleActionKind.INDEX
    )
    content = canonical_json_bytes(
        {
            "schema": INTENT_INPUTS_SCHEMA,
            "execution_plan": execution.to_dict(),
            "candidate": inputs.candidate.to_dict(),
            "contract": inputs.contract.to_dict(),
            "native_sdk_inputs": [item.uri for item in inputs.native_sdk_inputs],
            "dependency_resolution": inputs.dependency_resolution,
            "index_result": _load(index_result),
        }
    )
    parsed_execution, parsed_inputs = _index_inputs(content)
    if (
        parsed_execution.identity != execution.identity
        or inputs.generation_plan_identity != parsed_inputs.generation_plan_identity
    ):
        _invalid()
    handoffs = {index_id: content}
    for acceptance in accepted_providers:
        key = lifecycle_action_id(
            acceptance.component_revision, LifecycleActionKind.ACCEPT
        )
        if key in handoffs:
            _invalid()
        handoffs[key] = canonical_json_bytes(acceptance.to_dict())
    action = next(
        node
        for node in plan_lifecycle_action_dag(execution, worker_ids=("encoder",))
        if node.component_revision == inputs.candidate.component_revision
        and node.kind is LifecycleActionKind.BUILD_INTENT
    )
    if inputs.packages or _providers(action, handoffs) != inputs.providers:
        _invalid()
    if (
        len(handoffs) + 1 > MAX_ACTION_RECORDS
        or sum(map(len, handoffs.values())) > MAX_ACTION_RECORD_BYTES
    ):
        _invalid()
    return handoffs


def execute_build_intent_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
) -> bytes:
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_intent.worker_mismatch", "receiver binds another worker"
        )
    if request.action.kind is not LifecycleActionKind.BUILD_INTENT:
        raise ActionWireError(
            "action_intent.unsupported_phase", "receiver requires BUILD_INTENT"
        )
    if request.deadline_identity != deadline.identity:
        _invalid()
    deadline.remaining()
    required = {request.action.payload_identity, *request.predecessor_result_identities}
    if (
        set(records) != required
        or len(records) > MAX_ACTION_RECORDS
        or len(request.predecessor_result_identities)
        != len(request.action.predecessor_ids)
        or len(set(request.predecessor_result_identities))
        != len(request.predecessor_result_identities)
        or any(not isinstance(content, bytes) for content in records.values())
    ):
        _invalid()
    if sum(map(len, records.values())) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(content) != identity for identity, content in records.items()
    ):
        _invalid()
    try:
        handoffs = {
            action: records[identity]
            for action, identity in zip(
                request.action.predecessor_ids,
                request.predecessor_result_identities,
                strict=True,
            )
        }
        index_id = lifecycle_action_id(
            request.action.component_revision, LifecycleActionKind.INDEX
        )
        execution, inputs = _index_inputs(handoffs[index_id])
        action = next(
            node
            for node in plan_lifecycle_action_dag(
                execution,
                worker_ids=request.action.eligible_worker_ids,
                cache_affinity={
                    request.action.action_id: request.action.cache_affinity_worker_ids
                },
            )
            if node.action_id == request.action.action_id
        )
        if action != request.action or _load(
            records[action.payload_identity]
        ) != lifecycle_action_payload(
            execution.identity,
            inputs.candidate.component_revision,
            LifecycleActionKind.BUILD_INTENT,
            inputs.generation_plan_identity,
        ):
            _invalid()

        intent = replace(inputs, providers=_providers(action, handoffs)).create()
        result = canonical_json_bytes(intent.to_dict())
        if len(result) > MAX_ACTION_RECORD_BYTES:
            _invalid()
        deadline.remaining()
        return result
    except ActionWireError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
        StopIteration,
    ) as exc:
        raise ActionWireError(
            "action_intent.invalid", "build-intent authority was refused"
        ) from exc

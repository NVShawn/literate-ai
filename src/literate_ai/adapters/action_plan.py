"""Bounded PLAN phase over exact portable authorization and command records."""

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
from literate_ai.application.action_dag_planning import lifecycle_action_id
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.application.standard_plan_finalization import (
    finalize_standard_component_plan,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes

PLAN_INPUTS_SCHEMA = "urn:literate-ai:schema:v2:standard-plan-action-inputs"
MAX_PLAN_ARTIFACTS = 4096
_FIELDS = frozenset(
    {
        "schema",
        "execution_plan_identity",
        "generation_plan_identity",
        "intent",
        "authorization",
        "contract",
        "provider_artifacts",
        "package_artifacts",
        "dependency_resolution",
    }
)


def _invalid():
    raise ActionWireError("action_plan.invalid", "plan phase inputs are invalid")


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
            "action_plan.invalid", "plan phase JSON is invalid"
        ) from exc
    if not isinstance(value, dict):
        _invalid()
    return value


def _inputs(document):
    if set(document) != _FIELDS or document["schema"] != PLAN_INPUTS_SCHEMA:
        _invalid()
    for key in ("provider_artifacts", "package_artifacts"):
        if (
            not isinstance(document[key], list)
            or len(document[key]) > MAX_PLAN_ARTIFACTS
        ):
            _invalid()
    execution = ContentIdentity.parse_uri(document["execution_plan_identity"])
    generation = ContentIdentity.parse_uri(document["generation_plan_identity"])
    intent = StandardComponentBuildIntent.from_dict(document["intent"])
    authorization = StandardBuildAuthorization.from_dict(document["authorization"])
    contract = ComponentCommandContract.from_dict(document["contract"])
    providers = tuple(
        ArtifactExport.from_dict(value) for value in document["provider_artifacts"]
    )
    packages = tuple(
        ArtifactExport.from_dict(value) for value in document["package_artifacts"]
    )
    mode = document["dependency_resolution"]
    if not isinstance(mode, str) or mode not in {"none", "npm", "python"}:
        _invalid()
    return (
        execution,
        generation,
        intent,
        authorization,
        contract,
        providers,
        packages,
        mode,
    )


def plan_action_inputs(
    execution_plan_identity: ContentIdentity,
    generation_plan_identity: ContentIdentity,
    intent: StandardComponentBuildIntent,
    authorization: StandardBuildAuthorization,
    contract: ComponentCommandContract,
    provider_artifacts: tuple[ArtifactExport, ...],
    package_artifacts: tuple[ArtifactExport, ...],
    *,
    dependency_resolution: str,
) -> bytes:
    """Encode the authorized predecessor's exact portable inputs, not host paths."""
    document = {
        "schema": PLAN_INPUTS_SCHEMA,
        "execution_plan_identity": execution_plan_identity.uri,
        "generation_plan_identity": generation_plan_identity.uri,
        "intent": intent.to_dict(),
        "authorization": authorization.to_dict(),
        "contract": contract.to_dict(),
        "provider_artifacts": [item.to_dict() for item in provider_artifacts],
        "package_artifacts": [item.to_dict() for item in package_artifacts],
        "dependency_resolution": dependency_resolution,
    }
    content = canonical_json_bytes(document)
    _inputs(_load(content))
    return content


def execute_plan_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
) -> bytes:
    """Finalize one authorized plan; this phase executes no generated host code."""
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_plan.worker_mismatch", "receiver binds another worker"
        )
    if request.action.action_id != lifecycle_action_id(
        request.action.component_revision, LifecycleActionKind.PLAN
    ) or request.action.predecessor_ids != (
        lifecycle_action_id(
            request.action.component_revision, LifecycleActionKind.AUTHORIZE
        ),
    ):
        _invalid()
    if request.action.kind is not LifecycleActionKind.PLAN:
        raise ActionWireError("action_plan.unsupported_phase", "receiver requires PLAN")
    if (
        request.deadline_identity != deadline.identity
        or len(request.predecessor_result_identities) != 1
    ):
        _invalid()
    deadline.remaining()
    required = {request.action.payload_identity, *request.predecessor_result_identities}
    if set(records) != required or any(
        not isinstance(value, bytes) for value in records.values()
    ):
        _invalid()
    if sum(len(value) for value in records.values()) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(value) != key for key, value in records.items()
    ):
        _invalid()
    try:
        payload = _load(records[request.action.payload_identity])
        if (
            set(payload)
            != {
                "schema",
                "execution_plan_identity",
                "generation_plan_identity",
                "component_revision",
                "kind",
            }
            or payload["schema"] != "literate-ai/lifecycle-action-payload@1"
            or payload["kind"] != LifecycleActionKind.PLAN.value
        ):
            _invalid()
        (
            execution,
            generation,
            intent,
            authorization,
            contract,
            providers,
            packages,
            mode,
        ) = _inputs(_load(records[request.predecessor_result_identities[0]]))
        if (
            execution.uri != payload["execution_plan_identity"]
            or generation.uri != payload["generation_plan_identity"]
            or intent.component_revision.uri != payload["component_revision"]
            or intent.component_revision != request.action.component_revision
            or authorization.grant.classification_digest
            != authorization.index_identity.uri
            or authorization.grant.privileges
            != intent.build_request.requested_privileges
        ):
            _invalid()
        authorization.grant.require_valid(intent.build_request, now=datetime.now(UTC))
        plan = finalize_standard_component_plan(
            intent,
            authorization,
            contract,
            providers,
            packages,
            dependency_resolution=mode,
        )
        deadline.remaining()
        authorization.grant.require_valid(intent.build_request, now=datetime.now(UTC))
        result = canonical_json_bytes(plan.to_dict())
        if len(result) > MAX_ACTION_RECORD_BYTES:
            _invalid()
        deadline.remaining()
        authorization.grant.require_valid(intent.build_request, now=datetime.now(UTC))
        return result
    except ActionWireError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise ActionWireError(
            "action_plan.invalid", "plan input authority was refused"
        ) from exc

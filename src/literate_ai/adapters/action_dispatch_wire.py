"""Bounded data-only wire custody for exact lifecycle-action dispatch."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)
from literate_ai.contracts.execution_dispatch import LIFECYCLE_ACTION_WIRE_PROTOCOL
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

MAX_ACTION_RECORD_BYTES = 16 * 1024 * 1024
MAX_ACTION_WIRE_BYTES = 24 * 1024 * 1024
MAX_ACTION_RECORDS = 4096
ACTION_WIRE_PROTOCOL = LIFECYCLE_ACTION_WIRE_PROTOCOL
_REQUEST_SCHEMA = "literate-ai/lifecycle-action-wire-request@1"
_RESPONSE_SCHEMA = "literate-ai/lifecycle-action-wire-response@1"


class ActionWireError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _invalid() -> None:
    raise ActionWireError(
        "action_wire.invalid", "invalid lifecycle-action wire document"
    )


def _fields(value: object, names: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(names.split()):
        _invalid()
    return value


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load(content: bytes) -> dict:
    if not isinstance(content, bytes) or len(content) > MAX_ACTION_WIRE_BYTES:
        _invalid()
    try:
        value = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_wire.invalid", "invalid lifecycle-action JSON"
        ) from exc
    if not isinstance(value, dict):
        _invalid()
    return value


def _dump(value: dict) -> bytes:
    content = canonical_json_bytes(value)
    if len(content) > MAX_ACTION_WIRE_BYTES:
        raise ActionWireError(
            "action_wire.oversized", "action wire exceeds its byte limit"
        )
    return content


def record_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


def _record(value: object) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) > (MAX_ACTION_RECORD_BYTES + 2) // 3 * 4
    ):
        _invalid()
    try:
        content = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ActionWireError(
            "action_wire.invalid", "invalid action record encoding"
        ) from exc
    if len(content) > MAX_ACTION_RECORD_BYTES:
        _invalid()
    return content


@dataclass(frozen=True, slots=True)
class ActionDispatchDeadline:
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            _invalid()

    def to_dict(self) -> dict:
        return {
            "schema": "literate-ai/lifecycle-action-deadline@1",
            "expires_at": self.expires_at.astimezone(UTC).isoformat(
                timespec="microseconds"
            ),
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def remaining(self, now: datetime | None = None) -> float:
        seconds = (self.expires_at - (now or datetime.now(UTC))).total_seconds()
        if seconds <= 0:
            raise ActionWireError("action_wire.expired", "action deadline has expired")
        if seconds > 86400:
            raise ActionWireError(
                "action_wire.deadline_unbounded", "action deadline exceeds one day"
            )
        return seconds


def _action_document(action: LifecycleActionNode) -> dict:
    return {
        "action_id": action.action_id,
        "component_revision": action.component_revision.uri,
        "kind": action.kind.value,
        "payload_identity": action.payload_identity.uri,
        "predecessor_ids": list(action.predecessor_ids),
        "eligible_worker_ids": list(action.eligible_worker_ids),
        "cache_affinity_worker_ids": list(action.cache_affinity_worker_ids),
    }


def _worker_document(worker: LifecycleActionWorker) -> dict:
    return {
        "worker_id": worker.worker_id,
        "worker_identity": worker.worker_identity.uri,
        "catalog_identity": worker.catalog_identity.uri,
        "observation_identity": worker.observation_identity.uri,
        "slots": worker.slots,
    }


def _validate_records(
    request: LifecycleActionDispatchRequest, records: Mapping[ContentIdentity, bytes]
) -> None:
    required = {request.action.payload_identity, *request.predecessor_result_identities}
    if set(records) != required or len(records) > MAX_ACTION_RECORDS:
        raise ActionWireError(
            "action_wire.inputs_mismatch",
            "action inputs differ from exact predecessor custody",
        )
    if len(request.predecessor_result_identities) != len(
        request.action.predecessor_ids
    ):
        raise ActionWireError(
            "action_wire.inputs_mismatch", "action predecessor records are incomplete"
        )
    size = 0
    for identity, content in records.items():
        if not isinstance(content, bytes):
            raise ActionWireError(
                "action_wire.record_corrupt",
                "action input record differs from its identity",
            )
        size += len(content)
        if size > MAX_ACTION_RECORD_BYTES:
            raise ActionWireError(
                "action_wire.oversized", "action input records exceed their byte limit"
            )
        if record_identity(content) != identity:
            raise ActionWireError(
                "action_wire.record_corrupt",
                "action input record differs from its identity",
            )


def encode_action_request(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
) -> bytes:
    if request.deadline_identity != deadline.identity:
        raise ActionWireError(
            "action_wire.deadline_mismatch", "action request binds another deadline"
        )
    _validate_records(request, records)
    if any(
        len(items) > MAX_ACTION_RECORDS
        for items in (
            request.action.predecessor_ids,
            request.action.eligible_worker_ids,
            request.action.cache_affinity_worker_ids,
            request.predecessor_result_identities,
        )
    ):
        raise ActionWireError(
            "action_wire.oversized", "action fields exceed their item limit"
        )
    return _dump(
        {
            "schema": _REQUEST_SCHEMA,
            "request_identity": request.identity.uri,
            "schedule_identity": request.schedule_identity.uri,
            "action": _action_document(request.action),
            "worker": _worker_document(request.worker),
            "slot": request.slot,
            "predecessor_result_identities": [
                item.uri for item in request.predecessor_result_identities
            ],
            "deadline": deadline.to_dict(),
            "records": [
                {
                    "identity": identity.uri,
                    "content": base64.b64encode(records[identity]).decode("ascii"),
                }
                for identity in sorted(records, key=lambda item: item.uri)
            ],
        }
    )


def decode_action_request(
    content: bytes, *, now: datetime | None = None
) -> tuple[
    LifecycleActionDispatchRequest, ActionDispatchDeadline, dict[ContentIdentity, bytes]
]:
    try:
        value = _fields(
            _load(content),
            "schema request_identity schedule_identity action worker slot "
            "predecessor_result_identities deadline records",
        )
        if value["schema"] != _REQUEST_SCHEMA:
            _invalid()
        action = _fields(
            value["action"],
            "action_id component_revision kind payload_identity predecessor_ids "
            "eligible_worker_ids cache_affinity_worker_ids",
        )
        worker = _fields(
            value["worker"],
            "worker_id worker_identity catalog_identity observation_identity slots",
        )
        date = _fields(value["deadline"], "schema expires_at")
        deadline = ActionDispatchDeadline(datetime.fromisoformat(date["expires_at"]))
        if deadline.to_dict() != date:
            _invalid()
        deadline.remaining(now)
        for items in (
            action["predecessor_ids"],
            action["eligible_worker_ids"],
            action["cache_affinity_worker_ids"],
            value["predecessor_result_identities"],
            value["records"],
        ):
            if not isinstance(items, list) or len(items) > MAX_ACTION_RECORDS:
                _invalid()
        request = LifecycleActionDispatchRequest(
            ContentIdentity.parse_uri(value["schedule_identity"]),
            LifecycleActionNode(
                action["action_id"],
                ContentIdentity.parse_uri(action["component_revision"]),
                LifecycleActionKind(action["kind"]),
                ContentIdentity.parse_uri(action["payload_identity"]),
                tuple(action["predecessor_ids"]),
                tuple(action["eligible_worker_ids"]),
                tuple(action["cache_affinity_worker_ids"]),
            ),
            LifecycleActionWorker(
                worker["worker_id"],
                ContentIdentity.parse_uri(worker["worker_identity"]),
                ContentIdentity.parse_uri(worker["catalog_identity"]),
                ContentIdentity.parse_uri(worker["observation_identity"]),
                worker["slots"],
            ),
            value["slot"],
            tuple(
                ContentIdentity.parse_uri(item)
                for item in value["predecessor_result_identities"]
            ),
            deadline.identity,
        )
        if request.identity.uri != value["request_identity"]:
            raise ActionWireError(
                "action_wire.request_mismatch",
                "wire action differs from its request identity",
            )
        records = {}
        for raw in value["records"]:
            item = _fields(raw, "identity content")
            identity = ContentIdentity.parse_uri(item["identity"])
            if identity in records:
                _invalid()
            records[identity] = _record(item["content"])
        _validate_records(request, records)
        return request, deadline, records
    except (ValueError, TypeError, AttributeError, ActionDagSchedulingError) as exc:
        raise ActionWireError(
            "action_wire.invalid", "invalid typed action request"
        ) from exc


def encode_action_response(
    request: LifecycleActionDispatchRequest,
    *,
    result_record: bytes | None = None,
    failure_code: str | None = None,
) -> bytes:
    if (result_record is None) == (failure_code is None):
        _invalid()
    if result_record is not None and (
        not isinstance(result_record, bytes)
        or len(result_record) > MAX_ACTION_RECORD_BYTES
    ):
        _invalid()
    if failure_code is not None and (
        not isinstance(failure_code, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,126}", failure_code) is None
    ):
        _invalid()
    return _dump(
        {
            "schema": _RESPONSE_SCHEMA,
            "request_identity": request.identity.uri,
            "worker_identity": request.worker.identity.uri,
            "result_identity": None
            if result_record is None
            else record_identity(result_record).uri,
            "result_record": None
            if result_record is None
            else base64.b64encode(result_record).decode("ascii"),
            "failure_code": failure_code,
        }
    )


def decode_action_response(
    content: bytes, request: LifecycleActionDispatchRequest
) -> tuple[LifecycleActionDispatchOutcome, bytes | None]:
    value = _fields(
        _load(content),
        "schema request_identity worker_identity result_identity result_record "
        "failure_code",
    )
    if value["schema"] != _RESPONSE_SCHEMA:
        _invalid()
    if (
        value["request_identity"] != request.identity.uri
        or value["worker_identity"] != request.worker.identity.uri
    ):
        raise ActionWireError(
            "action_wire.response_mismatch",
            "worker response differs from selected request or worker",
        )
    record = None if value["result_record"] is None else _record(value["result_record"])
    expected = encode_action_response(
        request, result_record=record, failure_code=value["failure_code"]
    )
    if _load(expected) != value:
        raise ActionWireError(
            "action_wire.record_corrupt",
            "worker result differs from its content identity",
        )
    return LifecycleActionDispatchOutcome(
        request.identity,
        None if record is None else record_identity(record),
        value["failure_code"],
    ), record

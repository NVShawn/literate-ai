"""Bounded traversal and proof reopening of exact dependency LINK results."""

import json
from collections.abc import Mapping

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_link_proof import reopen_link_component
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.application.action_dag_planning import lifecycle_action_id
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import ContentIdentity


def _invalid():
    raise ActionWireError(
        "action_link.dependencies_invalid", "LINK dependency closure refused"
    )


def reopen_link_result_closure(
    roots,
    *,
    execution_plan_identity,
    records,
    cas,
    deadline,
    admission_guard,
    blob_source=None,
):
    """Verify each reachable Component once; descriptor admission precedes blob IO."""
    if not callable(admission_guard) or (
        blob_source is not None and not callable(blob_source)
    ):
        raise TypeError("LINK closure requires live admission and explicit transport")

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    if not isinstance(records, Mapping):
        _invalid()
    records = dict(records)
    if (
        not isinstance(execution_plan_identity, ContentIdentity)
        or len(records) > MAX_BUILD_EVIDENCE_RECORDS
        or any(
            not isinstance(key, ContentIdentity)
            or not isinstance(raw, bytes)
            or len(raw) > MAX_ACTION_RECORD_BYTES
            for key, raw in records.items()
        )
    ):
        _invalid()
    if sum(len(raw) for raw in records.values()) > MAX_BUILD_EVIDENCE_BYTES:
        _invalid()
    if not isinstance(roots, tuple) or len(roots) > MAX_BUILD_EVIDENCE_RECORDS:
        _invalid()
    pending = list(roots)
    visited, used = {}, set()
    try:
        while pending:
            current()
            action_id, result_id = pending.pop()
            if not isinstance(action_id, str) or not isinstance(
                result_id, ContentIdentity
            ):
                _invalid()
            previous = visited.get(action_id)
            if previous is not None:
                if previous[0] != result_id:
                    _invalid()
                continue
            raw_result = records[result_id]
            if record_identity(raw_result) != result_id:
                _invalid()
            document = json.loads(raw_result)
            input_id = ContentIdentity.parse_uri(document["input_identity"])
            raw_input = records[input_id]
            value = LinkWorkerInput.admit(raw_input, input_id, deadline)
            result = LinkWorkerResult.admit(
                raw_result,
                result_id,
                input_record=raw_input,
                input_identity=input_id,
                deadline=deadline,
            )
            if (
                value.build.execution_plan_identity != execution_plan_identity
                or lifecycle_action_id(
                    value.build.plan.component_revision, LifecycleActionKind.LINK
                )
                != action_id
            ):
                _invalid()
            visited[action_id] = (result_id, input_id, value, result)
            used.update((result_id, input_id))
            pending.extend(value.dependency_links)
        if used != set(records):
            _invalid()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_link.dependencies_invalid", "LINK dependency records refused"
        ) from exc
    ordered = tuple(visited[key] for key in sorted(visited))
    for _, input_id, _, _ in ordered:
        current()
        reopen_link_component(
            input_record=records[input_id],
            input_identity=input_id,
            cas=cas,
            deadline=deadline,
            admission_guard=current,
            blob_source=blob_source,
        )
    current()
    return tuple((value, result) for _, _, value, result in ordered)

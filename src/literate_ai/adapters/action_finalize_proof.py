"""FINALIZE reopens exact PACKAGE bytes and the accepted full Component plans."""

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
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.adapters.action_package_result import import_package_result
from literate_ai.contracts import ContentIdentity


def _invalid():
    raise ActionWireError(
        "action_finalize.proof_invalid", "FINALIZE predecessor records refused"
    )


def reopen_finalize_input(
    *,
    input_record,
    input_identity,
    records,
    cas,
    deadline,
    admission_guard,
    verify_package,
    blob_source=None,
):
    """Return verified intent/package, without granting root execution authority."""
    if not callable(admission_guard) or not callable(verify_package):
        raise TypeError(
            "FINALIZE requires live admission and private package verification"
        )

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    value = FinalizeWorkerInput.admit(input_record, input_identity, deadline)
    if not isinstance(records, Mapping):
        _invalid()
    records = dict(records)
    if (
        len(records) > MAX_BUILD_EVIDENCE_RECORDS
        or any(
            not isinstance(identity, ContentIdentity)
            or not isinstance(raw, bytes)
            or len(raw) > MAX_ACTION_RECORD_BYTES
            for identity, raw in records.items()
        )
        or sum(map(len, records.values())) > MAX_BUILD_EVIDENCE_BYTES
        or any(record_identity(raw) != identity for identity, raw in records.items())
    ):
        _invalid()
    try:
        package_raw = records.pop(value.package_result_identity)
        plans = []
        for _, identity in value.package_input.link_results:
            current()
            result_raw = records[identity]
            link_id = ContentIdentity.parse_uri(
                json.loads(result_raw)["input_identity"]
            )
            link_raw = records[link_id]
            linked = LinkWorkerInput.admit(link_raw, link_id, deadline)
            LinkWorkerResult.admit(
                result_raw,
                identity,
                input_record=link_raw,
                input_identity=link_id,
                deadline=deadline,
            )
            plans.append(linked.build.plan)
        if tuple(sorted(plans, key=lambda plan: plan.component_revision.uri)) != (
            value.project_plan.components
        ):
            raise ActionWireError(
                "action_finalize.plan_mismatch",
                "FINALIZE project plans differ from accepted LINK plans",
            )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_finalize.proof_invalid", "FINALIZE predecessor records refused"
        ) from exc
    # Descriptors above only compare intent. Import reopens every LINK's complete
    # proof and rejects unused records before accepting any PACKAGE result.
    package_input = value.package_input.to_bytes()
    package = import_package_result(
        content=package_raw,
        result_identity=value.package_result_identity,
        input_record=package_input,
        input_identity=record_identity(package_input),
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        verify_package=verify_package,
        blob_source=blob_source,
    )
    current()
    return value, package

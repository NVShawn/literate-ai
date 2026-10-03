"""Fetch exact worker EXECUTE evidence before publishing controller EXECUTE custody."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.contracts import ContentIdentity
from literate_ai.storage.cas import BlobNotFoundError


def import_execute_result(
    *,
    content,
    result_identity,
    input_record,
    input_identity,
    deadline,
    ports,
    cas,
    admission_guard,
    blob_source=None,
):
    """Import verified custody without local execution or implicit return transport."""
    if not callable(admission_guard) or (
        blob_source is not None and not callable(blob_source)
    ):
        raise TypeError("EXECUTE import requires a live guard and explicit blob source")
    admitted = ExecuteWorkerInput.admit(input_record, input_identity, deadline)
    result = ExecuteWorkerResult.admit(
        content,
        result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
    )
    ports.retained_evidence_records()
    build = admitted.build_input

    def current():
        deadline.remaining()
        if ports.build_execution_inputs(build.plan) != build.inputs:
            raise ActionWireError(
                "action_execute.input_changed", "EXECUTE controller inputs changed"
            )

    def worker_current():
        current()
        admission_guard()
        current()

    worker_current()

    def fetch(reference):
        current()
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            payload = blob_source(reference)
            current()
            if (
                not isinstance(payload, bytes)
                or len(payload) != reference.size
                or record_identity(payload).uri != reference.identity
            ):
                raise ActionWireError(
                    "action_execute.record_invalid", "EXECUTE evidence bytes differ"
                ) from None
            if cas.put_bytes(payload, media_type=reference.media_type) != reference:
                raise ActionWireError(
                    "action_execute.record_invalid",
                    "EXECUTE evidence reference differs",
                ) from None
        payload = cas.get_bytes(reference)
        current()
        return payload

    records = tuple(
        (ContentIdentity.parse_uri(reference.identity), fetch(reference))
        for reference in result.evidence_records
    )
    worker_current()
    # The bounded response must survive with its supporting records before the
    # controller publishes EXECUTE. Failed retention leaves it unregistered.
    ports.retain_evidence_record(result_identity, content)
    return ports.admit_transferred_execution(
        plan=build.plan,
        exports=admitted.build_result.evidence.exports,
        evidence=result.evidence,
        records=records,
        admission_guard=worker_current,
        scope=admitted.scope,
        provider_artifacts=admitted.provider_artifacts,
    )

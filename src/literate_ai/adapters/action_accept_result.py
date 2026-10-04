"""Fetch exact worker ACCEPT evidence before publishing controller ACCEPT custody."""

from literate_ai.adapters.action_accept_record import AcceptWorkerInput
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.contracts import ContentIdentity
from literate_ai.storage.cas import BlobNotFoundError


def import_accept_result(
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
        raise TypeError("ACCEPT import requires a live guard and explicit blob source")
    admitted = AcceptWorkerInput.admit(input_record, input_identity, deadline)
    result = AcceptWorkerResult.admit(
        content,
        result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
    )
    ports.retained_evidence_records()
    build = admitted.execution_input.build_input

    def current():
        deadline.remaining()
        if ports.build_execution_inputs(build.plan) != build.inputs:
            raise ActionWireError(
                "action_accept.input_changed", "ACCEPT controller inputs changed"
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
                    "action_accept.record_invalid", "ACCEPT evidence bytes differ"
                ) from None
            if cas.put_bytes(payload, media_type=reference.media_type) != reference:
                raise ActionWireError(
                    "action_accept.record_invalid",
                    "ACCEPT evidence reference differs",
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
    # lifecycle receives ACCEPT. Failed retention cannot return a receipt.
    ports.retain_evidence_record(result_identity, content)
    return ports.admit_transferred_acceptance(
        plan=build.plan,
        test_identity=admitted.test_result.evidence.identity,
        execution_identity=admitted.execution_result.evidence.identity,
        generation_plan=build.generation_plan,
        evidence=result.evidence,
        records=records,
        admission_guard=worker_current,
    )

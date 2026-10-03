"""Fetch exact worker TEST evidence before publishing controller TEST custody."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_record import TestWorkerInput, TestWorkerResult
from literate_ai.contracts import ContentIdentity
from literate_ai.storage.cas import BlobNotFoundError


def import_test_result(
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
    """Import is data custody; no local TEST execution or implicit return transport."""
    if not callable(admission_guard) or (
        blob_source is not None and not callable(blob_source)
    ):
        raise TypeError("TEST import requires a live guard and explicit blob source")
    admitted = TestWorkerInput.admit(input_record, input_identity, deadline)
    result = TestWorkerResult.admit(
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
                "action_test.input_changed", "TEST controller inputs changed"
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
                    "action_test.record_invalid", "TEST evidence bytes differ"
                ) from None
            if cas.put_bytes(payload, media_type=reference.media_type) != reference:
                raise ActionWireError(
                    "action_test.record_invalid", "TEST evidence reference differs"
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
    # controller can publish any TEST result. Failed retention leaves it unregistered.
    ports.retain_evidence_record(result_identity, content)
    return ports.admit_transferred_tests(
        plan=build.plan,
        exports=admitted.build_result.evidence.exports,
        evidence=result.evidence,
        records=records,
        admission_guard=worker_current,
    )

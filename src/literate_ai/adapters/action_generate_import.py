"""Controller admission of complete worker source and generation proof."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
from literate_ai.contracts import ContentIdentity
from literate_ai.storage.cas import BlobNotFoundError


def import_generate_result(
    *,
    content,
    result_identity,
    input_record,
    input_identity,
    deadline,
    cas,
    admission_guard,
    blob_source=None,
):
    """Verify the complete bounded proof before publishing it to controller CAS.

    This imports immutable records only. It neither registers a source workspace
    nor publishes an accepted source-cache entry; those require later admission.
    """
    if not callable(admission_guard) or (
        blob_source is not None and not callable(blob_source)
    ):
        raise TypeError(
            "GENERATE import requires live admission and explicit transport"
        )

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    result = GenerateWorkerResult.admit(
        content,
        result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
    )
    records = []
    for reference in result.evidence_records:
        current()
        try:
            payload = cas.get_bytes(reference)
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
                "action_generate.record_invalid", "GENERATE evidence bytes differ"
            )
        records.append((ContentIdentity.parse_uri(reference.identity), payload))
    result.verify_records(tuple(records))
    current()
    # Nothing fetched is made visible before every record and its relationships
    # have been verified. CAS failures can leave immutable blobs, never a result.
    for reference, (_, payload) in zip(result.evidence_records, records, strict=True):
        current()
        if cas.put_bytes(payload, media_type=reference.media_type) != reference:
            raise ActionWireError(
                "action_generate.record_invalid", "GENERATE evidence reference differs"
            )
    current()
    if cas.put_bytes(content).identity != result_identity.uri:
        raise ActionWireError(
            "action_generate.result_invalid", "GENERATE result retention differs"
        )
    current()
    return result

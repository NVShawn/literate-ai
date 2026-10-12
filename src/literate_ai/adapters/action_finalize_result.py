"""Import FINALIZE after upstream custody, byte and private proof checks."""

from types import MappingProxyType

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_proof import reopen_finalize_input
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.storage.cas import BlobNotFoundError


def import_finalize_result(
    *,
    content,
    result_identity,
    input_record,
    input_identity,
    records,
    cas,
    deadline,
    admission_guard,
    verify_package,
    verify_stages,
    blob_source=None,
):
    """Private stage verifier must raise on missing or invalid oracle evidence.

    No records are published as accepted evidence here. CAS storage is only byte
    custody. The verifier receives an immutable snapshot of every returned record
    and the reopened exact input; it must verify the complete stage proof closure.
    """
    if (
        not callable(admission_guard)
        or not callable(verify_package)
        or not callable(verify_stages)
        or (blob_source is not None and not callable(blob_source))
    ):
        raise TypeError("FINALIZE import requires live admission and private verifiers")

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    returned = FinalizeWorkerResult.admit(
        content,
        result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
    )
    value, package = reopen_finalize_input(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        verify_package=verify_package,
        blob_source=blob_source,
    )
    if returned.evidence.package_result != package:
        raise ActionWireError(
            "action_finalize.package_mismatch", "returned package custody differs"
        )
    verified = {}
    for reference in returned.evidence_records:
        current()
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            raw = blob_source(reference)
            current()
            if (
                not isinstance(raw, bytes)
                or len(raw) != reference.size
                or record_identity(raw).uri != reference.identity
            ):
                raise ActionWireError(
                    "action_finalize.record_invalid", "returned evidence bytes differ"
                ) from None
            if cas.put_bytes(raw, media_type=reference.media_type) != reference:
                raise ActionWireError(
                    "action_finalize.record_invalid", "returned CAS custody differs"
                ) from None
        raw = cas.get_bytes(reference)
        current()
        if len(raw) != reference.size or record_identity(raw).uri != reference.identity:
            raise ActionWireError(
                "action_finalize.record_invalid", "cached evidence bytes differ"
            )
        verified[ContentIdentity.parse_uri(reference.identity)] = raw
    evidence = returned.evidence
    if verified[evidence.identity] != canonical_json_bytes(evidence.to_dict()):
        raise ActionWireError(
            "action_finalize.record_invalid", "root evidence record differs"
        )
    current()
    verify_stages(value, evidence, MappingProxyType(verified))
    current()
    return evidence

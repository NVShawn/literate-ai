"""Load the bounded exact FINALIZE predecessor record set from staged CAS."""

import json

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.blobs import BlobRef


def load_finalize_records(value, cas, deadline, *, admission_guard):
    """Load descriptors only; execution must still reopen all predecessor proof.

    Identity-only records acquire a bounded size from local CAS metadata, then use
    the CAS same-handle verified reader. No cache enumeration or remote fallback.
    """
    if not isinstance(value, FinalizeWorkerInput) or not callable(admission_guard):
        raise TypeError(
            "FINALIZE proof loading requires admitted intent and live guard"
        )
    records = {}
    total = 0

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    def read(identity):
        nonlocal total
        current()
        if identity in records:
            return records[identity]
        path = cas.path_for(BlobRef(identity.digest, 0))
        size = path.stat(follow_symlinks=False).st_size
        if (
            size > MAX_ACTION_RECORD_BYTES
            or total + size > MAX_BUILD_EVIDENCE_BYTES
            or len(records) >= MAX_BUILD_EVIDENCE_RECORDS
        ):
            raise ActionWireError(
                "action_finalize.proof_oversized", "staged proof exceeds bounds"
            )
        raw = cas.get_bytes(BlobRef(identity.digest, size))
        current()
        records[identity] = raw
        total += size
        return raw

    current()
    read(value.package_result_identity)
    try:
        for _, identity in value.package_input.link_results:
            raw_result = read(identity)
            input_identity = ContentIdentity.parse_uri(
                json.loads(raw_result)["input_identity"]
            )
            raw_input = read(input_identity)
            LinkWorkerResult.admit(
                raw_result,
                identity,
                input_record=raw_input,
                input_identity=input_identity,
                deadline=deadline,
            )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_finalize.proof_invalid", "staged LINK descriptors refused"
        ) from exc
    current()
    return records

"""The exact FINALIZE grant request a receiver measured, for operator signing.

A describe-only FINALIZE dispatch returns this record instead of executing. It
names the private runtime the receiver measured for the exact input, so an
operator can authorize precisely that request. The controller accepts it only
when it reconstructs the same request from the planned intent; the runtime
identity itself is private worker state that only the operator can judge.
"""

import json

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_authority import finalize_execution_request
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.security import BuildRequest

FINALIZE_GRANT_REQUEST_SCHEMA = "literate-ai/finalize-grant-request@1"
MAX_FINALIZE_GRANT_REQUEST_BYTES = 16384


def _invalid():
    raise ActionWireError(
        "action_finalize.grant_request_invalid", "FINALIZE grant request invalid"
    )


def encode_finalize_grant_request(value, request):
    if not isinstance(value, FinalizeWorkerInput) or not isinstance(
        request, BuildRequest
    ):
        raise TypeError("grant request requires typed intent and request")
    return canonical_json_bytes(
        {
            "schema": FINALIZE_GRANT_REQUEST_SCHEMA,
            "input_identity": record_identity(value.to_bytes()).uri,
            "request": request.to_dict(),
        }
    )


def decode_finalize_grant_request(content, value):
    """Return the request only if it is exactly the planned intent's request."""
    if not isinstance(value, FinalizeWorkerInput):
        raise TypeError("grant request requires the planned intent")
    if (
        not isinstance(content, bytes)
        or not 0 < len(content) <= MAX_FINALIZE_GRANT_REQUEST_BYTES
    ):
        _invalid()
    try:
        document = json.loads(content)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "input_identity", "request"}
            or document["schema"] != FINALIZE_GRANT_REQUEST_SCHEMA
            or canonical_json_bytes(document) != content
        ):
            _invalid()
        request = BuildRequest.from_dict(document["request"])
        runtime = ContentIdentity.parse_uri(request.toolchain_digest)
    except ActionWireError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ActionWireError(
            "action_finalize.grant_request_invalid", "FINALIZE grant request invalid"
        ) from exc
    if document["input_identity"] != record_identity(
        value.to_bytes()
    ).uri or request != finalize_execution_request(value, runtime):
        _invalid()
    return request

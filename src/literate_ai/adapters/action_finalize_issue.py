"""Operator issuance of a FINALIZE grant for one described request.

The operator reviews a `finalize-grant-request@1` record that a receiver
described for an exact planned input and measured runtime, then authorizes
exactly that request. The grant file is replaced atomically, so revocation is
a later replacement with `revoked` set. Nothing here contacts a controller.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_finalize_grant_request import (
    FINALIZE_GRANT_REQUEST_SCHEMA,
    MAX_FINALIZE_GRANT_REQUEST_BYTES,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile

MAX_FINALIZE_GRANT_SECONDS = 24 * 60 * 60


class FinalizeGrantIssueError(ValueError):
    """The described request cannot be granted as given."""


def read_grant_request(path):
    content = Path(path).read_bytes()
    if not 0 < len(content) <= MAX_FINALIZE_GRANT_REQUEST_BYTES:
        raise FinalizeGrantIssueError("grant request size is out of bounds")
    try:
        document = json.loads(content)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "input_identity", "request"}
            or document["schema"] != FINALIZE_GRANT_REQUEST_SCHEMA
            or canonical_json_bytes(document) != content
        ):
            raise ValueError("not a canonical grant request")
        input_identity = ContentIdentity.parse_uri(document["input_identity"])
        request = BuildRequest.from_dict(document["request"])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise FinalizeGrantIssueError("grant request is invalid") from exc
    if (
        request.source_bundle_digest != input_identity.uri
        or request.requested_privileges != ("execute-project",)
    ):
        raise FinalizeGrantIssueError("grant request is not a FINALIZE request")
    return input_identity, request


def issue_finalize_grant(
    request_path, grant_path, *, actor, reason, expires_in_seconds, now=None
):
    if not isinstance(actor, str) or not actor or not isinstance(reason, str):
        raise FinalizeGrantIssueError("grant requires an actor and reason")
    if (
        type(expires_in_seconds) is not int
        or not 1 <= expires_in_seconds <= MAX_FINALIZE_GRANT_SECONDS
    ):
        raise FinalizeGrantIssueError("grant lifetime is out of bounds")
    input_identity, request = read_grant_request(request_path)
    issued = datetime.now(UTC) if now is None else now
    grant = BuildAuthorization(
        "finalize:" + input_identity.digest[:24],
        input_identity.uri,
        canonical_identity(request.to_dict()).uri,
        request.effective_revision_digest,
        actor,
        reason,
        SecurityProfile.CONSTRAINED,
        request.requested_privileges,
        issued,
        issued + timedelta(seconds=expires_in_seconds),
    )
    grant_path = Path(grant_path)
    descriptor, temporary = tempfile.mkstemp(prefix=".grant-", dir=grant_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(grant.to_dict()))
        os.chmod(temporary, 0o600)
        os.replace(temporary, grant_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return grant

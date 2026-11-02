"""Deadline-bound retrieval from a privately configured read-only source CAS."""

from __future__ import annotations

import hashlib
import io
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_source_index import MAX_SOURCE_BYTES
from literate_ai.contracts.blobs import BlobRef


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ActionWireError(
            "action_source.redirect_refused", "source CAS redirect refused"
        )


class HttpActionBlobSource:
    """Retrieve exact bytes using only private endpoint and credential bindings."""

    def __init__(
        self,
        endpoint: str,
        deadline: ActionDispatchDeadline,
        *,
        bearer_token: str | None = None,
        allow_http: bool = False,
    ) -> None:
        if (
            not isinstance(deadline, ActionDispatchDeadline)
            or type(allow_http) is not bool
        ):
            raise TypeError("source CAS requires an exact deadline and HTTP policy")
        try:
            parsed = urlsplit(endpoint)
            if (
                not isinstance(endpoint, str)
                or len(endpoint) > 4096
                or any(ord(item) < 33 or ord(item) > 126 for item in endpoint)
                or parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
                or not parsed.hostname
                or (parsed.port is not None and parsed.port < 1)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("invalid endpoint")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ActionWireError(
                "action_source.endpoint_invalid", "invalid private source CAS endpoint"
            ) from exc
        if bearer_token is not None and (
            not isinstance(bearer_token, str)
            or not 1 <= len(bearer_token) <= 8192
            or any(ord(item) < 33 or ord(item) > 126 for item in bearer_token)
        ):
            raise ActionWireError(
                "action_source.credential_invalid",
                "invalid private source CAS credential",
            )
        self.endpoint = endpoint.rstrip("/")
        self.deadline = deadline
        self.bearer_token = bearer_token
        self.opener = None

    def fetch(self, reference: BlobRef) -> bytes:
        output = io.BytesIO()
        self.copy_to(
            reference,
            output,
            maximum_bytes=MAX_SOURCE_BYTES,
            admission_guard=lambda: None,
        )
        return output.getvalue()

    def copy_to(
        self,
        reference: BlobRef,
        output,
        *,
        maximum_bytes: int,
        admission_guard,
        cancelled=lambda: False,
    ) -> int:
        """Stream an explicitly bounded blob into caller-owned private staging.

        This method grants no admission and never publishes a destination path.
        The caller discards staging on failure and independently verifies the
        complete artifact before any live PACKAGE admission. Legacy fetch keeps
        its original source-byte ceiling.
        """
        if (
            type(maximum_bytes) is not int
            or not 1 <= maximum_bytes <= 2**63 - 1
            or not callable(admission_guard)
            or not callable(cancelled)
            or not callable(getattr(output, "write", None))
        ):
            raise TypeError("streamed CAS requires finite policy, sink and live guard")
        if not isinstance(reference, BlobRef) or reference.size > maximum_bytes:
            raise ActionWireError(
                "action_source.blob_invalid", "source blob exceeds its bound"
            )

        def current():
            self.deadline.remaining()
            if cancelled():
                raise ActionWireError(
                    "action_source.cancelled", "source CAS retrieval was cancelled"
                )
            admission_guard()
            self.deadline.remaining()

        current()
        headers = {"Accept-Encoding": "identity"}
        if self.bearer_token is not None:
            headers["Authorization"] = "Bearer " + self.bearer_token
        request = Request(
            f"{self.endpoint}/blobs/sha256/{reference.digest[:2]}/{reference.digest}",
            headers=headers,
            method="GET",
        )
        try:
            if self.opener is None:
                self.opener = build_opener(_NoRedirect())
            with self.opener.open(
                request, timeout=min(30, self.deadline.remaining())
            ) as response:
                if (
                    response.status != 200
                    or response.headers.get("Content-Encoding", "identity")
                    != "identity"
                ):
                    raise ActionWireError(
                        "action_source.response_invalid", "invalid source CAS response"
                    )
                lengths = response.headers.get_all("Content-Length", [])
                if len(lengths) > 1:
                    raise ActionWireError(
                        "action_source.response_invalid", "ambiguous source CAS size"
                    )
                declared = response.headers.get("Content-Length")
                if declared is not None and (
                    not declared.isdecimal() or int(declared) != reference.size
                ):
                    raise ActionWireError(
                        "action_source.size_mismatch", "source CAS size mismatch"
                    )
                count = 0
                digest = hashlib.sha256()
                while True:
                    current()
                    chunk = response.read1(min(64 * 1024, reference.size - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > reference.size:
                        raise ActionWireError(
                            "action_source.size_mismatch", "source CAS size mismatch"
                        )
                    digest.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        current()
                        written = output.write(remaining)
                        if type(written) is not int or not 1 <= written <= len(
                            remaining
                        ):
                            raise ActionWireError(
                                "action_source.sink_invalid",
                                "source CAS staging write was incomplete",
                            )
                        remaining = remaining[written:]
                current()
                if count != reference.size or digest.hexdigest() != reference.digest:
                    raise ActionWireError(
                        "action_source.digest_mismatch",
                        "source CAS bytes differ from manifest",
                    )
                return count
        except (HTTPError, URLError, HTTPException, OSError, ValueError) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            raise ActionWireError(
                "action_source.fetch_failed", "source CAS retrieval failed"
            ) from exc

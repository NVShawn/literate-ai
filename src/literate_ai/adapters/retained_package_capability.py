"""Fresh canonical capability measurement bound to one private PACKAGE action.

No observation is cached. The probe owns real receiver measurement and one full
validation after it; cheap registration/ownership checks bracket that boundary.
Custom callbacks retain the original full-currentness composition.
"""

import secrets
from dataclasses import dataclass

from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    decode_capability_request,
    decode_capability_response,
    encode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.contracts import canonical_json_bytes

_REGISTERED_PROBE = object()


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class RegisteredRetainedPackageCapabilityProbe:
    """A private operation-bound measurement, never a decoded success token."""

    _worker: object
    _request: object
    _deadline: object
    _expected_worker: object
    _capability_identity: object
    _admission_guard: object
    _http_source: bool
    _registration: object
    _catalog: object
    _input: bytes
    _poll_guard: object
    _seal: object

    def __init__(
        self,
        worker,
        request,
        deadline,
        expected_worker,
        capability_identity,
        admission_guard,
        http_source,
        *,
        _seal=None,
    ):
        if _seal is not _REGISTERED_PROBE:
            raise TypeError("retained.capability.private-probe-required")
        for name, value in {
            "_worker": worker,
            "_request": request,
            "_deadline": deadline,
            "_expected_worker": expected_worker,
            "_capability_identity": capability_identity,
            "_admission_guard": admission_guard,
            "_http_source": http_source,
            "_registration": worker.registration_identity,
            "_catalog": worker.catalog_identity,
            "_input": worker.execution_request_bytes,
            "_poll_guard": worker.poll_guard,
            "_seal": _seal,
        }.items():
            object.__setattr__(self, name, value)

    def require_binding(
        self, worker, request, deadline, expected_worker, capability_identity, guard
    ):
        if (
            getattr(self, "_seal", None) is not _REGISTERED_PROBE
            or worker is not self._worker
            or request != self._request
            or deadline != self._deadline
            or expected_worker != self._expected_worker
            or capability_identity != self._capability_identity
            or guard is not self._admission_guard
            or worker.registration_identity != self._registration
            or worker.catalog_identity != self._catalog
            or worker.execution_request_bytes != self._input
            or worker.poll_guard is not self._poll_guard
        ):
            raise ValueError("retained.capability.private-probe-binding-differs")
        deadline.remaining()

    def require_registered_current(self):
        self.require_binding(
            self._worker,
            self._request,
            self._deadline,
            self._expected_worker,
            self._capability_identity,
            self._admission_guard,
        )
        self._poll_guard()
        self._admission_guard()
        self._deadline.remaining()

    def __call__(self):
        self.require_registered_current()
        content = canonical_json_bytes(
            {
                "schema": CAPABILITY_REQUEST_SCHEMA,
                "worker_identity": self._expected_worker.uri,
                "nonce": secrets.token_hex(16),
                "deadline": self._deadline.to_dict(),
            }
        )
        challenge, _ = decode_capability_request(content)
        response = encode_capability_response(
            challenge,
            self._deadline,
            self._expected_worker,
            base_actions=(),
            http_source=self._http_source,
            package_profile=self._worker.identity,
        )
        measured = decode_capability_response(
            response, content, receiver_code_identity(deadline=self._deadline)
        )
        # This is a fresh full check on EVERY invocation, after measurement.
        # There is no reusable "already validated" flag or observation cache.
        self._worker.require_current()
        self.require_registered_current()
        if measured.capability_identity != self._capability_identity:
            raise ActionWireError(
                "action_admission.runtime_changed",
                "worker runtime or supported capabilities changed",
            )
        return measured


def bind_retained_package_capability_probe(
    worker,
    request,
    deadline,
    records,
    expected_worker,
    capability_identity,
    admission_guard,
    *,
    http_source,
):
    """Compose only the canonical single-role receiver's admitted request."""
    from literate_ai.adapters.action_retained_unit_package_worker import (
        ConfiguredRetainedUnitPackageWorker,
    )

    if (
        not isinstance(worker, ConfiguredRetainedUnitPackageWorker)
        or not callable(worker.poll_guard)
        or not callable(admission_guard)
        or type(http_source) is not bool
    ):
        raise TypeError("retained.capability.registered-receiver-required")
    worker.admit_request(
        request, deadline, records, expected_worker, capability_identity
    )
    return RegisteredRetainedPackageCapabilityProbe(
        worker,
        request,
        deadline,
        expected_worker,
        capability_identity,
        admission_guard,
        http_source,
        _seal=_REGISTERED_PROBE,
    )

"""Crash-retained local reservation for one registered native receiver attempt.

Only the actual parent may release its own exact marker after native empty-subtree
proof or a proven refusal before native launch. Existing marker bytes are never
parsed into ownership or cleared by age,
PID disappearance, a timeout, a new registration, or controller transport exit.
"""

import hashlib
import json
import os
import secrets
from contextlib import contextmanager, suppress

from literate_ai._cache_lock import exclusive_cache_lock
from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.contracts import canonical_json_bytes


class _OwnedReceiverAttempt:
    def __init__(self, path, request, registration):
        self.path = path
        self.request = request
        self.registration = registration
        self.pid = os.getpid()
        self.nonce = secrets.token_hex(16)
        self.content = canonical_json_bytes(
            {
                "schema": "literate-ai/retained-receiver-active-attempt@1",
                "request_identity": request.identity.uri,
                "worker_identity": request.worker.worker_identity.uri,
                "registration_identity": registration.uri,
                "owner_pid": self.pid,
                "attempt_nonce": self.nonce,
                "requires_native_empty_proof": True,
            }
        )
        self.descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        self.released = False
        try:
            os.set_inheritable(self.descriptor, False)
            self.node = os.fstat(self.descriptor)
            remaining = memoryview(self.content)
            while remaining:
                count = os.write(self.descriptor, remaining)
                if not 0 < count <= len(remaining):
                    raise OSError("receiver reservation did not accept its record")
                remaining = remaining[count:]
            os.fsync(self.descriptor)
            self.require_current()
            directory = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            os.close(self.descriptor)
            # Even an incomplete marker refuses reuse. Never erase uncertain
            # ownership while unwinding an interrupted publication.
            raise

    def require_current(self):
        require_safe_directory(self.path.parent)
        current = self.path.lstat()
        if (
            self.released
            or os.getpid() != self.pid
            or (current.st_dev, current.st_ino) != (self.node.st_dev, self.node.st_ino)
            or current.st_nlink != 1
            or _read_regular_file(
                self.path, maximum_bytes=1024, code="retained_receiver.attempt_changed"
            )
            != self.content
        ):
            raise ValueError("retained.receiver.attempt-ownership-changed")

    def release_after_native_empty(self):
        self.require_current()
        self.path.unlink()
        self.released = True

    def release_before_native_start(self, error):
        """Release this parent's marker only for a canonical prelaunch refusal."""
        from literate_ai.adapters.freebsd_owned_attempt import FreeBsdOwnedAttemptError

        if (
            not isinstance(error, FreeBsdOwnedAttemptError)
            or error.guardian_pid is not None
            or error.cleanup_verified is not True
            or error.reason not in {"prelaunch-admission-revoked", "cancelled"}
        ):
            raise ValueError("retained.receiver.proven-prelaunch-refusal-required")
        self.require_current()
        self.path.unlink()
        self.released = True

    def retain_diagnostic(self, error):
        """Bounded recovery facts, not a termination or fresh-execution grant."""
        self.require_current()
        content = canonical_json_bytes(
            {
                "schema": "literate-ai/retained-receiver-cleanup-diagnostic@1",
                "request_identity": self.request.identity.uri,
                "worker_identity": self.request.worker.worker_identity.uri,
                "registration_identity": self.registration.uri,
                "guardian_pid": error.guardian_pid,
                "reason": error.reason,
                "cleanup_verified": error.cleanup_verified,
                "quarantine_required": error.quarantine_required,
            }
        )
        if len(content) > 2048:
            raise ValueError("retained.receiver.cleanup-diagnostic-exceeds-bound")
        # One unique diagnostic per request; never overwrite prior recovery facts.
        diagnostic = self._diagnostic_path()
        self._write_diagnostic(diagnostic, content)

    def _diagnostic_path(self):
        return self.path.with_name(
            self.path.name
            + "."
            + self.request.identity.digest
            + "."
            + self.nonce
            + ".json"
        )

    def _write_diagnostic(self, path, content):
        self.require_current()
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.require_current()

    def retain_completion(self, result):
        """Retain this live wrapper return; documents grant no execution/release."""
        from literate_ai import freebsd_attempt_supervisor as protocol
        from literate_ai.adapters.freebsd_owned_attempt import (
            FreeBsdOwnedAttemptResult,
            _response,
        )

        self.require_current()
        if (
            not isinstance(result, FreeBsdOwnedAttemptResult)
            or not isinstance(result.recovery_frame, bytes)
            or not 0 < len(result.recovery_frame) <= protocol.MAX_REQUEST_BYTES
        ):
            raise ValueError("retained.receiver.live-native-response-required")
        value = json.loads(result.recovery_frame)
        if type(value) is not dict or not isinstance(value.get("nonce"), str):
            raise ValueError("retained.receiver.live-native-response-required")
        response, empty, output = _response(
            result.recovery_frame,
            value["nonce"],
            result.guardian_pid,
            16 * 1024**2,
            65536,
        )
        if (
            not empty
            or response["reason"] != "completed"
            or response["returncode"] != result.returncode
            or response["elapsed_milliseconds"] != result.elapsed_milliseconds
            or output != [result.stdout, result.stderr]
        ):
            raise ValueError("retained.receiver.native-response-differs")
        diagnostic = self._diagnostic_path()
        guardian = diagnostic.with_suffix(".guardian.json")
        content = canonical_json_bytes(
            {
                "schema": "literate-ai/retained-receiver-completed-attempt@1",
                "request_identity": self.request.identity.uri,
                "worker_identity": self.request.worker.worker_identity.uri,
                "registration_identity": self.registration.uri,
                "guardian_pid": result.guardian_pid,
                "returncode": result.returncode,
                "elapsed_milliseconds": result.elapsed_milliseconds,
                "cleanup_verified": True,
                "quarantine_required": False,
                "guardian_response_file": guardian.name,
                "guardian_response_sha256": hashlib.sha256(
                    result.recovery_frame
                ).hexdigest(),
                "guardian_response_bytes": len(result.recovery_frame),
                "admitted": False,
                "release_authorized": False,
            }
        )
        if len(content) > 2048:
            raise ValueError("retained.receiver.cleanup-diagnostic-exceeds-bound")
        self._write_diagnostic(guardian, result.recovery_frame)
        self._write_diagnostic(diagnostic, content)


@contextmanager
def reserve_retained_receiver_attempt(
    config_path, request, registration, deadline, *, preflight=None
):
    if preflight is not None and not callable(preflight):
        raise TypeError("retained.receiver.preflight-must-be-callable")
    path = config_path.with_name(config_path.name + ".attempt")
    lock = config_path.with_name(config_path.name + ".attempt.lock")
    # The marker survives process death; the advisory lock serializes publication
    # and release but is never itself interpreted as evidence of native cleanup.
    with exclusive_cache_lock(lock, timeout_seconds=min(10, deadline.remaining())):
        deadline.remaining()
        if preflight is not None:
            preflight()
            deadline.remaining()
        owned = _OwnedReceiverAttempt(path, request, registration)
        try:
            yield owned
        finally:
            with suppress(OSError):
                os.close(owned.descriptor)

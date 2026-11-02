"""Stream immutable return blobs through a privately configured command worker."""

import hashlib
import os
import stat
import tempfile
import threading
from pathlib import Path

from literate_ai.adapters.action_command_dispatch import command_worker_environment
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_source_index import MAX_SOURCE_BYTES
from literate_ai.adapters.action_transport import (
    action_receiver_command,
    supports_action_transport,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.execution_dispatch import ExecutionWorker
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.return_cas import (
    MAX_READ_REQUEST_BYTES,
    MAX_RETURN_BLOB_BYTES,
    READ_SCHEMA,
)


class CommandActionBlobSource:
    """Data custody only; command/root selection never comes from the request."""

    def __init__(self, worker, deadline, *, registration_identity, cwd, environment):
        if (
            not isinstance(worker, ExecutionWorker)
            or not supports_action_transport(worker)
            or not isinstance(deadline, ActionDispatchDeadline)
            or not isinstance(registration_identity, ContentIdentity)
        ):
            raise TypeError(
                "returned CAS requires a configured worker and registration"
            )
        self.worker, self.deadline = worker, deadline
        self.registration_identity = registration_identity
        self.cwd = Path(cwd)
        if (
            not self.cwd.is_absolute()
            or self.cwd.resolve(strict=True) != self.cwd
            or not self.cwd.is_dir()
        ):
            raise ValueError("returned CAS controller directory must be direct")
        self.environment = command_worker_environment(worker, environment)

    def fetch(self, reference):
        # The command copier owns a regular-file descriptor. Only this legacy
        # bounded convenience API materializes the completed bytes in memory.
        with tempfile.TemporaryFile() as output:
            self.copy_to(
                reference,
                output,
                maximum_bytes=MAX_SOURCE_BYTES,
                admission_guard=lambda: None,
            )
            output.seek(0)
            return output.read()

    def copy_to(
        self,
        reference,
        output,
        *,
        maximum_bytes,
        admission_guard,
        cancelled=lambda: False,
    ):
        if (
            not isinstance(reference, BlobRef)
            or type(maximum_bytes) is not int
            or not 1 <= maximum_bytes <= MAX_RETURN_BLOB_BYTES
            or reference.size > maximum_bytes
            or len(reference.media_type.encode()) > 512
            or not callable(admission_guard)
            or not callable(cancelled)
            or not callable(getattr(output, "fileno", None))
        ):
            raise TypeError(
                "returned CAS requires finite bytes, a sink and live guards"
            )

        def current():
            self.deadline.remaining()
            if cancelled():
                raise ActionWireError(
                    "action_source.cancelled", "return retrieval was cancelled"
                )
            admission_guard()
            self.deadline.remaining()

        current()
        content = canonical_json_bytes(
            {
                "schema": READ_SCHEMA,
                "worker_identity": self.worker.identity.uri,
                "registration_identity": self.registration_identity.uri,
                "reference": reference.to_dict(),
                "deadline": self.deadline.to_dict(),
            }
        )
        if len(content) > MAX_READ_REQUEST_BYTES:
            raise ActionWireError(
                "action_source.blob_invalid", "return request exceeds its bound"
            )
        # Duplicate the selected descriptor before starting a reader. Arbitrary
        # caller write callbacks cannot be interrupted, and must never run on a
        # daemon reader that may outlive its supervisor. An owned regular-file
        # write can delay cancellation in the kernel; quiescence takes precedence
        # over returning while failed staging is still being mutated.
        try:
            descriptor = os.dup(output.fileno())
        except (OSError, ValueError, TypeError) as exc:
            raise TypeError(
                "returned CAS sink requires a writable regular file"
            ) from exc
        try:
            os.set_inheritable(descriptor, False)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise TypeError("returned CAS sink must be a regular file")
            os.write(descriptor, b"")  # Refuse a read-only descriptor before launch.
        except BaseException:
            os.close(descriptor)
            raise
        digest, count = hashlib.sha256(), 0
        write_lock = threading.Lock()
        stopped = False

        class Sink:
            def writable(_self):
                return True

            def write(_self, block):
                nonlocal count
                with write_lock:
                    if stopped:
                        raise OSError("returned CAS staging writer is closed")
                    # Admission stays on the supervising thread. Only byte
                    # custody, deadline and cancellation are checked here.
                    self.deadline.remaining()
                    if cancelled() or count + len(block) > reference.size:
                        raise OSError(
                            "returned CAS stream exceeded its live byte bound"
                        )
                    remaining = memoryview(block)
                    while remaining:
                        self.deadline.remaining()
                        if cancelled():
                            raise OSError("returned CAS stream was cancelled")
                        written = os.write(descriptor, remaining)
                        if not 0 < written <= len(remaining):
                            raise OSError("returned CAS sink refused bytes")
                        remaining = remaining[written:]
                    digest.update(block)
                    count += len(block)
                    return len(block)

        try:
            result = run_bounded_process(
                action_receiver_command(
                    self.worker, self.deadline, mode="--read-return-blob"
                ),
                cwd=self.cwd,
                environment=self.environment,
                timeout_seconds=self.deadline.remaining(),
                stdout_limit_bytes=max(1, reference.size),
                stderr_limit_bytes=65536,
                error_prefix="action_return_cas",
                input_bytes=content,
                input_limit_bytes=MAX_READ_REQUEST_BYTES,
                interrupt_guard=current,
                terminate_descendants=True,
                poll_interval_seconds=0.1,
                trace=False,
                stdout_sink=Sink(),
            )
        except BuildError as exc:
            current()
            raise ActionWireError(
                "action_source.transfer_failed", "returned CAS stream failed"
            ) from exc
        finally:
            # A delayed reader may still wake after process cleanup. Mark it
            # closed while holding the same lock as every write, then close only
            # our duplicate. No writer can mutate staging after this returns.
            with write_lock:
                stopped = True
                os.close(descriptor)
        current()
        if (
            result.returncode != 0
            or count != reference.size
            or digest.hexdigest() != reference.digest
        ):
            raise ActionWireError(
                "action_source.blob_invalid", "returned CAS bytes differ"
            )
        return count

"""Bounded verified streaming from an explicitly selected local source CAS."""

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.repository_refresh import _local_identity_wire
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.storage.cas import FileSystemCAS


class FileSystemActionBlobSource:
    """Byte custody only; source selection belongs to the private operator."""

    def __init__(self, store, deadline):
        if not isinstance(store, FileSystemCAS) or not isinstance(
            deadline, ActionDispatchDeadline
        ):
            raise TypeError("local blob source requires a CAS and exact deadline")
        self.store, self.deadline = store, deadline
        self._node = directory_node(store.root)
        self.identity = canonical_identity(
            {
                "schema": "literate-ai/filesystem-action-source-binding@1",
                "root": str(store.root),
                "node": _local_identity_wire(self._node),
            }
        )

    def require_current(self):
        self.deadline.remaining()
        if directory_node(self.store.root) != self._node:
            raise ActionWireError(
                "action_source.custody_changed", "selected source CAS was replaced"
            )

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
            or not 1 <= maximum_bytes <= 2**63 - 1
            or not callable(getattr(output, "write", None))
            or not callable(admission_guard)
            or not callable(cancelled)
        ):
            raise TypeError(
                "local blob streaming requires finite policy and live guards"
            )
        if reference.size > maximum_bytes:
            raise ActionWireError(
                "action_source.blob_invalid", "source blob exceeds its bound"
            )

        def current():
            self.require_current()
            if cancelled():
                raise ActionWireError(
                    "action_source.cancelled", "source CAS retrieval was cancelled"
                )
            admission_guard()
            self.require_current()

        class GuardedSink:
            def write(self, content):
                current()
                count = output.write(content)
                current()
                return count

        current()
        count = self.store.stream_to(reference, GuardedSink())
        current()
        return count

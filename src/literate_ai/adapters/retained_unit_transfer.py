"""Bounded package-return custody and portable inspection, never admission.

The authenticated action owner supplies current request/worker/LINK guards. This
owner receives only exact CAS references through private transport bindings and
does not interpret a successful download as retained or remote execution proof.
"""

import tempfile
import time
from pathlib import Path

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_command_blob_source import CommandActionBlobSource
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_filesystem_blob_source import (
    FileSystemActionBlobSource,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.freebsd_composed_tree_packaging import (
    FreeBsdComposedPackageSetProjection,
    inspect_freebsd_composed_package_set,
)
from literate_ai.adapters.retained_unit_results import read_retained_unit_results
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_unit_packages import RetainedUnitPackageSetPlan
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.storage.cas import BlobNotFoundError, FileSystemCAS


class RetainedUnitReturnCustody:
    """One operation's finite budget shared by metadata and archive delivery.

    This owner grants byte custody only. It can begin before a returned receipt
    has been opened and its exact package projection reconstructed.
    """

    def __init__(self, budget, store, source, parent, deadline, guard, cancelled):
        if (
            not isinstance(
                budget, (RetainedUnitPackagePolicy, RetainedUnitPackageSetPlan)
            )
            or not isinstance(store, FileSystemCAS)
            or not isinstance(
                source,
                (
                    HttpActionBlobSource,
                    CommandActionBlobSource,
                    FileSystemActionBlobSource,
                ),
            )
            or not isinstance(deadline, ActionDispatchDeadline)
            or source.deadline != deadline
            or not callable(guard)
            or not callable(cancelled)
        ):
            raise TypeError("retained.unit.typed-transfer-context-required")
        parent = Path(parent).absolute()
        directory_node(parent)
        self.budget, self.store, self.source = budget, store, source
        self.parent, self.deadline = parent, deadline
        self.guard, self.cancelled = guard, cancelled
        self.reserved = self.received = self.fetches = self.staging_peak = 0

    def current(self):
        self.deadline.remaining()
        if self.cancelled():
            raise ActionWireError(
                "retained_unit_transfer.cancelled", "package return was cancelled"
            )
        self.guard()
        self.deadline.remaining()

    def ensure(self, reference, *, maximum):
        self.current()
        if not isinstance(reference, BlobRef) or not 0 <= reference.size <= maximum:
            raise ValueError("retained.unit.transfer-object-budget-exceeded")
        try:
            self.store.verify(reference)
        except BlobNotFoundError:
            pass
        else:
            return
        plan = self.budget
        if self.reserved + reference.size > plan.max_transport_bytes:
            raise ValueError("retained.unit.transfer-aggregate-budget-exceeded")
        if 2 * reference.size > plan.max_staging_bytes:
            raise ValueError("retained.unit.transfer-staging-budget-exceeded")
        # Reserve before network or staging writes. A failed attempt never makes
        # its allowance reusable within this operation.
        self.reserved += reference.size
        with tempfile.TemporaryDirectory(prefix="unit-return-", dir=self.parent) as raw:
            destination = Path(raw) / "received"
            with destination.open("xb") as output:
                count = self.source.copy_to(
                    reference,
                    output,
                    maximum_bytes=maximum,
                    admission_guard=self.current,
                    cancelled=self.cancelled,
                )
            self.current()
            if count != reference.size:
                raise ValueError("retained.unit.transfer-size-differs")
            if (
                self.store.put_file(destination, media_type=reference.media_type)
                != reference
            ):
                raise ValueError("retained.unit.transfer-custody-differs")
            self.received += count
            self.fetches += 1
            self.staging_peak = max(self.staging_peak, 2 * count)
            self.current()

    def metadata_store(self, *, maximum_bytes):
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 64 * 1024**2:
            raise ValueError("retained.unit.bounded-metadata-read-required")
        return _ResultMetadataCAS(self, maximum_bytes)


class _ResultMetadataCAS(FileSystemCAS):
    """Fetch metadata only when the canonical bounded reader actually needs it."""

    def __init__(self, receiver, maximum):
        super().__init__(receiver.store.root, create=False)
        self.receiver, self.maximum = receiver, maximum

    def get_bytes(self, reference):
        self.receiver.ensure(reference, maximum=self.maximum)
        return super().get_bytes(reference)

    def verify(self, reference):
        self.receiver.ensure(reference, maximum=self.maximum)
        return super().verify(reference)


def receive_retained_unit_package_set(
    projection,
    reference,
    *,
    store,
    source,
    object_root,
    deadline,
    admission_guard,
    cancelled=lambda: False,
    max_descriptor_bytes,
    max_descriptor_total_bytes,
    max_index_bytes,
    max_metadata_memory_bytes,
    custody=None,
):
    """Verify exact returned archives; the result remains an unadmitted assertion.

    Metadata is faulted in lazily so an exhausted/forged first shard cannot cause
    retrieval of later descriptors. Archive bytes are streamed only after the
    complete selected result membership and declared totals have been checked.
    """
    if (
        not isinstance(projection, FreeBsdComposedPackageSetProjection)
        or not isinstance(store, FileSystemCAS)
        or not isinstance(
            source,
            (HttpActionBlobSource, CommandActionBlobSource, FileSystemActionBlobSource),
        )
        or not isinstance(deadline, ActionDispatchDeadline)
        or source.deadline != deadline
        or not callable(admission_guard)
        or not callable(cancelled)
    ):
        raise TypeError("retained.unit.typed-transfer-context-required")
    parent = Path(object_root).absolute()
    directory_node(parent)
    if custody is None:
        receiver = RetainedUnitReturnCustody(
            projection.set_plan,
            store,
            source,
            parent,
            deadline,
            admission_guard,
            cancelled,
        )
    else:
        receiver = custody
        if (
            not isinstance(receiver, RetainedUnitReturnCustody)
            or receiver.store is not store
            or receiver.source is not source
            or receiver.parent != parent
            or receiver.deadline != deadline
            or receiver.guard is not admission_guard
            or receiver.cancelled is not cancelled
            or receiver.budget.max_transport_bytes
            != projection.set_plan.max_transport_bytes
            or receiver.budget.max_staging_bytes
            != projection.set_plan.max_staging_bytes
        ):
            raise TypeError("retained.unit.same-operation-custody-required")
    started = time.monotonic()
    receiver.current()
    index, results = read_retained_unit_results(
        projection,
        reference,
        _ResultMetadataCAS(receiver, max(max_descriptor_bytes, max_index_bytes)),
        max_descriptor_bytes=max_descriptor_bytes,
        max_descriptor_total_bytes=max_descriptor_total_bytes,
        max_index_bytes=max_index_bytes,
        max_metadata_memory_bytes=max_metadata_memory_bytes,
    )
    for result in results:
        receiver.ensure(result.archive, maximum=projection.set_plan.max_archive_bytes)
    inspect_freebsd_composed_package_set(
        projection, results, store=store, object_root=parent
    )
    receiver.current()
    return (
        index,
        results,
        {
            "payload_bytes_received": receiver.received,
            "blob_fetches": receiver.fetches,
            # Accounted transfer/CAS-copy upper bound, not a measurement of archive
            # inspection staging, process RSS or complete native qualification.
            "transfer_staging_upper_bound_bytes": receiver.staging_peak,
            "elapsed_milliseconds": max(0, int((time.monotonic() - started) * 1000)),
            "archive_bytes_verified": True,
            "admitted": False,
            "native_verified": False,
            "release_authorized": False,
        },
    )

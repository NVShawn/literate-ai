"""Explicit original-byte selection and bounded custody for fresh verification."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.adapters.action_filesystem_blob_source import (
    FileSystemActionBlobSource,
)
from literate_ai.adapters.retained_unit_inspection import (
    retained_unit_metadata_reference,
)
from literate_ai.adapters.retained_unit_transfer import (
    receive_retained_unit_package_set,
)
from literate_ai.adapters.retained_unit_verification import (
    RetainedUnitVerificationSelection,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.storage.cas import FileSystemCAS


def verification_selection_from_args(args, policy):
    """Bind private custody and a complete root reference without host execution."""
    seconds = args.verification_timeout_seconds
    if type(seconds) is not int or not 1 <= seconds <= 3 * 60 * 60:
        raise ValueError("retained.unit.finite-verification-deadline-required")
    store = FileSystemCAS(Path(args.custody_root), create=False)
    deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=seconds))
    source = FileSystemActionBlobSource(store, deadline)
    reference = retained_unit_metadata_reference(
        store,
        ContentIdentity.parse_uri(args.result),
        maximum_bytes=policy.max_index_bytes,
    )
    selection = RetainedUnitVerificationSelection(reference, source.identity, source)
    selection.require_current(policy)
    return selection


def receive_selected_unit_packages(selection, owner, *, object_root, receiver_context):
    """Transfer original selected bytes outside the accepted lifecycle payload."""
    if not isinstance(selection, RetainedUnitVerificationSelection):
        raise TypeError("retained.unit.explicit-verification-selection-required")
    owner.require_current()
    if receiver_context is None:
        source = selection.source
        if source is None:
            raise ValueError("retained.unit.private-verification-source-required")
        deadline = source.deadline

        def guard():
            source.require_current()

    else:
        source = receiver_context.package_source
        deadline = receiver_context.deadline
        guard = receiver_context.require_custody_current
    if source is None:
        raise ValueError("retained.unit.private-verification-source-required")
    receive_retained_unit_package_set(
        owner.projection,
        selection.result,
        store=owner.admission.store,
        source=source,
        object_root=object_root,
        deadline=deadline,
        admission_guard=guard,
        **owner.tree_policy,
    )
    guard()
    owner.require_current()
    return selection.result

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
    evidence = None
    if getattr(args, "original_evidence", None) is not None:
        from literate_ai.adapters.retained_original_unit_evidence import (
            OriginalRetainedUnitEvidence,
        )

        from .retained_project import _document

        _raw, document = _document(Path(args.original_evidence), maximum_bytes=65536)
        evidence = OriginalRetainedUnitEvidence.from_dict(document)
    selection = RetainedUnitVerificationSelection(
        reference, source.identity, source, original_evidence=evidence
    )
    selection.require_current(policy)
    return selection


def receive_selected_unit_packages(
    selection, owner, *, object_root, receiver_context, original_context=None
):
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

    def cancelled():
        return False

    custody = None
    if original_context is not None:
        from literate_ai.adapters.retained_unit_transfer import (
            RetainedUnitReturnCustody,
        )

        custody = original_context.transfer_custody
        if custody is None:
            custody = RetainedUnitReturnCustody(
                original_context.projection.set_plan,
                owner.admission.store,
                source,
                object_root,
                deadline,
                guard,
                cancelled,
            )
        elif (
            not isinstance(custody, RetainedUnitReturnCustody)
            or custody.source is not source
            or custody.store.root != owner.admission.store.root
            or custody.parent != Path(object_root).absolute()
            or custody.deadline != deadline
        ):
            raise ValueError("retained.unit.original-transfer-operation-differs")
        # The canonical lifecycle opens another CAS view of the same namespace.
        # Keep the original transfer owner and its reservation/guard unchanged.
        source, deadline = custody.source, custody.deadline
        guard, cancelled = custody.guard, custody.cancelled
        references = dict(original_context.inputs.store.observed)
        for parameters in original_context.projection.parameters:
            for resource in parameters.evidence:
                references[resource.blob.identity] = resource.blob
        for reference in references.values():
            custody.ensure(
                reference,
                maximum=min(
                    64 * 1024**2, owner.provider_policy["max_provider_metadata_bytes"]
                ),
            )
    receive_retained_unit_package_set(
        owner.projection if original_context is None else original_context.projection,
        selection.result,
        store=owner.admission.store if custody is None else custody.store,
        source=source,
        object_root=object_root,
        deadline=deadline,
        admission_guard=guard,
        custody=custody,
        cancelled=cancelled,
        **owner.tree_policy,
    )
    guard()
    owner.require_current()
    return selection.result


def write_original_verification_evidence(path, evidence):
    """Publish explicit assertions without overwriting another operation's record."""
    import os

    from literate_ai.adapters.retained_original_unit_evidence import (
        OriginalRetainedUnitEvidence,
    )
    from literate_ai.contracts import canonical_json_bytes

    if not isinstance(evidence, OriginalRetainedUnitEvidence):
        raise TypeError("retained.unit.typed-original-evidence-required")
    content = canonical_json_bytes(evidence.to_dict())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def original_context_for_verification(
    selection,
    *,
    project,
    binding,
    manifest,
    planning,
    selected,
    policy,
    pkg_tool,
    tar_tool,
    request,
    specification_context,
    receiver_context=None,
    object_root=None,
    store=None,
):
    """Preflight original named roots before starting a fresh retained lifecycle."""
    from literate_ai.adapters.component_lock_application import (
        current_project_component_locks,
    )
    from literate_ai.adapters.retained_original_unit_context import (
        OriginalUnitMetadataBudget,
        read_original_unit_context,
    )
    from literate_ai.adapters.retained_sdk_authority import (
        current_retained_sdk_snapshots,
    )
    from literate_ai.adapters.retained_unit_transfer import RetainedUnitReturnCustody

    evidence = selection.original_evidence
    if evidence is None:
        return None
    locks = current_project_component_locks(project)
    custody = None
    if receiver_context is None:
        if selection.source is None:
            raise ValueError("retained.unit.original-source-custody-required")
        guard = selection.source.require_current
        metadata = selection.source.store
    else:
        if object_root is None or store is None:
            raise ValueError("retained.unit.original-receiver-custody-required")
        guard = receiver_context.require_custody_current
        custody = RetainedUnitReturnCustody(
            policy,
            store,
            receiver_context.package_source,
            object_root,
            receiver_context.deadline,
            guard,
            lambda: False,
        )
        metadata = custody.metadata_store(
            maximum_bytes=min(64 * 1024**2, policy.max_provider_metadata_bytes)
        )
    budget = OriginalUnitMetadataBudget(metadata, policy, guard)
    expected_request = {
        key: value for key, value in request.items() if key not in selection.to_dict()
    }
    expected_request["operation"] = "execute"
    context = read_original_unit_context(
        evidence,
        source=project.root,
        manifest=manifest,
        planning=planning,
        component_locks=locks,
        store=budget,
        policy=policy,
        pkg_tool=pkg_tool,
        tar_tool=tar_tool,
        test_receipt_policy=project.definition.test_receipt_policy,
        admission_guard=guard,
        expected_request=expected_request,
        expected_worker_identity=selected.worker.identity,
        sdk_snapshots=(
            current_retained_sdk_snapshots(project, locks)
            if any(m.role == "sdk-policy" for m in manifest.members)
            else ()
        ),
        specification_context=specification_context,
        transfer_custody=custody,
    )
    if selection.source is not None:
        selection.require_original_source(
            context.projection,
            policy,
            admission_guard=guard,
        )
    return context

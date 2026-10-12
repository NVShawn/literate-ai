"""Controller PACKAGE import verifies LINK authority, byte custody and output format."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.adapters.action_package_proof import reopen_package_input
from literate_ai.storage.cas import BlobNotFoundError


def verify_deterministic_package(plan, result, read_blob, *, adapter_factory):
    """Reconstruct deterministic format output using private controller composition."""
    if not callable(adapter_factory):
        raise TypeError("package verifier requires a private adapter factory")
    inputs = {item.blob for item in plan.inputs}

    def read_input(reference):
        if reference not in inputs:
            raise ActionWireError(
                "action_package.input_undeclared", "verifier requested undeclared input"
            )
        return read_blob(reference)

    expected = adapter_factory().package(plan, read_blob=read_input)
    if expected != result:
        raise ActionWireError(
            "action_package.format_mismatch",
            "returned package differs from independently reconstructed format",
        )


def import_package_result(
    *,
    content,
    result_identity,
    input_record,
    input_identity,
    records,
    cas,
    deadline,
    admission_guard,
    verify_package,
    blob_source=None,
):
    """CAS bytes remain untrusted until format verification and final admission pass."""
    if (
        not callable(admission_guard)
        or not callable(verify_package)
        or (blob_source is not None and not callable(blob_source))
    ):
        raise TypeError(
            "PACKAGE return requires live admission and private format verification"
        )

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    value = reopen_package_input(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        blob_source=blob_source,
    )

    def read(reference):
        current()
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            raw = blob_source(reference)
            current()
            if (
                not isinstance(raw, bytes)
                or len(raw) != reference.size
                or record_identity(raw).uri != reference.identity
            ):
                raise ActionWireError(
                    "action_package.blob_invalid", "returned package blob differs"
                ) from None
            if cas.put_bytes(raw, media_type=reference.media_type) != reference:
                raise ActionWireError(
                    "action_package.blob_invalid", "returned CAS custody differs"
                ) from None
        raw = cas.get_bytes(reference)
        current()
        return raw

    result = PackageWorkerResult.admit(
        content,
        result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        read_blob=read,
    )
    current()
    # The result verifier has access only to the already admitted package closure.
    allowed = {item.blob for item in (*result.package.files, *result.package.artifacts)}

    def read_verified(reference):
        if reference not in allowed:
            raise ActionWireError(
                "action_package.input_undeclared",
                "verifier requested unrelated package blob",
            )
        return read(reference)

    verify_package(value.plan, result.package, read_verified)
    current()
    return result.package

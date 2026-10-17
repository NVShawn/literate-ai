"""Reopen accepted Component proof and artifact bytes for a LINK operation."""

from datetime import UTC, datetime

from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.action_provider_build import read_provider_build
from literate_ai.adapters.action_provider_record import ProviderBuildTransfer


def reopen_link_component(
    *, input_record, input_identity, cas, deadline, admission_guard, blob_source=None
):
    """Verify own ACCEPT proof; dependency LINK proofs remain separate inputs."""
    if not callable(admission_guard) or (
        blob_source is not None and not callable(blob_source)
    ):
        raise TypeError("LINK proof requires live admission and explicit transport")
    value = LinkWorkerInput.admit(input_record, input_identity, deadline)

    def current():
        deadline.remaining()
        admission_guard()
        # Reapply time-sensitive build authorization throughout evidence transfer.
        value.build.inputs.authorization.grant.require_valid(
            value.build.inputs.intent.build_request, now=datetime.now(UTC)
        )
        deadline.remaining()

    current()
    receipt = value.acceptance_result.evidence
    transfer = ProviderBuildTransfer(
        receipt.identity,
        value.acceptance_input.execution_input.build_result.artifact_archive,
        value.acceptance_result.evidence_records,
        value.build.source_validation,
    )
    files, reader = read_provider_build(
        transfer=transfer,
        receipt=receipt,
        cas=cas,
        deadline=deadline,
        require_current=current,
        generation_plan=value.build.generation_plan,
        blob_source=blob_source,
    )
    current()
    # The returned manifest is recomputed from the same re-admitted input.
    raw = value.to_bytes()
    admitted = LinkWorkerInput.admit(raw, record_identity(raw), deadline)
    return admitted.manifest, files, reader

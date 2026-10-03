"""Hydrate and reopen complete ACCEPT stage proof before allocating a worker job."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_provider_build import verify_accepted_component_records
from literate_ai.contracts import (
    ContentIdentity,
    StandardComponentAcceptanceEvidence,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.storage.cas import BlobNotFoundError


def hydrate_accept_proof(handoff, *, cas, blob_source, require_current):
    """Call after bounded handoff admission and BUILD artifact verification."""
    require_current()
    execution = handoff.execution_input
    build = execution.build_input
    refs = {}
    for stage in (
        execution.build_result,
        handoff.test_result,
        handoff.execution_result,
    ):
        refs.update((ref.identity, ref) for ref in stage.evidence_records)
    records = {}
    for ref in refs.values():
        require_current()
        try:
            cas.verify(ref)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            content = blob_source(ref)
            require_current()
            if (
                not isinstance(content, bytes)
                or len(content) != ref.size
                or record_identity(content).uri != ref.identity
                or cas.put_bytes(content, media_type=ref.media_type) != ref
            ):
                raise ActionWireError(
                    "action_accept.proof_invalid", "ACCEPT proof bytes differ"
                ) from None
        records[ContentIdentity.parse_uri(ref.identity)] = cas.get_bytes(ref)
        require_current()
    policy = {
        "schema": "literate-ai/local-standard-acceptance-policy@1",
        "requires": ["build", "generated-tests", "execution"],
    }
    receipt = StandardComponentAcceptanceEvidence(
        component_revision=build.plan.component_revision,
        source_generation_identity=build.source_generation_identity,
        generated_test_suite_identity=build.candidate.generated_test_suite_identity,
        build=execution.build_result.evidence,
        generated_tests=handoff.test_result.evidence,
        execution=handoff.execution_result.evidence,
        acceptance_policy_identity=canonical_identity(policy),
    )
    records[receipt.acceptance_policy_identity] = canonical_json_bytes(policy)
    records[receipt.identity] = canonical_json_bytes(receipt.to_dict())
    verify_accepted_component_records(
        tuple(sorted(records.items(), key=lambda item: item[0].uri)),
        receipt,
        build.source_validation,
        build.generation_plan,
    )
    require_current()

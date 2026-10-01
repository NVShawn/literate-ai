"""Capture complete BUILD input custody for command BUILD and remote TEST handoff."""

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
    validate_build_provider_receipts,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_provider_build import capture_provider_build
from literate_ai.adapters.action_provider_record import validate_provider_transfers
from literate_ai.contracts.generation_cache import CachedSourceFile


def capture_build_input(indexer, ports, plan, provider_artifacts, receipts):
    generations = {
        item.component_revision: item
        for item in indexer.execution_plan.generation_plans
    }
    validate_build_provider_receipts(provider_artifacts, receipts)
    ports.retained_evidence_records()
    inputs = ports.build_execution_inputs(plan)
    if inputs.providers != provider_artifacts:
        raise ActionWireError(
            "action_build.providers_changed", "BUILD provider inputs differ"
        )
    source = plan.request.source_tree_identity
    candidate = indexer._candidate(plan.component_revision, source)
    custody = ports.source_trees.evidence(source)
    snapshot = indexer._snapshot(source)
    files = []
    for path, content in sorted(snapshot.items()):
        indexer.deadline.remaining()
        files.append(CachedSourceFile(path, indexer.cas.put_bytes(content)))

    def require_consumer():
        indexer.deadline.remaining()
        if ports.build_execution_inputs(plan) != inputs:
            raise ActionWireError("action_build.inputs_changed", "BUILD inputs changed")

    transfers = capture_provider_transfers(indexer, ports, receipts, require_consumer)
    value = BuildWorkerInput(
        indexer.execution_plan.identity,
        candidate.component_generation_plan_identity,
        candidate,
        plan,
        inputs,
        tuple(files),
        ports.source_trees.validation_inputs(source),
        custody.source_generation_identity,
        custody.identity,
        generations[plan.component_revision],
        indexer.execution_plan,
        receipts,
        tuple(transfers),
    )
    return value


def capture_provider_transfers(indexer, ports, receipts, require_current):
    """Reopen accepted provider proof and artifacts under current consumer authority."""
    generations = {
        item.component_revision: item
        for item in indexer.execution_plan.generation_plans
    }
    transfers = []
    for receipt in receipts:
        roots = {ports.artifact_path(item).parent for item in receipt.build.exports}
        if len(roots) != 1:
            raise ActionWireError(
                "action_build.provider_invalid", "provider artifact roots differ"
            )
        transfers.append(
            capture_provider_build(
                receipt=receipt,
                artifact_root=roots.pop(),
                records=ports.retained_evidence_records(),
                cas=indexer.cas,
                deadline=indexer.deadline,
                require_current=require_current,
                source_validation=ports.source_trees.validation_inputs(
                    receipt.build.source_tree_identity
                ),
                generation_plan=generations[receipt.component_revision],
            )
        )
        validate_provider_transfers(receipts[: len(transfers)], tuple(transfers))
    require_current()
    return tuple(transfers)

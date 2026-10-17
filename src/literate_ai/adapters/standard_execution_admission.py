"""Reopen transferred EXECUTE records against current controller authority."""

import json

from literate_ai.adapters.qualification_capture import verify_qualification_execution
from literate_ai.contracts import ComponentCommandPhase, canonical_json_bytes
from literate_ai.contracts.standard_execution_inputs import (
    standard_execution_runtime_identity,
)


def verify_transferred_execution(
    reader,
    *,
    plan,
    build,
    source_custody,
    contract,
    evidence,
    scope,
    provider_artifacts,
    now,
):
    """Verify full process proof and return the acceptance-visible stdout value."""
    providers = tuple(
        sorted(
            (item.identity for item in provider_artifacts), key=lambda item: item.uri
        )
    )
    if (
        evidence.component_revision != plan.component_revision
        or evidence.build_evidence_identity != build.identity
        or evidence.export_identities != build.export_identities
        or evidence.artifact_custody_identity != build.artifact_custody_identity
        or build.build_plan_identity != plan.identity
        or build.source_custody_identity != source_custody.identity
        or build.source_tree_identity != plan.request.source_tree_identity
        or contract.component_revision != plan.component_revision
        or evidence.provider_artifact_identities != providers
    ):
        raise ValueError("transferred EXECUTE build or provider authority differs")
    authority = evidence.execution_authority
    if scope is None:
        if authority is not None or providers != plan.provider_artifact_identities:
            raise ValueError("transferred EXECUTE scope differs")
    else:
        if (
            authority is None
            or authority.input_scope != scope
            or authority.source_tree_identity != plan.request.source_tree_identity
            or authority.command_contract_identity != contract.identity
            or authority.runtime_identity
            != standard_execution_runtime_identity(contract)
            or providers != scope.provider_artifact_identities
        ):
            raise ValueError("transferred EXECUTE authority differs")
        # The grant must have covered the execution when it ran.
        evidence.require_authorized(now=now)
    for identity, document in (
        (build.identity, build.to_dict()),
        (evidence.identity, evidence.to_dict()),
    ):
        if reader.read_bytes(identity) != canonical_json_bytes(document):
            raise ValueError("transferred EXECUTE retained record differs")
    exports = {item.export_id: item for item in build.exports}
    root = exports.get(contract.artifact_export.export_id)
    if root is None or evidence.root_export_identity != root.identity:
        raise ValueError("transferred EXECUTE root export differs")
    if contract.is_multi_entrypoint:
        entries = contract.entrypoint_command_contracts()
        units = evidence.entrypoint_evidence
        if units is None or len(units) != len(entries):
            raise ValueError("transferred EXECUTE entrypoint membership differs")
        expected = {
            (
                entry.entrypoint_identity,
                entry.deployment_unit,
                exports[entry.artifact_export.export_id].identity,
                entry.command(ComponentCommandPhase.EXECUTE).identity,
                entry.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity,
            )
            for entry in entries
        }
        actual = {
            (
                unit.entrypoint_identity,
                unit.deployment_unit,
                unit.export_identity,
                unit.execution_contract_identity,
                unit.runtime_identity,
            )
            for unit in units
        }
        if actual != expected:
            raise ValueError("transferred EXECUTE entrypoint command differs")
    elif (
        evidence.entrypoint_evidence is not None
        or evidence.execution_contract_identity
        != contract.command(ComponentCommandPhase.EXECUTE).identity
        or evidence.runtime_identity
        != contract.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity
    ):
        raise ValueError("transferred EXECUTE command or runtime differs")
    verify_qualification_execution(
        reader,
        build_plan_identity=plan.identity,
        execution=evidence,
        expected_execution_plan_identity=None
        if scope is None
        else scope.execution_plan_identity,
    )
    stdout = reader.read_json(evidence.stdout_identity)
    if contract.is_multi_entrypoint:
        return json.dumps(
            {name: value.strip() for name, value in sorted(stdout.items())},
            sort_keys=True,
            separators=(",", ":"),
        )
    return stdout.strip()

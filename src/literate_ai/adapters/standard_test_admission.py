"""Reopen worker TEST evidence against current controller-owned authority."""

from literate_ai.adapters.qualification_capture import (
    verify_qualification_generated_tests,
    verify_qualification_suite_membership,
)
from literate_ai.contracts import ComponentCommandPhase, canonical_json_bytes


def verify_transferred_tests(
    reader, *, plan, build, source_custody, contract, evidence
):
    """Verify data only; caller owns live guards, retention and registration."""
    suite = source_custody.generated_test_suite
    if (
        evidence.generated_test_suite_identity.uri != suite.content_identity
        or build.source_custody_identity != source_custody.identity
        or build.source_tree_identity != plan.request.source_tree_identity
        or contract.component_revision != plan.component_revision
    ):
        raise ValueError("transferred TEST source authority differs")

    def same(identity, document):
        if reader.read_bytes(identity) != canonical_json_bytes(document):
            raise ValueError("transferred TEST record differs")

    same(evidence.identity, evidence.to_dict())
    same(build.identity, build.to_dict())
    verify_qualification_generated_tests(
        reader, build_plan_identity=plan.identity, build=build, tests=evidence
    )
    verify_qualification_suite_membership(reader, suite=suite, tests=evidence)
    if not contract.is_multi_entrypoint:
        runner = contract.tool_binding(ComponentCommandPhase.TEST).toolchain_identity
        if (
            evidence.entrypoint_evidence is not None
            or evidence.runner_identity != runner
        ):
            raise ValueError("transferred TEST runner differs")
        same(
            evidence.test_custody_identity,
            {
                "schema": "literate-ai/local-generated-test-custody@1",
                "source_custody_identity": source_custody.identity.uri,
                "suite_identity": suite.content_identity,
                "runner_identity": runner.uri,
            },
        )
        return
    expected = {
        entrypoint.entrypoint_identity: entrypoint
        for entrypoint in contract.entrypoint_command_contracts()
    }
    units = evidence.entrypoint_evidence
    if (
        units is None
        or len(units) != len(expected)
        or {unit.entrypoint_identity for unit in units} != set(expected)
    ):
        raise ValueError("transferred TEST entrypoint membership differs")
    exports = {export.export_id: export for export in build.exports}
    for unit in units:
        entrypoint = expected[unit.entrypoint_identity]
        export = exports[entrypoint.artifact_export.export_id]
        runner = entrypoint.tool_binding(ComponentCommandPhase.TEST).toolchain_identity
        if (
            unit.deployment_unit != entrypoint.deployment_unit
            or unit.export_identity != export.identity
            or unit.command_identity
            != entrypoint.command(ComponentCommandPhase.TEST).identity
            or unit.runner_identity != runner
        ):
            raise ValueError("transferred TEST entrypoint authority differs")
        same(
            unit.test_custody_identity,
            {
                "schema": "literate-ai/local-entrypoint-generated-test-custody@1",
                "source_custody_identity": source_custody.identity.uri,
                "suite_identity": suite.content_identity,
                "entrypoint_identity": entrypoint.entrypoint_identity.uri,
                "export_identity": export.identity.uri,
                "runner_identity": runner.uri,
            },
        )

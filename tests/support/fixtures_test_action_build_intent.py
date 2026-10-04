from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_action_build_intent``."""



from dataclasses import replace














from tests.support.fixtures_test_standard_post_source_evidence import _evidence

def provider_evidence(revision):
    old = _evidence()
    exports = tuple(
        replace(item, component_revision=revision) for item in old.build.exports
    )
    build = replace(
        old.build,
        component_revision=revision,
        exports=exports,
        resolved_sbom_export_identities=tuple(item.identity for item in exports),
    )
    tests = replace(
        old.generated_tests,
        component_revision=revision,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
    )
    execution = replace(
        old.execution,
        component_revision=revision,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        root_export_identity=exports[0].identity,
    )
    return replace(
        old,
        component_revision=revision,
        build=build,
        generated_tests=tests,
        execution=execution,
    )


"""Typed Standard root integration evidence contract tests."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import StandardRootIntegrationEvidence
from literate_ai.contracts._validation import ContractValidationError
from tests.unit.test_package_release_contracts import PackageReleaseContractTests
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_standard_project_lifecycle import _identity


class StandardRootIntegrationEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = PackageReleaseContractTests()
        fixture.setUp()
        plan = fixture.plan()
        result = fixture.result(plan)
        self.evidence = StandardRootIntegrationEvidence(
            component_lock_identity=plan.component_lock_identity,
            execution_plan_identity=_identity("execution-plan"),
            project_build_plan_identity=_identity("project-build-plan"),
            artifact_graph=fixture.graph,
            link_plan=fixture.graph.link_plans[0],
            package_plan=plan,
            package_result=result,
            root_generated_integration_test_identity=_identity(
                "root-generated-integration-test"
            ),
            packaged_execution_identity=_identity("packaged-execution"),
            independent_acceptance_identity=_identity("independent-acceptance"),
        )
        self.schemas = SchemaCatalog()

    def test_round_trips_with_stable_identity_and_validates_schema(self) -> None:
        document = self.evidence.to_dict()
        self.schemas.validate(self.evidence.SCHEMA, document)
        decoded = StandardRootIntegrationEvidence.from_dict(document)
        self.assertEqual(decoded, self.evidence)
        self.assertEqual(decoded.identity, self.evidence.identity)

    def test_wire_rejects_unknown_fields_and_wrong_schema(self) -> None:
        document = self.evidence.to_dict()
        with self.assertRaises(ContractValidationError):
            StandardRootIntegrationEvidence.from_dict({**document, "extra": True})
        with self.assertRaises(ContractValidationError):
            StandardRootIntegrationEvidence.from_dict(
                {**document, "schema": "urn:example:substitution"}
            )

    def test_rejects_substituted_lock_graph_and_link(self) -> None:
        for mutation, message in (
            (
                {"component_lock_identity": _identity("foreign-lock")},
                "exact lock",
            ),
            (
                {
                    "artifact_graph": replace(
                        self.evidence.artifact_graph,
                        build_system_driver_identity=_identity("foreign-driver"),
                        manifests=tuple(
                            replace(
                                item,
                                build_system_driver_identity=_identity(
                                    "foreign-driver"
                                ),
                            )
                            for item in self.evidence.artifact_graph.manifests
                        ),
                    )
                },
                "artifact graph",
            ),
            (
                {
                    "link_plan": replace(
                        self.evidence.link_plan,
                        root_artifact_identity=next(
                            item
                            for item in (
                                self.evidence.link_plan.ordered_artifact_identities
                            )
                            if item != self.evidence.link_plan.root_artifact_identity
                        ),
                    )
                },
                "exact link plan",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    replace(self.evidence, **mutation)

    def test_rejects_package_plan_result_and_file_substitution(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact result"):
            replace(
                self.evidence,
                package_result=replace(
                    self.evidence.package_result,
                    package_plan_identity=_identity("foreign-package-plan"),
                ),
            )
        substituted_plan = replace(
            self.evidence.package_plan,
            packager_identity=_identity("foreign-packager"),
        )
        with self.assertRaisesRegex(ValueError, "exact result"):
            replace(self.evidence, package_plan=substituted_plan)

        non_entrypoint = next(
            item
            for item in self.evidence.package_result.files
            if item.path != self.evidence.package_result.entrypoints[0].path
        )
        files = tuple(
            item
            for item in self.evidence.package_result.files
            if item != non_entrypoint
        )
        with self.assertRaisesRegex(ValueError, "every and only exact planned"):
            replace(
                self.evidence,
                package_result=replace(
                    self.evidence.package_result,
                    files=files,
                ),
            )

    def test_independent_stage_evidence_cannot_be_reused(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            replace(
                self.evidence,
                independent_acceptance_identity=(
                    self.evidence.packaged_execution_identity
                ),
            )


if __name__ == "__main__":
    unittest.main()

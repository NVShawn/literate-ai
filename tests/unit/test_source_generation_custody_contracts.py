"""Exact wire and application custody for source-only Standard generation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.application.standard_project_services import (
    PreparedExecutableProject,
    StandardProjectApplicationService,
)
from literate_ai.contracts import ContractValidationError
from literate_ai.contracts.executable_components import (
    PROJECT_SOURCE_GENERATION_CUSTODY_SCHEMA,
    ComponentSourceWorkspaceCustody,
    ProjectSourceGenerationCustody,
)
from literate_ai.contracts.identity import canonical_identity
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_generation_scheduling import (
    _decision,
    _names,
    _prepared_execution,
)
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_standard_project_lifecycle import LifecyclePorts, _prepared_nodes

ROOT = Path(__file__).resolve().parents[2]


def _generated_custody() -> ProjectSourceGenerationCustody:
    lock = _diamond_lock()
    execution, requests = _prepared_execution(lock)
    prepared_by_revision = _prepared_nodes(execution, requests)
    prepared = PreparedExecutableProject(
        execution,
        tuple(
            prepared_by_revision[plan.component_revision.uri]
            for plan in execution.generation_plans
        ),
    )
    names = _names(lock)
    return StandardProjectApplicationService.generate_sources(
        prepared,
        invalidation=_decision(execution, names, "money", tuple(names.values())),
        runner=LifecyclePorts(execution, names),
        max_parallelism=2,
    )


class SourceGenerationCustodyContractTests(unittest.TestCase):
    def test_service_retains_every_independent_component_workspace_and_output(self):
        custody = _generated_custody()

        self.assertTrue(custody.successful)
        self.assertEqual(len(custody.components), 4)
        self.assertEqual(
            tuple(item.component_revision.uri for item in custody.components),
            tuple(sorted(item.component_revision.uri for item in custody.components)),
        )
        self.assertEqual(
            len({item.workspace_locator for item in custody.components}), 4
        )
        self.assertTrue(all(item.output is not None for item in custody.components))
        self.assertEqual(
            ProjectSourceGenerationCustody.from_dict(custody.to_dict()), custody
        )

        SchemaCatalog(ROOT / "schemas" / "v2").validate(
            PROJECT_SOURCE_GENERATION_CUSTODY_SCHEMA,
            custody.to_dict(),
        )

    def test_workspace_identity_cannot_be_detached_from_its_retained_locator(self):
        custody = _generated_custody()
        first = custody.components[0]

        with self.assertRaisesRegex(
            ContractValidationError, "exact retained workspace locator"
        ):
            replace(first, workspace_locator="fixture://another-workspace")
        with self.assertRaisesRegex(
            ContractValidationError, "exact retained workspace locator"
        ):
            replace(first, workspace_identity=canonical_identity({"forged": True}))

    def test_component_wire_reader_rejects_future_contract_versions(self):
        document = _generated_custody().components[0].to_dict()
        document["schema"] = (
            "urn:literate-ai:schema:v2:component-source-workspace-custody"
        )

        with self.assertRaises(ContractValidationError):
            ComponentSourceWorkspaceCustody.from_dict(document)


if __name__ == "__main__":
    unittest.main()

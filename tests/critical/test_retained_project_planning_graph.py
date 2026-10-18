"""Read-only retained result preserves graph/plan origin and never grants admission."""

import unittest
from dataclasses import replace

from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.sbom import project_component_lock_managed_graph
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


class RetainedProjectPlanningGraphTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.locks = retained_project_sbom_fixture()
        self.graph = project_retained_project_managed_graph(
            self.plan, self.locks, version="1.0.0"
        )

    def test_public_result_exposes_exact_graph_without_execution_or_admission(self):
        result = RetainedProjectPlanningResult(self.plan, self.graph).to_dict()
        self.assertEqual(result["plan"], self.plan.to_dict())
        self.assertEqual(result["managed_sbom_graph"], self.graph.to_dict())
        self.assertEqual(result["authorization_identity"], self.plan.identity.uri)
        self.assertEqual(result["release_authority"], "original-source")
        self.assertIs(result["admitted"], False)

    def test_changed_plan_cannot_keep_an_old_graph(self):
        changed = replace(
            self.plan, manifest_identity=canonical_identity("changed source")
        )
        with self.assertRaisesRegex(ValueError, "managed-graph-mismatch"):
            RetainedProjectPlanningResult(changed, self.graph)

    def test_ordinary_component_graph_cannot_replace_original_project_origin(self):
        ordinary = project_component_lock_managed_graph(
            self.locks[0], self.locks[0].root_revision
        )
        with self.assertRaisesRegex(ValueError, "managed-graph-mismatch"):
            RetainedProjectPlanningResult(self.plan, ordinary)

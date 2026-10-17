"""Pre-package LINK assembly consumes exact accepted plans and root selection."""

import unittest
from dataclasses import replace

import tests.support.fixtures_test_standard_project_lifecycle as fixture_module
from literate_ai.application.release_artifacts import (
    ReleaseArtifactAssemblyError,
    assemble_standard_project_artifacts,
)
from literate_ai.contracts import canonical_identity
from tests.support.fixtures_test_standard_project_lifecycle import (
    LifecyclePorts,
    _decision,
    _service,
)


class StandardArtifactAssemblyTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.StandardProjectLifecycleTests()
        f.setUp()
        self.fixture = f
        ports = LifecyclePorts(f.execution, f.names)
        self.result = _service(ports).execute(
            f.execution,
            component_lock=f.lock,
            invalidation=_decision(
                f.execution, f.names, "money", tuple(f.names.values())
            ),
            prepared_nodes=f.nodes,
            max_parallelism=2,
        )
        self.assertTrue(self.result.successful)
        self.root = next(
            item
            for item in self.result.node_results
            if item.component_revision == f.execution.root_revision
        )

    def assemble(self, **changes):
        arguments = dict(
            component_lock=self.fixture.lock,
            execution_plan=self.fixture.execution,
            project_build_plan=self.result.project_build_plan,
            results=self.result.node_results,
            primary_export_id=self.root.exports[0].export_id,
        )
        arguments.update(changes)
        return assemble_standard_project_artifacts(**arguments)

    def test_matches_existing_integration_graph_and_link(self):
        graph, link = self.assemble()
        self.assertEqual(graph, self.result.root_integration.artifact_graph)
        self.assertEqual(link, self.result.root_integration.link_plan)
        self.assertEqual(
            self.assemble(results=tuple(reversed(self.result.node_results))),
            (graph, link),
        )

    def test_missing_or_duplicate_component_refuses(self):
        results = self.result.node_results
        for changed in (results[:-1], (*results, results[0])):
            with self.assertRaises(ReleaseArtifactAssemblyError):
                self.assemble(results=changed)

    def test_rehashed_result_cannot_name_another_build_plan(self):
        results = self.result.node_results
        changed = replace(
            results[0], build_plan_identity=canonical_identity("foreign-plan")
        )
        with self.assertRaises(ReleaseArtifactAssemblyError):
            self.assemble(results=(changed, *results[1:]))

    def test_unknown_primary_export_refuses(self):
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError, "primary root export"
        ):
            self.assemble(primary_export_id="missing")

    def test_verified_receipts_preserve_lifecycle_dependency_bindings(self):
        from literate_ai.application.release_artifacts import (
            plan_accepted_assembly_dependencies,
            plan_standard_assembly_dependencies,
        )
        from literate_ai.contracts import DependencyKind

        f = self.fixture
        f._configure_dependencies(DependencyKind.PACKAGING)
        ports = fixture_module.ContractEvidenceLifecyclePorts(f.execution, f.names)
        result = _service(ports).execute(
            f.execution,
            component_lock=f.lock,
            invalidation=_decision(
                f.execution, f.names, "money", tuple(f.names.values())
            ),
            prepared_nodes=f.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        receipts = tuple(node.acceptance_evidence for node in result.node_results)
        expected = plan_standard_assembly_dependencies(f.execution, result.node_results)
        self.assertTrue(expected)
        self.assertEqual(
            plan_accepted_assembly_dependencies(f.execution, receipts), expected
        )
        self.assertEqual(
            plan_accepted_assembly_dependencies(f.execution, tuple(reversed(receipts))),
            expected,
        )
        for changed in (receipts[:-1], (*receipts, receipts[0])):
            with self.assertRaises(ReleaseArtifactAssemblyError):
                plan_accepted_assembly_dependencies(f.execution, changed)

"""Pre-package LINK assembly consumes exact accepted plans and root selection."""

import unittest

import tests.support.fixtures_test_standard_project_lifecycle as fixture_module
from literate_ai.application.release_artifacts import (
    assemble_standard_project_artifacts,
)
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

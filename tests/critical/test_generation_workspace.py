"""Private workspace rebinding preserves logical source allocation and live custody."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.generation_workspace import GenerationWorkspaceBinding
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationError
from literate_ai.contracts import canonical_identity
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class GenerationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        self.value = GenerateWorkerInput.capture(
            execution, f.node, f.cas, self.deadline
        )
        self.workspace = f.root / "worker-source"
        self.workspace.mkdir()
        self.binding = GenerationWorkspaceBinding.admit(
            self.value, self.workspace, self.deadline
        )
        self.prepared = self.binding.prepare(
            f.node.definition, f.node.recipe, f.node.request.prompt
        )

    def test_actual_runner_preserves_allocation_in_relocated_workspace(self):
        f = self.fixture
        f.runner.workspace_binding = self.binding
        output = f.runner(self.prepared)
        self.assertEqual(
            output.candidate.workspace_allocation_identity,
            f.node.workspace.allocation_identity,
        )
        self.assertEqual(
            output.provenance.workspace_allocation_identity,
            f.node.workspace.allocation_identity,
        )
        self.assertTrue(any(self.workspace.iterdir()))
        self.assertEqual(list(Path(f.node.workspace.locator).iterdir()), [])
        self.assertEqual(len(f.generator.calls), 1)

    def test_default_local_runner_still_refuses_relocated_allocation(self):
        with self.assertRaises(CachedCodingCliSourceGenerationError):
            self.fixture.runner(self.prepared)
        self.assertEqual(self.fixture.generator.calls, [])

    def test_nonempty_directory_refuses_before_model(self):
        (self.workspace / "existing").write_text("old source")
        self.fixture.runner.workspace_binding = self.binding
        with self.assertRaises(ActionWireError):
            self.fixture.runner(self.prepared)
        self.assertEqual(self.fixture.generator.calls, [])

    def test_substituted_allocation_or_recipe_refuses(self):
        f = self.fixture
        f.runner.workspace_binding = self.binding
        for changed in (
            replace(
                self.prepared,
                workspace=replace(
                    self.prepared.workspace,
                    allocation_identity=canonical_identity("foreign"),
                ),
            ),
            replace(self.prepared, recipe=replace(f.node.recipe, recipe_id="foreign")),
        ):
            with (
                self.subTest(prepared=changed.workspace.allocation_identity),
                self.assertRaises(ActionWireError),
            ):
                f.runner(changed)
        self.assertEqual(f.generator.calls, [])

    def test_directory_replacement_during_generation_cannot_publish_candidate(self):
        f = self.fixture
        f.runner.workspace_binding = self.binding
        original = f.generator.generate

        def generate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.workspace.rename(self.workspace.with_name("moved-source"))
            self.workspace.mkdir()
            return result

        with patch.object(f.generator, "generate", side_effect=generate):
            with self.assertRaises(ActionWireError):
                f.runner(self.prepared)
        self.assertEqual(f.runner._pending_candidates, {})
        self.assertEqual(f.runner._candidate_cache_keys, {})

    def test_expired_binding_cannot_prepare_or_generate(self):
        expired = replace(
            self.binding,
            deadline=ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1)),
        )
        with self.assertRaises(ActionWireError):
            expired.prepare(
                self.fixture.node.definition,
                self.fixture.node.recipe,
                self.fixture.node.request.prompt,
            )
        self.fixture.runner.workspace_binding = expired
        with self.assertRaises(ActionWireError):
            self.fixture.runner(self.prepared)
        self.assertEqual(self.fixture.generator.calls, [])

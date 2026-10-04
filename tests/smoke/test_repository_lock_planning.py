"""Portable root lock candidates and real-Git, read-only reobservation."""

from __future__ import annotations

import unittest

from literate_ai.adapters import repository_lock_planning as planning
from literate_ai.adapters.orchestration_planning import plan_orchestration
from literate_ai.adapters.orchestration_scaffold import prepare_orchestration_scaffold
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
)
from literate_ai.project_authority_graph import project_authority_graph
from tests.support import fixtures_test_orchestration_planning as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RepositoryLockPlanningTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.OrchestrationPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base
        self.binding = RepositoryOrchestration.from_dict(
            plan_orchestration(self.root, fixture.declaration)["repository_authority"]
        )
        self.materialize(self.binding)

    def materialize(self, binding):
        scaffold = prepare_orchestration_scaffold(
            binding, project_id="super", version="1.0.0"
        )
        for relative, content in scaffold.files:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_prepares_exact_reviewed_root_without_writes_or_component_invention(self):
        before = snapshot(self.base)
        prepared = planning.prepare_repository_lock(self.root)
        self.assertEqual(prepared.lock.repository_orchestration, self.binding)
        self.assertEqual(
            prepared.lock.authority_graph_identity,
            project_authority_graph(self.root, include_component_locks=False).identity,
        )
        plan = prepared.to_plan()
        self.assertEqual(plan, planning.prepare_repository_lock(self.root).to_plan())
        self.assertFalse(plan["writes"])
        self.assertFalse(plan["execution"])
        self.assertEqual(plan["execution_order"], "not-inferred")
        self.assertEqual(plan["publication"], "not-checked")
        self.assertEqual(plan["child_authority"], "independent")
        self.assertNotIn("components", plan)
        self.assertEqual(snapshot(self.base), before)

    def test_shallow_clone_reconstructs_identical_lock_and_plan(self):
        git(
            self.root,
            "add",
            "SKILL.md",
            "PROJECT.md",
            "literate.project.json",
            ".literate",
        )
        git(self.root, "commit", "-q", "-m", "root authority")
        original = planning.prepare_repository_lock(self.root)
        clone = self.base / "clone"
        git(self.base, "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone))
        reconstructed = planning.prepare_repository_lock(clone)
        self.assertEqual(original.lock, reconstructed.lock)
        self.assertEqual(original.to_plan(), reconstructed.to_plan())
        self.assertNotEqual(original.root, reconstructed.root)

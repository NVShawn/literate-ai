"""Real-Git preservation and owned rollback for reviewed root initialization."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import orchestration_initialization as initialization
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.projects import discover_project
from tests.support import fixtures_test_orchestration_planning as planning_fixtures
from tests.support.fixtures_test_repository_orchestration import (
    snapshot,
)


class OrchestrationInitializationTests(unittest.TestCase):
    def setUp(self):
        fixture = planning_fixtures.OrchestrationPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base, self.declaration = (
            fixture.root,
            fixture.base,
            fixture.declaration,
        )
        (self.root / "README.md").write_bytes(b"Existing project README\n")

    def plan(self, **overrides):
        return initialization.plan_orchestration_initialization(
            self.root,
            self.declaration,
            project_id="super",
            version=overrides.get("version", "1.0.0"),
        )

    def initialize(self, plan=None, **overrides):
        plan = self.plan() if plan is None else plan
        options = dict(
            project_id="super",
            version="1.0.0",
            expected_plan_identity=plan["plan_identity"],
            acknowledged=True,
        )
        options.update(overrides)
        return initialization.initialize_orchestration(
            self.root, self.declaration, **options
        )

    def test_initialize_preserves_originals_and_publishes_manifest_last(self):
        before = snapshot(self.root)
        plan = self.plan()
        link = os.link
        published = []

        def observe(source, target, **kwargs):
            self.assertFalse((self.root / "literate.project.json").exists())
            published.append(Path(target).relative_to(self.root).as_posix())
            return link(source, target, **kwargs)

        with patch.object(initialization.os, "link", side_effect=observe):
            result = self.initialize(plan)
        self.assertEqual(result["state"], "initialized")
        self.assertEqual(result["cleanup_retained"], [])
        self.assertEqual(published[-1], "literate.project.json")
        after = snapshot(self.root)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(set(after) - set(before), set(result["created_files"]))
        self.assertEqual(
            discover_project(self.root).definition.identity.uri,
            result["project_identity"],
        )
        self.assertFalse((self.root / ".literate/.orchestration-stage").exists())

    def test_public_initialization_requires_review_and_acknowledgement(self):
        options = ("--project-id", "super", "--project-version", "1.0.0")
        before = snapshot(self.root)
        code, envelope = self.fixture.invoke("plan", *options)
        self.assertEqual(code, 0, envelope)
        plan = envelope["result"]
        reviewed = (*options, "--expected-plan-identity", plan["plan_identity"])
        code, envelope = self.fixture.invoke("check", *reviewed)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["state"], "current")
        code, envelope = self.fixture.invoke("initialize", *reviewed)
        self.assertNotEqual(code, 0, envelope)
        self.assertIn("orchestration.acknowledgement_required", str(envelope))
        self.assertEqual(snapshot(self.root), before)
        code, envelope = self.fixture.invoke("initialize", *reviewed, "--acknowledge")
        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(result["state"], "initialized")
        self.assertTrue(result["writes"])
        self.assertFalse(result["execution"])
        after = snapshot(self.root)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(set(after) - set(before), set(result["created_files"]))

    def test_final_validation_failure_rolls_back_all_owned_additions(self):
        plan = self.plan()
        before = snapshot(self.root)
        validate = initialization.FilesystemProjectValidationAdapter.validate

        def refuse(adapter, root, **options):
            if root == self.root:
                raise ValueError("fixture final validation failure")
            return validate(adapter, root, **options)

        with patch.object(
            initialization.FilesystemProjectValidationAdapter, "validate", new=refuse
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.initialization_failed")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".literate").exists())

"""Pre-execution plan binding with explicitly injected runtime/model boundaries.

The actual rebuild adapter, authored current-input reads and namespace ownership
are real. Planning results and downstream execution are fixture ports: no model,
compiler or generated application executes in these controls.
"""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.qualification import FilesystemQualificationError
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
    FilesystemStandardProjectRuntime,
    PlannedStandardProject,
    StandardProjectRuntimeReadiness,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildAdapter,
    FilesystemStandardRebuildError,
    FilesystemStandardRebuildRequest,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_runner_boundary as authored
from tests.support import fixtures_test_standard_rebuild_adapter as rebuild


class AuthoredExecutionPlanGuardTests(unittest.TestCase):
    def setUp(self):
        f = rebuild.FilesystemStandardRebuildAdapterTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        f.project.definition.test_receipt_policy = None
        self.adapter = FilesystemStandardRebuildAdapter(
            project=f.project,
            binding=f.binding,
            runtime=f.runtime,
            source_cache_configuration=f.source_cache_configuration,
            authority_validator=lambda _: rebuild._identity("reviewed-project"),
        )
        self.plan_identity = canonical_identity({"complete-selected-model-plan": 1})
        self.planned = PlannedStandardProject(
            SimpleNamespace(require_unchanged=lambda: None),
            SimpleNamespace(identity=self.plan_identity),
        )

    def request(self, expected):
        f = self.fixture
        return FilesystemStandardRebuildRequest(
            f.prepared,
            f.source_root,
            f.invalidation,
            expected_execution_plan_identity=expected,
        )

    def authored_fixture(self):
        f = authored.AuthoredQualificationRunnerBoundaryTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_substituted_model_plan_refuses_before_readiness_or_execution(self):
        with (
            patch.object(
                FilesystemStandardProjectRuntime, "plan", return_value=self.planned
            ),
            patch.object(
                FilesystemStandardProjectRuntime, "production_readiness"
            ) as readiness,
            patch.object(FilesystemStandardProjectRuntime, "execute") as execute,
        ):
            with self.assertRaises(FilesystemStandardRebuildError) as error:
                self.adapter.rebuild(
                    self.request(canonical_identity({"substitution": True}))
                )
            self.assertEqual(
                error.exception.code, "standard_rebuild.execution_plan_changed"
            )
            readiness.assert_not_called()
            execute.assert_not_called()
            self.assertFalse(self.fixture.source_root.exists())

    def test_exact_guard_and_legacy_unguarded_request_reach_same_execution_plan(self):
        for expected in (None, self.plan_identity):
            with (
                self.subTest(expected=expected),
                patch.object(
                    FilesystemStandardProjectRuntime, "plan", return_value=self.planned
                ),
                patch.object(
                    FilesystemStandardProjectRuntime,
                    "production_readiness",
                    return_value=StandardProjectRuntimeReadiness(True, ()),
                ),
                patch.object(
                    FilesystemStandardProjectRuntime,
                    "execute",
                    side_effect=RuntimeError("execution boundary"),
                ) as execute,
            ):
                with self.assertRaisesRegex(RuntimeError, "execution boundary"):
                    self.adapter.rebuild(self.request(expected))
                self.assertIs(execute.call_args.args[1].planned, self.planned)
        with self.assertRaises(TypeError):
            self.request(self.plan_identity.uri)

    def test_current_owner_reopens_selected_models_before_execution(self):
        f = self.authored_fixture()
        lock = f.oracles.authority.locked.authority.lock
        changed = plan_component_execution(
            lock,
            model_identities={
                node.revision.identity.uri: canonical_identity(
                    {"different-model": node.revision.identity.uri}
                )
                for node in lock.nodes
            },
        )
        with (
            patch.object(
                FilesystemStandardProjectPlanningAdapter,
                "plan",
                return_value=replace(f.current.standard, execution_plan=changed),
            ),
            patch.object(f.port, "_execute_current") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "plan_changed"):
                f.port.execute(f.request)
            execute.assert_not_called()

    def test_live_guard_preserves_legacy_request_and_invocation_identities(self):
        observed = []

        def remember(value):
            identity = canonical_identity(value)
            if value.get("schema") in {
                "literate-ai/standard-filesystem-rebuild-request@1",
                "literate-ai/standard-in-process-invocation@1",
            }:
                observed.append((value, identity))
            return identity

        pairs = []
        for expected in (None, self.plan_identity):
            observed.clear()
            with (
                patch.object(
                    FilesystemStandardProjectRuntime, "plan", return_value=self.planned
                ),
                patch.object(
                    FilesystemStandardProjectRuntime,
                    "production_readiness",
                    return_value=StandardProjectRuntimeReadiness(True, ()),
                ),
                patch.object(
                    FilesystemStandardProjectRuntime,
                    "execute",
                    side_effect=RuntimeError("execution boundary"),
                ),
                patch(
                    "literate_ai.adapters.standard_rebuild.canonical_identity",
                    side_effect=remember,
                ),
                self.assertRaisesRegex(RuntimeError, "execution boundary"),
            ):
                self.adapter.rebuild(self.request(expected))
            self.assertEqual(len(observed), 2)
            pairs.append(tuple(observed))
        self.assertEqual(pairs[0], pairs[1])

    def test_original_project_review_and_binding_cannot_be_substituted(self):
        f = self.authored_fixture()
        with self.assertRaises(FilesystemQualificationError) as error:
            type(f.port)(**(f.arguments | {"binding": replace(f.current.binding)}))
        self.assertEqual(
            error.exception.code, "qualification.authored.binding_owner_required"
        )
        f.planning.review_identity = canonical_identity({"review-changed": True})
        with patch.object(f.port, "_execute_current") as execute:
            with self.assertRaisesRegex(ValueError, "plan_changed"):
                f.port.execute(f.request)
            execute.assert_not_called()

    def test_authored_run_forwards_exact_plan_to_shared_rebuild_guard(self):
        f = self.authored_fixture()
        adapter = SimpleNamespace(rebuild=lambda _: None)
        with (
            patch(
                "literate_ai.adapters.qualification.assemble_filesystem_standard_rebuild_adapter",
                return_value=adapter,
            ),
            patch.object(
                adapter, "rebuild", side_effect=RuntimeError("shared rebuild boundary")
            ) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "shared rebuild boundary"):
                f.port.execute(f.request)
            request = execute.call_args.args[0]
            self.assertEqual(
                request.expected_execution_plan_identity,
                f.current.plan.execution_plan.identity,
            )
            self.assertFalse(request.update_receipt)

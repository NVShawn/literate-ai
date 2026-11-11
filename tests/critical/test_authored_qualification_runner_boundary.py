"""Current authored admission around an injected Standard execution boundary.

These controls execute no model or generated code. Project/lock/oracle ownership
and allocator admission are real; prepared recipe, driver and Standard execution
are explicit fixtures, so this is not complete lifecycle qualification.
"""

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.generation_preparation import PreparedLockedGeneration
from literate_ai.adapters.qualification import (
    FilesystemQualificationError,
    FilesystemQualificationWorkspaceAllocator,
    FilesystemStandardQualificationLifecyclePort,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentInvalidationDecision,
)
from literate_ai.contracts import ComponentChangeSurface
from literate_ai.contracts.authored_qualification_requests import (
    AuthoredQualificationLifecycleRequest,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_planning as plan_fixtures
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class AuthoredQualificationRunnerBoundaryTests(unittest.TestCase):
    def setUp(self):
        planning = plan_fixtures.AuthoredQualificationPlanningTests("runTest")
        planning.setUp()
        self.addCleanup(planning.doCleanups)
        self.planning = planning
        self.current = planning.plan()
        fixture = planning.fixture
        fixture.authority = self.current.oracles.authority
        self.fixture = fixture
        self.oracles = self.current.oracles
        self.plan = self.current.plan.identity
        self.allocator = FilesystemQualificationWorkspaceAllocator(fixture.root)
        self.addCleanup(self.allocator.close)
        lock = fixture.authority.locked.authority.lock
        revisions = tuple(node.revision.identity for node in lock.nodes)
        root_revision = next(
            node.revision
            for node in lock.nodes
            if node.revision.identity == lock.root_revision
        )
        self.arguments = {
            "project": fixture.authority.project,
            "prepared": PreparedLockedGeneration(
                component_root=fixture.component,
                boundary=fixture.root,
                definition=None,
                recipe=None,
                selected_flavors=(),
                component_revision=root_revision,
                toolchain_constraints=(),
                input_closure=fixture.authority.locked.input_closure,
                flavor_catalog_audit_identity=fixture.authority.locked.catalog_audit_identity,
                locked_authority_snapshot=fixture.authority.locked,
            ),
            "binding": self.current.binding,
            "invalidation": ComponentInvalidationDecision(
                "fixture-authored-qualification",
                lock.root_revision,
                ComponentChangeSurface.LOCAL_AUTHORITY,
                revisions,
                revisions,
                revisions,
            ),
            "workspaces": self.allocator,
            "authored_plan": self.current,
        }
        self.port = FilesystemStandardQualificationLifecyclePort(**self.arguments)
        run = canonical_identity({"fixture-run": 1})
        workspace = self.allocator.allocate_authored(run)
        self.request = AuthoredQualificationLifecycleRequest(
            fixture.authority.origin,
            self.plan,
            self.oracles.identity,
            run,
            workspace.allocation_identity,
        )

    def test_closed_request_round_trips_without_source_history_or_authority(self):
        wire = self.request.to_dict()
        SchemaCatalog().validate(self.request.SCHEMA, wire)
        self.assertEqual(
            AuthoredQualificationLifecycleRequest.from_dict(wire), self.request
        )
        for changed in (
            {**wire, "source_snapshot_identity": self.plan.to_dict()},
            {**wire, "promotion_tree_identity": self.plan.to_dict()},
            {**wire, "force_regeneration": False},
            {**wire, "force_regeneration": 1},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    AuthoredQualificationLifecycleRequest.from_dict(changed)

    def test_exact_current_owner_wraps_the_standard_execution_once(self):
        with patch.object(
            self.port, "_execute_current", return_value="fixture-result"
        ) as run:
            self.assertEqual(self.port.execute(self.request), "fixture-result")
            run.assert_called_once_with(self.request)
            with self.assertRaisesRegex(
                FilesystemQualificationError, "unused workspace"
            ):
                self.port.execute(self.request)
            self.assertEqual(run.call_count, 1)

    def test_plan_origin_or_oracle_substitution_cannot_enter_standard_execution(self):
        other = canonical_identity({"substituted": True})
        for changed in (
            replace(self.request, plan_identity=other),
            replace(self.request, oracle_set_identity=other),
            replace(
                self.request,
                origin=replace(
                    self.request.origin, authored_input_closure_identity=other
                ),
            ),
        ):
            with self.subTest(changed=changed):
                with patch.object(self.port, "_execute_current") as run:
                    with self.assertRaisesRegex(
                        FilesystemQualificationError, "exact current"
                    ):
                        self.port.execute(changed)
                    run.assert_not_called()

    def test_workspace_cannot_be_borrowed_prefilled_or_reused_after_failure(self):
        with patch.object(
            self.port, "_execute_current", side_effect=RuntimeError("native failure")
        ) as run:
            with self.assertRaisesRegex(
                FilesystemQualificationError, "unused workspace"
            ):
                self.port.execute(replace(self.request, run_identity=self.plan))
            run.assert_not_called()
            path = self.allocator.resolve(self.request.workspace_allocation_identity)
            sentinel = path / "stale"
            sentinel.write_text("not clean")
            with self.assertRaisesRegex(FilesystemQualificationError, "still-empty"):
                self.port.execute(self.request)
            run.assert_not_called()
            sentinel.unlink()
            with self.assertRaisesRegex(RuntimeError, "native failure"):
                self.port.execute(self.request)
            with self.assertRaisesRegex(
                FilesystemQualificationError, "unused workspace"
            ):
                self.port.execute(self.request)
            self.assertEqual(run.call_count, 1)

    def test_private_input_drift_during_execution_cannot_return_success(self):
        def mutate(_request):
            path = self.fixture.oracle_root / "harness.py"
            path.write_bytes(path.read_bytes() + b"\n")
            return "fixture-success"

        with patch.object(self.port, "_execute_current", side_effect=mutate):
            with self.assertRaises(ValueError):
                self.port.execute(self.request)

    def test_origin_dto_and_mixed_profiles_cannot_replace_the_current_owner(self):
        for fields in (
            {"authored_plan": self.fixture.authority.origin},
            {"authored_plan": self.current.plan},
            {"profile": object()},
            {"authored_plan": None},
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(FilesystemQualificationError):
                    FilesystemStandardQualificationLifecyclePort(
                        **(self.arguments | fields)
                    )

    def test_replaced_workspace_cannot_enter_or_return_standard_execution(self):
        path = self.allocator.resolve(self.request.workspace_allocation_identity)
        retired = path.with_name("retired-" + path.name)
        if os.name == "nt":
            # The actual held native handles refuse renaming while custody lives.
            with self.assertRaises(OSError):
                path.rename(retired)
            return

        def substitute():
            path.rename(retired)
            path.mkdir()

        def restore():
            path.rmdir()
            retired.rename(path)

        with patch.object(self.port, "_execute_current") as run:
            substitute()
            try:
                with self.assertRaises(ValueError):
                    self.port.execute(self.request)
                run.assert_not_called()
            finally:
                restore()

        def late_substitute(_request):
            substitute()
            return "fixture-success"

        with patch.object(self.port, "_execute_current", side_effect=late_substitute):
            try:
                with self.assertRaises(ValueError):
                    self.port.execute(self.request)
            finally:
                restore()

    def test_unheld_or_expired_namespace_cannot_be_restored_from_request(self):
        other_run = canonical_identity({"unheld-run": True})
        allocation = self.allocator.allocate(other_run)
        unheld = replace(
            self.request,
            run_identity=other_run,
            workspace_allocation_identity=allocation.allocation_identity,
        )
        with patch.object(self.port, "_execute_current") as run:
            with self.assertRaisesRegex(FilesystemQualificationError, "live namespace"):
                self.port.execute(unheld)
            run.assert_not_called()
            self.allocator.close()
            with self.assertRaisesRegex(ValueError, "workspace_owner_expired"):
                self.port.execute(self.request)
            run.assert_not_called()

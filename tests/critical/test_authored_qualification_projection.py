"""Canonical recipe/tool projection and lifetime, without application generation.

Current project/lock/private inputs, recipes, commands and native Python tool
observations are real. Project review/framework/provider admission and the
fixture's macOS platform selection are injected. No generated/native application,
publication or retained consumer runs here.
"""

import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_execution import (
    CurrentAuthoredQualificationExecution,
)
from literate_ai.adapters.authored_qualification_projection import (
    open_authored_execution_projection,
    open_current_authored_qualification_projection,
    reopen_projected_authored_qualification,
)
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PinnedInputClosureError
from tests.critical import test_authored_qualification_planning as planning
from tests.smoke import test_python_toolchain as python_fixtures

MODULE = "literate_ai.adapters.authored_qualification_projection."


class AuthoredQualificationProjectionTests(unittest.TestCase):
    def setUp(self):
        f = planning.AuthoredQualificationPlanningTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        self.current = f.plan()

    @contextmanager
    def projection(self):
        # The lock fixture explicitly targets macOS; tool observations still use
        # the actual unchanged interpreter on this controller, without running an
        # application or claiming native macOS qualification.
        with patch(
            "literate_ai.adapters.standard_project._host_platform_target",
            return_value="macos",
        ):
            with open_current_authored_qualification_projection(self.current) as owner:
                yield owner

    def test_complete_canonical_projection_reads_no_archive_or_project_output(self):
        root = self.current.oracles.authority.project.root

        def files():
            return {
                p.relative_to(root): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }

        before = files()
        with self.projection() as owner:
            expected = {
                p.component_revision
                for p in self.current.plan.execution_plan.generation_plans
            }
            self.assertEqual(set(owner.recipes), expected)
            self.assertEqual(set(owner.commands), expected)
            for revision in expected:
                self.assertEqual(
                    owner.recipes[revision].component_lock_identity,
                    self.current.plan.origin.component_lock_identity,
                )
                self.assertEqual(owner.commands[revision].component_revision, revision)
                self.assertEqual(
                    owner.recipes[revision].library_import_surface,
                    owner.commands[revision].library_import_surface,
                )
            with self.assertRaises(TypeError):
                owner.recipes[next(iter(expected))] = object()
        self.assertEqual(files(), before)

    def test_scope_expiry_blocks_saved_owner_and_reopening(self):
        with self.projection() as owner:
            owner.require_unchanged()
        for operation in (
            owner.require_unchanged,
            lambda: owner.recipes,
            lambda: owner.commands,
        ):
            with self.assertRaisesRegex(ValueError, "projection_owner_expired"):
                operation()
        with (
            patch(MODULE + "reopen_authored_qualification") as reopen,
            self.assertRaisesRegex(ValueError, "projection_owner_expired"),
        ):
            reopen_projected_authored_qualification(
                owner, None, canonical_identity("fixture-products"), max_package_bytes=1
            )
        reopen.assert_not_called()

    def test_passive_plan_or_maps_cannot_restore_projection_or_execution(self):
        with patch(MODULE + "project_locked_standard_toolchain_closure") as native:
            for passive in (self.current.plan, self.current.plan.to_dict(), {}):
                with self.subTest(passive=type(passive).__name__):
                    with self.assertRaises(TypeError):
                        with open_current_authored_qualification_projection(passive):
                            self.fail("passive data restored projection")
                    with self.assertRaises(TypeError):
                        with open_authored_execution_projection(passive):
                            self.fail("passive data restored execution")
            native.assert_not_called()

    def test_authored_input_drift_closes_original_scope(self):
        with self.assertRaises(PinnedInputClosureError):
            with self.projection() as owner:
                path = self.fixture.fixture.component / "specs/spec.md"
                path.write_bytes(path.read_bytes() + b"\nChanged after projection.\n")
                owner.require_unchanged()
        with self.assertRaisesRegex(ValueError, "projection_owner_expired"):
            owner.require_unchanged()

    def test_original_private_oracle_drift_refuses(self):
        with self.assertRaises(PinnedInputClosureError):
            with self.projection() as owner:
                path = self.fixture.fixture.oracle_root / "harness.py"
                path.write_bytes(path.read_bytes() + b"\n# changed\n")
                owner.require_unchanged()

    def test_borrowed_toolchain_plan_is_refused_before_yield(self):
        def substituted(*args, **kwargs):
            closure = project_locked_standard_toolchain_closure(*args, **kwargs)
            return replace(
                closure,
                record=replace(
                    closure.record,
                    execution_plan_identity=canonical_identity("foreign-plan"),
                ),
            )

        with (
            patch(
                MODULE + "project_locked_standard_toolchain_closure",
                side_effect=substituted,
            ),
            self.assertRaisesRegex(ValueError, "projection_changed"),
            self.projection(),
        ):
            self.fail("borrowed closure produced an owner")

    def test_reopening_requires_owned_projections(self):
        with self.projection() as owner:
            with patch(MODULE + "reopen_authored_qualification") as reopen:
                with self.assertRaises(TypeError):
                    reopen_projected_authored_qualification(
                        dict(owner.recipes), None, None, max_package_bytes=1
                    )
                reopen.assert_not_called()
                expected = object()
                reopen.return_value = expected
                self.assertIs(
                    reopen_projected_authored_qualification(
                        owner, None, None, max_package_bytes=1
                    ),
                    expected,
                )
                self.assertIs(reopen.call_args.args[0], self.current)
                self.assertIs(reopen.call_args.kwargs["current_recipes"], owner.recipes)
                self.assertIs(
                    reopen.call_args.kwargs["current_commands"], owner.commands
                )

    def test_reopening_final_guard_refuses_private_input_drift(self):
        def change(*args, **kwargs):
            path = self.fixture.fixture.oracle_root / "harness.py"
            path.write_bytes(path.read_bytes() + b"\n# changed during reopening\n")
            return object()

        with self.assertRaises(PinnedInputClosureError):
            with self.projection() as owner:
                with patch(
                    MODULE + "reopen_authored_qualification", side_effect=change
                ):
                    reopen_projected_authored_qualification(
                        owner, None, None, max_package_bytes=1
                    )

    def test_actual_selected_interpreter_byte_drift_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                python_fixtures, "symlinks_followable", return_value=False
            ):
                executable = python_fixtures._install_python_alias(
                    Path(temporary), "python"
                )
            self.assertFalse(executable.is_symlink())
            tool = discover_python_toolchain(pinned_command=(str(executable),))

            def selected(*args, **kwargs):
                return project_locked_standard_toolchain_closure(
                    *args, **kwargs, toolchain_discoverer=lambda *_: tool
                )

            with (
                patch(
                    MODULE + "project_locked_standard_toolchain_closure",
                    side_effect=selected,
                ),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "executable changed"
                ),
                self.projection() as owner,
            ):
                # Only this test-owned copy changes. The native tool guard must
                # refuse its changed hash before invoking it again.
                executable.write_bytes(executable.read_bytes() + b"\0")
                owner.require_unchanged()

    def test_expired_execution_refuses_before_projection_or_prepared_inputs(self):
        def expired():
            raise ValueError("fixture original execution expired")

        execution = CurrentAuthoredQualificationExecution(None, None, None, expired)
        with (
            patch(MODULE + "project_locked_standard_toolchain_closure") as native,
            self.assertRaisesRegex(ValueError, "original execution expired"),
            open_authored_execution_projection(execution),
        ):
            self.fail("expired execution produced a projection")
        native.assert_not_called()

    def test_passive_native_sdk_inputs_refuse_before_tool_projection(self):
        with patch(MODULE + "project_locked_standard_toolchain_closure") as native:
            for passive in ({}, object()):
                with self.subTest(passive=type(passive).__name__):
                    with self.assertRaises((TypeError, ValueError)):
                        with open_current_authored_qualification_projection(
                            self.current, native_sdk_inputs=passive
                        ):
                            self.fail("passive SDK data supplied native custody")
            native.assert_not_called()

    def test_copied_scope_cannot_borrow_original_guard_or_replace_maps(self):
        with self.projection() as owner:
            for copied in (
                replace(owner),
                replace(owner, _recipes={}),
                replace(owner, _commands={}),
                replace(owner, current=replace(self.current)),
            ):
                with self.subTest(copied=copied):
                    with self.assertRaisesRegex(
                        ValueError, "original_projection_required"
                    ):
                        copied.require_unchanged()
                    with (
                        patch(MODULE + "reopen_authored_qualification") as reopen,
                        self.assertRaisesRegex(
                            ValueError, "original_projection_required"
                        ),
                    ):
                        reopen_projected_authored_qualification(
                            copied, None, None, max_package_bytes=1
                        )
                    reopen.assert_not_called()

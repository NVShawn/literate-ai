"""Locked closures preserve live graph and aliased role observation guards."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import canonical_identity
from tests.unit import test_standard_command_projection as fixture


class StandardDependencyGuardTests(unittest.TestCase):
    def test_live_graph_guard_runs_once_per_closure_check_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = fixture._locked_snapshot(Path(temporary))
            guard = Mock()
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=lambda role, *_: fixture._tool(role),
                dependency_observer=fixture._observation,
                observer_identity=canonical_identity("worker"),
                dependency_guard=guard,
                command_phases=(),
            )
            guard.reset_mock()
            closure.require_unchanged()
            guard.assert_called_once_with()
            guard.side_effect = RuntimeError("remote graph changed")
            with self.assertRaisesRegex(RuntimeError, "remote graph changed"):
                closure.require_unchanged()

    def test_alias_guards_survive_deduplication_and_conflicting_metadata_refuses(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = fixture._locked_snapshot(
                Path(temporary), language="cpp"
            )
            roles = {}
            conflicting = False

            def discover(role, *_):
                tool = SimpleNamespace(
                    identity=canonical_identity("shared worker tool").uri,
                    command=(
                        "/worker/other"
                        if conflicting and role == "python"
                        else "/worker/tool",
                    ),
                    environment=(),
                    require_unchanged=Mock(),
                )
                roles[role] = tool
                return tool

            arguments = dict(
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=fixture._observation,
                observer_identity=canonical_identity("worker"),
                command_phases=(),
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot, execution, **arguments
            )
            self.assertEqual(set(roles), {"cpp", "python"})
            self.assertEqual(len(closure.toolchain_authorities), 1)
            for role in roles.values():
                role.require_unchanged.reset_mock()
            closure.require_unchanged()
            for role in roles.values():
                role.require_unchanged.assert_called_once_with()
            roles["python"].require_unchanged.side_effect = RuntimeError(
                "alias changed"
            )
            with self.assertRaises(ValueError):
                closure.require_unchanged()
            conflicting = True
            with self.assertRaisesRegex(
                ValueError, "aliased tool observations disagree"
            ):
                project_locked_standard_toolchain_closure(
                    snapshot, execution, **arguments
                )

"""Lock-derived command authority can retain observations from another host."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
)
from literate_ai.adapters.remote_standard_toolchains import RemoteStandardToolchains
from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    assemble_standard_lifecycle_ports,
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import canonical_identity
from tests.unit import test_standard_command_projection as fixture
from tests.unit.test_remote_standard_toolchains import snapshot as worker_snapshot
from tests.unit.test_standard_toolchain_observations import observation


class LockedScopedProjectionTests(unittest.TestCase):
    def test_locked_targets_project_without_any_controller_executable(self):
        profiles = (
            {},
            {"package_python": True},
            {"language": "javascript", "package_npm": True},
            {"language": "cpp", "build_system": "cmake"},
            {"language": "cpp", "build_system": "bazel"},
            {"language": "rust", "build_system": "cargo"},
        )
        for profile in profiles:
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                _, snapshot, execution = fixture._locked_snapshot(root, **profile)
                guard = Mock()

                def discover(name, *_, root=root, guard=guard):
                    return SimpleNamespace(
                        command=(str(root / "absent-worker-tools" / name),),
                        environment=(),
                        identity=canonical_identity({"worker-tool": name}).uri,
                        require_unchanged=guard,
                    )

                def npm(node, _environment):
                    result = discover("npm")
                    result.node = node
                    return result

                observation = canonical_identity("explicit-worker-observation")
                with patch.object(
                    LocalComponentToolBinding,
                    "from_observed_toolchain",
                    side_effect=AssertionError("controller constructed a launcher"),
                ):
                    closure = project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform="macos",
                        toolchain_discoverer=discover,
                        npm_toolchain_discoverer=npm,
                        dependency_observer=fixture._observation,
                        observer_identity=observation,
                        command_phases=(),
                    )
                    ports = assemble_standard_lifecycle_ports(
                        source_trees=LocalSourceTreeRegistry(),
                        object_root=root / "objects",
                        toolchain_closure=closure,
                        command_phases=(),
                    ).ports
                self.assertEqual(closure.record.observer_identity, observation)
                self.assertEqual(closure.tool_bindings, ())
                self.assertEqual(ports.tool_bindings, {})
                self.assertFalse((root / "absent-worker-tools").exists())
                guard.assert_called()
                guard.side_effect = RuntimeError("remote tool changed")
                with self.assertRaisesRegex(ValueError, "observed toolchain changed"):
                    closure.require_unchanged()

    def test_missing_explicit_observation_refuses_before_any_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = fixture._locked_snapshot(Path(temporary))
            discover, observe = Mock(), Mock()
            arguments = dict(
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=observe,
                observer_identity=canonical_identity("worker"),
                command_phases=(),
            )
            for missing in (
                "host_platform",
                "toolchain_discoverer",
                "dependency_observer",
                "observer_identity",
            ):
                with (
                    self.subTest(missing=missing),
                    self.assertRaises(StandardCommandProjectionError) as refused,
                ):
                    project_locked_standard_toolchain_closure(
                        snapshot, execution, **{**arguments, missing: None}
                    )
                self.assertEqual(
                    refused.exception.code, "standard_command.observation_required"
                )
            discover.assert_not_called()
            observe.assert_not_called()

    def test_scoped_projection_preserves_locked_contracts_and_closure_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = fixture._locked_snapshot(Path(temporary))
            arguments = dict(
                host_platform="macos",
                toolchain_discoverer=lambda name, *_: fixture._tool(name),
                dependency_observer=fixture._observation,
                observer_identity=canonical_identity("explicit-worker-observation"),
            )
            full = project_locked_standard_toolchain_closure(
                snapshot, execution, **arguments
            )
            custody = project_locked_standard_toolchain_closure(
                snapshot, execution, command_phases=(), **arguments
            )
            self.assertEqual(full.contracts, custody.contracts)
            self.assertEqual(full.record, custody.record)
            self.assertTrue(full.tool_bindings)
            self.assertEqual(custody.tool_bindings, ())

    def test_npm_custody_cannot_fall_back_to_host_npm_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = fixture._locked_snapshot(
                Path(temporary),
                language="javascript",
                platform="linux",
                package_npm=True,
            )
            node = replace(observation("node"), version_info=(22, 0, 0))
            npm = replace(
                node,
                role="npm",
                toolchain_identity=canonical_identity("worker-npm"),
                command=("/worker/tools/npm",),
                version_info=(11, 0, 0),
                node_identity=node.toolchain_identity,
            )
            for version, accepted in (((11, 0, 0), True), ((99, 0, 0), False)):
                consumer = RemoteStandardToolchains(
                    worker_snapshot(node, replace(npm, version_info=version)),
                    require_current=lambda: None,
                )
                discover = Mock(wraps=consumer.discover)
                with (
                    self.subTest(version=version),
                    patch(
                        "literate_ai.adapters.standard_project.discover_npm_toolchain",
                        side_effect=AssertionError("controller npm discovery"),
                    ),
                    patch.object(
                        LocalComponentToolBinding,
                        "from_observed_toolchain",
                        side_effect=AssertionError("controller launcher"),
                    ),
                ):
                    arguments = dict(
                        host_platform=consumer.platform,
                        toolchain_discoverer=discover,
                        dependency_observer=fixture._observation,
                        observer_identity=consumer.observer_identity,
                        command_phases=(),
                    )
                    if accepted:
                        closure = project_locked_standard_toolchain_closure(
                            snapshot, execution, **arguments
                        )
                        self.assertEqual(closure.tool_bindings, ())
                    else:
                        with self.assertRaises(
                            StandardCommandProjectionError
                        ) as refused:
                            project_locked_standard_toolchain_closure(
                                snapshot, execution, **arguments
                            )
                        self.assertEqual(
                            refused.exception.code,
                            "standard_command.npm_toolchain_unavailable",
                        )
                    npm_call = next(
                        call
                        for call in discover.call_args_list
                        if call.args[0] == "npm"
                    )
                    self.assertEqual(npm_call.args[1].toolchain, "npm")
                    npm_call.args[1].version_range()

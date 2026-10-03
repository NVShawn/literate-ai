"""Received tools satisfy exact Flavor constraints without controller discovery."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_capabilities import ActionWorkerCapabilities
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_hardware import (
    HARDWARE_PROBE_TIMEOUT_SECONDS,
    probe_command_hardware,
)
from literate_ai.adapters.action_tool_observation import WorkerToolObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.remote_standard_toolchains import (
    RemoteStandardToolchains,
    project_remote_standard_toolchain_closure,
)
from literate_ai.adapters.standard_toolchain_observations import (
    StandardToolObservations,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import ToolchainConstraint, canonical_identity
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from tests.unit import test_action_tool_observation as transport_fixture
from tests.unit import test_standard_command_projection as projection_fixture
from tests.unit.test_standard_toolchain_observations import observation


def snapshot(*tools):
    inventory = StandardToolObservations(
        "linux", tuple(sorted(tools, key=lambda item: item.role))
    )
    capability = ActionWorkerCapabilities(
        canonical_identity("request"),
        canonical_identity("worker"),
        canonical_identity("receiver"),
        canonical_identity("python"),
        (3, 14, 0),
        (LifecycleActionKind.BUILD,),
        ("filesystem-cas",),
        datetime.now(UTC),
        canonical_identity("profile"),
        tuple(
            sorted(
                {tool.toolchain_identity for tool in tools}, key=lambda item: item.uri
            )
        ),
        inventory.identity,
    )
    return WorkerToolObservation(capability, inventory)


class RemoteStandardToolchainTests(unittest.TestCase):
    def test_worker_verified_alias_retains_live_selector_guard(self):
        verify = Mock(side_effect=lambda selectors: canonical_identity(selectors))
        consumer = RemoteStandardToolchains(
            snapshot(observation()),
            require_current=lambda: None,
            verify_selectors=verify,
        )
        with patch("shutil.which", side_effect=AssertionError("controller PATH")):
            tool = consumer.discover(
                "python", ToolchainConstraint("python", command=("python",)), {}
            )
            self.assertEqual(tool.command, observation().command)
            tool.require_unchanged()
            self.assertEqual(verify.call_args.args[0], {"python": ("python",)})
            with self.assertRaises(ActionWireError):
                consumer.discover(
                    "python",
                    ToolchainConstraint("python", command=("other-python",)),
                    {},
                )
            verify.side_effect = ActionWireError(
                "action_tools.selector_mismatch", "new PATH shadow"
            )
            with self.assertRaises(ActionWireError):
                tool.require_unchanged()

    def test_prefix_minimum_and_exclusive_maximum_boundaries(self):
        guard = Mock()
        consumer = RemoteStandardToolchains(
            snapshot(observation()), require_current=guard
        )
        for arguments, accepted in (
            ({}, True),
            ({"minimum_version": (3, 14)}, True),
            ({"required_version": (3, 14)}, True),
            ({"maximum_exclusive_version": (3, 15)}, True),
            ({"minimum_version": (3, 14, 1)}, False),
            ({"required_version": (3, 13)}, False),
            ({"maximum_exclusive_version": (3, 14)}, False),
        ):
            with self.subTest(arguments=arguments):
                constraint = ToolchainConstraint("python", **arguments)
                if accepted:
                    tool = consumer.discover("python", constraint, {})
                    self.assertEqual(
                        tool.identity, observation().toolchain_identity.uri
                    )
                    tool.require_unchanged()
                else:
                    with self.assertRaises(ActionWireError):
                        consumer.discover("python", constraint, {})
        self.assertGreater(guard.call_count, 1)

    def test_unknown_versions_and_default_runtime_floors_refuse(self):
        for role, version in (
            ("python", (3, 10, 9)),
            ("node", (19, 9, 9)),
            ("zig", (0, 12, 0)),
            ("zig-cc", (0, 12, 0)),
        ):
            value = replace(observation(role), version_info=version)
            with self.subTest(role=role), self.assertRaises(ActionWireError):
                RemoteStandardToolchains(
                    snapshot(value), require_current=lambda: None
                ).discover(role, None, {})
        value = replace(observation("cpp"), version_info=None)
        consumer = RemoteStandardToolchains(
            snapshot(value), require_current=lambda: None
        )
        self.assertIsNone(consumer.discover("cpp", None, {}).version_info)
        with self.assertRaises(ActionWireError):
            consumer.discover(
                "cpp", ToolchainConstraint("cpp", minimum_version=(1,)), {}
            )

    def test_commands_are_exact_data_and_never_resolved_against_controller_path(self):
        consumer = RemoteStandardToolchains(
            snapshot(observation()), require_current=lambda: None
        )
        with (
            patch(
                "shutil.which", side_effect=AssertionError("controller PATH accessed")
            ),
            patch.object(
                LocalComponentToolBinding,
                "from_observed_toolchain",
                side_effect=AssertionError("local launcher"),
            ),
        ):
            tool = consumer.discover(
                "python",
                ToolchainConstraint("python", command=observation().command),
                {},
            )
            self.assertEqual(tool.command, observation().command)
            with self.assertRaises(ActionWireError):
                consumer.discover(
                    "python",
                    ToolchainConstraint("python", command=("python",)),
                    {"PATH": "/worker/tools"},
                )
            with self.assertRaises(ActionWireError):
                consumer.discover("python", ToolchainConstraint("node"), {})
            with self.assertRaises(ActionWireError):
                consumer.discover("missing", None, {})

    def test_npm_keeps_node_relationship_and_requires_bounded_constraint(self):
        node = replace(observation("node"), version_info=(22, 0, 0))
        npm = replace(
            node,
            role="npm",
            toolchain_identity=canonical_identity("npm"),
            version_info=(11, 0, 0),
            node_identity=node.toolchain_identity,
        )
        consumer = RemoteStandardToolchains(
            snapshot(node, npm), require_current=lambda: None
        )
        tool = consumer.discover(
            "npm",
            ToolchainConstraint(
                "npm", minimum_version=(10,), maximum_exclusive_version=(12,)
            ),
            {},
        )
        self.assertEqual(tool.node.identity, node.toolchain_identity.uri)
        for constraint in (
            None,
            ToolchainConstraint("npm", minimum_version=(10,)),
            ToolchainConstraint(
                "npm", minimum_version=(9,), maximum_exclusive_version=(11,)
            ),
        ):
            with (
                self.subTest(constraint=constraint),
                self.assertRaises(ActionWireError),
            ):
                consumer.discover("npm", constraint, {})

    def test_fresh_challenges_keep_stable_observer_identity_and_drift_guard(self):
        value = snapshot(observation())
        guard = Mock()
        first = RemoteStandardToolchains(value, require_current=guard)
        second = RemoteStandardToolchains(
            replace(
                value,
                capability=replace(
                    value.capability,
                    request_identity=canonical_identity("new challenge"),
                    observed_at=datetime.now(UTC) + timedelta(seconds=1),
                ),
            ),
            require_current=lambda: None,
        )
        self.assertEqual(first.observer_identity, second.observer_identity)
        tool = first.discover("python", None, {})
        guard.side_effect = RuntimeError("worker drift")
        with self.assertRaisesRegex(RuntimeError, "worker drift"):
            tool.require_unchanged()
        with self.assertRaises(ValueError):
            RemoteStandardToolchains(
                replace(
                    value,
                    capability=replace(
                        value.capability,
                        build_standard_tools=canonical_identity("other"),
                    ),
                ),
                require_current=lambda: None,
            )

    def test_actual_admitted_receiver_supplies_discovery_and_live_profile_guard(self):
        fixture = transport_fixture.ActionToolObservationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.environment["LITAI_ACTION_WORKER_IDENTITY"] = (
            fixture.worker.identity.uri
        )
        hardware = probe_command_hardware(
            fixture.worker,
            timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
            cwd=fixture.root,
            environment=fixture.environment,
        )
        catalog = ExecutionWorkerCatalog((fixture.worker,))
        pool = CommandActionWorkerPool(
            lambda: catalog,
            lambda: WorkerHardwareObservationCatalog((hardware,)),
            lambda worker: canonical_identity({"healthy": worker.identity.uri}),
            fixture.deadline,
            phase=LifecycleActionKind.INDEX,
            source_handoff="filesystem-cas",
            target_profile="host",
            cwd=fixture.root,
            environment=fixture.environment,
        )
        consumer = RemoteStandardToolchains.from_admission(pool, pool.workers[0])
        _, locked, execution = projection_fixture._locked_snapshot(
            fixture.root / "project", platform=consumer.platform
        )
        with patch.object(
            LocalComponentToolBinding,
            "from_observed_toolchain",
            side_effect=AssertionError("controller launcher"),
        ):
            closure = project_remote_standard_toolchain_closure(
                locked, execution, pool, pool.workers[0]
            )
            self.assertEqual(closure.tool_bindings, ())
            self.assertTrue(closure.dependency_observation.components)
            closure.require_unchanged()
        with patch.object(
            LocalComponentToolBinding,
            "require_unchanged",
            side_effect=AssertionError("controller tool access"),
        ):
            tool = consumer.discover(
                "python", ToolchainConstraint("python", minimum_version=(3, 11)), {}
            )
            tool.require_unchanged()
        pool.environment["BUILD_SETTING"] = "changed"
        with self.assertRaises(ActionWireError):
            tool.require_unchanged()
        with self.assertRaises(ActionWireError):
            closure.require_unchanged()

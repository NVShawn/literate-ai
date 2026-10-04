"""Received tools satisfy exact Flavor constraints without controller discovery."""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

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
from tests.support import fixtures_test_action_tool_observation as transport_fixture
from tests.support import (
    fixtures_test_standard_command_projection as projection_fixture,
)
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_standard_toolchain_observations import observation


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
            maximum_hardware_age=ACTION_TEST_DEADLINE,
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

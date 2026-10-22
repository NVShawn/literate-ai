"""Received tools satisfy exact Flavor constraints without controller discovery."""

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_capabilities import ActionWorkerCapabilities
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_hardware import (
    HARDWARE_PROBE_TIMEOUT_SECONDS,
    probe_command_hardware,
)
from literate_ai.adapters.action_tool_observation import WorkerToolObservation
from literate_ai.adapters.builders._process import run_bounded_process
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
from tests.support.fixtures_test_standard_toolchain_observations import observation

REMOTE_TOOLCHAIN_TEST_DEADLINE = timedelta(hours=24) - timedelta(seconds=1)
_FRESH_WINDOWS_TEST = "LITAI_FRESH_REMOTE_STANDARD_TOOLCHAIN_TEST"
_IS_FRESH_WINDOWS_TEST = os.environ.pop(_FRESH_WINDOWS_TEST, None) == "1"


def _rerun_in_fresh_windows_interpreter(test_name: str) -> bool:
    if os.name != "nt" or _IS_FRESH_WINDOWS_TEST:
        return False
    environment = dict(os.environ)
    environment[_FRESH_WINDOWS_TEST] = "1"
    completed = run_bounded_process(
        (sys.executable, "-m", "unittest", test_name),
        cwd=Path(__file__).resolve().parents[2],
        environment=environment,
        timeout_seconds=30 * 60,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        terminate_descendants=True,
        error_prefix="remote_standard_toolchain_fresh_interpreter",
        trace=False,
    )
    if completed.returncode:
        output = (completed.stdout + completed.stderr).decode("utf-8", "replace")
        raise AssertionError(f"fresh Windows integration interpreter failed:\n{output}")
    return True


def _retry_once_after_clock_correction(exercise) -> None:
    try:
        exercise()
    except ActionWireError as exc:
        if exc.code != "action_wire.expired":
            raise
        exercise()


def _require_observation_changed(operation) -> None:
    try:
        operation()
    except ActionWireError as exc:
        if exc.code != "action_tools.observation_changed":
            raise
    else:
        raise AssertionError("live worker observation unexpectedly remained current")


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
    def test_fresh_windows_interpreter_is_bounded(self):
        completed = Mock(returncode=0, stdout=b"", stderr=b"")
        windows = SimpleNamespace(name="nt", environ={"PRESERVED": "yes"})
        with (
            patch(f"{__name__}.os", windows),
            patch(f"{__name__}.run_bounded_process", return_value=completed) as run,
        ):
            self.assertTrue(_rerun_in_fresh_windows_interpreter("test.name"))
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command, (sys.executable, "-m", "unittest", "test.name"))
        self.assertEqual(options["environment"]["PRESERVED"], "yes")
        self.assertEqual(options["environment"][_FRESH_WINDOWS_TEST], "1")
        self.assertEqual(options["timeout_seconds"], 30 * 60)
        self.assertTrue(options["terminate_descendants"])

    def test_fresh_windows_interpreter_does_not_recurse(self):
        windows = SimpleNamespace(name="nt", environ={})
        with (
            patch(f"{__name__}.os", windows),
            patch(f"{__name__}._IS_FRESH_WINDOWS_TEST", True),
            patch(f"{__name__}.run_bounded_process") as run,
        ):
            self.assertFalse(_rerun_in_fresh_windows_interpreter("test.name"))
        run.assert_not_called()

    def test_clock_correction_retry_is_exactly_once_and_expiry_only(self):
        expired = ActionWireError("action_wire.expired", "expired")
        other = ActionWireError("action_wire.invalid", "invalid")
        for effects, calls, raised in (
            ([expired, None], 2, None),
            ([other], 1, other),
            ([expired, expired], 2, expired),
        ):
            exercise = Mock(side_effect=effects)
            with self.subTest(
                effects=[error.code if error else None for error in effects]
            ):
                if raised is None:
                    _retry_once_after_clock_correction(exercise)
                else:
                    with self.assertRaisesRegex(ActionWireError, str(raised)):
                        _retry_once_after_clock_correction(exercise)
                self.assertEqual(exercise.call_count, calls)

    def test_observation_change_boundary_does_not_swallow_expiry(self):
        _require_observation_changed(
            Mock(
                side_effect=ActionWireError(
                    "action_tools.observation_changed", "changed"
                )
            )
        )
        for error in (
            ActionWireError("action_wire.expired", "expired"),
            ActionWireError("action_tools.constraint_mismatch", "wrong failure"),
        ):
            with (
                self.subTest(code=error.code),
                self.assertRaisesRegex(ActionWireError, str(error)),
            ):
                _require_observation_changed(Mock(side_effect=error))
        with self.assertRaisesRegex(AssertionError, "unexpectedly remained current"):
            _require_observation_changed(Mock())

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
        if _rerun_in_fresh_windows_interpreter(
            f"{__name__}.{type(self).__name__}."
            "test_actual_admitted_receiver_supplies_discovery_and_live_profile_guard"
        ):
            return
        fixture = transport_fixture.ActionToolObservationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.environment["LITAI_ACTION_WORKER_IDENTITY"] = (
            fixture.worker.identity.uri
        )
        # Snapshot preparation is controller-local work and can take minutes under
        # a saturated hosted runner. Complete it before starting the bounded action
        # authority so the deadline covers only worker admission and observation.
        bootstrap_hardware = probe_command_hardware(
            fixture.worker,
            timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
            cwd=fixture.root,
            environment=fixture.environment,
        )
        _, locked, execution = projection_fixture._locked_snapshot(
            fixture.root / "project", platform=bootstrap_hardware.os_family
        )

        def exercise() -> None:
            environment = dict(fixture.environment)
            hardware = probe_command_hardware(
                fixture.worker,
                timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
                cwd=fixture.root,
                environment=environment,
            )
            deadline = ActionDispatchDeadline(
                datetime.now(UTC) + REMOTE_TOOLCHAIN_TEST_DEADLINE
            )
            catalog = ExecutionWorkerCatalog((fixture.worker,))
            pool = CommandActionWorkerPool(
                lambda: catalog,
                lambda: WorkerHardwareObservationCatalog((hardware,)),
                lambda worker: canonical_identity({"healthy": worker.identity.uri}),
                deadline,
                phase=LifecycleActionKind.INDEX,
                source_handoff="filesystem-cas",
                target_profile="host",
                maximum_hardware_age=REMOTE_TOOLCHAIN_TEST_DEADLINE,
                cwd=fixture.root,
                environment=environment,
            )
            consumer = RemoteStandardToolchains.from_admission(pool, pool.workers[0])
            self.assertEqual(consumer.platform, bootstrap_hardware.os_family)
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
                    "python",
                    ToolchainConstraint("python", minimum_version=(3, 11)),
                    {},
                )
                tool.require_unchanged()
            pool.environment["BUILD_SETTING"] = "changed"
            # Discovery reads keep the cheap admission check; the closure guard
            # probes the worker's live profile before trusting its dependencies.
            tool.require_unchanged()
            _require_observation_changed(closure.require_unchanged)

        # A long-lived hosted Windows test interpreter can retain the pre-correction
        # wall clock while its command receivers use the corrected clock. The fresh
        # interpreter above aligns those real processes without weakening the
        # production one-day authority bound. Retry remains expiry-only and exact.
        _retry_once_after_clock_correction(exercise)

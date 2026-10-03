"""Actual receiver observation transport binds admitted profiles and exact tool data."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_capabilities import (
    ACTION_OBSERVATION_TIMEOUT_SECONDS,
    probe_command_action_capabilities,
    run_command_observation,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_tool_observation import (
    MAX_TOOL_RESPONSE_BYTES,
    decode_tool_observation_response,
    probe_command_tool_observations,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.storage import FileSystemCAS
from tests.unit.action_deadline import ACTION_TEST_DEADLINE

_RECEIVER = """
import os, sys
from literate_ai.action_worker import main
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_test_worker import ConfiguredTestWorker
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.action_accept_worker import ConfiguredAcceptWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
tool = discover_python_toolchain(pinned_command=(sys.executable,))
binding = LocalComponentToolBinding.from_observed_toolchain(tool)
worker = ConfiguredBuildWorker(binding, (binding,), environment=dict(os.environ),
    standard_tools={'python':tool} if os.environ['OBSERVE_TOOLS'] == 'yes' else None)
test_worker = ConfiguredTestWorker(binding, (binding,), environment=dict(os.environ))
execute_worker = ConfiguredExecuteWorker(
    binding, (binding,), environment=dict(os.environ))
accept_worker = ConfiguredAcceptWorker(binding, environment=dict(os.environ))
raise SystemExit(main(build_worker=worker, test_worker=test_worker,
    execute_worker=execute_worker, accept_worker=accept_worker))
"""


class ActionToolObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cas = FileSystemCAS(self.root / "cas")
        self.workspace = self.root / "jobs"
        self.workspace.mkdir()
        receiver = self.root / "receiver.py"
        receiver.write_text(_RECEIVER, encoding="utf-8")
        self.worker = ExecutionWorker(
            "tools",
            ExecutionWorkerKind.COMMAND,
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
            command=(
                sys.executable,
                str(receiver),
                "--cas",
                str(self.cas.root),
                "--workspace",
                str(self.workspace),
            ),
            environment=tuple(
                ExecutionWorkerEnvironment(name, name, True)
                for name in (
                    "BUILD_SETTING",
                    "LITAI_ACTION_WORKER_IDENTITY",
                    "OBSERVE_TOOLS",
                    "PYTHONPATH",
                )
            ),
        )
        self.environment = dict(
            os.environ,
            BUILD_SETTING="initial",
            OBSERVE_TOOLS="yes",
            PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)

    def admission(self):
        self.environment["LITAI_ACTION_WORKER_IDENTITY"] = self.worker.identity.uri
        return probe_command_action_capabilities(
            self.worker, self.deadline, cwd=self.root, environment=self.environment
        )

    def probe(self, admitted):
        return probe_command_tool_observations(
            self.worker,
            admitted,
            self.deadline,
            cwd=self.root,
            environment=self.environment,
        )

    def test_actual_stdio_and_request_file_observations_match_admitted_inventory(self):
        for request_file in (False, True):
            with self.subTest(request_file=request_file):
                if request_file:
                    self.worker = replace(
                        self.worker,
                        command=(
                            *self.worker.command,
                            "--request-file",
                            "{request_file}",
                        ),
                    )
                admitted = self.admission()
                self.assertIsNotNone(admitted.test_profile)
                self.assertIsNotNone(admitted.execute_profile)
                self.assertIsNotNone(admitted.accept_profile)
                with patch(
                    "literate_ai.adapters.action_tool_observation.run_command_observation",
                    wraps=run_command_observation,
                ) as transport:
                    result = self.probe(admitted)
                self.assertEqual(
                    result.capability.capability_identity, admitted.capability_identity
                )
                self.assertEqual(result.tools.identity, admitted.build_standard_tools)
                self.assertEqual(result.tools.tools[0].role, "python")
                self.assertEqual(
                    result.tools.tools[0].version_info, tuple(sys.version_info[:3])
                )
                self.assertEqual(
                    transport.call_args.kwargs["stdout_limit_bytes"],
                    MAX_TOOL_RESPONSE_BYTES,
                )
                self.assertLessEqual(
                    transport.call_args.args[1].remaining(),
                    ACTION_OBSERVATION_TIMEOUT_SECONDS,
                )
                self.assertEqual(list(self.workspace.iterdir()), [])

    def test_changed_private_profile_and_unconfigured_observations_refuse(self):
        admitted = self.admission()
        self.environment["BUILD_SETTING"] = "changed"
        with self.assertRaises(ActionWireError):
            self.probe(admitted)
        admitted = self.admission()
        self.environment["OBSERVE_TOOLS"] = "no"
        with self.assertRaises(ActionWireError):
            self.probe(admitted)
        current = self.admission()
        self.assertIsNone(current.build_standard_tools)
        with patch(
            "literate_ai.adapters.action_tool_observation.run_command_observation"
        ) as transport:
            with self.assertRaises(ActionWireError):
                self.probe(current)
            transport.assert_not_called()

    def test_wire_substitution_replay_unknown_fields_and_bounds_refuse(self):
        admitted = self.admission()
        captured = {}

        def transport(*args, **kwargs):
            captured["request"] = args[2]
            captured["response"] = run_command_observation(*args, **kwargs)
            return captured["response"]

        with patch(
            "literate_ai.adapters.action_tool_observation.run_command_observation",
            side_effect=transport,
        ):
            self.probe(admitted)
        document = json.loads(captured["response"])
        altered = json.loads(captured["response"])
        altered["observations"]["tools"][0]["version"] += " changed"
        request = json.loads(captured["request"])
        request["nonce"] = "0" * 32
        for content, query, authority in (
            (canonical_json_bytes(altered), captured["request"], admitted),
            (captured["response"], canonical_json_bytes(request), admitted),
            (
                canonical_json_bytes({**document, "extra": True}),
                captured["request"],
                admitted,
            ),
            (captured["response"] + b"\n", captured["request"], admitted),
            (b"x" * (MAX_TOOL_RESPONSE_BYTES + 1), captured["request"], admitted),
            (b'{"schema":"x","schema":"x"}', captured["request"], admitted),
            (
                captured["response"],
                captured["request"],
                replace(admitted, build_profile=canonical_identity("other")),
            ),
            (
                captured["response"],
                captured["request"],
                replace(admitted, observed_at=datetime.now(UTC) - timedelta(hours=1)),
            ),
        ):
            with self.subTest(content=content[:80]), self.assertRaises(ActionWireError):
                decode_tool_observation_response(
                    content, query, admitted.receiver_identity, authority
                )

    def test_expired_deadline_and_stale_admission_refuse_before_transport(self):
        admitted = self.admission()
        for deadline, authority in (
            (
                ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1)),
                admitted,
            ),
            (
                self.deadline,
                replace(admitted, observed_at=datetime.now(UTC) - timedelta(hours=1)),
            ),
        ):
            self.deadline = deadline
            with patch(
                "literate_ai.adapters.action_tool_observation.run_command_observation"
            ) as transport:
                with self.assertRaises(ActionWireError):
                    self.probe(authority)
                transport.assert_not_called()

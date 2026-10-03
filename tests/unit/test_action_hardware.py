"""Hardware facts originate at the bound command receiver, with bounded custody."""

from __future__ import annotations

import io
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_capabilities import (
    receiver_code_identity,
    run_command_observation,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_hardware import (
    HARDWARE_PROBE_TIMEOUT_SECONDS,
    decode_hardware_request,
    decode_hardware_response,
    encode_hardware_response,
    probe_command_hardware,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.worker_capabilities import (
    WorkerCapabilityProbeError,
    probe_worker_capabilities,
)
from literate_ai.cli.dispatch import main
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorkerCatalog,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit import test_action_source_index as source_fixture
from tests.unit.action_deadline import ACTION_TEST_DEADLINE


class ActionHardwareTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Some cases probe and then exercise the receiver again in one scenario.
        # Each production operation retains its independent bounded timeout.
        self.fixture.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + ACTION_TEST_DEADLINE
        )
        self.worker = replace(
            self.fixture.worker, action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL
        )

    def probe(self, worker=None):
        worker = worker or self.worker
        return probe_command_hardware(
            worker,
            timeout_seconds=HARDWARE_PROBE_TIMEOUT_SECONDS,
            cwd=self.fixture.root,
            environment={**os.environ, "INDEX_WORKER_IDENTITY": worker.identity.uri},
        )

    def request(self, nonce="a" * 32):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/action-hardware-request@1",
                "worker_id": self.worker.worker_id,
                "challenge": {
                    "schema": "literate-ai/action-capability-request@1",
                    "worker_identity": self.worker.identity.uri,
                    "nonce": nonce,
                    "deadline": self.fixture.deadline.to_dict(),
                },
            }
        )

    def test_expiration_identifies_stage_without_relaxing_deadlines_or_other_errors(
        self,
    ):
        names = (
            "receiver_code_identity",
            "run_command_observation",
            "decode_hardware_response",
        )
        for index, stage in enumerate(("code_identity", "transport", "response")):
            for code in ("action_wire.expired", "action_hardware.mismatch"):
                error = ActionWireError(
                    code, "private detail must not become a stage name"
                )
                with (
                    self.subTest(stage=stage, code=code),
                    patch(
                        "literate_ai.adapters.action_hardware." + names[0],
                        return_value=canonical_identity("receiver"),
                    ) as identity,
                    patch(
                        "literate_ai.adapters.action_hardware." + names[1],
                        return_value=b"response",
                    ) as transport,
                    patch(
                        "literate_ai.adapters.action_hardware." + names[2]
                    ) as response,
                ):
                    (identity, transport, response)[index].side_effect = error
                    caller = ActionDispatchDeadline(
                        datetime.now(UTC) + timedelta(seconds=20)
                    )
                    with self.assertRaises(ActionWireError) as caught:
                        probe_command_hardware(
                            self.worker,
                            timeout_seconds=60,
                            deadline=caller,
                            cwd=self.fixture.root,
                            environment={},
                        )
                    self.assertEqual(
                        identity.call_args.kwargs["deadline"].expires_at,
                        caller.expires_at,
                    )
                    if code == "action_wire.expired":
                        self.assertEqual(
                            caught.exception.code, f"action_hardware.{stage}_expired"
                        )
                        self.assertNotIn("private detail", str(caught.exception))
                    else:
                        self.assertIs(caught.exception, error)

    def test_real_stdin_and_file_receivers_collect_hardware_without_workspace_writes(
        self,
    ):
        for worker in (
            self.worker,
            replace(
                self.worker,
                command=(*self.worker.command, "--request-file", "{request_file}"),
            ),
        ):
            with self.subTest(command_file=worker != self.worker):
                started = datetime.now(UTC)
                observation = self.probe(worker)
                self.assertEqual(observation.worker_id, worker.worker_id)
                self.assertGreater(observation.memory_mib, 0)
                self.assertGreater(observation.logical_cpu_cores, 0)
                self.assertGreaterEqual(
                    datetime.fromisoformat(observation.observed_at), started
                )
                self.assertLessEqual(
                    datetime.fromisoformat(observation.observed_at), datetime.now(UTC)
                )
                self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_public_probe_uses_bound_command_not_controller_runner(self):
        with (
            patch.dict(os.environ, {"INDEX_WORKER_IDENTITY": self.worker.identity.uri}),
            patch(
                "literate_ai.adapters.worker_capabilities._run",
                side_effect=AssertionError("controller runner forbidden"),
            ) as run,
        ):
            observed = probe_worker_capabilities(
                self.worker, runner=run, timeout_seconds=60
            )
        run.assert_not_called()
        self.assertEqual(observed.worker_id, self.worker.worker_id)

    def test_legacy_command_and_wrong_private_binding_refuse(self):
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process"
        ) as run:
            with self.assertRaises(WorkerCapabilityProbeError) as raised:
                probe_worker_capabilities(self.fixture.worker)
            self.assertEqual(raised.exception.code, "worker.probe_protocol_unsupported")
            run.assert_not_called()
        with patch.dict(
            os.environ, {"INDEX_WORKER_IDENTITY": canonical_identity("wrong").uri}
        ):
            with self.assertRaises(WorkerCapabilityProbeError) as raised:
                probe_worker_capabilities(self.worker, timeout_seconds=60)
            self.assertEqual(raised.exception.worker_id, self.worker.worker_id)
            self.assertNotIn(str(self.fixture.root), str(raised.exception))

    def test_replay_worker_code_and_malformed_facts_refuse(self):
        content = self.request()
        request, deadline = decode_hardware_request(content)
        started = datetime.now(UTC)
        response = encode_hardware_response(request, deadline, self.worker.identity)
        expected = receiver_code_identity()
        decoded = decode_hardware_response(response, content, expected, started)
        self.assertEqual(decoded.observed_at, started.isoformat())
        with self.assertRaises(ActionWireError):
            decode_hardware_response(
                response, self.request("b" * 32), expected, started
            )
        original = json.loads(response)
        changes = (
            {"worker_identity": canonical_identity("wrong").uri},
            {"receiver_identity": canonical_identity("wrong").uri},
            {"observation": {**original["observation"], "worker_id": "wrong"}},
            {"observation": {**original["observation"], "memory_mib": 0}},
            {"unknown": True},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                decode_hardware_response(
                    canonical_json_bytes({**original, **change}),
                    content,
                    expected,
                    started,
                )

    def test_duplicate_unknown_oversized_and_invalid_worker_requests_refuse(self):
        valid = json.loads(self.request())
        for content in (
            b'{"schema":"x","schema":"y"}',
            b" " * 16385,
            canonical_json_bytes({**valid, "unknown": True}),
            canonical_json_bytes({**valid, "worker_id": "../private"}),
            canonical_json_bytes({**valid, "challenge": None}),
        ):
            with self.subTest(length=len(content)), self.assertRaises(ActionWireError):
                decode_hardware_request(content)

    def test_cli_persists_only_verified_command_observations(self):
        catalog = self.fixture.root / "workers.json"
        output = self.fixture.root / "observations.json"
        catalog.write_bytes(
            canonical_json_bytes(ExecutionWorkerCatalog((self.worker,)).to_dict())
        )
        argv = [
            "worker",
            "probe",
            "--worker-id",
            self.worker.worker_id,
            "--worker-config",
            str(catalog),
            "--output",
            str(output),
            "--timeout-seconds",
            "60",
            "--json",
        ]
        stream = io.StringIO()
        with patch.dict(
            os.environ, {"INDEX_WORKER_IDENTITY": canonical_identity("wrong").uri}
        ):
            self.assertNotEqual(main(argv, stdout=stream, stderr=stream), 0)
        self.assertFalse(output.exists())
        stream = io.StringIO()
        with patch.dict(
            os.environ, {"INDEX_WORKER_IDENTITY": self.worker.identity.uri}
        ):
            self.assertEqual(
                main(argv, stdout=stream, stderr=stream), 0, stream.getvalue()
            )
        self.assertEqual(
            json.loads(output.read_bytes())["workers"][0]["worker_id"],
            self.worker.worker_id,
        )
        stream = io.StringIO()
        with patch.dict(
            os.environ, {"INDEX_WORKER_IDENTITY": canonical_identity("wrong").uri}
        ):
            self.assertNotEqual(main(argv, stdout=stream, stderr=stream), 0)
        self.assertEqual(json.loads(output.read_bytes())["workers"], [])
        self.assertTrue(json.loads(stream.getvalue())["result"]["written"])

    def test_probe_and_collector_have_finite_output_and_owned_process_bounds(self):
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process",
            wraps=run_bounded_process,
        ) as run:
            self.probe()
        options = run.call_args.kwargs
        self.assertLessEqual(options["timeout_seconds"], HARDWARE_PROBE_TIMEOUT_SECONDS)
        self.assertEqual(options["stdout_limit_bytes"], 65536)
        self.assertEqual(options["stderr_limit_bytes"], 4096)
        self.assertTrue(options["terminate_descendants"])
        self.assertTrue(callable(options["interrupt_guard"]))
        request, deadline = decode_hardware_request(self.request())
        with patch(
            "literate_ai.adapters.action_hardware.run_bounded_process",
            wraps=run_bounded_process,
        ) as run:
            encode_hardware_response(request, deadline, self.worker.identity)
        options = run.call_args.kwargs
        self.assertLessEqual(options["timeout_seconds"], HARDWARE_PROBE_TIMEOUT_SECONDS)
        self.assertEqual(options["stdout_limit_bytes"], 262144)
        self.assertEqual(options["stderr_limit_bytes"], 4096)
        self.assertTrue(options["terminate_descendants"])
        self.assertTrue(callable(options["interrupt_guard"]))

    def test_oversized_request_file_is_refused_before_launch(self):
        worker = replace(
            self.worker,
            command=(*self.worker.command, "--request-file", "{request_file}"),
        )
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process"
        ) as run:
            with self.assertRaises(ActionWireError):
                run_command_observation(
                    worker,
                    self.fixture.deadline,
                    b" " * 16385,
                    cwd=self.fixture.root,
                    environment={},
                    mode="--describe-hardware",
                    protocol="literate-ai/action-hardware@1",
                )
        run.assert_not_called()

    def test_outer_action_deadline_cannot_be_extended_by_hardware_probe(self):
        from datetime import timedelta

        from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline

        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with patch(
            "literate_ai.adapters.action_capabilities.run_bounded_process"
        ) as run:
            with self.assertRaises(ActionWireError):
                probe_command_hardware(
                    self.worker,
                    timeout_seconds=60,
                    cwd=self.fixture.root,
                    deadline=expired,
                )
        run.assert_not_called()

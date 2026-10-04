"""Real disposable storage probes and bounded, private protocol refusal."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_storage import (
    WorkerStorageBindings,
    _probe_command,
    _run_probe,
    observe_worker_storage,
)
from literate_ai.application.worker_capacity import (
    CapacityDecision,
    assess_worker_capacity,
)
from literate_ai.contracts import (
    CapacityProbeStatus,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from tests.support.fixtures_test_worker_capacity import (
    JOB,
    ROLES,
    policy,
)

FAMILY = {"darwin": "macos", "linux": "linux", "win32": "windows"}[sys.platform]


def bindings(root):
    return WorkerStorageBindings(
        ExecutionWorker("worker.fixture", ExecutionWorkerKind.LOCAL),
        FAMILY,
        tuple((role, str(root / role)) for role in ROLES),
    )


def selected_policy(bound):
    return replace(policy(), storage_bindings_identity=bound.identity)


class WorkerStorageTests(unittest.TestCase):
    def test_real_probe_groups_one_volume_keeps_paths_private_and_writes_nothing(self):
        with tempfile.TemporaryDirectory(prefix="private worker ") as directory:
            root = Path(directory)
            for role in ROLES:
                (root / role).mkdir()
            (root / "workspace" / "keep.txt").write_text("preserve")
            before = sorted(str(item.relative_to(root)) for item in root.rglob("*"))
            bound = bindings(root)
            selected = selected_policy(bound)
            with patch(
                "literate_ai.adapters.builders._process.trace_subprocess"
            ) as trace:
                observed = observe_worker_storage(bound, selected, job_identity=JOB)
            trace.assert_not_called()
            self.assertEqual(observed.policy_identity, selected.identity)
            self.assertEqual(observed.job_identity, JOB)
            self.assertEqual(
                {item.available_bytes.status for item in observed.samples},
                {CapacityProbeStatus.MEASURED},
            )
            self.assertEqual(len({item.volume for item in observed.samples}), 1)
            self.assertNotIn(str(root), json.dumps(observed.to_dict()))
            self.assertEqual(
                before, sorted(str(item.relative_to(root)) for item in root.rglob("*"))
            )
            self.assertEqual((root / "workspace" / "keep.txt").read_text(), "preserve")
            if FAMILY == "macos":
                self.assertEqual(
                    {
                        quota.available_bytes.status
                        for item in observed.samples
                        for quota in item.quotas
                    },
                    {CapacityProbeStatus.UNSUPPORTED},
                )
                assessed = assess_worker_capacity(
                    selected,
                    observed,
                    worker_id=bound.worker.worker_id,
                    job_identity=JOB,
                    now_ms=time.time_ns() // 1_000_000,
                )
                self.assertNotEqual(assessed.decision, CapacityDecision.PROCEED)

    def test_real_ssh_receiver_protocol_over_disposable_local_transport(self):
        with tempfile.TemporaryDirectory(prefix="private ' $ space ") as directory:
            root = Path(directory)
            for role in ROLES:
                (root / role).mkdir()
            worker = ExecutionWorker(
                "worker.fixture",
                ExecutionWorkerKind.SSH,
                endpoint="fixture@worker.example",
                workspace="/worker",
            )
            bound = replace(
                bindings(root), worker=worker, python_executable=sys.executable
            )

            def run(command, *, environment, input_bytes, timeout_seconds):
                if FAMILY == "windows":
                    # Native Windows command-line interpretation still needs its
                    # hosted SSH qualification; this exercises the same receiver.
                    receiver = _probe_command(
                        executable=sys.executable, timeout_ms=5000
                    )
                else:
                    shell = shlex.split(command[-1])
                    self.assertEqual(shell[:2], ["bash", "-lic"])
                    receiver = tuple(shlex.split(shell[2]))
                return _run_probe(
                    receiver,
                    environment=environment,
                    input_bytes=input_bytes,
                    timeout_seconds=timeout_seconds,
                )

            observed = observe_worker_storage(
                bound, selected_policy(bound), job_identity=JOB, runner=run
            )
            self.assertEqual(
                {v.available_bytes.status for v in observed.samples},
                {CapacityProbeStatus.MEASURED},
            )
            self.assertEqual(len({v.volume for v in observed.samples}), 1)
            self.assertNotIn(directory, json.dumps(observed.to_dict()))
            self.assertTrue(all(not list((root / role).iterdir()) for role in ROLES))

    def test_live_output_flood_and_timeout_are_contained(self):
        bound = bindings(Path.cwd())
        selected = replace(
            selected_policy(bound), maximum_age_ms=100, probe_timeout_ms=100
        )

        def stalled(_command, *, environment, timeout_seconds):
            return _run_probe(
                (sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"),
                environment=environment,
                timeout_seconds=timeout_seconds,
            )

        started = time.monotonic()
        observed = observe_worker_storage(
            bound, selected, job_identity=JOB, runner=stalled
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.TIMED_OUT
        )
        self.assertGreaterEqual(observed.completed_at_ms, observed.expires_at_ms)
        self.assertEqual(type(observed).from_dict(observed.to_dict()), observed)
        result = assess_worker_capacity(
            selected,
            observed,
            worker_id=bound.worker.worker_id,
            job_identity=JOB,
            now_ms=observed.completed_at_ms,
        )
        self.assertNotEqual(result.decision, CapacityDecision.PROCEED)

        selected = selected_policy(bound)

        def flood(_command, *, environment, timeout_seconds):
            return _run_probe(
                (
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import os\nwhile True: os.write(1,b'private'*16384)",
                ),
                environment=environment,
                timeout_seconds=timeout_seconds,
            )

        observed = observe_worker_storage(
            bound, selected, job_identity=JOB, runner=flood
        )
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.MALFORMED
        )
        self.assertNotIn("privateprivate", json.dumps(observed.to_dict()))


if __name__ == "__main__":
    unittest.main()

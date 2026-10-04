"""Explicit command health receivers, private credentials and typed stdin custody."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai import worker_storage_probe as probe
from literate_ai.adapters.worker_storage import (
    WorkerStorageCommand,
    observe_worker_storage,
)
from literate_ai.contracts import (
    CapacityProbeStatus,
    ExecutionWorker,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from tests.support.fixtures_test_worker_capacity import JOB, ROLES
from tests.support.fixtures_test_worker_storage import (
    FAMILY,
    bindings,
    reply,
    selected_policy,
    success,
)


def command_bindings(root):
    return replace(
        bindings(root),
        worker=ExecutionWorker(
            "worker.fixture",
            ExecutionWorkerKind.COMMAND,
            command=("lifecycle-must-not-run",),
            environment=(
                ExecutionWorkerEnvironment("LIFECYCLE_TOKEN", "LIFECYCLE_SOURCE"),
            ),
        ),
        health_command=WorkerStorageCommand(
            (sys.executable, "-B", str(Path(probe.__file__)))
        ),
    )


class WorkerStorageCommandTests(unittest.TestCase):
    def test_real_command_receiver_measures_child_paths_without_request_files(self):
        with tempfile.TemporaryDirectory(prefix="health private ") as directory:
            root = Path(directory)
            for role in ROLES:
                (root / role).mkdir()
            bound = command_bindings(root)
            with patch(
                "literate_ai.adapters.builders._process.trace_subprocess"
            ) as trace:
                observed = observe_worker_storage(
                    bound, selected_policy(bound), job_identity=JOB
                )
            trace.assert_not_called()
            self.assertEqual(
                {s.available_bytes.status for s in observed.samples},
                {CapacityProbeStatus.MEASURED},
            )
            self.assertEqual(len({s.volume for s in observed.samples}), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), list(ROLES))
            self.assertTrue(all(not list((root / r).iterdir()) for r in ROLES))
            self.assertNotIn(directory, json.dumps(observed.to_dict()))

    def test_command_request_binds_context_and_only_declared_credentials_are_forwarded(
        self,
    ):
        bound = command_bindings(Path.cwd())
        bound = replace(
            bound,
            health_command=replace(
                bound.health_command,
                environment=(
                    ExecutionWorkerEnvironment("PROVIDER_TOKEN", "HEALTH_SOURCE"),
                ),
            ),
        )
        selected = selected_policy(bound)

        def run(command, *, environment, input_bytes, timeout_seconds):
            self.assertEqual(command[:-3], bound.health_command.command)
            self.assertEqual(
                command[-3:],
                ("--receive", "--timeout-ms", str(selected.probe_timeout_ms)),
            )
            self.assertEqual(environment["PROVIDER_TOKEN"], "private-health-secret")
            for secret in (
                "UNRELATED_SECRET",
                "LIFECYCLE_SOURCE",
                "LIFECYCLE_TOKEN",
                "HEALTH_SOURCE",
                probe.REQUEST_ENVIRONMENT,
            ):
                self.assertNotIn(secret, environment)
            value = json.loads(input_bytes)
            for key, expected in {
                "schema": probe.REQUEST_PROTOCOL,
                "worker_id": bound.worker.worker_id,
                "worker_identity": bound.worker.identity.uri,
                "policy_identity": selected.identity.uri,
                "job_identity": JOB.uri,
                "os_family": FAMILY,
                "timeout_ms": selected.probe_timeout_ms,
            }.items():
                self.assertEqual(value[key], expected)
            self.assertNotIn("private-health-secret", input_bytes.decode())
            for _, path in bound.paths:
                self.assertNotIn(path, " ".join(command))
            return success(
                reply({probe.REQUEST_ENVIRONMENT: input_bytes.decode()}, selected)
            )

        with (
            patch.dict(
                os.environ,
                {
                    "HEALTH_SOURCE": "private-health-secret",
                    "LIFECYCLE_SOURCE": "private-lifecycle-secret",
                    "UNRELATED_SECRET": "private-unrelated-secret",
                },
            ),
            patch(
                "os.stat",
                side_effect=AssertionError("controller must not stat worker paths"),
            ),
        ):
            observed = observe_worker_storage(
                bound, selected, job_identity=JOB, runner=run
            )
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.MEASURED
        )
        self.assertNotIn("private-health-secret", json.dumps(observed.to_dict()))
        self.assertNotIn(
            "private-health-secret", json.dumps(bound.health_command.to_dict())
        )


if __name__ == "__main__":
    unittest.main()

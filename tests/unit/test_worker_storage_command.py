"""Explicit command health receivers, private credentials and typed stdin custody."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai import worker_storage_probe as probe
from literate_ai.adapters.worker_storage import (
    WorkerStorageCommand,
    WorkerStorageProbeError,
    _run_probe,
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


def request():
    return {
        "schema": probe.REQUEST_PROTOCOL,
        "worker_id": "worker.fixture",
        "worker_identity": "sha256:" + "0" * 64,
        "policy_identity": "sha256:" + "1" * 64,
        "job_identity": None,
        "os_family": FAMILY,
        "timeout_ms": 200,
        "paths": [[r, "C:\\" if FAMILY == "windows" else "/"] for r in ROLES],
        "nonce": "0" * 32,
    }


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

    def test_required_and_optional_credentials_have_distinct_admission(
        self,
    ):
        bound = command_bindings(Path.cwd())
        bound = replace(
            bound,
            health_command=replace(
                bound.health_command,
                environment=(
                    ExecutionWorkerEnvironment(
                        "PROVIDER_TOKEN", "MISSING_HEALTH_SOURCE"
                    ),
                ),
            ),
        )
        with patch.dict(os.environ, {}, clear=True):
            result = observe_worker_storage(
                bound,
                selected_policy(bound),
                job_identity=None,
                runner=lambda *_a, **_kw: self.fail("missing credential must refuse"),
            )
        self.assertEqual(
            result.samples[0].available_bytes.status, CapacityProbeStatus.DENIED
        )
        bound = replace(
            bound,
            health_command=replace(
                bound.health_command,
                environment=(
                    ExecutionWorkerEnvironment(
                        "PROVIDER_TOKEN", "MISSING_HEALTH_SOURCE", False
                    ),
                ),
            ),
        )
        selected = selected_policy(bound)

        def run(_command, *, environment, input_bytes, **_kw):
            self.assertNotIn("PROVIDER_TOKEN", environment)
            return success(
                reply({probe.REQUEST_ENVIRONMENT: input_bytes.decode()}, selected)
            )

        with patch.dict(os.environ, {}, clear=True):
            result = observe_worker_storage(
                bound, selected, job_identity=None, runner=run
            )
        self.assertEqual(
            result.samples[0].available_bytes.status, CapacityProbeStatus.MEASURED
        )

    def test_changed_health_command_or_credential_mapping_invalidates_policy(self):
        bound = command_bindings(Path.cwd())
        selected = selected_policy(bound)
        changes = (
            replace(bound.health_command, command=("another-receiver",)),
            replace(
                bound.health_command,
                environment=(ExecutionWorkerEnvironment("TOKEN", "TOKEN_SOURCE"),),
            ),
        )
        for command in changes:
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(WorkerStorageProbeError, "binding_mismatch"),
            ):
                observe_worker_storage(
                    replace(bound, health_command=command),
                    selected,
                    job_identity=JOB,
                    runner=lambda *_a, **_kw: self.fail("changed policy must refuse"),
                )

    def test_invalid_or_oversized_private_command_configuration_refuses(self):
        for value in (
            (),
            ["receiver"],
            ("",),
            ("receiver", "{request_file}"),
            ("x" * 4096,) * 3,
        ):
            with self.subTest(command=value), self.assertRaises(ValueError):
                WorkerStorageCommand(value)
        binding = ExecutionWorkerEnvironment("TOKEN", "TOKEN_SOURCE")
        for values in (
            (binding, binding),
            (ExecutionWorkerEnvironment(probe.REQUEST_ENVIRONMENT, "TOKEN_SOURCE"),),
        ):
            with self.assertRaises(WorkerStorageProbeError):
                WorkerStorageCommand(("receiver",), values)
        with self.assertRaises(WorkerStorageProbeError):
            replace(
                bindings(Path.cwd()), health_command=WorkerStorageCommand(("receiver",))
            )

    def test_oversized_private_environment_is_malformed_before_launch(self):
        bound = command_bindings(Path.cwd())
        bound = replace(
            bound,
            health_command=replace(
                bound.health_command,
                environment=(ExecutionWorkerEnvironment("TOKEN", "HEALTH_SOURCE"),),
            ),
        )
        with patch.dict(os.environ, {"HEALTH_SOURCE": "private" * 4096}):
            observed = observe_worker_storage(
                bound,
                selected_policy(bound),
                job_identity=JOB,
                runner=lambda *_a, **_kw: self.fail("oversized credential must refuse"),
            )
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.MALFORMED
        )
        self.assertNotIn("privateprivate", json.dumps(observed.to_dict()))

    def test_receiver_rejects_invalid_request_context_before_measurement(self):
        original = request()
        changes = (
            {"schema": "other"},
            {"worker_id": "../private"},
            {"worker_identity": "other"},
            {"policy_identity": None},
            {"job_identity": []},
            {"os_family": "other"},
            {"os_family": "linux" if FAMILY != "linux" else "windows"},
            {"timeout_ms": True},
            {"timeout_ms": 0},
            {"timeout_ms": 60001},
            {"nonce": "x" * 32},
            {"paths": [[r, "relative"] for r in ROLES]},
        )
        with patch.object(
            probe, "collect", side_effect=AssertionError("no measurement")
        ):
            for change in changes:
                with self.subTest(change=change):
                    self.assertEqual(
                        probe.main(raw=json.dumps({**original, **change}).encode()), 2
                    )
            self.assertEqual(probe.main(raw=b'{"schema":1,"schema":2}'), 2)

    def test_receiver_honors_request_deadline_even_with_larger_command_ceiling(self):
        source = Path(probe.__file__).read_text()
        script = (
            "import time;s={'__name__':'fixture'};exec("
            + repr(source)
            + ",s);s['collect']=lambda paths:time.sleep(60);"
            + "raise SystemExit(s['receive'](10000))"
        )
        payload = json.dumps(request()).encode()
        started = time.monotonic()
        result = _run_probe(
            (sys.executable, "-I", "-S", "-B", "-c", script),
            environment=dict(os.environ),
            timeout_seconds=5,
            input_bytes=payload,
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.returncode, 0)
        response = json.loads(result.stdout)
        self.assertEqual(
            response["request_identity"],
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(
            {s["available_bytes"]["status"] for s in response["samples"]}, {"timed-out"}
        )

    def test_timeout_still_exits_when_no_reporting_thread_can_start(self):
        source = Path(probe.__file__).read_text()
        script = (
            "import time\n"
            "s={'__name__':'fixture'}\n"
            "exec(" + repr(source) + ",s)\n"
            "original_timer=s['threading'].Timer\n"
            "class UnavailableThread:\n"
            " def start(self): raise RuntimeError('no thread capacity')\n"
            "s['threading'].Timer=lambda delay,target: "
            "UnavailableThread() if delay==0.1 else original_timer(delay,target)\n"
            "s['collect']=lambda paths:time.sleep(60)\n"
            "raise SystemExit(s['receive'](200))\n"
        )
        result = _run_probe(
            (sys.executable, "-I", "-S", "-B", "-c", script),
            environment=dict(os.environ),
            timeout_seconds=5,
            input_bytes=json.dumps(request()).encode(),
        )
        self.assertEqual(result.returncode, 124)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_command_reply_cannot_substitute_request_context(self):
        bound = command_bindings(Path.cwd())
        selected = selected_policy(bound)

        def run(_command, *, input_bytes, **_kw):
            changed = json.loads(input_bytes)
            changed["job_identity"] = None
            value = reply({probe.REQUEST_ENVIRONMENT: json.dumps(changed)}, selected)
            return success(value)

        observed = observe_worker_storage(bound, selected, job_identity=JOB, runner=run)
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.MALFORMED
        )


if __name__ == "__main__":
    unittest.main()

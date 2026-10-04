"""Real disposable storage probes and bounded, private protocol refusal."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import worker_storage_probe as probe
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.worker_storage import (
    WorkerStorageBindings,
    WorkerStorageProbeError,
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
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from tests.support.fixtures_test_worker_capacity import JOB, NOW, ROLES, observation, policy

FAMILY = {"darwin": "macos", "linux": "linux", "win32": "windows"}[sys.platform]


def bindings(root):
    return WorkerStorageBindings(
        ExecutionWorker("worker.fixture", ExecutionWorkerKind.LOCAL),
        FAMILY,
        tuple((role, str(root / role)) for role in ROLES),
    )


def selected_policy(bound):
    return replace(policy(), storage_bindings_identity=bound.identity)


def reply(environment, selected):
    raw = environment[probe.REQUEST_ENVIRONMENT].encode()
    return {
        "schema": probe.PROTOCOL,
        "request_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "os_family": FAMILY,
        "samples": [item.to_dict() for item in observation(selected).samples],
    }


def success(value):
    return BoundedProcessResult(0, json.dumps(value).encode(), b"")


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

    def test_missing_directory_is_not_created_or_treated_as_parent_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bound = bindings(root)
            observed = observe_worker_storage(
                bound, selected_policy(bound), job_identity=None
            )
            self.assertEqual(
                {item.available_bytes.status for item in observed.samples},
                {CapacityProbeStatus.UNAVAILABLE},
            )
            self.assertEqual(list(root.iterdir()), [])

    def test_ssh_worker_uses_private_stdin_and_never_stats_controller_paths(self):
        worker = ExecutionWorker(
            "worker.fixture",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="linux"),
            endpoint="fixture@worker.example",
            workspace="/worker",
            transport="fixture-ssh",
        )
        bound = WorkerStorageBindings(
            worker, "linux", tuple((role, f"/private/{role}") for role in ROLES)
        )
        selected = selected_policy(bound)

        def run(command, *, environment, input_bytes, timeout_seconds):
            self.assertEqual(command[0], "fixture-ssh")
            self.assertIn(worker.endpoint, command)
            self.assertNotIn(probe.REQUEST_ENVIRONMENT, environment)
            for _role, path in bound.paths:
                self.assertNotIn(path, " ".join(command))
                self.assertIn(path.encode(), input_bytes)
            value = reply({probe.REQUEST_ENVIRONMENT: input_bytes.decode()}, selected)
            value["os_family"] = "linux"
            return success(value)

        with patch("os.stat", side_effect=AssertionError("controller stat")):
            observed = observe_worker_storage(
                bound, selected, job_identity=JOB, runner=run
            )
        self.assertEqual(
            {item.available_bytes.status for item in observed.samples},
            {CapacityProbeStatus.MEASURED},
        )
        self.assertNotIn("worker.example", json.dumps(observed.to_dict()))

    def test_command_worker_still_requires_explicit_health_protocol(self):
        bound = replace(
            bindings(Path.cwd()),
            worker=ExecutionWorker(
                "worker.fixture", ExecutionWorkerKind.COMMAND, command=("dispatcher",)
            ),
        )
        observed = observe_worker_storage(
            bound,
            selected_policy(bound),
            job_identity=JOB,
            runner=lambda *_args, **_kwargs: self.fail("lifecycle is not health"),
        )
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.UNSUPPORTED
        )

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

    def test_ssh_transport_failure_and_remote_watchdog_are_distinct(self):
        bound = replace(
            bindings(Path.cwd()),
            worker=ExecutionWorker(
                "worker.fixture",
                ExecutionWorkerKind.SSH,
                endpoint="fixture@worker.example",
                workspace="/worker",
            ),
        )
        for code, status in (
            (255, CapacityProbeStatus.UNREACHABLE),
            (124, CapacityProbeStatus.TIMED_OUT),
            (127, CapacityProbeStatus.UNAVAILABLE),
            (2, CapacityProbeStatus.MALFORMED),
        ):
            with self.subTest(code=code):
                observed = observe_worker_storage(
                    bound,
                    selected_policy(bound),
                    job_identity=JOB,
                    runner=lambda *_a, code=code, **_kw: BoundedProcessResult(
                        code, b"", b"private error"
                    ),
                )
                self.assertEqual(observed.samples[0].available_bytes.status, status)
                self.assertNotIn("private error", json.dumps(observed.to_dict()))

    def test_transport_refusal_remains_an_unsupported_observation(self):
        # The worker contract admits IPv6, but this shared SSH transport does
        # not currently admit colon-bearing endpoints. Do not fabricate capacity
        # or leak its private endpoint when transport validation refuses it.
        bound = replace(
            bindings(Path.cwd()),
            worker=ExecutionWorker(
                "worker.fixture",
                ExecutionWorkerKind.SSH,
                endpoint="fixture@2001:db8::1",
                workspace="/worker",
            ),
        )
        observed = observe_worker_storage(
            bound,
            selected_policy(bound),
            job_identity=JOB,
            runner=lambda *_a, **_kw: self.fail("transport refused before launch"),
        )
        self.assertEqual(
            observed.samples[0].available_bytes.status, CapacityProbeStatus.UNSUPPORTED
        )
        self.assertNotIn("2001:db8", json.dumps(observed.to_dict()))

    def test_remote_watchdog_stops_incomplete_input_without_controller_timeout(self):
        process = subprocess.Popen(
            _probe_command(timeout_ms=200),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.wait(timeout=5), 124)
            stdout, stderr = process.communicate()
            self.assertEqual((stdout, stderr), (b"", b""))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_remote_watchdog_stops_blocked_measurement(self):
        # The real receiver's watchdog remains active through a stalled OS probe.
        source = Path(probe.__file__).read_text()
        script = (
            "import time;s={'__name__':'fixture'};exec(" + repr(source) + ",s);"
            "s['collect']=lambda paths:time.sleep(60);"
            "raise SystemExit(s['receive'](200))"
        )
        payload = json.dumps(
            {
                "paths": [[r, "C:\\" if FAMILY == "windows" else "/"] for r in ROLES],
                "nonce": "0" * 32,
                "schema": probe.REQUEST_PROTOCOL,
                "worker_id": "worker.fixture",
                "worker_identity": "sha256:" + "0" * 64,
                "policy_identity": "sha256:" + "1" * 64,
                "job_identity": None,
                "os_family": FAMILY,
                "timeout_ms": 200,
            }
        ).encode()
        result = _run_probe(
            (sys.executable, "-I", "-S", "-B", "-c", script),
            environment=dict(os.environ),
            timeout_seconds=5,
            input_bytes=payload,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        response = json.loads(result.stdout)
        self.assertEqual(
            response["request_identity"],
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(
            {s["available_bytes"]["status"] for s in response["samples"]}, {"timed-out"}
        )

    def test_windows_receiver_command_has_no_shell_metacharacters_from_configuration(
        self,
    ):
        worker = ExecutionWorker(
            "worker.fixture",
            ExecutionWorkerKind.SSH,
            endpoint="fixture@worker.example",
            workspace="~/worker",
        )
        bound = WorkerStorageBindings(
            worker,
            "windows",
            tuple((r, "C:\\") for r in ROLES),
            python_executable="C:\\Program Files\\Python\\python.exe",
        )
        for invalid in (
            "%TEMP%/python.exe",
            "python&other",
            'python"evil',
            "$(command)",
        ):
            with (
                self.subTest(value=invalid),
                self.assertRaises(WorkerStorageProbeError),
            ):
                replace(bound, python_executable=invalid)

    def test_changed_binding_refuses_before_dispatch_and_paths_are_not_in_argv(self):
        bound = bindings(Path.cwd())
        selected = selected_policy(bound)
        changed = replace(
            bound, paths=tuple((role, path + "changed") for role, path in bound.paths)
        )
        with self.assertRaisesRegex(WorkerStorageProbeError, "binding_mismatch"):
            observe_worker_storage(
                changed,
                selected,
                job_identity=JOB,
                runner=lambda *_args, **_kwargs: self.fail("must refuse"),
            )

        def run(command, *, environment, timeout_seconds):
            self.assertGreater(timeout_seconds, 0)
            self.assertLessEqual(timeout_seconds, selected.probe_timeout_ms / 1000)
            request = json.loads(environment[probe.REQUEST_ENVIRONMENT])
            self.assertEqual(request["paths"], [list(item) for item in bound.paths])
            for _role, path in bound.paths:
                self.assertNotIn(path, " ".join(command))
            return success(reply(environment, selected))

        observed = observe_worker_storage(bound, selected, job_identity=JOB, runner=run)
        self.assertTrue(
            all(
                item.available_bytes.status is CapacityProbeStatus.MEASURED
                for item in observed.samples
            )
        )

    def test_protocol_substitution_and_unbounded_responses_are_malformed(self):
        bound = bindings(Path.cwd())
        selected = selected_policy(bound)
        changes = (
            lambda value: {**value, "request_identity": "sha256:" + "0" * 64},
            lambda value: {**value, "os_family": "other"},
            lambda value: {**value, "schema": "other"},
            lambda value: {**value, "samples": value["samples"][:-1]},
            lambda value: {**value, "samples": value["samples"][::-1]},
            lambda value: {**value, "private_path": "/secret"},
        )
        for change in changes:
            with self.subTest(change=change):
                observed = observe_worker_storage(
                    bound,
                    selected,
                    job_identity=JOB,
                    runner=lambda _command, *, environment, change=change, **_kwargs: (
                        success(change(reply(environment, selected)))
                    ),
                )
                self.assertEqual(
                    {item.available_bytes.status for item in observed.samples},
                    {CapacityProbeStatus.MALFORMED},
                )
        for raw in (
            b"x" * (probe.MAX_RESPONSE_BYTES + 1),
            b'{"schema":1,"schema":2}',
            b"\xff",
            b"[]",
            b"[" * 2000 + b"]" * 2000,
        ):
            with self.subTest(length=len(raw)):
                observed = observe_worker_storage(
                    bound,
                    selected,
                    job_identity=JOB,
                    runner=lambda *_args, raw=raw, **_kwargs: BoundedProcessResult(
                        0, raw, b""
                    ),
                )
                self.assertEqual(
                    observed.samples[0].available_bytes.status,
                    CapacityProbeStatus.MALFORMED,
                )

    def test_live_timeout_returns_failed_observation_after_expiry(self):
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

    def test_live_output_flood_is_stopped_and_retains_no_payload(self):
        bound = bindings(Path.cwd())
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

    @unittest.skipIf(os.name == "nt", "POSIX statvfs accounting fixture")
    def test_unix_probe_uses_unprivileged_blocks_and_inodes(self):
        node = SimpleNamespace(st_dev=7, st_ino=2)
        space = SimpleNamespace(
            f_blocks=1000,
            f_frsize=4096,
            f_bavail=10,
            f_bfree=100,
            f_files=100,
            f_favail=5,
            f_ffree=50,
            f_fsid=4,
        )
        with (
            patch.object(probe.os, "open", return_value=10),
            patch.object(probe.os, "close"),
            patch.object(probe.os, "fstat", return_value=node),
            patch.object(probe.os, "stat", return_value=node),
            patch.object(probe.os, "fstatvfs", return_value=space),
            patch.object(
                probe,
                "_native_quotas",
                return_value=[
                    {
                        "domain": "unix-unobserved",
                        "available_bytes": {"status": "unsupported", "value": None},
                        "available_inodes": {"status": "unsupported", "value": None},
                    }
                ],
            ),
        ):
            value = probe._unix_sample("temp", "/private")
        self.assertEqual(value["available_bytes"]["value"], 10 * 4096)
        self.assertEqual(value["available_inodes"]["value"], 5)
        self.assertEqual(value["quotas"][0]["available_bytes"]["status"], "unsupported")

    def test_windows_probe_uses_caller_quota_capacity_and_volume_guid(self):
        class Function:
            def __init__(self, run):
                self.run = run

            def __call__(self, *args):
                return self.run(*args)

        def mount(_path, out, _length):
            out.value = "C:\\"
            return 1

        def guid(_path, out, _length):
            out.value = "volume-fixture"
            return 1

        def capacity(_path, available, total, physical_free):
            available._obj.value = 10
            total._obj.value = 100
            physical_free._obj.value = 1000
            return 1

        kernel = SimpleNamespace(
            GetVolumePathNameW=Function(mount),
            GetVolumeNameForVolumeMountPointW=Function(guid),
            GetDiskFreeSpaceExW=Function(capacity),
        )
        node = SimpleNamespace(st_dev=7, st_ino=2, st_mode=0o040700)
        with patch.object(probe.os, "stat", return_value=node):
            value = probe._windows_sample("cache", "C:\\private", kernel=kernel)
        self.assertEqual(value["available_bytes"]["value"], 10)
        self.assertEqual(value["total_bytes"], 100)
        self.assertEqual(value["quotas"][0]["available_bytes"]["value"], 10)
        self.assertEqual(value["available_inodes"]["status"], "not-applicable")
        self.assertNotIn("volume-fixture", json.dumps(value))
        kernel.GetVolumeNameForVolumeMountPointW = Function(lambda *_args: 0)
        with (
            patch.object(probe.os, "stat", return_value=node),
            patch.object(probe.ctypes, "get_last_error", return_value=5, create=True),
        ):
            denied = probe._windows_sample("cache", "C:\\private", kernel=kernel)
        self.assertEqual(denied["available_bytes"]["status"], "denied")

    @unittest.skipIf(os.name == "nt", "POSIX permission classification fixture")
    def test_denied_native_probe_reports_status_without_private_error_text(self):
        with patch.object(
            probe,
            "_unix_sample",
            side_effect=PermissionError(13, "denied", "/private-sensitive/path"),
        ):
            _family, samples = probe.collect([("temp", "/private-sensitive/path")])
        self.assertEqual(samples[0]["available_bytes"]["status"], "denied")
        self.assertNotIn("private-sensitive", json.dumps(samples))

    def test_backward_clock_and_relative_bindings_refuse(self):
        bound = bindings(Path.cwd())
        selected = selected_policy(bound)
        ticks = iter((NOW, NOW - 1))
        with self.assertRaisesRegex(WorkerStorageProbeError, "clock_reversed"):
            observe_worker_storage(
                bound,
                selected,
                job_identity=JOB,
                clock_ms=lambda: next(ticks),
                runner=lambda _command, *, environment, **_kwargs: success(
                    reply(environment, selected)
                ),
            )
        with self.assertRaisesRegex(WorkerStorageProbeError, "not_absolute"):
            replace(bound, paths=tuple((role, "relative") for role in ROLES))


if __name__ == "__main__":
    unittest.main()

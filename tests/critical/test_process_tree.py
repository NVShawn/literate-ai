from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from unittest import mock

from literate_ai.adapters import _processes
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    encode_action_request,
)
from literate_ai.adapters.freebsd_owned_attempt import (
    FreeBsdOwnedAttemptError,
    run_owned_freebsd_attempt,
)
from literate_ai.adapters.retained_receiver_attempt import (
    reserve_retained_receiver_attempt,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.retained_package_receiver import CONFIG_SCHEMA, _Registration


@unittest.skipUnless(
    sys.platform.startswith("freebsd"), "requires native FreeBSD reaper"
)
class FreeBsdAttemptOwnershipTests(unittest.TestCase):
    """Real kernel ownership must reach nested sessions and survive parent loss."""

    def test_completed_orphans_are_reaped_before_primary_failure_exit(self):
        root = Path(tempfile.mkdtemp(prefix="freebsd-live-reaping-")).resolve()
        cleanup_verified = False
        driver = r"""
import json,os,pathlib,subprocess,sys,time
from literate_ai.adapters._processes import run_with_tree_kill
root=pathlib.Path.cwd()
pid_file=root/'grandchild.pid'
child=root/'grandchild.py'
child.write_text(
 "import os,pathlib,time;pathlib.Path("+repr(str(pid_file))+
 ").write_text(str(os.getpid()));time.sleep(60)"
)
parent=root/'parent.py'
parent.write_text(
 "import subprocess,sys,time;subprocess.Popen([sys.executable,"+
 repr(str(child))+"]);time.sleep(60)"
)
try:run_with_tree_kill([sys.executable,str(parent)],timeout=1)
except subprocess.TimeoutExpired:pass
else:raise RuntimeError('fixture did not time out')
pid=int(pid_file.read_text())
deadline=time.monotonic()+5
while True:
 try:os.kill(pid,0)
 except ProcessLookupError:break
 if time.monotonic()>=deadline:
  raise RuntimeError('dead orphan retained until primary exit')
 time.sleep(0.05)
print(json.dumps({'orphan_reaped_before_primary_exit':True,'orphan_pid':pid}),flush=True)
raise SystemExit(17)
"""
        try:
            try:
                result = run_owned_freebsd_attempt(
                    [sys.executable, "-I", "-c", driver],
                    cwd=root,
                    staging_root=root,
                    environment={"PATH": os.environ["PATH"], "HOME": str(root)},
                    expires_at=datetime.now(UTC) + timedelta(seconds=30),
                    admission_guard=lambda: None,
                    stdout_limit_bytes=4096,
                    stderr_limit_bytes=4096,
                )
                cleanup_verified = True
            except FreeBsdOwnedAttemptError as exc:
                cleanup_verified = exc.cleanup_verified
                raise
            self.assertEqual(result.returncode, 17, result.stderr.decode())
            observation = json.loads(result.stdout)
            self.assertTrue(observation["orphan_reaped_before_primary_exit"])
            with self.assertRaises(ProcessLookupError):
                os.kill(observation["orphan_pid"], 0)
        finally:
            if cleanup_verified:
                shutil.rmtree(root)
            else:
                raise RuntimeError(
                    f"native ownership unproven; fixture retained at {root}"
                )

    def test_parent_boundary_preserves_failure_status_and_cleans_revoked_nested_job(
        self,
    ):
        root = Path(tempfile.mkdtemp(prefix="freebsd-owned-parent-test-")).resolve()
        cleanup_verified = True
        options = {
            "cwd": root,
            "staging_root": root,
            "environment": {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "TMPDIR": str(root),
                "LC_ALL": "C",
            },
            "stdout_limit_bytes": 4096,
            "stderr_limit_bytes": 4096,
        }
        try:
            cleanup_verified = False
            dispatch, deadline, registration = self.dispatch_request()
            config_path = root / "private-receiver.json"

            def refuse_prelaunch():
                raise RuntimeError("fixture admission revoked before launch")

            with self.assertRaisesRegex(RuntimeError, "before launch"):
                with reserve_retained_receiver_attempt(
                    config_path,
                    dispatch,
                    registration,
                    deadline,
                    preflight=refuse_prelaunch,
                ):
                    self.fail("refused preflight published an owned attempt")
            self.assertFalse((root / "private-receiver.json.attempt").exists())
            never_started = root / "never-started.pid"
            with reserve_retained_receiver_attempt(
                config_path, dispatch, registration, deadline
            ) as attempt:
                try:
                    run_owned_freebsd_attempt(
                        [
                            sys.executable,
                            "-c",
                            f"open({str(never_started)!r},'w').write('started')",
                        ],
                        **options,
                        expires_at=datetime.now(UTC) + timedelta(seconds=20),
                        admission_guard=refuse_prelaunch,
                    )
                except FreeBsdOwnedAttemptError as exc:
                    cleanup_verified = exc.cleanup_verified
                    self.assertIsNone(exc.guardian_pid)
                    self.assertEqual(exc.reason, "prelaunch-admission-revoked")
                    self.assertFalse(exc.quarantine_required)
                    attempt.retain_diagnostic(exc)
                    attempt.release_before_native_start(exc)
                else:
                    self.fail("refused initial admission launched native work")
            self.assertFalse(never_started.exists())
            self.assertFalse((root / "private-receiver.json.attempt").exists())
            cleanup_verified = False
            with reserve_retained_receiver_attempt(
                config_path, dispatch, registration, deadline
            ) as attempt:
                result = run_owned_freebsd_attempt(
                    [
                        sys.executable,
                        "-c",
                        "import sys;"
                        "from literate_ai.adapters import "
                        "freebsd_owned_attempt as owned;"
                        "owned.OwnedFreeBsdChild().require_current();"
                        "sys.stdout.buffer.write(sys.stdin.buffer.read());"
                        "sys.stderr.write('owned');sys.exit(17)",
                    ],
                    **options,
                    input_bytes=b"owned-input\x00",
                    expires_at=datetime.now(UTC) + timedelta(seconds=20),
                    admission_guard=lambda: None,
                )
                attempt.retain_completion(result)
                # A second publication must preserve existing recovery bytes.
                # The wrapper has already proved cleanup, so the actual parent
                # still releases its own marker when journaling refuses.
                with self.assertRaises(FileExistsError):
                    try:
                        attempt.retain_completion(result)
                    finally:
                        attempt.release_after_native_empty()
            self.assertFalse((root / "private-receiver.json.attempt").exists())
            cleanup_verified = True
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertEqual(result.stdout, b"owned-input\x00")
            self.assertEqual(result.stderr, b"owned")
            completed = [
                json.loads(path.read_bytes())
                for path in root.glob("private-receiver.json.attempt.*.json")
                if json.loads(path.read_bytes()).get("schema")
                == "literate-ai/retained-receiver-completed-attempt@1"
            ]
            self.assertEqual(len(completed), 1)
            journal = completed[0]
            frame = (root / journal["guardian_response_file"]).read_bytes()
            self.assertEqual(
                hashlib.sha256(frame).hexdigest(), journal["guardian_response_sha256"]
            )
            report = json.loads(frame)
            self.assertEqual(report["guardian_pid"], result.guardian_pid)
            self.assertTrue(report["cleanup"]["empty"])
            self.assertEqual(report["cleanup"]["children"], 0)
            self.assertEqual(report["cleanup"]["descendants"], 0)
            self.assertEqual(base64.b64decode(report["stdout"]), b"owned-input\x00")
            self.assertEqual(base64.b64decode(report["stderr"]), b"owned")
            self.assertEqual(journal["returncode"], 17)
            self.assertFalse(journal["admitted"])
            # Cancellation remains sticky when the requesting child exits zero
            # before the guardian polls its pipe, and also kills a blocked job.
            for after in ("sys.exit(0)", "time.sleep(60)"):
                cleanup_verified = False
                with reserve_retained_receiver_attempt(
                    config_path, dispatch, registration, deadline
                ) as attempt:
                    try:
                        run_owned_freebsd_attempt(
                            [
                                sys.executable,
                                "-c",
                                "import sys,time;"
                                "from literate_ai.adapters import "
                                "freebsd_owned_attempt as owned;"
                                "owned.OwnedFreeBsdChild().cancel();" + after,
                            ],
                            **options,
                            expires_at=datetime.now(UTC) + timedelta(seconds=20),
                            admission_guard=lambda: None,
                        )
                    except FreeBsdOwnedAttemptError as exc:
                        cleanup_verified = exc.cleanup_verified
                        try:
                            attempt.retain_diagnostic(exc)
                        finally:
                            if exc.cleanup_verified:
                                attempt.release_after_native_empty()
                        self.assertEqual(exc.reason, "cancelled")
                        self.assertTrue(exc.cleanup_verified)
                        self.assertFalse(exc.quarantine_required)
                    else:
                        self.fail("child cancellation returned a result")
                self.assertFalse((root / "private-receiver.json.attempt").exists())
            diagnostics = tuple(
                path
                for path in root.glob("private-receiver.json.attempt.*.json")
                if json.loads(path.read_bytes()).get("schema")
                == "literate-ai/retained-receiver-cleanup-diagnostic@1"
            )
            self.assertEqual(len(diagnostics), 3)
            self.assertTrue(
                all(json.loads(p.read_bytes())["cleanup_verified"] for p in diagnostics)
            )
            child_pid = root / "nested.pid"
            for directory in ("project", "build/retained-project-cas", "objects"):
                (root / directory).mkdir(parents=True)
            worker_config = root / "workers.json"
            worker_config.write_text("{}")
            registration_document = {
                "schema": CONFIG_SCHEMA,
                "project": str(root / "project"),
                "worker_config": str(worker_config),
                "worker": "native-fixture",
                "target": "host",
                "retained_project": "retained.json",
                "retained_project_profile": "profile.json",
                "policy": "policy.json",
                "tools": "tools.json",
                "authorization_identity": registration.uri,
                "build_dir": str(root / "build"),
                "obj_dir": str(root / "objects"),
                "child_environment": options["environment"],
            }
            config_path.write_bytes(canonical_json_bytes(registration_document))
            private_registration = _Registration(config_path)
            sleeper = root / "sleeper.py"
            sleeper.write_text(
                "import os,sys,time\n"
                "open(sys.argv[1],'w').write(str(os.getpid()))\n"
                # A real owned descendant revokes its private registration.
                f"open({str(config_path)!r},'w').write('{{}}')\n"
                "time.sleep(60)\n"
            )

            def current():
                private_registration.require_unchanged()

            cleanup_verified = False
            try:
                run_owned_freebsd_attempt(
                    [
                        sys.executable,
                        "-c",
                        "import subprocess,sys,time;"
                        "subprocess.Popen(sys.argv[1:],start_new_session=True);"
                        "time.sleep(60)",
                        sys.executable,
                        str(sleeper),
                        str(child_pid),
                    ],
                    **options,
                    expires_at=datetime.now(UTC) + timedelta(seconds=20),
                    admission_guard=current,
                )
            except FreeBsdOwnedAttemptError as exc:
                cleanup_verified = exc.cleanup_verified
                self.assertEqual(exc.reason, "admission-revoked")
                self.assertTrue(exc.cleanup_verified)
                self.assertFalse(exc.quarantine_required)
            else:
                self.fail("revoked admission returned a result")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if cleanup_verified:
                shutil.rmtree(root)
            else:
                raise RuntimeError(
                    f"native ownership unproven; fixture retained at {root}"
                )

    def test_owned_attempt_cleans_nested_sessions_and_preserves_unrelated_process(self):
        with self.fixture() as (root, processes, guardians):
            supervisor = Path(
                str(files("literate_ai").joinpath("freebsd_attempt_supervisor.py"))
            )
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(60)"]
            )
            processes.append(unrelated)
            sleeper = root / "sleeper.py"
            sleeper.write_text(
                "import os,sys,time\n"
                "open(sys.argv[1],'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            nested = root / "nested.py"
            nested.write_text(
                "import os,sys\n"
                "from pathlib import Path\n"
                "from literate_ai.adapters.builders._process "
                "import run_bounded_process\n"
                "run_bounded_process(sys.argv[1:],cwd=Path.cwd(),environment=os.environ,"
                "timeout_seconds=45,stdout_limit_bytes=1024,stderr_limit_bytes=1024,"
                "error_prefix='fixture.nested',terminate_descendants=True)\n"
            )
            orphan = root / "orphan.py"
            orphan.write_text(
                "import os,sys,time\n"
                "child=os.fork()\n"
                "if child==0:\n"
                " os.setsid()\n"
                " if os.fork():os._exit(0)\n"
                " for fd in (0,1,2):\n"
                "  try:os.close(fd)\n"
                "  except OSError:pass\n"
                " open(sys.argv[1],'w').write(str(os.getpid()))\n"
                " time.sleep(60)\n"
                " os._exit(0)\n"
                "os.waitpid(child,0)\n"
                "while not os.path.exists(sys.argv[1]):time.sleep(.01)\n"
            )
            for label, command, seconds, expected in (
                (
                    "nested",
                    [
                        sys.executable,
                        str(nested),
                        sys.executable,
                        str(sleeper),
                        str(root / "nested.pid"),
                    ],
                    8,
                    "deadline",
                ),
                (
                    "orphan",
                    [sys.executable, str(orphan), str(root / "orphan.pid")],
                    20,
                    "completed",
                ),
            ):
                with self.subTest(case=label):
                    request = self.request(root, command, seconds)
                    read_end, write_end = os.pipe()
                    try:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                str(supervisor),
                                "--control-fd",
                                str(read_end),
                            ],
                            pass_fds=(read_end,),
                            start_new_session=True,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        guardians.append((process, None))
                        output, errors = process.communicate(
                            json.dumps(request).encode(), timeout=40
                        )
                    finally:
                        os.close(read_end)
                        os.close(write_end)
                    self.assertTrue(output, errors.decode(errors="replace"))
                    response = json.loads(output)
                    self.assertEqual(response["nonce"], request["nonce"])
                    self.assertEqual(response["reason"], expected)
                    self.assertTrue(response["cleanup"]["empty"])
                    self.assertEqual(response["cleanup"]["signal_failures"], 0)
                    self.assertEqual(response["cleanup"]["descendants"], 0)
                    pid = int((root / (label + ".pid")).read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    self.assertIsNone(unrelated.poll())

    def test_parent_disappearance_cancels_and_drains_the_owned_attempt(self):
        with self.fixture() as (root, processes, guardians):
            supervisor = str(
                files("literate_ai").joinpath("freebsd_attempt_supervisor.py")
            )
            child_pid = root / "owned.pid"
            request = self.request(
                root,
                [
                    sys.executable,
                    "-c",
                    "import os,sys,time;"
                    "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)",
                    str(child_pid),
                ],
                30,
            )
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request))
            output_file = root / "response.json"
            guardians.append((None, output_file))
            driver = root / "driver.py"
            dispatch, deadline, registration = self.dispatch_request()
            config_path = root / "private-receiver.json"
            wire_path = root / "dispatch.json"
            wire_path.write_bytes(
                encode_action_request(
                    dispatch,
                    deadline,
                    {
                        dispatch.action.payload_identity: canonical_json_bytes(
                            "native-receiver-attempt-fixture"
                        )
                    },
                )
            )
            driver.write_text(
                "import os,subprocess,sys,time\n"
                "from pathlib import Path\n"
                "from literate_ai.adapters.action_dispatch_wire "
                "import decode_attested_action_request\n"
                "from literate_ai.adapters.retained_receiver_attempt "
                "import reserve_retained_receiver_attempt\n"
                "from literate_ai.contracts import ContentIdentity\n"
                f"request,deadline,_,_=decode_attested_action_request(Path({str(wire_path)!r}).read_bytes())\n"
                f"reservation=reserve_retained_receiver_attempt(Path({str(config_path)!r}),request,ContentIdentity.parse_uri({registration.uri!r}),deadline)\n"
                "reservation.__enter__()\n"
                "r,w=os.pipe()\n"
                f"p=subprocess.Popen([sys.executable,{supervisor!r},'--control-fd',str(r)],"
                f"pass_fds=(r,),start_new_session=True,stdin=open({str(request_file)!r},'rb'),"
                f"stdout=open({str(output_file)!r},'wb'),"
                f"stderr=open({str(root / 'guardian.stderr')!r},'wb'))\n"
                f"open({str(root / 'guardian.pid')!r},'w').write(str(p.pid))\n"
                "os.close(r)\n"
                "time.sleep(60)\n"
            )
            parent = subprocess.Popen([sys.executable, str(driver)])
            processes.append(parent)
            until = time.monotonic() + 15
            while not child_pid.exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists())
            parent.kill()
            parent.wait(timeout=5)
            until = time.monotonic() + 15
            while time.monotonic() < until:
                if output_file.exists() and output_file.read_bytes().endswith(b"\n"):
                    break
                time.sleep(0.02)
            response = json.loads(output_file.read_bytes())
            self.assertEqual(response["nonce"], request["nonce"])
            self.assertEqual(response["reason"], "cancelled")
            self.assertTrue(response["cleanup"]["empty"])
            self.assertEqual(response["cleanup"]["signal_failures"], 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
            # The original parent died without receiving its native empty proof.
            # A new process must not infer authority from the saved response or
            # disappearance of the old PID and silently reuse that reservation.
            marker = root / "private-receiver.json.attempt"
            retained = marker.read_bytes()
            with self.assertRaises(FileExistsError):
                with reserve_retained_receiver_attempt(
                    config_path, dispatch, registration, deadline
                ):
                    self.fail("parent-loss reservation was silently recovered")
            self.assertEqual(marker.read_bytes(), retained)

    @staticmethod
    def dispatch_request():
        identity = canonical_identity("native-receiver-attempt-fixture")
        worker = LifecycleActionWorker("native-fixture", identity, identity, identity)
        node = LifecycleActionNode(
            "native-fixture-package",
            identity,
            LifecycleActionKind.PACKAGE,
            identity,
            (),
            (worker.worker_id,),
        )
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=60))
        request = LifecycleActionDispatchRequest(
            identity, node, worker, 0, (), deadline.identity
        )
        return request, deadline, identity

    @staticmethod
    def request(root, command, seconds):
        return {
            "schema": "literate-ai/freebsd-owned-attempt-request@1",
            "nonce": "0123456789abcdef" * 2,
            "command": command,
            "cwd": str(root),
            "staging_root": str(root),
            "environment": {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "TMPDIR": str(root),
                "LC_ALL": "C",
            },
            "input": None,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            "stdout_limit_bytes": 4096,
            "stderr_limit_bytes": 4096,
            "cleanup_timeout_milliseconds": 10000,
        }

    @contextmanager
    def fixture(self):
        root = Path(tempfile.mkdtemp(prefix="freebsd-reaper-test-")).resolve()
        processes, guardians = [], []
        try:
            yield root, processes, guardians
        finally:
            errors = []
            for process in processes:
                self.stop_fixture_process(process)
            for process, response_path in guardians:
                try:
                    if process is not None:
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                        output, _errors = process.communicate(timeout=15)
                        response_path = root / f"guardian-{process.pid}-final.json"
                        response_path.write_bytes(output)
                    else:
                        until = time.monotonic() + 15
                        while time.monotonic() < until:
                            if (
                                response_path.exists()
                                and response_path.read_bytes().endswith(b"\n")
                            ):
                                break
                            time.sleep(0.02)
                    response = json.loads(response_path.read_bytes())
                    if response.get("cleanup", {}).get("empty") is not True:
                        raise RuntimeError("native subtree emptiness unproven")
                except Exception as exc:
                    errors.append(type(exc).__name__)
            if errors:
                raise RuntimeError(
                    "native guardian cleanup unproven; "
                    f"fixture retained at {root}: {errors}"
                )
            shutil.rmtree(root)

    @staticmethod
    def stop_fixture_process(process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class _Process:
    pid = 1729

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True


class ProcessTreeTerminationTests(unittest.TestCase):
    def test_windows_taskkill_uses_only_validated_system32_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = root / "Windows"
            system32 = windows / "System32"
            system32.mkdir(parents=True)
            taskkill = system32 / "taskkill.exe"
            taskkill.write_bytes(b"system taskkill")
            taskkill.chmod(0o755)
            decoy = root / "project-bin"
            decoy.mkdir()
            (decoy / "taskkill.exe").write_bytes(b"project decoy")
            environment = {
                "SystemRoot": str(windows),
                "PATH": str(decoy),
                "LITERATE_AI_SECRET": "must-not-be-forwarded",
            }
            process = _Process()
            terminator = mock.Mock()
            terminator.wait.return_value = 0

            with mock.patch.object(
                _processes.subprocess, "Popen", return_value=terminator
            ) as popen:
                _processes.terminate_process_tree(
                    process, platform_name="nt", environment=environment
                )

            command = popen.call_args.args[0]
            options = popen.call_args.kwargs
            resolved_windows = windows.resolve()
            resolved_system32 = resolved_windows / "System32"
            resolved_taskkill = resolved_system32 / "taskkill.exe"
            self.assertEqual(command[0], str(resolved_taskkill))
            self.assertNotEqual(command[0], str(decoy / "taskkill.exe"))
            self.assertEqual(options["cwd"], resolved_system32)
            self.assertEqual(options["env"], {"SystemRoot": str(resolved_windows)})
            self.assertNotIn("PATH", options["env"])
            self.assertNotIn("LITERATE_AI_SECRET", options["env"])
            terminator.wait.assert_called_once_with(timeout=5)
            self.assertTrue(process.killed)


class RunWithTreeKillTests(unittest.TestCase):
    """Regression coverage for #79: `subprocess.run(timeout=)` call sites that
    only killed the direct child, leaving helper trees (git/pip/conan/worker
    bootstrap descendants) alive. `run_with_tree_kill` is the drop-in
    replacement every affected call site now routes through."""

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_the_whole_process_tree_not_just_the_direct_child(
        self,
    ) -> None:
        """Reproducer from the issue: a wrapper that daemonizes a sleeper,
        then the runner times out. The sleeper must not remain."""

        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "grandchild.pid"
            grandchild_script = Path(temporary) / "grandchild.py"
            grandchild_script.write_text(
                "import os, time, sys\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            parent_script = Path(temporary) / "parent.py"
            parent_script.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}])\n"
                "time.sleep(60)\n"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                _processes.run_with_tree_kill(
                    (sys.executable, str(parent_script)),
                    timeout=1,
                )

            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            grandchild_pid = int(pid_file.read_text().strip())

            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(
                alive, "grandchild survived timeout; only the direct child was killed"
            )


if __name__ == "__main__":
    unittest.main()

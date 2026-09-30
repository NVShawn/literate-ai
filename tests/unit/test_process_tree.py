from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import _processes


class _FakeKernel32:
    """Minimal stand-in for the real Win32 `kernel32` used by tests.

    Mimics just enough of `CreateJobObjectW`/`SetInformationJobObject`/
    `OpenProcess`/`AssignProcessToJobObject`/`TerminateJobObject`/
    `CloseHandle` to exercise `_processes`'s job-object code path on a
    non-Windows CI host, without touching any real OS primitive.
    """

    def __init__(
        self,
        *,
        assign_succeeds: bool = True,
        open_process_succeeds: bool = True,
    ) -> None:
        self.assign_succeeds = assign_succeeds
        self.open_process_succeeds = open_process_succeeds
        self._next_handle = 1000
        self.closed_handles: list[int] = []
        self.terminated_jobs: list[int] = []
        self.assigned: list[tuple[int, int]] = []
        self.set_information_calls: list[tuple[int, int, int]] = []

    def _allocate_handle(self) -> int:
        self._next_handle += 1
        return self._next_handle

    def CreateJobObjectW(self, security_attributes: object, name: object) -> int:
        return self._allocate_handle()

    def SetInformationJobObject(
        self,
        handle: int,
        information_class: int,
        info_ref: object,
        info_size: int,
    ) -> int:
        info = ctypes.cast(
            info_ref, ctypes.POINTER(_processes._JobObjectExtendedLimitInformation)
        ).contents
        self.set_information_calls.append(
            (handle, information_class, info.BasicLimitInformation.LimitFlags)
        )
        return 1

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        if not self.open_process_succeeds:
            return 0
        return self._allocate_handle()

    def AssignProcessToJobObject(self, job_handle: int, process_handle: int) -> int:
        self.assigned.append((job_handle, process_handle))
        return 1 if self.assign_succeeds else 0

    def TerminateJobObject(self, job_handle: int, exit_code: int) -> int:
        self.terminated_jobs.append(job_handle)
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.closed_handles.append(handle)
        return 1


class _Process:
    pid = 1729

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True


class _BlockingJob:
    """Expose whether more than one thread can claim one Job Object."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.terminate_calls = 0
        self.close_calls = 0
        self._count_lock = threading.Lock()

    def terminate(self) -> bool:
        with self._count_lock:
            self.terminate_calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError(
                "concurrent termination regression did not release job"
            )
        return True

    def close(self) -> None:
        with self._count_lock:
            self.close_calls += 1


class ProcessTreeTerminationTests(unittest.TestCase):
    def test_process_group_options_select_platform_ownership_primitive(self) -> None:
        self.assertEqual(
            _processes.process_group_options(platform_name="posix"),
            {"start_new_session": True},
        )
        self.assertEqual(
            _processes.process_group_options(platform_name="nt"),
            {
                "creationflags": getattr(
                    _processes.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            },
        )
        self.assertEqual(_processes.process_group_options(platform_name="unknown"), {})

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

    def test_windows_taskkill_rejects_relative_or_missing_system_root(self) -> None:
        self.assertIsNone(
            _processes.windows_taskkill_executable({"SystemRoot": "Windows"})
        )
        self.assertIsNone(_processes.windows_taskkill_executable({"PATH": "bin"}))

    def test_create_windows_job_object_sets_kill_on_close(self) -> None:
        kernel32 = _FakeKernel32()

        job = _processes.create_windows_job_object(kernel32)

        self.assertIsNotNone(job)
        self.assertEqual(len(kernel32.set_information_calls), 1)
        _handle, info_class, limit_flags = kernel32.set_information_calls[0]
        self.assertEqual(
            info_class, _processes._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS
        )
        self.assertEqual(limit_flags, _processes._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

    @unittest.skipIf(os.name == "nt", "requires a non-Windows host")
    def test_create_windows_job_object_returns_none_off_windows(self) -> None:
        # No injected kernel32 and no real Windows kernel32 available on this
        # host: must fail open (None) rather than raise, so callers can fall
        # back to taskkill/kill.
        self.assertIsNone(_processes.create_windows_job_object())

    def test_process_tree_ownership_binds_before_grandchild_spawn(
        self,
    ) -> None:
        """Regression for #72: a Windows tree kill must not depend on a
        `taskkill /T` snapshot catching every descendant. Binding the root
        process to the job immediately after spawn makes every later
        grandchild -- however late it spawns -- a job member too, because
        child processes inherit their parent's job at creation time. Killing
        the job must then work even when `taskkill` itself is unavailable
        (e.g. no validated `SystemRoot`, as in `terminate_process_tree`'s own
        `environment={}` case)."""

        kernel32 = _FakeKernel32()
        ownership = _processes.create_process_tree_ownership(
            platform_name="nt", kernel32=kernel32
        )
        self.assertIsNotNone(ownership.job)

        ownership.bind(4242)
        self.assertEqual(len(kernel32.assigned), 1)

        process = _Process()
        # No SystemRoot at all: taskkill is unavailable, exactly like the
        # SSH-timeout scenario in #70. The job must still be the mechanism
        # that reaches the (simulated) late-spawned grandchild.
        _processes.terminate_process_tree(
            process, platform_name="nt", environment={}, ownership=ownership
        )

        self.assertEqual(len(kernel32.terminated_jobs), 1)
        self.assertIsNone(ownership.job)
        self.assertTrue(process.killed)

    def test_process_tree_ownership_bind_abandons_job_on_assign_failure(self) -> None:
        kernel32 = _FakeKernel32(assign_succeeds=False)
        ownership = _processes.create_process_tree_ownership(
            platform_name="nt", kernel32=kernel32
        )
        self.assertIsNotNone(ownership.job)

        ownership.bind(4242)

        self.assertIsNone(ownership.job)
        # The abandoned job's handle must be released, not leaked (in
        # addition to the transient OpenProcess handle used for the failed
        # assignment attempt).
        self.assertEqual(len(kernel32.closed_handles), 2)

    def test_concurrent_windows_termination_claims_job_exactly_once(self) -> None:
        job = _BlockingJob()
        ownership = _processes.ProcessTreeOwnership({}, job=job)  # type: ignore[arg-type]
        process = _Process()
        failures: list[BaseException] = []

        def terminate() -> None:
            try:
                _processes.terminate_process_tree(
                    process,
                    platform_name="nt",
                    environment={},
                    ownership=ownership,
                )
            except BaseException as exc:
                failures.append(exc)

        first = threading.Thread(target=terminate)
        second = threading.Thread(target=terminate)
        first.start()
        self.assertTrue(job.entered.wait(timeout=2))
        second.start()
        second.join(timeout=2)
        second_returned_before_release = not second.is_alive()
        job.release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertTrue(second_returned_before_release)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(job.terminate_calls, 1)
        self.assertEqual(job.close_calls, 1)
        self.assertIsNone(ownership.job)

    def test_create_process_tree_ownership_has_no_job_on_posix(self) -> None:
        ownership = _processes.create_process_tree_ownership(platform_name="posix")

        self.assertIsNone(ownership.job)
        self.assertEqual(ownership.popen_options, {"start_new_session": True})


class RunWithTreeKillTests(unittest.TestCase):
    """Regression coverage for #79: `subprocess.run(timeout=)` call sites that
    only killed the direct child, leaving helper trees (git/pip/conan/worker
    bootstrap descendants) alive. `run_with_tree_kill` is the drop-in
    replacement every affected call site now routes through."""

    def test_returns_a_completed_process_like_subprocess_run(self) -> None:
        result = _processes.run_with_tree_kill(
            (sys.executable, "-c", "print('ok')"),
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_check_true_raises_called_process_error_on_nonzero_exit(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            _processes.run_with_tree_kill(
                (sys.executable, "-c", "import sys; sys.exit(3)"),
                timeout=10,
                check=True,
            )

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


class InPackageSpawnSiteTests(unittest.TestCase):
    def test_timeout_popen_sites_bind_process_tree_ownership(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "literate_ai"
        excluded = {
            root / "remote_source_guard.py",
            root / "remote_worker_bootstrap.py",
            root / "adapters" / "_processes.py",
        }
        missing: list[str] = []
        for path in root.rglob("*.py"):
            if path in excluded:
                continue
            text = path.read_text(encoding="utf-8")
            if "subprocess.Popen(" not in text:
                continue
            if "create_process_tree_ownership" not in text:
                missing.append(path.relative_to(root).as_posix())
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

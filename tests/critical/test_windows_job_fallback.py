"""Windows Job success avoids fallback without weakening tree cleanup."""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import _processes
from literate_ai.adapters.builders import BuildError, run_bounded_process


class WindowsJobFallbackTests(unittest.TestCase):
    def terminate(self, ownership, *, running=False):
        process = mock.Mock(pid=1729)
        process.poll.return_value = None if running else 0
        with (
            mock.patch.object(
                _processes,
                "windows_taskkill_executable",
                return_value=Path("/Windows/System32/taskkill.exe"),
            ),
            mock.patch.object(_processes.subprocess, "Popen") as popen,
        ):
            _processes.terminate_process_tree(
                process, platform_name="nt", ownership=ownership
            )
        return process, popen

    def test_successful_job_retirement_never_starts_fallback_in_repeated_cleanup(self):
        job = mock.Mock()
        job.terminate.return_value = True
        ownership = _processes.ProcessTreeOwnership({}, job)
        for _ in range(2):
            process, popen = self.terminate(ownership)
            popen.assert_not_called()
            process.kill.assert_not_called()
        job.terminate.assert_called_once_with()
        job.close.assert_called_once_with()

    def test_successful_job_still_kills_a_root_reported_running(self):
        job = mock.Mock()
        job.terminate.return_value = True
        process, popen = self.terminate(
            _processes.ProcessTreeOwnership({}, job), running=True
        )
        popen.assert_not_called()
        process.kill.assert_called_once_with()

    def test_missing_failed_or_released_job_retains_fallback_and_root_kill(self):
        for role in ("missing", "failed", "released"):
            with self.subTest(role=role):
                job = None if role == "missing" else mock.Mock()
                if job is not None:
                    job.terminate.return_value = False
                ownership = _processes.ProcessTreeOwnership({}, job)
                if role == "released":
                    ownership.release()
                process, popen = self.terminate(ownership, running=True)
                popen.assert_called_once()
                popen.return_value.wait.assert_called_once_with(timeout=5)
                process.kill.assert_called_once_with()
                if job is not None:
                    job.close.assert_called_once_with()

    def test_pending_job_claim_is_not_reported_as_success(self):
        entered, finish = threading.Event(), threading.Event()
        job = mock.Mock()

        def terminate():
            entered.set()
            if not finish.wait(5):
                raise AssertionError("test Job termination was not released")
            return True

        job.terminate.side_effect = terminate
        ownership = _processes.ProcessTreeOwnership({}, job)
        results = []
        thread = threading.Thread(target=lambda: results.append(ownership.terminate()))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            _, popen = self.terminate(ownership)
            popen.assert_called_once()
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])
        _, popen = self.terminate(ownership)
        popen.assert_not_called()
        job.close.assert_called_once_with()


@unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
class NativeWindowsJobCleanupTests(unittest.TestCase):
    def exercise(self, mode):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid, armed = root / "child.pid", root / "armed"
            handles, errors, owners = [], [], []
            code = (
                "import os,pathlib,subprocess,sys,time\n"
                "sys.stdin.buffer.read(1)\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(60)'])\n"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
                f"armed=pathlib.Path({str(armed)!r})\n"
                "end=time.monotonic()+8\n"
                "while not armed.exists() and time.monotonic()<end: time.sleep(.01)\n"
                "assert armed.exists()\n"
                + (
                    {
                        "success": "",
                        "timeout": "time.sleep(60)\n",
                        "overflow": (
                            "sys.stdout.buffer.write(b'x'*8192);"
                            "sys.stdout.buffer.flush();time.sleep(60)\n"
                        ),
                    }[mode]
                )
            )

            def observe():
                try:
                    end = time.monotonic() + 8
                    while not child_pid.exists() and time.monotonic() < end:
                        time.sleep(0.01)
                    handle = kernel.OpenProcess(
                        0x100000, False, int(child_pid.read_text())
                    )
                    self.assertTrue(handle, "could not hold exact owned child handle")
                    handles.append(handle)
                    armed.write_bytes(b"ready")
                except BaseException as error:
                    errors.append(error)

            factory = _processes.create_process_tree_ownership

            def capture_owner():
                owner = factory()
                self.assertIsNotNone(owner.job, "native Job setup unavailable")
                owners.append(owner)
                return owner

            observer = threading.Thread(target=observe)
            observer.start()
            try:
                with mock.patch(
                    "literate_ai.adapters.builders._process.create_process_tree_ownership",
                    side_effect=capture_owner,
                ):
                    kwargs = dict(
                        cwd=root,
                        environment=dict(os.environ),
                        timeout_seconds=12 if mode == "success" else 10,
                        stdout_limit_bytes=128,
                        stderr_limit_bytes=1024,
                        error_prefix="fixture.job",
                        input_bytes=b"1",
                        terminate_descendants=True,
                        trace=False,
                    )
                    if mode == "success":
                        result = run_bounded_process(
                            [sys.executable, "-c", code], **kwargs
                        )
                        self.assertEqual(result.returncode, 0)
                    else:
                        with self.assertRaises(BuildError) as raised:
                            run_bounded_process([sys.executable, "-c", code], **kwargs)
                        self.assertEqual(
                            raised.exception.code,
                            "fixture.job_"
                            + ("timeout" if mode == "timeout" else "output_limit"),
                        )
                observer.join(9)
                self.assertFalse(observer.is_alive())
                self.assertFalse(errors, errors)
                self.assertEqual(len(owners), 1)
                self.assertTrue(
                    owners[0].terminate(), "Job termination was not confirmed"
                )
                self.assertEqual(len(handles), 1)
                self.assertEqual(
                    kernel.WaitForSingleObject(handles[0], 5000),
                    0,
                    "owned descendant survived cleanup",
                )
            finally:
                for owner in owners:
                    owner.release()
                observer.join(9)
                for handle in handles:
                    kernel.CloseHandle(handle)

    def test_success_retires_descendant_after_root_exit(self):
        self.exercise("success")

    def test_timeout_retires_descendant(self):
        self.exercise("timeout")

    def test_output_overflow_retires_descendant(self):
        self.exercise("overflow")

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _ClockedProcess:
    def __init__(self, clock: _Clock, completion_seconds: float) -> None:
        self.clock = clock
        self.completion_seconds = completion_seconds
        self.pid = 99_999
        self.returncode: int | None = None
        self.stdout = io.BytesIO(b"forced fetch progress\n")
        self.stderr = io.BytesIO()
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            self.clock.value = self.completion_seconds
            self.returncode = 0
            return self.returncode
        if self.clock.value + timeout < self.completion_seconds:
            self.clock.value += timeout
            raise subprocess.TimeoutExpired(("git", "fetch"), timeout)
        self.clock.value = self.completion_seconds
        self.returncode = 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class _HeartbeatStream(io.BytesIO):
    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self.clock = clock
        self.chunks = [f"{index}\n".encode("ascii") for index in range(8)]

    def read1(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        self.clock.value += 0.2
        chunk = self.chunks.pop(0)
        assert len(chunk) <= size
        return chunk


class _InlineThread:
    """Deterministically schedule stream observations before the supervisor polls."""

    def __init__(self, target, args, **_kwargs) -> None:
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


class BoundedProcessTests(unittest.TestCase):
    def test_default_execution_preserves_start_and_terminal_diagnostics(self):
        with patch("literate_ai.adapters.builders._process.trace_subprocess") as trace:
            result = run_bounded_process(
                (sys.executable, "-I", "-S", "-c", "print('complete')"),
                cwd=None,
                environment={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="fixture",
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(trace.call_count, 2)
        self.assertEqual(trace.call_args.kwargs["stdout"], result.stdout)
        self.assertEqual(result.stdout.strip(), b"complete")

    def test_bounded_input_roundtrips_binary_bytes_without_diagnostics(self):
        payload = bytes(range(256)) * 200
        with patch("literate_ai.adapters.builders._process.trace_subprocess") as trace:
            result = run_bounded_process(
                (
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ),
                cwd=None,
                environment=dict(os.environ),
                timeout_seconds=5,
                stdout_limit_bytes=65536,
                stderr_limit_bytes=1024,
                error_prefix="fixture",
                input_bytes=payload,
                trace=False,
            )
        self.assertEqual(result.stdout, payload)
        trace.assert_not_called()

    def test_stalled_input_consumer_obeys_total_deadline(self):
        started = time.monotonic()
        with self.assertRaises(BuildError) as raised:
            run_bounded_process(
                (sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"),
                cwd=None,
                environment=dict(os.environ),
                timeout_seconds=0.1,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="fixture",
                input_bytes=b"x" * 65536,
                trace=False,
            )
        self.assertEqual(raised.exception.code, "fixture_timeout")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(
            any(t.name == "literate-ai-tool-stdin" for t in threading.enumerate())
        )

    def test_exited_parent_cannot_leave_a_blocked_input_writer(self):
        released = threading.Event()

        class RetainedInput:
            def write(self, _data):
                if not released.wait(5):
                    raise OSError("owned descendant kept input open")
                raise BrokenPipeError("owned descendant stopped")

            def close(self):
                pass

        clock = _Clock()
        process = _ClockedProcess(clock, 0)
        process.stdin = RetainedInput()
        process.stdout = io.BytesIO()
        with (
            patch(
                "literate_ai.adapters.builders._process._terminate_process_tree",
                side_effect=lambda *_a, **_kw: released.set(),
            ),
            self.assertRaises(BuildError) as raised,
        ):
            run_bounded_process(
                ("fixture",),
                cwd=None,
                environment={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="fixture",
                input_bytes=b"private",
                process_factory=lambda *_a, **_kw: process,
            )
        self.assertEqual(raised.exception.code, "fixture_input_write")
        self.assertTrue(released.is_set())
        self.assertFalse(
            any(t.name == "literate-ai-tool-stdin" for t in threading.enumerate())
        )

    def test_oversized_input_refuses_before_process_launch(self):
        with patch("subprocess.Popen") as launch, self.assertRaises(ValueError):
            run_bounded_process(
                ("never",),
                cwd=None,
                environment={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="fixture",
                input_bytes=b"x" * 65537,
                process_factory=launch,
            )
        launch.assert_not_called()

    def test_observed_progress_resets_inactivity_deadline(self) -> None:
        # The contract is elapsed time since observed bytes, independent of a real
        # interpreter's startup latency or scheduler contention. The process spans
        # 2 seconds, beyond the initial .75-second bound, and either pipe refreshes
        # that bound at .2-second intervals through the production drain function.
        for stream in ("stdout", "stderr"):
            clock = _Clock()
            process = _ClockedProcess(clock, 2.0)
            process.stdout = io.BytesIO()
            setattr(process, stream, _HeartbeatStream(clock))
            scheduling = SimpleNamespace(
                Thread=_InlineThread, Lock=threading.Lock, Event=threading.Event
            )
            with (
                self.subTest(stream=stream),
                patch("literate_ai.adapters.builders._process.threading", scheduling),
            ):
                result = run_bounded_process(
                    ("test-heartbeats",),
                    cwd=Path.cwd(),
                    environment={},
                    timeout_seconds=4,
                    inactivity_timeout_seconds=0.75,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                    error_prefix="test.progress",
                    clock=clock,
                    process_factory=lambda *_args, selected=process, **_kwargs: (
                        selected
                    ),
                )

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.elapsed_seconds, 2.0)
                self.assertFalse(process.terminated)
                self.assertEqual(
                    getattr(result, stream).splitlines(),
                    [str(index).encode("ascii") for index in range(8)],
                )

    def test_silent_process_hits_inactivity_deadline(self) -> None:
        with self.assertRaises(BuildError) as raised:
            run_bounded_process(
                (sys.executable, "-c", "import time; time.sleep(2)"),
                cwd=Path.cwd(),
                environment=dict(os.environ),
                timeout_seconds=2,
                inactivity_timeout_seconds=0.1,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="test.silent",
            )

        self.assertEqual(raised.exception.code, "test.silent_no_progress_timeout")
        self.assertIn("deadline_seconds=0.1", str(raised.exception))

    def test_injected_clock_allows_valid_fetch_beyond_five_minutes(self) -> None:
        clock = _Clock()
        process = _ClockedProcess(clock, 301.452)

        result = run_bounded_process(
            ("git", "fetch"),
            cwd=Path.cwd(),
            environment={},
            timeout_seconds=3600,
            inactivity_timeout_seconds=600,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            error_prefix="repository_lineage.git",
            clock=clock,
            process_factory=lambda *_args, **_kwargs: process,
            poll_interval_seconds=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.elapsed_seconds, 301.452)
        self.assertFalse(process.terminated)

    def test_injected_clock_fails_over_bound_and_terminates_process(self) -> None:
        clock = _Clock()
        process = _ClockedProcess(clock, 601)
        process.stdout = io.BytesIO()

        def terminate(selected, **_kwargs) -> None:
            self.assertIs(selected, process)
            selected.kill()

        with (
            patch(
                "literate_ai.adapters.builders._process._terminate_process_tree",
                side_effect=terminate,
            ) as terminated,
            self.assertRaises(BuildError) as raised,
        ):
            run_bounded_process(
                ("git", "fetch"),
                cwd=Path.cwd(),
                environment={},
                timeout_seconds=3600,
                inactivity_timeout_seconds=600,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                error_prefix="repository_lineage.git",
                clock=clock,
                process_factory=lambda *_args, **_kwargs: process,
                poll_interval_seconds=10,
            )

        self.assertEqual(
            raised.exception.code, "repository_lineage.git_no_progress_timeout"
        )
        self.assertIn("elapsed_seconds=600.000", str(raised.exception))
        self.assertIn("deadline_seconds=600", str(raised.exception))
        self.assertTrue(process.terminated)
        terminated.assert_called_once_with(process, ownership=ANY)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_successful_tool_reaps_descendant_holding_output_streams(self) -> None:
        parent = (
            "import subprocess, sys\n"
            "subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print('direct-tool-complete', flush=True)\n"
        )

        started = time.monotonic()
        result = run_bounded_process(
            (sys.executable, "-c", parent),
            cwd=Path.cwd(),
            environment=dict(os.environ),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            error_prefix="test.tool",
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"direct-tool-complete\n")
        self.assertEqual(result.stderr, b"")
        self.assertLess(elapsed, 15)


if __name__ == "__main__":
    unittest.main()

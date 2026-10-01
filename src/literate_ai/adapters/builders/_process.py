"""Bounded subprocess execution shared by host toolchain adapters."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.diagnostics import inherited_verbose_environment, trace_subprocess

from .._processes import create_process_tree_ownership
from .._processes import terminate_process_tree as _terminate_process_tree
from .python import BuildError

_PIPE_READ_BYTES = 64 * 1024
_PIPE_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float = 0.0


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    if process.stdout is not None:
        with suppress(OSError):
            process.stdout.close()
    if process.stderr is not None:
        with suppress(OSError):
            process.stderr.close()


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path | None,
    environment: Mapping[str, str],
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    error_prefix: str,
    inactivity_timeout_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    poll_interval_seconds: float = 0.25,
    trace: bool = True,
    input_bytes: bytes | None = None,
    input_limit_bytes: int = 64 * 1024,
    interrupt_guard: Callable[[], None] | None = None,
    terminate_descendants: bool = False,
) -> BoundedProcessResult:
    """Drain compiler streams concurrently and kill its process tree on failure."""

    if timeout_seconds <= 0:
        raise ValueError("tool process timeout must be positive")
    if inactivity_timeout_seconds is not None and inactivity_timeout_seconds <= 0:
        raise ValueError("tool process inactivity timeout must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("tool process poll interval must be positive")
    if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
        raise ValueError("tool process output limits must be positive")
    if (
        isinstance(input_limit_bytes, bool)
        or not isinstance(input_limit_bytes, int)
        or not 1 <= input_limit_bytes <= 64 * 1024 * 1024
    ):
        raise ValueError("tool input byte limit must be between one and 64 MiB")
    if input_bytes is not None and (
        not isinstance(input_bytes, bytes) or len(input_bytes) > input_limit_bytes
    ):
        raise ValueError("tool process input exceeds its configured byte limit")
    if interrupt_guard is not None:
        interrupt_guard()
    ownership = create_process_tree_ownership()
    child_environment = inherited_verbose_environment(environment)
    started_monotonic = clock()
    try:
        _started = datetime.now(UTC)
        if trace:
            trace_subprocess(
                command, cwd=cwd or Path.cwd(), environment=child_environment
            )
        process = process_factory(
            list(command),
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({} if input_bytes is None else {"bufsize": 0}),
            **ownership.popen_options,
        )
    except OSError as exc:
        ownership.release()
        raise BuildError(
            f"{error_prefix}_launch_failed", "Tool process could not start"
        ) from exc
    ownership.bind(process.pid)
    assert process.stdout is not None and process.stderr is not None
    streams = {
        "stdout": (process.stdout, stdout_limit_bytes),
        "stderr": (process.stderr, stderr_limit_bytes),
    }
    buffers = {name: bytearray() for name in streams}
    overflow = threading.Event()
    read_errors: list[OSError | ValueError] = []
    state_lock = threading.Lock()
    last_activity = [started_monotonic]
    input_errors: list[OSError | ValueError] = []

    def send_input() -> None:
        assert process.stdin is not None and input_bytes is not None
        try:
            remaining = memoryview(input_bytes)
            while remaining:
                written = process.stdin.write(remaining[:_PIPE_READ_BYTES])
                if not written:
                    raise OSError("tool input stream stopped accepting bytes")
                remaining = remaining[written:]
        except (OSError, ValueError) as exc:
            input_errors.append(exc)
        finally:
            with suppress(OSError, ValueError):
                process.stdin.close()

    writer = None
    if input_bytes is not None:
        writer = threading.Thread(
            target=send_input, name="literate-ai-tool-stdin", daemon=True
        )
        writer.start()

    def drain(name: str) -> None:
        stream, limit = streams[name]
        try:
            while True:
                remaining = limit - len(buffers[name])
                read_available = getattr(stream, "read1", stream.read)
                chunk = read_available(min(_PIPE_READ_BYTES, remaining + 1))
                if not chunk:
                    return
                if len(chunk) > remaining:
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                    with state_lock:
                        last_activity[0] = clock()
                    overflow.set()
                    if process.poll() is None:
                        _terminate_process_tree(process, ownership=ownership)
                    return
                buffers[name].extend(chunk)
                with state_lock:
                    last_activity[0] = clock()
        except (OSError, ValueError) as exc:
            with state_lock:
                read_errors.append(exc)
            if process.poll() is None:
                _terminate_process_tree(process, ownership=ownership)

    readers = tuple(
        threading.Thread(
            target=drain,
            args=(name,),
            name=f"literate-ai-tool-{name}",
            daemon=True,
        )
        for name in streams
    )
    for reader in readers:
        reader.start()
    try:
        try:
            while True:
                if interrupt_guard is not None:
                    interrupt_guard()
                now = clock()
                elapsed = now - started_monotonic
                with state_lock:
                    inactive = now - last_activity[0]
                total_remaining = timeout_seconds - elapsed
                inactive_remaining = (
                    None
                    if inactivity_timeout_seconds is None
                    else inactivity_timeout_seconds - inactive
                )
                if total_remaining <= 0:
                    timeout_kind = "total"
                    deadline = timeout_seconds
                    break
                if inactive_remaining is not None and inactive_remaining <= 0:
                    timeout_kind = "no-progress"
                    deadline = inactivity_timeout_seconds
                    break
                wait_for = min(
                    poll_interval_seconds,
                    total_remaining,
                    *(() if inactive_remaining is None else (inactive_remaining,)),
                )
                try:
                    returncode = process.wait(timeout=wait_for)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            raise
        if "timeout_kind" in locals():
            _terminate_process_tree(process, ownership=ownership)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            _close_process_pipes(process)
            for reader in readers:
                reader.join(timeout=1)
            raise BuildError(
                (
                    f"{error_prefix}_timeout"
                    if timeout_kind == "total"
                    else f"{error_prefix}_no_progress_timeout"
                ),
                "Tool process exceeded its "
                f"{timeout_kind} deadline "
                f"(elapsed_seconds={clock() - started_monotonic:.3f}, "
                f"deadline_seconds={deadline:g})",
            )

        if terminate_descendants:
            _terminate_process_tree(process, ownership=ownership)
        pipe_close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT_SECONDS
        for reader in readers:
            reader.join(timeout=max(0.0, pipe_close_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            _terminate_process_tree(process, ownership=ownership)
            descendant_close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT_SECONDS
            for reader in readers:
                reader.join(
                    timeout=max(0.0, descendant_close_deadline - time.monotonic())
                )
            if any(reader.is_alive() for reader in readers):
                _close_process_pipes(process)
                for reader in readers:
                    reader.join(timeout=1)
                raise BuildError(
                    f"{error_prefix}_output_stream",
                    "Tool descendants left inherited output streams open",
                )
        if overflow.is_set():
            raise BuildError(
                f"{error_prefix}_output_limit",
                "Tool process exceeded its stdout or stderr byte budget",
            )
        if read_errors:
            raise BuildError(
                f"{error_prefix}_output_read",
                "Tool process output could not be read",
            ) from read_errors[0]
        if writer is not None:
            writer.join(timeout=1)
            if writer.is_alive() or input_errors:
                # The direct child may have exited while a descendant retains
                # stdin. Stop the owned tree before releasing its identity.
                _terminate_process_tree(process, ownership=ownership)
                raise BuildError(
                    f"{error_prefix}_input_write",
                    "Tool process did not accept its complete input",
                )
        result = BoundedProcessResult(
            returncode=returncode,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            elapsed_seconds=clock() - started_monotonic,
        )
        if trace:
            trace_subprocess(
                command,
                cwd=cwd or Path.cwd(),
                environment=child_environment,
                status=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                started_at=_started,
            )
        return result
    finally:
        if process.poll() is None or terminate_descendants:
            _terminate_process_tree(process, ownership=ownership)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        ownership.release()
        _close_process_pipes(process)
        if writer is not None:
            if process.stdin is not None:
                with suppress(OSError, ValueError):
                    process.stdin.close()
            writer.join(timeout=1)


__all__ = ["BoundedProcessResult", "run_bounded_process"]

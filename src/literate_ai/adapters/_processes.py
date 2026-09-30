"""Shared, fail-closed process-tree termination helpers."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# -- Win32 Job Object primitives -------------------------------------------
#
# `CREATE_NEW_PROCESS_GROUP` (used by `process_group_options` below) only
# controls console CTRL_BREAK signal routing; it is not a `killpg`-equivalent
# tree-ownership primitive. `taskkill /T` is a point-in-time snapshot of the
# process tree, so a grandchild spawned after the snapshot (or while
# `taskkill` is unavailable, e.g. no validated `SystemRoot`) is never killed
# and can keep inherited pipe handles open forever.
#
# A Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is the actual
# `killpg`-equivalent primitive on Windows: once the root process is assigned
# to the job, every process it spawns afterwards — no matter when — is
# automatically a job member too (child processes inherit their parent's job
# by default), and terminating/closing the job kills the whole membership in
# one atomic OS call. `taskkill` is retained as a fallback for the case where
# job-object setup itself failed.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_kernel32() -> Any | None:
    """Resolve the real `kernel32` without raising on non-Windows hosts."""

    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


class WindowsProcessTreeJob:
    """A live handle to a Win32 Job Object used as a `killpg` equivalent."""

    def __init__(self, handle: int, *, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32
        self._closed = False

    def assign(self, pid: int) -> bool:
        """Bind `pid` to this job so its future descendants join it too."""

        if self._closed:
            return False
        try:
            process_handle = self._kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
            )
        except OSError:
            return False
        if not process_handle:
            return False
        try:
            assigned = self._kernel32.AssignProcessToJobObject(
                self._handle, process_handle
            )
        except OSError:
            assigned = 0
        finally:
            with suppress(OSError):
                self._kernel32.CloseHandle(process_handle)
        return bool(assigned)

    def terminate(self) -> bool:
        """Kill every process currently in the job, in one atomic call."""

        if self._closed:
            return False
        try:
            terminated = self._kernel32.TerminateJobObject(self._handle, 1)
        except OSError:
            terminated = 0
        return bool(terminated)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            self._kernel32.CloseHandle(self._handle)


def create_windows_job_object(
    kernel32: Any | None = None,
) -> WindowsProcessTreeJob | None:
    """Create a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` set.

    Returns `None` (fail open to the taskkill/kill fallback) if job-object
    support is unavailable or setup fails for any reason.
    """

    resolved = kernel32 if kernel32 is not None else _windows_kernel32()
    if resolved is None:
        return None
    try:
        handle = resolved.CreateJobObjectW(None, None)
    except OSError:
        return None
    if not handle:
        return None
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        applied = resolved.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except OSError:
        applied = 0
    if not applied:
        with suppress(OSError):
            resolved.CloseHandle(handle)
        return None
    return WindowsProcessTreeJob(handle, kernel32=resolved)


@dataclass
class ProcessTreeOwnership:
    """Popen kwargs plus (on Windows) the job object to bind after spawn."""

    popen_options: dict[str, object]
    job: WindowsProcessTreeJob | None = None
    _job_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def _claim_job(self) -> WindowsProcessTreeJob | None:
        """Atomically transfer the live Job Object to exactly one caller."""

        with self._job_lock:
            job = self.job
            self.job = None
            return job

    def bind(self, pid: int) -> None:
        """Assign the just-spawned root process to the job, if any.

        Must be called as soon as possible after `Popen` returns: a process
        joins its parent's job at creation time, so binding early is what
        makes every later grandchild a job member too. If binding fails, the
        job is abandoned (closed) so `terminate_process_tree` falls back to
        `taskkill`/`kill` instead of silently no-op'ing on a job nothing was
        ever assigned to.
        """

        with self._job_lock:
            job = self.job
            if job is None:
                return
            if job.assign(pid):
                return
            self.job = None
        job.close()

    def terminate(self) -> None:
        """Terminate and close the Job Object once across concurrent callers."""

        job = self._claim_job()
        if job is None:
            return
        try:
            job.terminate()
        finally:
            job.close()

    def release(self) -> None:
        """Release the job handle without terminating anything."""

        job = self._claim_job()
        if job is not None:
            job.close()


def create_process_tree_ownership(
    *,
    platform_name: str | None = None,
    kernel32: Any | None = None,
) -> ProcessTreeOwnership:
    """Return the popen kwargs and (on Windows) a bindable tree-kill job."""

    selected_platform = os.name if platform_name is None else platform_name
    popen_options = process_group_options(platform_name=selected_platform)
    job = None
    if selected_platform == "nt":
        job = create_windows_job_object(kernel32)
    return ProcessTreeOwnership(popen_options=popen_options, job=job)


def windows_taskkill_executable(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve the real System32 taskkill without consulting PATH or the CWD."""

    selected = os.environ if environment is None else environment
    configured_roots = {
        value
        for key, value in selected.items()
        if key.casefold() == "systemroot" and isinstance(value, str) and value
    }
    if len(configured_roots) != 1:
        return None
    configured_root = Path(configured_roots.pop())
    if not configured_root.is_absolute() or configured_root.is_symlink():
        return None
    try:
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            return None
        system32 = root / "System32"
        if system32.is_symlink() or not system32.is_dir():
            return None
        resolved_system32 = system32.resolve(strict=True)
        if resolved_system32 != system32:
            return None
        candidate = resolved_system32 / "taskkill.exe"
        if candidate.is_symlink() or not candidate.is_file():
            return None
        executable = candidate.resolve(strict=True)
    except OSError:
        return None
    if (
        executable != candidate
        or executable.parent != resolved_system32
        or not os.access(executable, os.X_OK)
    ):
        return None
    return executable


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    ownership: ProcessTreeOwnership | None = None,
) -> None:
    """Best-effort termination without executing an ambient helper from PATH.

    On Windows, `ownership.job` (from `create_process_tree_ownership` +
    `ProcessTreeOwnership.bind`) is the primary kill mechanism: it is the
    only primitive here that reliably reaches grandchildren spawned after a
    `taskkill /T` snapshot, or when `taskkill` itself is unavailable.
    `taskkill` and `process.kill()` remain as fallbacks for callers that have
    not (yet) adopted job-object ownership, or when job setup failed.
    """

    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "posix":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
        return
    if selected_platform == "nt":
        if ownership is not None:
            ownership.terminate()
        executable = windows_taskkill_executable(environment)
        if executable is not None:
            try:
                terminator = subprocess.Popen(
                    [str(executable), "/PID", str(process.pid), "/T", "/F"],
                    cwd=executable.parent,
                    env={"SystemRoot": str(executable.parent.parent)},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    terminator.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminator.kill()
                    with suppress(subprocess.TimeoutExpired):
                        terminator.wait(timeout=5)
            except OSError:
                pass
    if process.poll() is None:
        with suppress(OSError):
            process.kill()


def process_group_options(*, platform_name: str | None = None) -> dict[str, object]:
    """Return Popen kwargs for the platform's process-group primitive.

    On POSIX, `start_new_session` makes the child a session/process-group
    leader, which is what makes `os.killpg` in `terminate_process_tree` a
    true whole-tree kill.

    On Windows, `CREATE_NEW_PROCESS_GROUP` only affects console CTRL_BREAK
    signal routing -- it is NOT a `killpg`-equivalent tree-ownership
    primitive and must never be treated as one. Use
    `create_process_tree_ownership`/`ProcessTreeOwnership.bind` for actual
    whole-tree kill guarantees on Windows.
    """

    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "posix":
        return {"start_new_session": True}
    if selected_platform == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


# Bound on how long we wait for the process tree to actually die after a
# terminate signal, before escalating to `kill()`/giving up. Cleanup after
# the caller's own `timeout` has already expired -- not additional budget
# for the bounded command itself.
_TREE_KILL_GRACE_SECONDS = 5


def _await_termination_or_give_up(process: subprocess.Popen[Any]) -> None:
    """Bound the post-terminate wait; never block indefinitely.

    `terminate_process_tree` is best-effort: on a host where tree kill is
    incomplete (a surviving child/grandchild), the process can still be
    alive afterward. A bare `process.communicate()` here would then hang
    forever holding the pipe open. Give the tree a short grace period to
    actually exit, escalate to `kill()` once, and then give up bounded
    rather than block the caller indefinitely.
    """

    try:
        process.communicate(timeout=_TREE_KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(OSError):
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=_TREE_KILL_GRACE_SECONDS)


def run_with_tree_kill(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
    text: bool = False,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    check: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run(timeout=...)`-compatible call with whole-tree kill.

    Callers spawning tools that may fork helpers of their own (git, pip,
    conan, npm, worker-bootstrap probes, dispatched artifacts, ...) must not
    use bare `subprocess.run(timeout=)`: on `TimeoutExpired` it kills only
    the direct child, leaving any helper tree alive to hold locks/pipes/
    workspace files. This wrapper runs the same command through `Popen` +
    `process_group_options()`, and on timeout routes through
    `terminate_process_tree` with a bound `ProcessTreeOwnership` before
    re-raising `TimeoutExpired` -- so existing call sites can swap
    `subprocess.run` for this helper without changing their exception
    handling.
    """

    ownership = create_process_tree_ownership()
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            **ownership.popen_options,
        )
        ownership.bind(process.pid)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process, environment=env, ownership=ownership)
            _await_termination_or_give_up(process)
            raise
    finally:
        ownership.release()
    result: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess(
        args, process.returncode, out, err
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, args, output=out, stderr=err
        )
    return result


__all__ = [
    "ProcessTreeOwnership",
    "WindowsProcessTreeJob",
    "create_process_tree_ownership",
    "create_windows_job_object",
    "process_group_options",
    "run_with_tree_kill",
    "terminate_process_tree",
    "windows_taskkill_executable",
]

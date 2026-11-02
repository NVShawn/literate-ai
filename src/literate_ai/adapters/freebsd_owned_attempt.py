"""Private parent boundary for one kernel-owned FreeBSD attempt.

Only a configured receiver may select this command and its environment. This
mechanical result is not retained admission, LINK, or remote action authority.
An absent cleanup proof quarantines ownership; the parent never kills the reaper.
"""

import base64
import ctypes
import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files

from literate_ai import freebsd_attempt_supervisor as protocol


class FreeBsdOwnedAttemptError(RuntimeError):
    def __init__(self, reason, *, guardian_pid, cleanup_verified):
        self.reason = reason
        self.guardian_pid = guardian_pid
        self.cleanup_verified = cleanup_verified
        self.quarantine_required = not cleanup_verified
        super().__init__("freebsd.attempt." + reason)


@dataclass(frozen=True, slots=True)
class FreeBsdOwnedAttemptResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_milliseconds: int
    guardian_pid: int
    recovery_frame: bytes = b""


class OwnedFreeBsdChild:
    """Observe this process directly below its actual kernel-owned guardian.

    This mechanical ownership check is not an execution grant. Receiver startup
    requires it in addition to the authenticated request and private registration.
    There is no document decoder or caller-selected PID.
    """

    __slots__ = ("_pid", "_parent", "_procctl", "_control", "_pipe", "_fcntl")

    def __init__(self):
        if not sys.platform.startswith("freebsd"):
            raise ValueError("freebsd.attempt.native-child-required")
        import fcntl

        control = os.environ.pop(protocol.CHILD_CONTROL_ENVIRONMENT, "")
        if (
            not isinstance(control, str)
            or len(control) > 128
            or re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+", control) is None
        ):
            raise ValueError("freebsd.attempt.private-child-control-required")
        descriptor, device, inode = map(int, control.split(":"))
        if descriptor <= 2:
            raise ValueError("freebsd.attempt.private-child-control-required")
        self._control, self._pipe, self._fcntl = descriptor, (device, inode), fcntl
        os.set_inheritable(self._control, False)
        self._pid, self._parent = os.getpid(), os.getppid()
        self._procctl = ctypes.CDLL(None, use_errno=True).procctl
        self._procctl.argtypes = [
            ctypes.c_int,
            ctypes.c_int64,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self._procctl.restype = ctypes.c_int
        if ctypes.sizeof(protocol._Status) != 80:
            raise ValueError("freebsd.attempt.native-child-abi-unsupported")
        self.require_current()

    @property
    def guardian_pid(self):
        return self._parent

    def _status(self, pid):
        value = protocol._Status()
        if self._procctl(protocol._P_PID, pid, protocol._STATUS, ctypes.byref(value)):
            raise OSError(ctypes.get_errno(), "native process ownership unavailable")
        return value

    def require_current(self):
        if (
            os.getpid() != self._pid
            or os.getppid() != self._parent
            or self._parent <= 1
        ):
            raise ValueError("freebsd.attempt.guardian-changed")
        child = self._status(self._pid)
        parent = self._status(self._parent)
        pipe = os.fstat(self._control)
        if (
            child.flags & protocol._OWNED
            or child.reaper != self._parent
            or not parent.flags & protocol._OWNED
            or parent.reaper != self._parent
            or parent.children < 1
            or parent.descendants < 1
            or os.getppid() != self._parent
            or not stat.S_ISFIFO(pipe.st_mode)
            or (pipe.st_dev, pipe.st_ino) != self._pipe
            or self._fcntl.fcntl(self._control, self._fcntl.F_GETFL) & os.O_ACCMODE
            not in (os.O_WRONLY, os.O_RDWR)
        ):
            raise ValueError("freebsd.attempt.native-parent-owner-required")

    def cancel(self):
        """Signal this actual guardian through its inherited pipe, never a PID."""
        self.require_current()
        if os.write(self._control, b"C") != 1:
            raise OSError("native guardian did not accept cancellation")


def _response(content, nonce, pid, stdout_limit, stderr_limit):
    value = json.loads(content, object_pairs_hook=protocol._pairs)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "nonce",
            "guardian_pid",
            "reason",
            "returncode",
            "stdout",
            "stderr",
            "elapsed_milliseconds",
            "cleanup",
        }
        or value["schema"] != protocol.RESPONSE_SCHEMA
        or value["nonce"] != nonce
        or type(value["guardian_pid"]) is not int
        or value["guardian_pid"] != pid
        or value["reason"]
        not in {
            "completed",
            "deadline",
            "cancelled",
            "signal",
            "output-limit",
            "supervisor-error",
            "cleanup-signal-failure",
        }
        or (
            value["returncode"] is not None
            and (
                type(value["returncode"]) is not int
                or not -255 <= value["returncode"] <= 255
            )
        )
        or type(value["elapsed_milliseconds"]) is not int
        or not 0 <= value["elapsed_milliseconds"] <= 86_431_000
    ):
        raise ValueError("invalid owned-attempt response")
    cleanup = value["cleanup"]
    if (
        not isinstance(cleanup, dict)
        or set(cleanup)
        != {
            "empty",
            "children",
            "descendants",
            "signal_deliveries",
            "signal_failures",
            "first_errno",
            "first_failed_pid",
        }
        or type(cleanup["empty"]) is not bool
        or any(type(cleanup[key]) is not int for key in cleanup if key != "empty")
    ):
        raise ValueError("invalid owned-attempt cleanup report")
    empty = (
        cleanup["empty"] is True
        and cleanup["children"] == cleanup["descendants"] == 0
        and cleanup["signal_failures"] >= 0
        and cleanup["signal_deliveries"] >= 0
        and cleanup["first_errno"] >= 0
        and cleanup["first_failed_pid"] >= -1
    )
    decoded = []
    for key, maximum in (("stdout", stdout_limit), ("stderr", stderr_limit)):
        encoded = value[key]
        if not isinstance(encoded, str) or len(encoded) > (maximum + 2) // 3 * 4:
            raise ValueError("owned-attempt output exceeds its bound")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > maximum:
            raise ValueError("owned-attempt output exceeds its bound")
        decoded.append(data)
    return value, empty, decoded


def run_owned_freebsd_attempt(
    command,
    *,
    cwd,
    staging_root,
    environment,
    expires_at,
    admission_guard,
    cancelled=lambda: False,
    input_bytes=None,
    stdout_limit_bytes=1024 * 1024,
    stderr_limit_bytes=65536,
    cleanup_timeout_milliseconds=5000,
):
    """Run a privately registered command under a dedicated native reaper.

    The caller must withhold action admission on every error and quarantine its
    worker lease when ``quarantine_required`` is true. Root command success is
    checked separately through ``returncode``. Parent death closes the private
    cancellation channel without relying on generic process-group termination.
    """
    if not sys.platform.startswith("freebsd"):
        raise ValueError("native FreeBSD ownership is required")
    if not callable(admission_guard) or not callable(cancelled):
        raise TypeError("owned attempt requires live admission and cancellation")
    if input_bytes is not None and (
        not isinstance(input_bytes, bytes)
        or len(input_bytes) > protocol.MAX_INPUT_BYTES
    ):
        raise ValueError("owned-attempt input exceeds its bound")
    if (
        not isinstance(command, (list, tuple))
        or not 1 <= len(command) <= 256
        or any(not isinstance(arg, str) or len(arg) > 65536 for arg in command)
        or sum(len(arg.encode()) for arg in command) > 65536
        or not isinstance(environment, dict)
        or len(environment) > 512
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) + len(value) > 65536
            for key, value in environment.items()
        )
        or sum(
            len(key.encode()) + len(value.encode())
            for key, value in environment.items()
        )
        > 65536
    ):
        raise ValueError("owned-attempt command/environment exceeds its bound")
    nonce = secrets.token_hex(16)
    document = {
        "schema": protocol.REQUEST_SCHEMA,
        "nonce": nonce,
        "command": list(command),
        "cwd": str(cwd),
        "staging_root": str(staging_root),
        "environment": environment,
        "input": None
        if input_bytes is None
        else base64.b64encode(input_bytes).decode(),
        "expires_at": expires_at.isoformat(),
        "stdout_limit_bytes": stdout_limit_bytes,
        "stderr_limit_bytes": stderr_limit_bytes,
        "cleanup_timeout_milliseconds": cleanup_timeout_milliseconds,
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    _request, _payload, expiry, deadline = protocol._request(raw)
    try:
        admission_guard()
        is_cancelled = cancelled()
    except BaseException as exc:
        # No control pipe, guardian or native child exists at this boundary.
        # Preserve that fact so the reserving parent can safely refuse launch.
        raise FreeBsdOwnedAttemptError(
            "prelaunch-admission-revoked", guardian_pid=None, cleanup_verified=True
        ) from exc
    if is_cancelled:
        raise FreeBsdOwnedAttemptError(
            "cancelled", guardian_pid=None, cleanup_verified=True
        )
    # The helper is stdlib-only; ignore ambient Python import paths and site hooks.
    supervisor = str(files("literate_ai").joinpath("freebsd_attempt_supervisor.py"))
    read_end, write_end = os.pipe()
    guardian = None
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": protocol.MAX_REQUEST_BYTES, "stderr": 65536}
    stopped, grace = None, None
    received_frame, frame_exit_deadline = False, None

    def stop(reason):
        nonlocal write_end, stopped, grace
        if stopped is None:
            stopped = reason
            grace = time.monotonic() + cleanup_timeout_milliseconds / 1000 + 2
        if write_end is not None:
            os.close(write_end)
            write_end = None

    try:
        with tempfile.TemporaryFile(dir=staging_root) as request_file:
            request_file.write(raw)
            request_file.seek(0)
            guardian = subprocess.Popen(
                [sys.executable, "-I", "-S", supervisor, "--control-fd", str(read_end)],
                cwd=cwd,
                env={"PATH": os.defpath, "LC_ALL": "C", "LANG": "C"},
                stdin=request_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(read_end,),
                start_new_session=True,
            )
        os.close(read_end)
        read_end = None
        for name in streams:
            pipe = getattr(guardian, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while guardian.poll() is None or selector.get_map():
            if stopped is None:
                try:
                    admission_guard()
                    if cancelled():
                        stop("cancelled")
                    elif time.monotonic() >= deadline or datetime.now(UTC) >= expiry:
                        stop("deadline")
                except BaseException:
                    stop("admission-revoked")
            if (
                grace is not None
                and time.monotonic() >= grace
                or frame_exit_deadline is not None
                and time.monotonic() >= frame_exit_deadline
            ):
                raise FreeBsdOwnedAttemptError(
                    "cleanup-unproven",
                    guardian_pid=guardian.pid,
                    cleanup_verified=False,
                )
            for key, _events in selector.select(0.05):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                room = limits[key.data] - len(streams[key.data])
                streams[key.data].extend(block[:room])
                if len(block) > room:
                    stop("output-limit")
            if not received_frame and streams["stdout"].endswith(b"\n"):
                early = _response(
                    streams["stdout"],
                    nonce,
                    guardian.pid,
                    stdout_limit_bytes,
                    stderr_limit_bytes,
                )
                received_frame = True
                if not early[1]:
                    # An unsuccessful bounded cleanup deliberately leaves the
                    # guardian alive. Report quarantine now, not at job expiry.
                    raise FreeBsdOwnedAttemptError(
                        "cleanup-unproven",
                        guardian_pid=guardian.pid,
                        cleanup_verified=False,
                    )
                del early
                frame_exit_deadline = (
                    time.monotonic() + cleanup_timeout_milliseconds / 1000 + 2
                )
        try:
            response, empty, output = _response(
                streams["stdout"],
                nonce,
                guardian.pid,
                stdout_limit_bytes,
                stderr_limit_bytes,
            )
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise FreeBsdOwnedAttemptError(
                "cleanup-unproven", guardian_pid=guardian.pid, cleanup_verified=False
            ) from None
        if guardian.returncode not in (0, 125) or not empty:
            raise FreeBsdOwnedAttemptError(
                "cleanup-unproven", guardian_pid=guardian.pid, cleanup_verified=False
            )
        if stopped is not None or response["reason"] != "completed":
            raise FreeBsdOwnedAttemptError(
                stopped or response["reason"],
                guardian_pid=guardian.pid,
                cleanup_verified=True,
            )
        if guardian.returncode != 0 or response["returncode"] is None:
            raise FreeBsdOwnedAttemptError(
                "supervisor-error", guardian_pid=guardian.pid, cleanup_verified=True
            )
        try:
            admission_guard()
        except BaseException:
            raise FreeBsdOwnedAttemptError(
                "admission-revoked", guardian_pid=guardian.pid, cleanup_verified=True
            ) from None
        if cancelled() or time.monotonic() >= deadline or datetime.now(UTC) >= expiry:
            raise FreeBsdOwnedAttemptError(
                "cancelled", guardian_pid=guardian.pid, cleanup_verified=True
            )
        return FreeBsdOwnedAttemptResult(
            response["returncode"],
            *output,
            response["elapsed_milliseconds"],
            guardian.pid,
            bytes(streams["stdout"]),
        )
    except FreeBsdOwnedAttemptError:
        raise
    except BaseException as exc:
        if guardian is None:
            raise
        raise FreeBsdOwnedAttemptError(
            "cleanup-unproven", guardian_pid=guardian.pid, cleanup_verified=False
        ) from exc
    finally:
        # EOF requests cooperative native cleanup. Never SIGKILL this process:
        # even an unproven result must leave the kernel owner alive to drain.
        if write_end is not None:
            with suppress(OSError):
                os.close(write_end)
        if read_end is not None:
            with suppress(OSError):
                os.close(read_end)
        with suppress(OSError, ValueError):
            selector.close()
        if guardian is not None:
            for name in streams:
                with suppress(OSError, ValueError):
                    getattr(guardian, name).close()

"""One isolated FreeBSD attempt with kernel descendant ownership.

This stdlib-only process is a private mechanical supervisor, not a remote action
receiver or an execution grant. The registered caller selects the command and
supplies a separate cancellation pipe. An unproven cleanup keeps this reaper
alive; killing it would abandon the descendants it still owns.
"""

import argparse
import base64
import ctypes
import errno
import json
import os
import re
import select
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

REQUEST_SCHEMA = "literate-ai/freebsd-owned-attempt-request@2"
LEGACY_REQUEST_SCHEMA = "literate-ai/freebsd-owned-attempt-request@1"
RESPONSE_SCHEMA = "literate-ai/freebsd-owned-attempt-response@1"
CHILD_CONTROL_ENVIRONMENT = "LITAI_FREEBSD_ATTEMPT_CONTROL"
MAX_REQUEST_BYTES = 24 * 1024**2
MAX_INPUT_BYTES = 16 * 1024**2
MAX_STDOUT_BYTES = 16 * 1024**2
MAX_STDERR_BYTES = 1024**2

# Native FreeBSD sys/wait.h and sys/procctl.h ABI. Status and kill structures
# include their reserved fifteen-u_int tails; omitting them corrupts memory.
_P_PID = 0
_ACQUIRE, _STATUS, _KILL = 2, 4, 6
_OWNED = 1


class _Status(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint),
        ("children", ctypes.c_uint),
        ("descendants", ctypes.c_uint),
        ("reaper", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("padding", ctypes.c_uint * 15),
    ]


class _Kill(ctypes.Structure):
    _fields_ = [
        ("signal", ctypes.c_int),
        ("flags", ctypes.c_uint),
        ("subtree", ctypes.c_int),
        ("killed", ctypes.c_uint),
        ("failed_pid", ctypes.c_int),
        ("padding", ctypes.c_uint * 15),
    ]


class _Reaper:
    def __init__(self):
        if not sys.platform.startswith("freebsd"):
            raise OSError(errno.ENOSYS, "native FreeBSD reaper required")
        if ctypes.sizeof(_Status) != 80 or ctypes.sizeof(_Kill) != 80:
            raise OSError(errno.ENOSYS, "unsupported native procctl ABI")
        self.pid = os.getpid()
        self.procctl = ctypes.CDLL(None, use_errno=True).procctl
        self.procctl.argtypes = [
            ctypes.c_int,
            ctypes.c_int64,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.procctl.restype = ctypes.c_int
        self.call(_ACQUIRE, None)
        self.status()

    def call(self, operation, value):
        if os.getpid() != self.pid:
            raise OSError(errno.EPERM, "reaper cannot be transferred after fork")
        if self.procctl(_P_PID, 0, operation, value) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))

    def status(self):
        result = _Status()
        self.call(_STATUS, ctypes.byref(result))
        if not result.flags & _OWNED:
            raise OSError(errno.EPERM, "attempt no longer owns its reaper")
        return result

    def kill(self):
        result = _Kill(signal=signal.SIGKILL, failed_pid=-1)
        try:
            self.call(_KILL, ctypes.byref(result))
            if result.failed_pid != -1:
                raise OSError(errno.EPERM, "native signal delivery was incomplete")
        except OSError as exc:
            exc.deliveries, exc.failed_pid = result.killed, result.failed_pid
            raise
        return result.killed


def _cleanup(reaper, seconds):
    until = time.monotonic() + seconds
    sent = failures = 0
    first_errno = 0
    first_failed_pid = -1
    while True:
        try:
            while True:
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
            status = reaper.status()
            if status.children == 0 and status.descendants == 0:
                return {
                    "empty": True,
                    "children": 0,
                    "descendants": 0,
                    "signal_deliveries": sent,
                    "signal_failures": failures,
                    "first_errno": first_errno,
                    "first_failed_pid": first_failed_pid,
                }
            try:
                sent += reaper.kill()
            except OSError as exc:
                sent += getattr(exc, "deliveries", 0)
                # A process may exit between status and kill. Fresh empty status
                # must still prove cleanup; other failures remain explicit.
                if exc.errno != errno.ESRCH:
                    failures += 1
                    first_errno = first_errno or exc.errno or errno.EIO
                    if first_failed_pid == -1:
                        first_failed_pid = getattr(exc, "failed_pid", -1)
        except OSError as exc:
            failures += 1
            first_errno = first_errno or exc.errno or errno.EIO
        if time.monotonic() >= until:
            return {
                "empty": False,
                "children": -1,
                "descendants": -1,
                "signal_deliveries": sent,
                "signal_failures": failures,
                "first_errno": first_errno,
                "first_failed_pid": first_failed_pid,
            }
        time.sleep(0.02)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


def _request(content):
    if not 0 < len(content) <= MAX_REQUEST_BYTES:
        raise ValueError("attempt request exceeds its byte bound")
    value = json.loads(content, object_pairs_hook=_pairs)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "nonce",
            "command",
            "cwd",
            "staging_root",
            "environment",
            "input",
            "expires_at",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
            "cleanup_timeout_milliseconds",
        }
        or value["schema"] not in (REQUEST_SCHEMA, LEGACY_REQUEST_SCHEMA)
    ):
        raise ValueError("invalid attempt request")
    if (
        not isinstance(value["nonce"], str)
        or re.fullmatch("[0-9a-f]{32}", value["nonce"]) is None
    ):
        raise ValueError("invalid attempt correlation")
    command = value["command"]
    environment = value["environment"]
    if (
        type(command) is not list
        or not 1 <= len(command) <= 256
        or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
        or sum(len(arg.encode("utf-8")) for arg in command) > 65536
        or type(environment) is not dict
        or len(environment) > 512
        or any(
            not isinstance(k, str)
            or not k
            or "=" in k
            or "\x00" in k
            or not isinstance(v, str)
            or "\x00" in v
            for k, v in environment.items()
        )
        or sum(len(k.encode()) + len(v.encode()) for k, v in environment.items())
        > 65536
    ):
        raise ValueError("attempt command/environment exceeds its bound")
    if CHILD_CONTROL_ENVIRONMENT in environment:
        raise ValueError("attempt child control is selected only by the guardian")
    for name in ("cwd", "staging_root"):
        path = Path(value[name])
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not path.is_dir()
        ):
            raise ValueError("attempt directories must be direct existing directories")
    for name, maximum in (
        ("stdout_limit_bytes", MAX_STDOUT_BYTES),
        ("stderr_limit_bytes", MAX_STDERR_BYTES),
        ("cleanup_timeout_milliseconds", 30000),
    ):
        if type(value[name]) is not int or not 1 <= value[name] <= maximum:
            raise ValueError("attempt limits must be finite positive integers")
    encoded = value["input"]
    if encoded is not None and (
        not isinstance(encoded, str) or len(encoded) > (MAX_INPUT_BYTES + 2) // 3 * 4
    ):
        raise ValueError("attempt input exceeds its bound")
    payload = None if encoded is None else base64.b64decode(encoded, validate=True)
    if payload is not None and len(payload) > MAX_INPUT_BYTES:
        raise ValueError("attempt input exceeds its bound")
    expiry = datetime.fromisoformat(value["expires_at"])
    if expiry.tzinfo is None:
        raise ValueError("attempt expiry must include a timezone")
    remaining = (expiry - datetime.now(UTC)).total_seconds()
    if not 0 < remaining <= 86400:
        raise ValueError("attempt expiry is past or unbounded")
    return value, payload, expiry, time.monotonic() + remaining


def _emit(value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(content) > 24 * 1024**2:
        raise ValueError("attempt response exceeds its byte bound")
    try:
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass


class _Stopped(Exception):
    pass


def supervise(control_fd, request, payload, expiry, monotonic_deadline):
    if not stat.S_ISFIFO(os.fstat(control_fd).st_mode):
        raise ValueError("attempt cancellation requires a private pipe")
    os.set_blocking(control_fd, False)
    reaper = _Reaper()  # No child has been created in this dedicated process.
    try:
        return _supervise_owned(
            reaper, control_fd, request, payload, expiry, monotonic_deadline
        )
    finally:
        # Diagnostic allocation, pipe closing and response emission must never
        # bypass ownership retention after a failed bounded cleanup.
        while True:
            try:
                if _cleanup(reaper, 1)["empty"]:
                    break
            except BaseException:
                time.sleep(0.02)


def _supervise_owned(reaper, control_fd, request, payload, expiry, monotonic_deadline):
    signalled = [0]
    child_read = child_write = None

    def stop(signum, _frame):
        signalled[0] = signum

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop)

    def boundary():
        nonlocal child_read
        if signalled[0]:
            raise _Stopped("signal")
        if time.monotonic() >= monotonic_deadline or datetime.now(UTC) >= expiry:
            raise _Stopped("deadline")
        if select.select([control_fd], [], [], 0)[0]:
            os.read(control_fd, 64)
            raise _Stopped("cancelled")
        if child_read is not None and select.select([child_read], [], [], 0)[0]:
            if os.read(child_read, 64):
                raise _Stopped("cancelled")
            selector.unregister(child_read)
            os.close(child_read)
            child_read = None

    streams = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {name: request[name + "_limit_bytes"] for name in streams}
    reason, returncode, process = "completed", None, None
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    selector.register(control_fd, selectors.EVENT_READ, "control")
    try:
        environment = dict(request["environment"])
        if request["schema"] == REQUEST_SCHEMA:
            child_read, child_write = os.pipe()
            os.set_blocking(child_read, False)
            os.set_blocking(child_write, False)
            node = os.fstat(child_write)
            environment[CHILD_CONTROL_ENVIRONMENT] = (
                f"{child_write}:{node.st_dev}:{node.st_ino}"
            )
            if (
                len(environment) > 512
                or sum(
                    len(k.encode()) + len(v.encode()) for k, v in environment.items()
                )
                > 65536
            ):
                raise ValueError("private attempt control exceeds environment policy")
            selector.register(child_read, selectors.EVENT_READ, "child-control")
        boundary()
        with tempfile.TemporaryFile(dir=request["staging_root"]) as input_file:
            if payload is not None:
                input_file.write(payload)
                input_file.seek(0)
            boundary()
            process = subprocess.Popen(
                request["command"],
                cwd=request["cwd"],
                env=environment,
                stdin=input_file if payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                pass_fds=() if child_write is None else (child_write,),
            )
            if child_write is not None:
                os.close(child_write)
                child_write = None
            for name in streams:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while process.poll() is None:
                boundary()
                for key, _events in selector.select(0.05):
                    if key.data in {"control", "child-control"}:
                        boundary()
                        continue
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    room = limits[key.data] - len(streams[key.data])
                    streams[key.data].extend(block[:room])
                    if len(block) > room:
                        reason = "output-limit"
                        break
                if reason != "completed":
                    break
            returncode = process.poll()
    except _Stopped as exc:
        reason = str(exc)
    except BaseException:
        # A Python failure cannot bypass native cleanup or abandon this reaper.
        reason = "supervisor-error"
    finally:
        cleanup = _cleanup(reaper, request["cleanup_timeout_milliseconds"] / 1000)
        # Reaper cleanup may have harvested the direct child after cancellation.
        # A missing status never becomes a fabricated successful exit.
        if process is not None and returncode is None:
            returncode = process.returncode
        # A child may request cancellation immediately before exiting. Drain
        # that signal after native subtree cleanup, before closing its pipe.
        if child_read is not None:
            with suppress(BlockingIOError):
                if os.read(child_read, 64) and reason == "completed":
                    reason = "cancelled"
            with suppress(OSError):
                os.close(child_read)
            child_read = None
        if child_write is not None:
            with suppress(OSError):
                os.close(child_write)
            child_write = None
        for name in streams:
            pipe = None if process is None else getattr(process, name)
            if pipe is not None:
                try:
                    while block := os.read(pipe.fileno(), 65536):
                        room = limits[name] - len(streams[name])
                        streams[name].extend(block[:room])
                        if len(block) > room:
                            reason = "output-limit"
                            break
                except (OSError, ValueError):
                    pass
                pipe.close()
        selector.close()
    if reason == "completed":
        try:
            boundary()
        except _Stopped as exc:
            reason = str(exc)
        if cleanup["signal_failures"]:
            reason = "cleanup-signal-failure"
    response = {
        "schema": RESPONSE_SCHEMA,
        "nonce": request["nonce"],
        "guardian_pid": reaper.pid,
        "reason": reason,
        "returncode": returncode,
        "stdout": base64.b64encode(streams["stdout"]).decode(),
        "stderr": base64.b64encode(streams["stderr"]).decode(),
        "elapsed_milliseconds": max(0, int((time.monotonic() - started) * 1000)),
        "cleanup": cleanup,
    }
    _emit(response)
    return 0 if reason == "completed" and response["cleanup"]["empty"] else 125


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-fd", required=True, type=int)
    args = parser.parse_args()
    request, payload, expiry, monotonic_deadline = _request(
        sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    )
    return supervise(args.control_fd, request, payload, expiry, monotonic_deadline)


if __name__ == "__main__":
    raise SystemExit(main())

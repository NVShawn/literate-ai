"""Stdlib-only storage measurement, run only in a supervised child process.

Private input arrives in the environment, never the command line or response.
This module performs no writes, traversal scans, cleanup, or tool installation.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time

REQUEST_ENVIRONMENT = "LITAI_WORKER_STORAGE_REQUEST"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
PROTOCOL = "literate-ai/worker-storage-probe@1"
REQUEST_PROTOCOL = "literate-ai/worker-storage-request@1"
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


def _metric(status, value=None):
    return {"status": status, "value": value}


def _failure(role, status):
    return {
        "role": role,
        "volume": None,
        "total_bytes": None,
        "available_bytes": _metric(status),
        "available_inodes": _metric(status),
        "quotas": [
            {
                "domain": "unobserved",
                "available_bytes": _metric(status),
                "available_inodes": _metric(status),
            }
        ],
    }


def _alias(prefix, value):
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:48]


def _same_node(before, after):
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def _native_quotas(descriptor, node, volume):
    implementation = globals().get("_native_quota_probe")
    if implementation is None:
        if __package__:
            from .worker_quota_probe import probe as implementation
        else:
            from worker_quota_probe import probe as implementation
    return implementation(descriptor, node, volume)


def _unix_sample(role, path):
    # All resolution, open, stat and filesystem calls happen in this child.
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        node = os.fstat(descriptor)
        space = os.fstatvfs(descriptor)
        fsid = getattr(space, "f_fsid", None)
        volume = _alias("volume-", f"{node.st_dev}:{fsid}")
        quotas = _native_quotas(descriptor, node, volume)
        if not _same_node(node, os.stat(path)):
            return _failure(role, "unavailable")
    finally:
        os.close(descriptor)
    total = space.f_blocks * space.f_frsize
    available = space.f_bavail * space.f_frsize
    if not 0 <= available <= total <= 2**63 - 1 or total == 0:
        return _failure(role, "malformed")
    inodes = _metric("unsupported")
    if space.f_files > 0 and 0 <= space.f_favail <= min(space.f_files, 2**63 - 1):
        inodes = _metric("measured", space.f_favail)
    return {
        "role": role,
        "volume": volume,
        "total_bytes": total,
        "available_bytes": _metric("measured", available),
        "available_inodes": inodes,
        "quotas": quotas,
    }


def _windows_sample(role, path, *, kernel=None):
    from ctypes import wintypes

    kernel = (
        ctypes.WinDLL("kernel32", use_last_error=True) if kernel is None else kernel
    )
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_path.restype = wintypes.BOOL
    volume_name = kernel.GetVolumeNameForVolumeMountPointW
    volume_name.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_name.restype = wintypes.BOOL
    disk_space = kernel.GetDiskFreeSpaceExW
    counter = ctypes.c_ulonglong
    disk_space.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(counter),
        ctypes.POINTER(counter),
        ctypes.POINTER(counter),
    )
    disk_space.restype = wintypes.BOOL
    before = os.stat(path)
    if not stat.S_ISDIR(before.st_mode):
        return _failure(role, "unavailable")
    mount = ctypes.create_unicode_buffer(32768)
    guid = ctypes.create_unicode_buffer(64)
    if not volume_path(path, mount, len(mount)) or not volume_name(
        mount.value, guid, len(guid)
    ):
        # Do not invent volume equality for UNC/unsupported volume providers.
        code = ctypes.get_last_error()
        status = (
            "denied"
            if code == 5
            else "unsupported"
            if code in {1, 50}
            else "unavailable"
        )
        return _failure(role, status)
    available, total, physical_free = counter(), counter(), counter()
    if not disk_space(
        path, ctypes.byref(available), ctypes.byref(total), ctypes.byref(physical_free)
    ):
        code = ctypes.get_last_error()
        return _failure(role, "denied" if code == 5 else "unavailable")
    if not _same_node(before, os.stat(path)):
        return _failure(role, "unavailable")
    if (
        not 0 <= available.value <= total.value <= 2**63 - 1
        or total.value == 0
        or available.value > physical_free.value
    ):
        return _failure(role, "malformed")
    volume = _alias("volume-", guid.value.casefold())
    return {
        "role": role,
        "volume": volume,
        "total_bytes": total.value,
        "available_bytes": _metric("measured", available.value),
        "available_inodes": _metric("not-applicable"),
        # This is a conservative quota budget: caller-available bytes include
        # quota and physical limits. Never substitute administrator free bytes.
        "quotas": [
            {
                "domain": _alias("quota-", guid.value.casefold()),
                "available_bytes": _metric("measured", available.value),
                "available_inodes": _metric("not-applicable"),
            }
        ],
    }


def collect(paths):
    family = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform)
    samples = []
    for role, path in paths:
        try:
            if family in {"macos", "linux"}:
                sample = _unix_sample(role, path)
            elif family == "windows":
                sample = _windows_sample(role, path)
            else:
                sample = _failure(role, "unsupported")
        except OSError as exc:
            sample = _failure(
                role,
                "denied" if exc.errno in {errno.EACCES, errno.EPERM} else "unavailable",
            )
        except (ValueError, OverflowError):
            sample = _failure(role, "malformed")
        samples.append(sample)
    return family, samples


def receive(timeout_ms):
    """Bound the remote process even after disconnect or missing input EOF."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60_000:
        return 2
    started = time.monotonic()
    timeout_response = [None]

    def expire():
        # Native SSH shells need not preserve nonzero child exit codes. Return a
        # request-bound failure when input was validated; never infer timeout
        # from a generic remote exit status. A second watchdog bounds a blocked
        # stdout write after the controller has disconnected/stopped reading.
        hard_stop = threading.Timer(0.1, lambda: os._exit(124))
        hard_stop.daemon = True
        try:
            hard_stop.start()
        except RuntimeError:
            # A worker already short of thread/process capacity must still stop;
            # never let failed timeout reporting remove the original deadline.
            os._exit(124)
        response = timeout_response[0]
        if response is not None:
            try:
                while response:
                    written = os.write(sys.stdout.fileno(), response)
                    if not written:
                        break
                    response = response[written:]
                if not response:
                    os._exit(0)
            except OSError:
                pass
        os._exit(124)

    def bind_timeout(raw, request):
        nonlocal watchdog
        family = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(
            sys.platform
        )
        timeout_response[0] = _response(
            raw,
            family,
            [_failure(role, "timed-out") for role, _path in request["paths"]],
        )
        # A provider's command-line ceiling cannot extend the request's budget.
        remaining = min(timeout_ms, request["timeout_ms"]) / 1000 - (
            time.monotonic() - started
        )
        watchdog.cancel()
        if remaining <= 0:
            expire()
        watchdog = threading.Timer(remaining, expire)
        watchdog.daemon = True
        watchdog.start()

    watchdog = threading.Timer(timeout_ms / 1000, expire)
    watchdog.daemon = True
    watchdog.start()
    # Keep the watchdog through stdout flush/process exit, not just measurement.
    return main(
        raw=sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1), on_request=bind_timeout
    )


def _response(raw, family, samples):
    result = {
        "schema": PROTOCOL,
        "request_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "os_family": family,
        "samples": samples,
    }
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main(*, raw=None, on_request=None):
    try:
        if raw is None:
            raw = os.environ.pop(REQUEST_ENVIRONMENT, "").encode("utf-8")
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            return 2
        request = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(request, dict) or set(request) != {
            "schema",
            "worker_id",
            "worker_identity",
            "policy_identity",
            "job_identity",
            "os_family",
            "timeout_ms",
            "paths",
            "nonce",
        }:
            return 2
        if (
            request["schema"] != REQUEST_PROTOCOL
            or not isinstance(request["worker_id"], str)
            or not _ALIAS.fullmatch(request["worker_id"])
            or any(
                not isinstance(request[k], str) or not _IDENTITY.fullmatch(request[k])
                for k in ("worker_identity", "policy_identity")
            )
            or (
                request["job_identity"] is not None
                and (
                    not isinstance(request["job_identity"], str)
                    or not _IDENTITY.fullmatch(request["job_identity"])
                )
            )
            or request["os_family"] not in {"linux", "macos", "windows"}
            or request["os_family"]
            != {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
                sys.platform
            )
            or type(request["timeout_ms"]) is not int
            or not 1 <= request["timeout_ms"] <= 60_000
        ):
            return 2
        paths = request["paths"]
        if not isinstance(paths, list) or not 4 <= len(paths) <= 16:
            return 2
        if any(
            not isinstance(item, list)
            or len(item) != 2
            or any(
                not isinstance(value, str) or not value or "\x00" in value
                for value in item
            )
            for item in paths
        ):
            return 2
        if (
            not isinstance(request["nonce"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", request["nonce"])
            or any(
                not _ALIAS.fullmatch(role)
                or len(path) > 4096
                or not os.path.isabs(path)
                for role, path in paths
            )
            or [role for role, _path in paths]
            != sorted({role for role, _path in paths})
        ):
            return 2
        if on_request is not None:
            on_request(raw, request)
        family, samples = collect(paths)
        encoded = _response(raw, family, samples)
        if len(encoded) > MAX_RESPONSE_BYTES:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return 2


def entrypoint(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receive", action="store_true")
    parser.add_argument("--timeout-ms", type=int)
    args = parser.parse_args(argv)
    if args.receive:
        return receive(args.timeout_ms)
    if args.timeout_ms is not None:
        return 2
    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())

"""Stdlib-only bounded worker pressure receiver for local and SSH execution."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

REQUEST_ENVIRONMENT = "LITAI_WORKER_PRESSURE_REQUEST"
REQUEST_PROTOCOL = "literate-ai/worker-pressure-request@1"
RESPONSE_PROTOCOL = "literate-ai/worker-pressure-response@1"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 64 * 1024


def _metric(status, value=None):
    return {"status": status, "value": value}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _cpu():
    try:
        return _metric(
            "measured",
            min(
                10000, max(0, round(os.getloadavg()[0] * 10000 / (os.cpu_count() or 1)))
            ),
        )
    except (AttributeError, OSError):
        return _metric("unsupported")


def _memory():
    try:
        if sys.platform.startswith("linux"):
            values = {}
            with open("/proc/meminfo", encoding="utf-8") as stream:
                for line in stream:
                    name, separator, raw = line.partition(":")
                    if separator:
                        values[name] = int(raw.strip().split()[0]) * 1024
            return _metric("measured", values["MemAvailable"])
        if sys.platform == "win32":

            class Status(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = Status()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return _metric("unavailable")
            return _metric("measured", status.available_physical)
        if sys.platform == "darwin":
            output = subprocess.run(
                ("/usr/bin/vm_stat",),
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout
            page_size = int(output.split("page size of ", 1)[1].split(" bytes", 1)[0])
            counts = {}
            for line in output.splitlines()[1:]:
                name, separator, raw = line.partition(":")
                if separator:
                    counts[name] = int(raw.strip().rstrip("."))
            pages = sum(
                counts.get(name, 0)
                for name in ("Pages free", "Pages inactive", "Pages speculative")
            )
            return _metric("measured", pages * page_size)
        return _metric(
            "measured",
            os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"),
        )
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        return _metric("unavailable")


def _paging():
    if not sys.platform.startswith("linux"):
        return None
    try:
        values = {}
        with open("/proc/vmstat", encoding="utf-8") as stream:
            for line in stream:
                name, raw = line.split()
                if name in {"pgpgin", "pgpgout"}:
                    values[name] = int(raw) * 1024
        return values["pgpgin"] + values["pgpgout"], time.monotonic_ns()
    except (KeyError, OSError, ValueError):
        return None


def _gpus():
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return []
    try:
        result = subprocess.run(
            (
                executable,
                "--query-gpu=index,utilization.gpu,memory.free",
                "--format=csv,noheader,nounits",
            ),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode:
            return []
        devices = []
        for line in result.stdout.splitlines()[:16]:
            index, utilization, free_mib = (item.strip() for item in line.split(","))
            devices.append(
                {
                    "device": f"gpu-{int(index)}",
                    "utilization_basis_points": _metric(
                        "measured", int(utilization) * 100
                    ),
                    "free_memory_bytes": _metric(
                        "measured", int(free_mib) * 1024 * 1024
                    ),
                    "allocation_failed": False,
                }
            )
        return devices
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def collect(request):
    count = request["sustained_samples"]
    interval = request["sample_interval_ms"]
    samples = []
    previous = _paging()
    previous_observed_at_ms = -1
    for index in range(count):
        if index and interval:
            time.sleep(interval / 1000)
        paging = _paging()
        if previous is None or paging is None or paging[1] <= previous[1]:
            paging_metric = _metric("unsupported")
        else:
            elapsed = (paging[1] - previous[1]) / 1_000_000_000
            paging_metric = _metric(
                "measured", max(0, round((paging[0] - previous[0]) / elapsed))
            )
        previous = paging
        observed_at_ms = max(time.time_ns() // 1_000_000, previous_observed_at_ms + 1)
        previous_observed_at_ms = observed_at_ms
        samples.append(
            {
                "observed_at_ms": observed_at_ms,
                "cpu_utilization_basis_points": _cpu(),
                "available_memory_bytes": _memory(),
                "paging_bytes_per_second": paging_metric,
                "progress": _metric("unavailable"),
                "queue_depth": _metric("unavailable"),
                "gpus": _gpus(),
            }
        )
    return samples


def receive():
    raw = (
        os.environ.pop(REQUEST_ENVIRONMENT).encode()
        if REQUEST_ENVIRONMENT in os.environ
        else sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    )
    if len(raw) > MAX_REQUEST_BYTES:
        return 2
    try:
        request = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            set(request)
            != {
                "schema",
                "os_family",
                "sustained_samples",
                "sample_interval_ms",
                "nonce",
            }
            or request["schema"] != REQUEST_PROTOCOL
            or request["os_family"]
            != {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
                sys.platform
            )
            or isinstance(request["sustained_samples"], bool)
            or not isinstance(request["sustained_samples"], int)
            or not 2 <= request["sustained_samples"] <= 32
            or isinstance(request["sample_interval_ms"], bool)
            or not isinstance(request["sample_interval_ms"], int)
            or not 0 <= request["sample_interval_ms"] <= 5000
            or not isinstance(request["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None
        ):
            return 2
        response = {
            "schema": RESPONSE_PROTOCOL,
            "request_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "os_family": request["os_family"],
            "samples": collect(request),
        }
        output = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(output) > MAX_RESPONSE_BYTES:
            return 2
        sys.stdout.buffer.write(output)
        return 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(receive())

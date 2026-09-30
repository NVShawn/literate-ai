"""Bounded local pressure sampling with explicit unsupported remote evidence."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

from literate_ai import worker_pressure_probe
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.ssh_transport import ssh_arguments
from literate_ai.adapters.worker_storage import (
    _command_environment,
    _run_probe,
    _unique_object,
)
from literate_ai.application.worker_pressure import (
    GpuPressureSample,
    PressurePolicy,
    PressureSample,
)
from literate_ai.contracts import ExecutionWorkerKind, canonical_json_bytes
from literate_ai.contracts.worker_capacity import CapacityMetric, CapacityProbeStatus


def _metric(value: int | None, status=CapacityProbeStatus.MEASURED) -> CapacityMetric:
    return CapacityMetric(
        status, value if status is CapacityProbeStatus.MEASURED else None
    )


def _cpu_basis_points() -> CapacityMetric:
    try:
        load = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return _metric(min(10000, max(0, round(load * 10000 / cores))))
    except (AttributeError, OSError):
        return _metric(None, CapacityProbeStatus.UNSUPPORTED)


def _available_memory() -> CapacityMetric:
    try:
        if sys.platform.startswith("linux"):
            values = {}
            with open("/proc/meminfo", encoding="utf-8") as stream:
                for line in stream:
                    name, separator, raw = line.partition(":")
                    if separator:
                        values[name] = int(raw.strip().split()[0]) * 1024
            return _metric(values["MemAvailable"])
        if sys.platform == "win32":

            class MemoryStatus(ctypes.Structure):
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

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return _metric(None, CapacityProbeStatus.UNAVAILABLE)
            return _metric(status.available_physical)
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
            return _metric(pages * page_size)
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return _metric(pages * size)
    except (
        KeyError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        return _metric(None, CapacityProbeStatus.UNAVAILABLE)


def _paging_counter() -> tuple[int, int] | None:
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
    except (OSError, KeyError, ValueError):
        return None


def _gpus() -> tuple[GpuPressureSample, ...]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return ()
    try:
        completed = subprocess.run(
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
        if completed.returncode:
            return ()
        devices = []
        for line in completed.stdout.splitlines()[:16]:
            index, utilization, free_mib = (item.strip() for item in line.split(","))
            devices.append(
                GpuPressureSample(
                    f"gpu-{int(index)}",
                    _metric(int(utilization) * 100),
                    _metric(int(free_mib) * 1024 * 1024),
                )
            )
        return tuple(devices)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ()


def _native_snapshot(previous_paging):
    paging = _paging_counter()
    if previous_paging is None or paging is None or paging[1] <= previous_paging[1]:
        paging_metric = _metric(None, CapacityProbeStatus.UNSUPPORTED)
    else:
        elapsed = (paging[1] - previous_paging[1]) / 1_000_000_000
        paging_metric = _metric(
            max(0, round((paging[0] - previous_paging[0]) / elapsed))
        )
    return (
        _cpu_basis_points(),
        _available_memory(),
        paging_metric,
        _gpus(),
        paging,
    )


def _probe_command(executable):
    source = Path(worker_pressure_probe.__file__).read_bytes()
    if len(source) > 64 * 1024:
        raise ValueError("worker pressure probe source exceeds its staging bound")
    encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
    script = (
        "import base64,zlib;s={'__name__':'worker_pressure_receiver'};exec("
        "compile(zlib.decompress(base64.b64decode("
        + repr(encoded)
        + ")), '<worker-pressure-probe>', 'exec'),s);"
        "raise SystemExit(s['receive']())"
    )
    return (executable, "-I", "-S", "-B", "-c", script)


def _unavailable_samples(policy, status):
    metric = _metric(None, status)
    return tuple(
        PressureSample(index, metric, metric, metric, metric, metric)
        for index in range(policy.sustained_samples)
    )


def _remote_samples(bindings, policy, runner):
    payload = canonical_json_bytes(
        {
            "schema": worker_pressure_probe.REQUEST_PROTOCOL,
            "os_family": bindings.os_family,
            "sustained_samples": policy.sustained_samples,
            "sample_interval_ms": policy.sample_interval_ms,
            "nonce": secrets.token_hex(16),
        }
    )
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    environment = {
        key: os.environ[key]
        for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if key in os.environ
    }
    timeout = min(
        60.0,
        max(
            5.0,
            policy.sample_interval_ms * (policy.sustained_samples - 1) / 1000 + 5,
        ),
    )
    if bindings.worker.kind is ExecutionWorkerKind.SSH:
        windows = bindings.os_family == "windows"
        receiver = _probe_command(
            bindings.python_executable or ("python" if windows else "python3")
        )
        remote_command = (
            subprocess.list2cmdline(receiver) if windows else shlex.join(receiver)
        )
        command = ssh_arguments(
            bindings.worker.endpoint,
            remote_command,
            timeout,
            transport=bindings.worker.transport,
            login_shell=not windows,
        )
        if "SSH_AUTH_SOCK" in os.environ:
            environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    elif bindings.worker.kind is ExecutionWorkerKind.COMMAND:
        if bindings.health_command is None:
            return _unavailable_samples(policy, CapacityProbeStatus.UNSUPPORTED)
        command = (*bindings.health_command.command, "--pressure-receive")
        try:
            environment.update(_command_environment(bindings.health_command))
        except PermissionError:
            return _unavailable_samples(policy, CapacityProbeStatus.DENIED)
        except ValueError:
            return _unavailable_samples(policy, CapacityProbeStatus.MALFORMED)
    else:
        raise AssertionError("remote pressure requires SSH or command worker")
    try:
        completed = runner(
            command,
            environment=environment,
            timeout_seconds=timeout,
            input_bytes=payload,
        )
        if completed.returncode:
            return _unavailable_samples(policy, CapacityProbeStatus.UNREACHABLE)
        try:
            response = json.loads(completed.stdout, object_pairs_hook=_unique_object)
        except (TypeError, ValueError):
            return _unavailable_samples(policy, CapacityProbeStatus.MALFORMED)
        if (
            set(response) != {"schema", "request_identity", "os_family", "samples"}
            or response["schema"] != worker_pressure_probe.RESPONSE_PROTOCOL
            or response["request_identity"] != expected
            or response["os_family"] != bindings.os_family
            or len(response["samples"]) != policy.sustained_samples
        ):
            return _unavailable_samples(policy, CapacityProbeStatus.MALFORMED)
        samples = []
        for item in response["samples"]:
            gpus = tuple(
                GpuPressureSample(
                    gpu["device"],
                    CapacityMetric.from_dict(gpu["utilization_basis_points"]),
                    CapacityMetric.from_dict(gpu["free_memory_bytes"]),
                    gpu["allocation_failed"],
                )
                for gpu in item["gpus"]
            )
            samples.append(
                PressureSample(
                    item["observed_at_ms"],
                    CapacityMetric.from_dict(item["cpu_utilization_basis_points"]),
                    CapacityMetric.from_dict(item["available_memory_bytes"]),
                    CapacityMetric.from_dict(item["paging_bytes_per_second"]),
                    CapacityMetric.from_dict(item["progress"]),
                    CapacityMetric.from_dict(item["queue_depth"]),
                    gpus,
                )
            )
        return tuple(samples)
    except BuildError:
        return _unavailable_samples(policy, CapacityProbeStatus.UNREACHABLE)
    except (OSError, TypeError, ValueError):
        return _unavailable_samples(policy, CapacityProbeStatus.MALFORMED)


def observe_worker_pressure(
    bindings, policy: PressurePolicy, *, snapshot=_native_snapshot, runner=_run_probe
):
    """Collect a bounded local/SSH/explicit-command pressure window."""

    if bindings.worker.kind is not ExecutionWorkerKind.LOCAL:
        return _remote_samples(bindings, policy, runner)
    samples = []
    previous_paging = None
    previous_observed_at_ms = -1
    for index in range(policy.sustained_samples):
        if index and policy.sample_interval_ms:
            time.sleep(policy.sample_interval_ms / 1000)
        cpu, memory, paging, gpus, previous_paging = snapshot(previous_paging)
        observed_at_ms = max(time.time_ns() // 1_000_000, previous_observed_at_ms + 1)
        previous_observed_at_ms = observed_at_ms
        samples.append(
            PressureSample(
                observed_at_ms,
                cpu,
                memory,
                paging,
                _metric(None, CapacityProbeStatus.UNAVAILABLE),
                _metric(None, CapacityProbeStatus.UNAVAILABLE),
                gpus,
            )
        )
    return tuple(samples)


__all__ = ["observe_worker_pressure"]

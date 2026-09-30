"""Bounded platform probes for private execution-worker inventory."""

from __future__ import annotations

import base64
import json
import os
import platform
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.ssh_transport import SshTransportError, ssh_arguments
from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    NvidiaProbeStatus,
    ObservedGpuDevice,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
    canonical_json_bytes,
)

MAX_PROBE_OUTPUT_BYTES = 256 * 1024
DEFAULT_PROBE_TIMEOUT_SECONDS = 20


class WorkerCapabilityProbeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ProbeRunner(Protocol):
    def __call__(
        self, argv: tuple[str, ...], timeout_seconds: int
    ) -> subprocess.CompletedProcess[bytes]: ...


def _run(
    argv: tuple[str, ...], timeout_seconds: int
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = run_with_tree_kill(
            argv,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerCapabilityProbeError(
            "worker.probe_transport_failed", f"probe command failed: {argv[0]}"
        ) from exc
    if (
        len(result.stdout) > MAX_PROBE_OUTPUT_BYTES
        or len(result.stderr) > MAX_PROBE_OUTPUT_BYTES
    ):
        raise WorkerCapabilityProbeError(
            "worker.probe_output_oversized", "worker probe exceeded its output limit"
        )
    return result


def _text(result: subprocess.CompletedProcess[bytes], *, label: str) -> str:
    if result.returncode != 0:
        raise WorkerCapabilityProbeError(
            "worker.probe_failed", f"{label} exited {result.returncode}"
        )
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorkerCapabilityProbeError(
            "worker.probe_malformed", f"{label} returned non-UTF-8 output"
        ) from exc


def _invoke(
    worker: ExecutionWorker, command: tuple[str, ...], runner: ProbeRunner, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    try:
        if worker.kind is ExecutionWorkerKind.LOCAL:
            return runner(command, timeout)
        if worker.kind is ExecutionWorkerKind.SSH:
            assert worker.endpoint is not None
            windows = worker.requirements.os_family == "windows"
            remote_command = " ".join(command) if windows else shlex.join(command)
            result = runner(
                ssh_arguments(
                    worker.endpoint,
                    remote_command,
                    timeout,
                    transport=worker.transport,
                    login_shell=not windows,
                ),
                timeout,
            )
            if result.returncode == 255:
                raise WorkerCapabilityProbeError(
                    "worker.probe_transport_failed",
                    "SSH transport or non-interactive authentication failed",
                )
            return result
    except (OSError, SshTransportError, subprocess.TimeoutExpired) as exc:
        raise WorkerCapabilityProbeError(
            "worker.probe_transport_failed", "worker probe transport failed"
        ) from exc
    raise WorkerCapabilityProbeError(
        "worker.probe_protocol_unsupported",
        "command workers need an explicit hardware-observation protocol",
    )


def _json_probe(
    worker: ExecutionWorker,
    script: str,
    runner: ProbeRunner,
    timeout: int,
    os_family: str,
) -> dict[str, object]:
    if os_family == "windows":
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        argv = (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        )
    else:
        argv = ("sh", "-c", script)
    raw = _text(
        _invoke(worker, argv, runner, timeout), label=f"{os_family} hardware probe"
    )
    # Parse one complete object while tolerating PowerShell's trailing CLIXML progress
    # stream. This also admits pretty-printed multi-line system_profiler JSON.
    start = raw.find("{")
    candidate = raw[start:] if start >= 0 else raw
    try:
        value, _remainder = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError as exc:
        raise WorkerCapabilityProbeError(
            "worker.probe_malformed", "hardware probe did not return one JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise WorkerCapabilityProbeError(
            "worker.probe_malformed", "hardware probe result must be an object"
        )
    return value


# These strings are platform programs, not Python statements. Keep protocol lines
# intact and exempt only the embedded programs from Python's line-length rule.
_LINUX_SCRIPT = r'''set -eu  # noqa: E501
physical=$(if command -v lscpu >/dev/null 2>&1; then lscpu -p=SOCKET,CORE | grep -v '^#' | sort -u | wc -l; else getconf _NPROCESSORS_ONLN; fi)  # noqa: E501
name=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"' | head -1)
version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"' | head -1)
gpu_status=absent; gpu_json='[]'; diagnostic=null
if command -v nvidia-smi >/dev/null 2>&1; then
  output=$(nvidia-smi --query-gpu=index,name,uuid,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>&1) && gpu_status=ok || gpu_status=degraded  # noqa: E501
  if [ "$gpu_status" = ok ]; then
    gpu_json=$(printf '%s\n' "$output" | awk -F', *' 'BEGIN{printf "["}{if(NR>1)printf ","; gsub(/\\/,"\\\\",$2); gsub(/"/,"\\\"",$2); printf "{\"index\":%d,\"model\":\"%s\",\"uuid\":\"%s\",\"memory_mib\":%d,\"compute_capability\":\"%s\",\"driver_version\":\"%s\"}",$1,$2,$3,$4,$5,$6}END{printf "]"}')  # noqa: E501
  else diagnostic='"nvidia-smi is installed but its device query failed"'; fi
fi
printf '{"os_name":"%s","os_version":"%s","architecture":"%s","physical_cores":%s,"logical_cores":%s,"memory_mib":%s,"nvidia_status":"%s","gpus":%s,"diagnostic":%s}\n' "$name" "$version" "$(uname -m)" "$physical" "$(getconf _NPROCESSORS_ONLN)" "$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)" "$gpu_status" "$gpu_json" "$diagnostic"'''  # noqa: E501

_MACOS_SCRIPT = r'''set -eu
gpu=$(system_profiler SPDisplaysDataType -json)
printf '{"os_name":"macos","os_version":"%s","architecture":"%s","physical_cores":%s,"logical_cores":%s,"memory_mib":%s,"nvidia_status":"not-applicable","system_profiler":%s,"diagnostic":null}\n' "$(sw_vers -productVersion)" "$(uname -m)" "$(sysctl -n hw.physicalcpu)" "$(sysctl -n hw.logicalcpu)" "$(( $(sysctl -n hw.memsize) / 1048576 ))" "$gpu"'''  # noqa: E501

_WINDOWS_SCRIPT = r"""$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$os=Get-CimInstance Win32_OperatingSystem
$release=if($os.Caption -match 'Windows ([0-9]+)'){$Matches[1]}else{$os.Version}
$cs=Get-CimInstance Win32_ComputerSystem
$cpus=@(Get-CimInstance Win32_Processor)
$smi=Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
$devices=@(); $status='absent'; $diagnostic=$null
if ($null -ne $smi) {
  $lines=@(& $smi.Source --query-gpu=index,name,uuid,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>&1)  # noqa: E501
  if ($LASTEXITCODE -eq 0) {
    $status='ok'; foreach($line in $lines){$v=$line -split ',\s*'; $devices += [ordered]@{index=[int]$v[0];model=$v[1];uuid=$v[2];memory_mib=[int]$v[3];compute_capability=$v[4];driver_version=$v[5]}}  # noqa: E501
  } else {$status='degraded';$diagnostic='nvidia-smi is installed but its device query failed'}  # noqa: E501
}
[ordered]@{os_name=$os.Caption;os_version=$release;architecture=$env:PROCESSOR_ARCHITECTURE;physical_cores=($cpus|Measure-Object NumberOfCores -Sum).Sum;logical_cores=($cpus|Measure-Object NumberOfLogicalProcessors -Sum).Sum;memory_mib=[math]::Floor($cs.TotalPhysicalMemory/1MB);nvidia_status=$status;gpus=$devices;diagnostic=$diagnostic}|ConvertTo-Json -Compress -Depth 5"""  # noqa: E501


def _device(value: dict[str, object], *, vendor: str) -> ObservedGpuDevice:
    return ObservedGpuDevice(
        vendor,
        str(value["model"]),
        int(value["index"]),
        str(value["uuid"]),
        int(value["memory_mib"]),
        str(value["compute_capability"]),
        str(value["driver_version"]),
    )


def probe_worker_capabilities(
    worker: ExecutionWorker,
    *,
    runner: ProbeRunner = _run,
    timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
    observed_at: str | None = None,
) -> WorkerHardwareObservation:
    family = worker.requirements.os_family
    if family is None:
        family = (
            {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(
                platform.system()
            )
            if worker.kind is ExecutionWorkerKind.LOCAL
            else None
        )
    if family not in {"linux", "macos", "windows"}:
        raise WorkerCapabilityProbeError(
            "worker.probe_os_required", "worker probing requires a declared OS family"
        )
    value = _json_probe(
        worker,
        {"linux": _LINUX_SCRIPT, "macos": _MACOS_SCRIPT, "windows": _WINDOWS_SCRIPT}[
            family
        ],
        runner,
        timeout_seconds,
        family,
    )
    devices: list[ObservedGpuDevice] = []
    if family == "macos":
        records = (
            value.get("system_profiler", {}).get("SPDisplaysDataType", [])
            if isinstance(value.get("system_profiler"), dict)
            else []
        )
        for record in records:
            if isinstance(record, dict) and record.get("sppci_model"):
                cores = record.get("sppci_cores")
                devices.append(
                    ObservedGpuDevice(
                        "apple",
                        str(record["sppci_model"]),
                        core_count=None if cores is None else int(cores),
                    )
                )
    else:
        raw_devices = value.get("gpus", [])
        if not isinstance(raw_devices, list):
            raise WorkerCapabilityProbeError(
                "worker.probe_malformed", "gpus must be a list"
            )
        devices.extend(
            _device(item, vendor="nvidia")
            for item in raw_devices
            if isinstance(item, dict)
        )
    stamp = observed_at or datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        return WorkerHardwareObservation(
            worker.worker_id,
            stamp,
            family,
            str(value["os_name"]),
            str(value["os_version"]),
            str(value["architecture"]),
            int(value["physical_cores"]),
            int(value["logical_cores"]),
            int(value["memory_mib"]),
            tuple(devices),
            NvidiaProbeStatus(str(value["nvidia_status"])),
            None if value.get("diagnostic") is None else str(value["diagnostic"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerCapabilityProbeError(
            "worker.probe_malformed",
            "hardware probe omitted or malformed required facts",
        ) from exc


def probe_worker_catalog(
    catalog: ExecutionWorkerCatalog,
    *,
    worker_ids: tuple[str, ...] | None = None,
    runner: ProbeRunner = _run,
    timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
    observed_at: str | None = None,
) -> WorkerHardwareObservationCatalog:
    selected = (
        catalog.workers
        if worker_ids is None
        else tuple(catalog.worker(item) for item in sorted(set(worker_ids)))
    )
    with ThreadPoolExecutor(max_workers=min(len(selected), 16) or 1) as pool:
        results = tuple(
            pool.map(
                lambda worker: probe_worker_capabilities(
                    worker,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                    observed_at=observed_at,
                ),
                selected,
            )
        )
    return WorkerHardwareObservationCatalog(
        tuple(sorted(results, key=lambda item: item.worker_id))
    )


def write_worker_observations(
    path: Path, catalog: WorkerHardwareObservationCatalog
) -> None:
    destination = path.expanduser()
    if not destination.is_absolute():
        raise OSError("worker observation destination must be absolute")
    if destination.is_symlink():
        raise OSError("worker observation destination must not be a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise OSError("worker observation directory must be a real directory")
    payload = canonical_json_bytes(catalog.to_dict())
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        if os.name != "nt":
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_worker_observations(path: Path) -> WorkerHardwareObservationCatalog:
    candidate = path.expanduser()
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError("worker observation catalog is not a regular file")
        payload = candidate.read_bytes()
    except OSError as exc:
        raise WorkerCapabilityProbeError(
            "worker.observation_catalog_unavailable",
            "worker observation catalog is unavailable; run `litai worker probe`",
        ) from exc
    if len(payload) > MAX_PROBE_OUTPUT_BYTES:
        raise WorkerCapabilityProbeError(
            "worker.observation_catalog_oversized",
            "worker observation catalog exceeds its size bound",
        )
    try:
        return WorkerHardwareObservationCatalog.from_dict(json.loads(payload))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise WorkerCapabilityProbeError(
            "worker.observation_catalog_invalid",
            "worker observation catalog is invalid",
        ) from exc


__all__ = [
    "WorkerCapabilityProbeError",
    "probe_worker_capabilities",
    "probe_worker_catalog",
    "load_worker_observations",
    "write_worker_observations",
]

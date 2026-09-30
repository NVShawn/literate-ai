"""Pure sustained-pressure classification for bounded worker samples."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.application.worker_capacity import CapacityHealth
from literate_ai.contracts.worker_capacity import CapacityMetric, CapacityProbeStatus


@dataclass(frozen=True, slots=True)
class PressurePolicy:
    sustained_samples: int
    cpu_warning_basis_points: int
    minimum_available_memory_bytes: int
    maximum_paging_bytes_per_second: int
    minimum_gpu_free_memory_bytes: int
    sample_interval_ms: int = 100
    require_cpu: bool = True
    require_memory: bool = True
    require_gpu: bool = False

    def __post_init__(self) -> None:
        if not 2 <= self.sustained_samples <= 32:
            raise ValueError("worker.pressure_sustained_samples_invalid")
        if not 0 <= self.sample_interval_ms <= 5000:
            raise ValueError("worker.pressure_sample_interval_invalid")
        if self.sample_interval_ms * (self.sustained_samples - 1) > 55000:
            raise ValueError("worker.pressure_window_invalid")
        if not all(
            isinstance(value, bool)
            for value in (self.require_cpu, self.require_memory, self.require_gpu)
        ):
            raise ValueError("worker.pressure_policy_invalid")
        for value, maximum in (
            (self.cpu_warning_basis_points, 10000),
            (self.minimum_available_memory_bytes, 2**63 - 1),
            (self.maximum_paging_bytes_per_second, 2**63 - 1),
            (self.minimum_gpu_free_memory_bytes, 2**63 - 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("worker.pressure_policy_invalid")
            if not 0 <= value <= maximum:
                raise ValueError("worker.pressure_policy_invalid")


@dataclass(frozen=True, slots=True)
class GpuPressureSample:
    device: str
    utilization_basis_points: CapacityMetric
    free_memory_bytes: CapacityMetric
    allocation_failed: bool = False


@dataclass(frozen=True, slots=True)
class PressureSample:
    observed_at_ms: int
    cpu_utilization_basis_points: CapacityMetric
    available_memory_bytes: CapacityMetric
    paging_bytes_per_second: CapacityMetric
    progress: CapacityMetric
    queue_depth: CapacityMetric
    gpus: tuple[GpuPressureSample, ...] = ()


@dataclass(frozen=True, slots=True)
class PressureFinding:
    resource: str
    health: CapacityHealth
    reason: str
    required: bool
    measured: int | None = None
    threshold: int | None = None

    @property
    def roles(self) -> tuple[str, ...]:
        return ("worker",)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource": self.resource,
            "health": self.health.value,
            "reason": self.reason,
            "required": self.required,
            "measured": self.measured,
            "threshold": self.threshold,
        }


def _unknown(resource: str, metric: CapacityMetric, required: bool) -> PressureFinding:
    return PressureFinding(
        resource,
        CapacityHealth.UNKNOWN,
        f"probe-{metric.status.value}",
        required,
    )


def _measured_tail(
    samples: tuple[PressureSample, ...], attribute: str, count: int
) -> tuple[int, ...] | None:
    metrics = tuple(getattr(sample, attribute) for sample in samples[-count:])
    if len(metrics) < count or any(
        metric.status is not CapacityProbeStatus.MEASURED for metric in metrics
    ):
        return None
    return tuple(metric.value for metric in metrics if metric.value is not None)


def assess_worker_pressure(
    policy: PressurePolicy, samples: tuple[PressureSample, ...]
) -> tuple[PressureFinding, ...]:
    """Distinguish sustained workload impact from ordinary busy utilization."""

    if not samples:
        missing = CapacityMetric(CapacityProbeStatus.UNAVAILABLE)
        return (
            _unknown("cpu", missing, policy.require_cpu),
            _unknown("memory", missing, policy.require_memory),
            _unknown("gpu", missing, policy.require_gpu),
        )
    if len(samples) > 32 or tuple(item.observed_at_ms for item in samples) != tuple(
        sorted({item.observed_at_ms for item in samples})
    ):
        raise ValueError("worker.pressure_samples_invalid")
    count = policy.sustained_samples
    latest = samples[-1]
    progress = _measured_tail(samples, "progress", count)
    queue = _measured_tail(samples, "queue_depth", count)
    stalled = progress is not None and len(set(progress)) == 1
    queue_impacted = queue is not None and queue[-1] > queue[0]
    workload_impact = stalled or queue_impacted
    findings: list[PressureFinding] = []

    cpu = _measured_tail(samples, "cpu_utilization_basis_points", count)
    if cpu is None:
        findings.append(
            _unknown("cpu", latest.cpu_utilization_basis_points, policy.require_cpu)
        )
    elif all(value >= policy.cpu_warning_basis_points for value in cpu):
        findings.append(
            PressureFinding(
                "cpu",
                CapacityHealth.WARNING if workload_impact else CapacityHealth.HEALTHY,
                "sustained-load-impact" if workload_impact else "busy-progressing",
                policy.require_cpu,
                min(cpu),
                policy.cpu_warning_basis_points,
            )
        )
    else:
        findings.append(
            PressureFinding(
                "cpu",
                CapacityHealth.HEALTHY,
                "load-not-sustained",
                policy.require_cpu,
                cpu[-1],
                policy.cpu_warning_basis_points,
            )
        )

    memory = _measured_tail(samples, "available_memory_bytes", count)
    paging = _measured_tail(samples, "paging_bytes_per_second", count)
    if memory is None:
        findings.append(
            _unknown("memory", latest.available_memory_bytes, policy.require_memory)
        )
    else:
        low = all(value < policy.minimum_available_memory_bytes for value in memory)
        if not low:
            findings.append(
                PressureFinding(
                    "memory",
                    CapacityHealth.HEALTHY,
                    "memory-sufficient",
                    policy.require_memory,
                    min(memory),
                    policy.minimum_available_memory_bytes,
                )
            )
            paging = None
        elif paging is None:
            findings.append(
                _unknown(
                    "paging", latest.paging_bytes_per_second, policy.require_memory
                )
            )
            paging = None
        if paging is None:
            pass
        else:
            active_paging = all(
                value > policy.maximum_paging_bytes_per_second for value in paging
            )
            overloaded = low and (active_paging or workload_impact)
            findings.append(
                PressureFinding(
                    "memory",
                    CapacityHealth.CRITICAL if overloaded else CapacityHealth.WARNING,
                    "sustained-memory-pressure" if overloaded else "memory-reserve-low",
                    policy.require_memory,
                    min(memory),
                    policy.minimum_available_memory_bytes,
                )
            )

    gpus = tuple(device for sample in samples[-count:] for device in sample.gpus)
    if not gpus:
        missing = CapacityMetric(CapacityProbeStatus.NOT_APPLICABLE)
        findings.append(_unknown("gpu", missing, policy.require_gpu))
    elif any(device.allocation_failed for device in gpus):
        findings.append(
            PressureFinding(
                "gpu",
                CapacityHealth.CRITICAL,
                "gpu-allocation-failed",
                policy.require_gpu,
            )
        )
    else:
        latest_devices = latest.gpus
        bad = next(
            (
                metric
                for device in latest_devices
                for metric in (
                    device.utilization_basis_points,
                    device.free_memory_bytes,
                )
                if metric.status is not CapacityProbeStatus.MEASURED
            ),
            None,
        )
        if bad is not None:
            findings.append(_unknown("gpu", bad, policy.require_gpu))
        else:
            free = min(device.free_memory_bytes.value or 0 for device in latest_devices)
            low = free < policy.minimum_gpu_free_memory_bytes
            findings.append(
                PressureFinding(
                    "gpu",
                    CapacityHealth.WARNING
                    if low and workload_impact
                    else CapacityHealth.HEALTHY,
                    "gpu-memory-impact"
                    if low and workload_impact
                    else "gpu-busy-progressing",
                    policy.require_gpu,
                    free,
                    policy.minimum_gpu_free_memory_bytes,
                )
            )
    return tuple(findings)


__all__ = [
    "GpuPressureSample",
    "PressureFinding",
    "PressurePolicy",
    "PressureSample",
    "assess_worker_pressure",
]

"""Observed worker capacity, kept distinct from authored routing requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    list_value,
    optional_string,
    string_value,
)
from .execution_dispatch import ExecutionRequirements
from .identity import ContentIdentity, canonical_identity

OBSERVED_GPU_DEVICE_SCHEMA = "urn:literate-ai:schema:v1:observed-gpu-device"
WORKER_HARDWARE_OBSERVATION_SCHEMA = (
    "urn:literate-ai:schema:v1:worker-hardware-observation"
)
WORKER_HARDWARE_CATALOG_SCHEMA = (
    "urn:literate-ai:schema:v1:worker-hardware-observation-catalog"
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_OS_FAMILIES = frozenset({"linux", "macos", "windows"})


def _identifier(value: Any, path: str) -> str:
    result = string_value(value, path, max_length=64)
    if not _IDENTIFIER.fullmatch(result):
        fail(path, "must be a portable lower-case identifier")
    return result


def _optional_positive_int(value: Any, path: str) -> int | None:
    return None if value is None else int_value(value, path, minimum=1)


class NvidiaProbeStatus(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    ABSENT = "absent"
    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ObservedGpuDevice:
    vendor: str
    model: str
    index: int | None = None
    uuid: str | None = None
    memory_mib: int | None = None
    compute_capability: str | None = None
    driver_version: str | None = None
    core_count: int | None = None

    SCHEMA: ClassVar[str] = OBSERVED_GPU_DEVICE_SCHEMA

    def __post_init__(self) -> None:
        if self.vendor not in {"apple", "nvidia", "other"}:
            fail("ObservedGpuDevice.vendor", "must be apple, nvidia, or other")
        string_value(self.model, "ObservedGpuDevice.model", max_length=256)
        if self.index is not None:
            int_value(self.index, "ObservedGpuDevice.index", minimum=0)
        for value, path in (
            (self.memory_mib, "ObservedGpuDevice.memory_mib"),
            (self.core_count, "ObservedGpuDevice.core_count"),
        ):
            _optional_positive_int(value, path)
        for value, path in (
            (self.uuid, "ObservedGpuDevice.uuid"),
            (self.compute_capability, "ObservedGpuDevice.compute_capability"),
            (self.driver_version, "ObservedGpuDevice.driver_version"),
        ):
            if value is not None:
                string_value(value, path, max_length=256)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "vendor": self.vendor,
            "model": self.model,
            "index": self.index,
            "uuid": self.uuid,
            "memory_mib": self.memory_mib,
            "compute_capability": self.compute_capability,
            "driver_version": self.driver_version,
            "core_count": self.core_count,
        }

    @property
    def capabilities(self) -> tuple[str, ...]:
        values: set[str] = set()
        if self.vendor == "nvidia":
            values.add("cuda")
        if self.compute_capability is not None:
            values.add(f"compute-capability-{self.compute_capability}")
            values.add(f"sm-{self.compute_capability.replace('.', '')}")
        return tuple(sorted(values))

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ObservedGpuDevice"
    ) -> ObservedGpuDevice:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "vendor",
                    "model",
                    "index",
                    "uuid",
                    "memory_mib",
                    "compute_capability",
                    "driver_version",
                    "core_count",
                }
            ),
        )
        return cls(
            string_value(data["vendor"], f"{path}.vendor"),
            string_value(data["model"], f"{path}.model", max_length=256),
            None
            if data["index"] is None
            else int_value(data["index"], f"{path}.index", minimum=0),
            optional_string(data["uuid"], f"{path}.uuid"),
            _optional_positive_int(data["memory_mib"], f"{path}.memory_mib"),
            optional_string(data["compute_capability"], f"{path}.compute_capability"),
            optional_string(data["driver_version"], f"{path}.driver_version"),
            _optional_positive_int(data["core_count"], f"{path}.core_count"),
        )


@dataclass(frozen=True, slots=True)
class WorkerHardwareObservation:
    worker_id: str
    observed_at: str
    os_family: str
    os_name: str
    os_version: str
    cpu_architecture: str
    physical_cpu_cores: int
    logical_cpu_cores: int
    memory_mib: int
    gpus: tuple[ObservedGpuDevice, ...]
    nvidia_status: NvidiaProbeStatus
    diagnostic: str | None = None

    SCHEMA: ClassVar[str] = WORKER_HARDWARE_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.worker_id, "WorkerHardwareObservation.worker_id")
        try:
            parsed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError:
            fail(
                "WorkerHardwareObservation.observed_at", "must be an RFC 3339 timestamp"
            )
        if parsed.tzinfo is None:
            fail("WorkerHardwareObservation.observed_at", "must include a timezone")
        if self.os_family not in _OS_FAMILIES:
            fail(
                "WorkerHardwareObservation.os_family",
                "must be linux, macos, or windows",
            )
        for value, path in (
            (self.os_name, "WorkerHardwareObservation.os_name"),
            (self.os_version, "WorkerHardwareObservation.os_version"),
            (self.cpu_architecture, "WorkerHardwareObservation.cpu_architecture"),
        ):
            string_value(value, path, max_length=256)
        for value, path in (
            (self.physical_cpu_cores, "WorkerHardwareObservation.physical_cpu_cores"),
            (self.logical_cpu_cores, "WorkerHardwareObservation.logical_cpu_cores"),
            (self.memory_mib, "WorkerHardwareObservation.memory_mib"),
        ):
            int_value(value, path, minimum=1)
        if self.physical_cpu_cores > self.logical_cpu_cores:
            fail(
                "WorkerHardwareObservation",
                "physical cores cannot exceed logical cores",
            )
        if not isinstance(self.nvidia_status, NvidiaProbeStatus):
            fail("WorkerHardwareObservation.nvidia_status", "must be typed")
        if (
            self.os_family == "macos"
            and self.nvidia_status is not NvidiaProbeStatus.NOT_APPLICABLE
        ):
            fail(
                "WorkerHardwareObservation.nvidia_status",
                "must be not-applicable on macOS",
            )
        nvidia_devices = tuple(item for item in self.gpus if item.vendor == "nvidia")
        if self.nvidia_status is NvidiaProbeStatus.OK and not nvidia_devices:
            fail(
                "WorkerHardwareObservation.gpus", "an ok NVIDIA probe requires a device"
            )
        if self.nvidia_status is not NvidiaProbeStatus.OK and nvidia_devices:
            fail("WorkerHardwareObservation.gpus", "NVIDIA devices require an ok probe")
        indexes = tuple(item.index for item in self.gpus if item.index is not None)
        if indexes != tuple(sorted(set(indexes))):
            fail(
                "WorkerHardwareObservation.gpus",
                "device indexes must be uniquely sorted",
            )
        if self.diagnostic is not None:
            string_value(
                self.diagnostic, "WorkerHardwareObservation.diagnostic", max_length=2048
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "worker_id": self.worker_id,
            "observed_at": self.observed_at,
            "os_family": self.os_family,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "cpu_architecture": self.cpu_architecture,
            "physical_cpu_cores": self.physical_cpu_cores,
            "logical_cpu_cores": self.logical_cpu_cores,
            "memory_mib": self.memory_mib,
            "gpus": [item.to_dict() for item in self.gpus],
            "nvidia_status": self.nvidia_status.value,
            "diagnostic": self.diagnostic,
        }

    def satisfies(self, requirements: ExecutionRequirements) -> bool:
        """Match authored minimums without selecting, ranking, or provisioning."""

        if (
            requirements.os_family is not None
            and self.os_family != requirements.os_family
        ):
            return False
        if (
            requirements.os_version is not None
            and self.os_version != requirements.os_version
        ):
            return False
        if (
            requirements.cpu_architecture is not None
            and self.cpu_architecture.casefold()
            != requirements.cpu_architecture.casefold()
        ):
            return False
        if (
            requirements.minimum_cpu_cores is not None
            and self.logical_cpu_cores < requirements.minimum_cpu_cores
        ):
            return False
        if (
            requirements.minimum_memory_mib is not None
            and self.memory_mib < requirements.minimum_memory_mib
        ):
            return False
        gpu = requirements.gpu
        if not gpu.constrained:
            return True
        required_capabilities = set(gpu.capabilities or ())
        matching = tuple(
            device
            for device in self.gpus
            if (gpu.vendor is None or device.vendor.casefold() == gpu.vendor.casefold())
            and (gpu.model is None or device.model.casefold() == gpu.model.casefold())
            and (
                gpu.minimum_memory_mib is None
                or (
                    device.memory_mib is not None
                    and device.memory_mib >= gpu.minimum_memory_mib
                )
            )
            and required_capabilities.issubset(device.capabilities)
        )
        return len(matching) >= (gpu.minimum_count or 1)

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "WorkerHardwareObservation"
    ) -> WorkerHardwareObservation:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "worker_id",
                    "observed_at",
                    "os_family",
                    "os_name",
                    "os_version",
                    "cpu_architecture",
                    "physical_cpu_cores",
                    "logical_cpu_cores",
                    "memory_mib",
                    "gpus",
                    "nvidia_status",
                    "diagnostic",
                }
            ),
        )
        return cls(
            _identifier(data["worker_id"], f"{path}.worker_id"),
            string_value(data["observed_at"], f"{path}.observed_at"),
            string_value(data["os_family"], f"{path}.os_family"),
            string_value(data["os_name"], f"{path}.os_name", max_length=256),
            string_value(data["os_version"], f"{path}.os_version", max_length=256),
            string_value(
                data["cpu_architecture"], f"{path}.cpu_architecture", max_length=256
            ),
            int_value(
                data["physical_cpu_cores"], f"{path}.physical_cpu_cores", minimum=1
            ),
            int_value(
                data["logical_cpu_cores"], f"{path}.logical_cpu_cores", minimum=1
            ),
            int_value(data["memory_mib"], f"{path}.memory_mib", minimum=1),
            tuple(
                ObservedGpuDevice.from_dict(item, path=f"{path}.gpus[{index}]")
                for index, item in enumerate(list_value(data["gpus"], f"{path}.gpus"))
            ),
            enum_value(
                NvidiaProbeStatus, data["nvidia_status"], f"{path}.nvidia_status"
            ),
            optional_string(data["diagnostic"], f"{path}.diagnostic"),
        )


@dataclass(frozen=True, slots=True)
class WorkerHardwareObservationCatalog:
    workers: tuple[WorkerHardwareObservation, ...]

    SCHEMA: ClassVar[str] = WORKER_HARDWARE_CATALOG_SCHEMA

    def __post_init__(self) -> None:
        ids = tuple(item.worker_id for item in self.workers)
        if ids != tuple(sorted(set(ids))):
            fail("WorkerHardwareObservationCatalog.workers", "must be uniquely sorted")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def worker(self, worker_id: str) -> WorkerHardwareObservation:
        selected = _identifier(worker_id, "WorkerHardwareObservationCatalog.worker_id")
        result = next(
            (item for item in self.workers if item.worker_id == selected), None
        )
        if result is None:
            fail(
                "WorkerHardwareObservationCatalog.worker_id",
                f"worker {selected!r} has no current observation",
            )
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "workers": [item.to_dict() for item in self.workers],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "WorkerHardwareObservationCatalog"
    ) -> WorkerHardwareObservationCatalog:
        data = contract_fields(
            value, path=path, schema_uri=cls.SCHEMA, required=frozenset({"workers"})
        )
        return cls(
            tuple(
                WorkerHardwareObservation.from_dict(
                    item, path=f"{path}.workers[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["workers"], f"{path}.workers")
                )
            )
        )


__all__ = [
    "NvidiaProbeStatus",
    "OBSERVED_GPU_DEVICE_SCHEMA",
    "ObservedGpuDevice",
    "WORKER_HARDWARE_CATALOG_SCHEMA",
    "WORKER_HARDWARE_OBSERVATION_SCHEMA",
    "WorkerHardwareObservation",
    "WorkerHardwareObservationCatalog",
]

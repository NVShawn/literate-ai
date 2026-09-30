"""Deterministic NVIDIA compatibility selection from retained exact authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from literate_ai.contracts import (
    NvidiaProbeStatus,
    WorkerHardwareObservation,
    canonical_identity,
)

NVIDIA_COMPATIBILITY_SCHEMA = "literate-ai/nvidia-stack-compatibility@1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NvidiaStackError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NvidiaStackSelection:
    candidate: dict[str, object]
    observation_identity: str
    compatibility_identity: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/nvidia-stack-selection@1",
            "observation_identity": self.observation_identity,
            "compatibility_identity": self.compatibility_identity,
            "candidate": self.candidate,
        }


def resolve_nvidia_stack(
    observation: WorkerHardwareObservation,
    compatibility: Any,
    *,
    python_abi: str,
    toolkit_pin: str | None = None,
    package_pins: tuple[str, ...] = (),
) -> NvidiaStackSelection:
    authority = _compatibility_document(compatibility)
    if (
        observation.os_family not in {"linux", "windows"}
        or observation.nvidia_status is not NvidiaProbeStatus.OK
    ):
        raise NvidiaStackError(
            "nvidia_stack.worker_ineligible",
            "selection requires a healthy Linux or Windows NVIDIA observation",
        )
    devices = tuple(item for item in observation.gpus if item.vendor == "nvidia")
    if any(
        not item.compute_capability or not item.driver_version or not item.uuid
        for item in devices
    ):
        raise NvidiaStackError(
            "nvidia_stack.observation_incomplete",
            "NVIDIA devices require UUID, driver, and compute capability",
        )
    requested_packages = _package_pins(package_pins)
    eligible = []
    for candidate in authority["candidates"]:
        if observation.os_family not in candidate["os_families"]:
            continue
        if observation.cpu_architecture not in candidate["cpu_architectures"]:
            continue
        if python_abi not in candidate["python_abis"]:
            continue
        toolkit = candidate["toolkit_version"]
        if toolkit_pin is not None and toolkit != toolkit_pin:
            continue
        capabilities = set(candidate["compute_capabilities"])
        if any(item.compute_capability not in capabilities for item in devices):
            continue
        minimum = candidate["minimum_driver_versions"].get(observation.os_family)
        if minimum is None or any(
            _version(item.driver_version) < _version(minimum) for item in devices
        ):
            continue
        packages = {item["name"]: item["version"] for item in candidate["packages"]}
        if any(packages.get(name) != version for name, version in requested_packages):
            continue
        eligible.append(candidate)
    if not eligible:
        raise NvidiaStackError(
            "nvidia_stack.no_compatible_candidate",
            "retained compatibility authority contains no compatible exact stack",
        )
    eligible.sort(key=lambda item: (_version(item["toolkit_version"]), item["id"]))
    selected = eligible[0]
    return NvidiaStackSelection(
        selected,
        observation.identity.uri,
        canonical_identity(authority).uri,
    )


def _compatibility_document(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema") != NVIDIA_COMPATIBILITY_SCHEMA
    ):
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "compatibility schema is invalid"
        )
    if set(value) != {"schema", "retrieved_at", "source_urls", "candidates"}:
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "compatibility fields are invalid"
        )
    if (
        not isinstance(value["retrieved_at"], str)
        or not value["retrieved_at"]
        or not _strings(value["source_urls"])
        or tuple(value["source_urls"]) != tuple(sorted(set(value["source_urls"])))
        or not isinstance(value["candidates"], list)
        or not value["candidates"]
    ):
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid",
            "compatibility authority is incomplete",
        )
    for candidate in value["candidates"]:
        _candidate(candidate)
    ids = tuple(item["id"] for item in value["candidates"])
    if len(ids) != len(set(ids)):
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "candidate IDs must be unique"
        )
    return value


def _candidate(value: Any) -> None:
    fields = {
        "id",
        "os_families",
        "cpu_architectures",
        "python_abis",
        "toolkit_version",
        "minimum_driver_versions",
        "compute_capabilities",
        "compiler_targets",
        "packages",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "candidate fields are invalid"
        )
    if (
        not isinstance(value["id"], str)
        or not value["id"]
        or not _strings(value["os_families"])
        or not set(value["os_families"]) <= {"linux", "windows"}
        or not _strings(value["cpu_architectures"])
        or not _strings(value["python_abis"])
        or not _strings(value["compute_capabilities"])
        or not _strings(value["compiler_targets"])
    ):
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "candidate selectors are invalid"
        )
    _version(value["toolkit_version"])
    drivers = value["minimum_driver_versions"]
    if not isinstance(drivers, dict) or not drivers:
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "driver floors are required"
        )
    for family, version in drivers.items():
        if family not in {"linux", "windows"}:
            raise NvidiaStackError(
                "nvidia_stack.compatibility_invalid", "driver OS is invalid"
            )
        _version(version)
    if not isinstance(value["packages"], list) or not value["packages"]:
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "exact packages are required"
        )
    names = []
    for package in value["packages"]:
        if (
            not isinstance(package, dict)
            or set(package) != {"name", "version", "sha256"}
            or not isinstance(package["name"], str)
            or not package["name"]
            or not isinstance(package["version"], str)
            or not package["version"]
            or not isinstance(package["sha256"], str)
            or _SHA256.fullmatch(package["sha256"]) is None
        ):
            raise NvidiaStackError(
                "nvidia_stack.compatibility_invalid", "package pin is invalid"
            )
        names.append(package["name"])
    if len(names) != len(set(names)):
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "package names must be unique"
        )


def _strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item) for item in value)
    )


def _version(value: Any) -> Version:
    try:
        return Version(value)
    except (InvalidVersion, TypeError) as exc:
        raise NvidiaStackError(
            "nvidia_stack.compatibility_invalid", "compatibility version is invalid"
        ) from exc


def _package_pins(values: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    parsed = []
    for value in values:
        name, separator, version = value.partition("==")
        if not separator or not name or not version:
            raise NvidiaStackError(
                "nvidia_stack.package_pin_invalid", "package pins must be NAME==VERSION"
            )
        parsed.append((name, version))
    if len({name for name, _version_value in parsed}) != len(parsed):
        raise NvidiaStackError(
            "nvidia_stack.package_pin_invalid", "package pins must be unique"
        )
    return tuple(parsed)


__all__ = [
    "NVIDIA_COMPATIBILITY_SCHEMA",
    "NvidiaStackError",
    "NvidiaStackSelection",
    "resolve_nvidia_stack",
]

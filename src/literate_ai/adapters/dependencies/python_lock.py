"""Inert, target-specific Python wheel-lock validation.

This module does not install packages or authorize generated execution. A caller
must bind the target observation and wheel bytes to its owned build inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
import zlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
from typing import BinaryIO

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from .python_archive import WheelPayload, inspect_wheel_payload
from .types import DependencyObservationError

SCHEMA = "literate-ai/python-wheel-lock@1"
_MAX_PACKAGES = 4096
_MAX_METADATA = 4 * 1024 * 1024
_MAX_WHEEL_BYTES = 8 * 1024 * 1024 * 1024
_ENVIRONMENT_KEYS = frozenset(default_environment())


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-lock-invalid", message)


def _fields(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("Python lock fields are incomplete or unknown")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_PACKAGES
        or any(
            not isinstance(item, str) or not item or len(item) > 4096 for item in value
        )
        or value != sorted(set(value))
    ):
        _fail("Python lock string collections must be bounded, sorted and unique")
    return tuple(value)


def _requirement(raw: str) -> Requirement:
    try:
        requirement = Requirement(raw)
    except InvalidRequirement as exc:
        raise DependencyObservationError(
            "dependencies.python-lock-invalid", "Invalid locked Python requirement"
        ) from exc
    if requirement.url is not None:
        _fail("Direct URL requirements are not supported by a wheel lock")
    return requirement


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate JSON member in Python lock")
        result[key] = value
    return result


@dataclass(frozen=True)
class LockedPythonWheel:
    name: str
    version: str
    filename: str
    sha256: str
    requires_python: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class PythonWheelLock:
    environment: tuple[tuple[str, str], ...]
    tags: tuple[str, ...]
    requirements: tuple[str, ...]
    packages: tuple[LockedPythonWheel, ...]
    edges: tuple[tuple[str, str], ...]

    def require_target(
        self, environment: Mapping[str, str], tags: tuple[str, ...]
    ) -> None:
        """Require independently observed target facts, never lock-provided facts."""
        if dict(self.environment) != dict(environment) or set(self.tags) != set(tags):
            raise DependencyObservationError(
                "dependencies.python-lock-target-mismatch",
                "Python wheel lock belongs to another interpreter or target",
            )


def parse_python_wheel_lock(content: str) -> PythonWheelLock:
    if not isinstance(content, str) or len(content.encode("utf-8")) > 16 * 1024 * 1024:
        _fail("Python lock exceeds the bounded document size")
    try:
        raw = json.loads(content, object_pairs_hook=_strict_object)
    except (ValueError, RecursionError) as exc:
        raise DependencyObservationError(
            "dependencies.python-lock-invalid", "Python lock is not valid bounded JSON"
        ) from exc
    document = _fields(
        raw, {"schema", "environment", "tags", "requirements", "packages"}
    )
    if document["schema"] != SCHEMA:
        _fail("Unsupported Python wheel lock schema")
    environment = _fields(document["environment"], set(_ENVIRONMENT_KEYS))
    if any(
        not isinstance(value, str) or not value or len(value) > 512
        for value in environment.values()
    ):
        _fail("Python lock target fields must be nonempty bounded strings")
    try:
        python_version = Version(environment["python_full_version"])
    except InvalidVersion as exc:
        raise DependencyObservationError(
            "dependencies.python-lock-invalid", "Invalid target Python version"
        ) from exc
    if environment["python_version"] != ".".join(map(str, python_version.release[:2])):
        _fail("Python target version fields disagree")
    tags = _strings(document["tags"])
    if not tags or any(
        not re.fullmatch(r"[A-Za-z0-9_]+-[A-Za-z0-9_]+-[A-Za-z0-9_]+", tag)
        for tag in tags
    ):
        _fail("Python lock requires exact expanded target wheel tags")
    requirements = _strings(document["requirements"])
    records = document["packages"]
    if not isinstance(records, list) or len(records) > _MAX_PACKAGES:
        _fail("Python lock package inventory exceeds its bound")
    packages: dict[str, LockedPythonWheel] = {}
    for record in records:
        record = _fields(
            record,
            {
                "name",
                "version",
                "filename",
                "sha256",
                "requires_python",
                "requires_dist",
            },
        )
        name, version, filename = record["name"], record["version"], record["filename"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or name in packages
        ):
            _fail("Python lock package names must be normalized and unique")
        if (
            not isinstance(filename, str)
            or "/" in filename
            or "\\" in filename
            or len(filename) > 255
        ):
            _fail("Python lock wheel filename is not a portable basename")
        try:
            wheel_name, wheel_version, _build, wheel_tags = parse_wheel_filename(
                filename
            )
            declared_version = Version(version) if isinstance(version, str) else None
        except (InvalidWheelFilename, InvalidVersion) as exc:
            raise DependencyObservationError(
                "dependencies.python-lock-invalid", "Invalid wheel filename or version"
            ) from exc
        if wheel_name != name or wheel_version != declared_version:
            _fail("Python wheel filename disagrees with package identity")
        if not set(map(str, wheel_tags)).intersection(tags):
            _fail("Python wheel is incompatible with the declared target")
        digest = record["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("Python wheel requires one exact SHA-256 digest")
        requires_python = record["requires_python"]
        if not isinstance(requires_python, str) or len(requires_python) > 4096:
            _fail("Invalid Requires-Python declaration")
        try:
            if not SpecifierSet(requires_python).contains(
                python_version, prereleases=True
            ):
                _fail("Python wheel excludes the declared interpreter version")
        except InvalidSpecifier as exc:
            raise DependencyObservationError(
                "dependencies.python-lock-invalid", "Invalid Requires-Python specifier"
            ) from exc
        requires_dist = _strings(record["requires_dist"])
        for requirement in requires_dist:
            _requirement(requirement)
        packages[name] = LockedPythonWheel(
            name, version, filename, digest, requires_python, requires_dist
        )
    if list(packages) != sorted(packages):
        _fail("Python wheel package inventory must be canonically ordered")

    # Recompute the complete selected graph from metadata. Extras can activate
    # new edges after a package was first reached, including through cycles.
    pending = deque(("@root", _requirement(raw)) for raw in requirements)
    extras: dict[str, set[str]] = {}
    processed: set[tuple[str, tuple[str, ...]]] = set()
    reached: set[str] = set()
    edges: set[tuple[str, str]] = set()
    while pending:
        parent, requirement = pending.popleft()
        contexts = {"", *extras.get(parent, set())}
        if requirement.marker and not any(
            requirement.marker.evaluate({**environment, "extra": extra})
            for extra in contexts
        ):
            continue
        name = str(canonicalize_name(requirement.name))
        package = packages.get(name)
        if package is None or not requirement.specifier.contains(
            package.version, prereleases=True
        ):
            _fail(
                "Python lock omits a selected dependency or selects an "
                "incompatible version"
            )
        edges.add((parent, name))
        reached.add(name)
        active = extras.setdefault(name, set())
        active.update(requirement.extras)
        state = (name, tuple(sorted(active)))
        if state in processed:
            continue
        processed.add(state)
        if len(processed) > _MAX_PACKAGES * 64:
            _fail("Python extras activation exceeds its bounded graph state")
        pending.extend((name, _requirement(raw)) for raw in package.requires_dist)
    if reached != set(packages):
        _fail("Python lock contains packages outside the selected closure")
    return PythonWheelLock(
        tuple(sorted(environment.items())),
        tags,
        requirements,
        tuple(packages.values()),
        tuple(sorted(edges)),
    )


def verify_locked_wheel(
    package: LockedPythonWheel, stream: BinaryIO
) -> tuple[WheelPayload, ...]:
    """Verify owned immutable wheel bytes and their dependency metadata.

    The caller owns stream custody and must not reuse this proof for another
    stream or grant installation from a mutable path based on this result alone.
    No wheel member is extracted or executed.
    """
    try:
        stream.seek(0)
        digest = hashlib.sha256()
        size = 0
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > _MAX_WHEEL_BYTES:
                _fail("Wheel archive exceeds its byte limit")
            digest.update(block)
        if digest.hexdigest() != package.sha256:
            raise DependencyObservationError(
                "dependencies.python-wheel-hash-mismatch",
                "Wheel bytes differ from the exact lock",
            )
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 100_000 or len(names) != len(set(names)):
                _fail("Wheel member inventory is oversized or ambiguous")
            metadata = [
                entry
                for entry in entries
                if entry.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata) != 1 or metadata[0].file_size > _MAX_METADATA:
                _fail("Wheel must contain exactly one bounded metadata record")
            directory, _, _ = metadata[0].filename.partition("/")
            identity = directory.removesuffix(".dist-info").rsplit("-", 1)
            if (
                metadata[0].filename != directory + "/METADATA"
                or len(identity) != 2
                or str(canonicalize_name(identity[0])) != package.name
                or identity[1] != package.version
            ):
                _fail("Wheel metadata directory disagrees with package identity")
            with archive.open(metadata[0]) as member:
                data = member.read(_MAX_METADATA + 1)
            if len(data) > _MAX_METADATA:
                _fail("Wheel metadata exceeds its bound")
            payload = inspect_wheel_payload(
                archive, directory=directory, filename=package.filename
            )
    except DependencyObservationError:
        raise
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise DependencyObservationError(
            "dependencies.python-wheel-invalid",
            "Wheel archive is unavailable or invalid",
        ) from exc
    metadata = BytesParser(policy=compat32).parsebytes(data)
    if metadata.defects or any(
        len(metadata.get_all(field, [])) > 1
        for field in ("Name", "Version", "Requires-Python")
    ):
        _fail("Wheel metadata is malformed or ambiguous")
    actual = (
        str(canonicalize_name(metadata.get("Name", ""))),
        metadata.get("Version"),
        metadata.get("Requires-Python", ""),
        tuple(sorted(metadata.get_all("Requires-Dist", []))),
    )
    expected = (
        package.name,
        package.version,
        package.requires_python,
        package.requires_dist,
    )
    if actual != expected:
        raise DependencyObservationError(
            "dependencies.python-wheel-metadata-mismatch",
            "Wheel metadata differs from the locked graph",
        )
    return payload


def verify_python_wheel_lock(
    lock: PythonWheelLock,
    wheels: Mapping[str, BinaryIO],
    *,
    environment: Mapping[str, str],
    tags: tuple[str, ...],
) -> None:
    """Verify the entire selected wheel closure against observed target facts.

    This verifies archives, not an installed environment or execution custody.
    Lifecycle adapters must retain the verified streams as owned immutable build
    inputs and independently check the resulting installed closure.
    """
    lock.require_target(environment, tags)
    if set(wheels) != {package.name for package in lock.packages}:
        raise DependencyObservationError(
            "dependencies.python-wheel-inventory-mismatch",
            "Acquired wheels do not exactly cover the selected dependency graph",
        )
    for package in lock.packages:
        verify_locked_wheel(package, wheels[package.name])

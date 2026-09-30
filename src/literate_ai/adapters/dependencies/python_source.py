"""Bind inert Python wheel-lock intent to its declared source manifest.

This is a pre-acquisition check, not permission to install or execute packages.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from .python_lock import PythonWheelLock, _requirement, parse_python_wheel_lock
from .types import DependencyObservationError

_MAX_MANIFEST_BYTES = 1024 * 1024
_AUTHORITIES = {
    "requirements.txt",
    "requirements.in",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pylock.toml",
    "python-wheel-lock.json",
    "pip.conf",
    "pip.ini",
}
_AUTHORITY_NAMES = frozenset(name.casefold() for name in _AUTHORITIES)


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-source-invalid", message)


def _path(value: str, names: set[str]) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _fail("Python authority path must be a portable relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or path.name not in names
    ):
        _fail("Python authority path must be canonical and supported")
    return path


def _key(raw: str) -> tuple:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        _fail("Python root requirement must be a bounded string")
    requirement = _requirement(raw)
    return (
        str(canonicalize_name(requirement.name)),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        str(requirement.specifier),
        str(requirement.marker) if requirement.marker else "",
    )


def _keys(requirements: list[str] | tuple[str, ...]) -> tuple:
    if len(requirements) > 4096:
        _fail("Python manifest root inventory exceeds its bound")
    keys = [_key(raw) for raw in requirements]
    if len(keys) != len(set(keys)):
        _fail("Python root requirements contain duplicate declarations")
    return tuple(sorted(keys))


def _requirements(content: str, name: str, lock: PythonWheelLock) -> list[str]:
    if name != "pyproject.toml":
        result = []
        for raw in content.splitlines():
            line = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-") or "\\" in line:
                _fail(
                    "Pip options, includes, editables and continuations are unsupported"
                )
            result.append(line)
        return result
    try:
        document = tomllib.loads(content)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise DependencyObservationError(
            "dependencies.python-source-invalid", "Invalid Python project manifest"
        ) from exc
    if "build-system" in document:
        _fail("Source-build dependencies require separate build authority")
    tool = document.get("tool", {})
    if not isinstance(tool, dict) or any(
        key in tool for key in ("poetry", "uv", "pdm")
    ):
        _fail("Alternate Python dependency authorities are unsupported")
    project = document.get("project")
    if not isinstance(project, dict):
        _fail("Python project manifest requires a project table")
    dynamic = project.get("dynamic", [])
    if (
        not isinstance(dynamic, list)
        or any(not isinstance(item, str) for item in dynamic)
        or {"dependencies", "optional-dependencies", "requires-python"}.intersection(
            dynamic
        )
        or project.get("optional-dependencies", {}) != {}
        or "dependency-groups" in document
    ):
        _fail("Dynamic, optional and grouped root dependencies are unsupported")
    requires_python = project.get("requires-python", "")
    if not isinstance(requires_python, str):
        _fail("Project Requires-Python must be a string")
    try:
        if not SpecifierSet(requires_python).contains(
            dict(lock.environment)["python_full_version"], prereleases=True
        ):
            _fail("Project excludes the locked Python interpreter")
    except InvalidSpecifier as exc:
        raise DependencyObservationError(
            "dependencies.python-source-invalid", "Invalid project Requires-Python"
        ) from exc
    requirements = project.get("dependencies", [])
    if not isinstance(requirements, list):
        _fail("Python project dependencies must be an array")
    return requirements


@dataclass(frozen=True)
class PythonSourceAuthority:
    """Exact manifest and lock bytes that agree on root intent; not wheel evidence."""

    manifest_path: str
    lock_path: str
    manifest_sha256: str
    lock_sha256: str
    lock: PythonWheelLock


def prepare_python_source_authority(
    files: Mapping[str, object],
    *,
    manifest_path: str,
    lock_path: str,
) -> PythonSourceAuthority:
    """Check one profile-selected manifest and lock without filesystem or execution.

    Callers must pass the complete accepted source inventory, not just two files.
    Multiple/nested package roots need independent lifecycle ownership and are
    rejected by this initial single-root profile, including empty extra manifests.
    """
    manifest = _path(
        manifest_path, {"requirements.txt", "requirements.in", "pyproject.toml"}
    )
    lock_file = _path(lock_path, {"python-wheel-lock.json"})
    if manifest.parent != lock_file.parent:
        _fail("Python manifest and lock must share one package root")
    for path in files:
        if not isinstance(path, str):
            _fail("Source inventory paths must be strings")
        name = PurePosixPath(path.replace("\\", "/")).name.casefold()
        is_authority = name in _AUTHORITY_NAMES or (
            name.startswith("pylock.") and name.endswith(".toml")
        )
        if is_authority and path not in {manifest_path, lock_path}:
            _fail("Source inventory contains an unselected Python dependency authority")
    content, lock_content = files.get(manifest_path), files.get(lock_path)
    if not isinstance(content, str) or not isinstance(lock_content, str):
        _fail("Python source authority requires text manifest and lock bytes")
    if len(content.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        _fail("Python manifest exceeds its byte limit")
    lock = parse_python_wheel_lock(lock_content)
    if _keys(_requirements(content, manifest.name, lock)) != _keys(lock.requirements):
        raise DependencyObservationError(
            "dependencies.python-source-lock-mismatch",
            "Python manifest requirements differ from the locked root intent",
        )
    return PythonSourceAuthority(
        manifest_path,
        lock_path,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        hashlib.sha256(lock_content.encode("utf-8")).hexdigest(),
        lock,
    )

"""Observe wheel compatibility in the selected interpreter, not the controller."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import packaging
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import (
    BuildError,
    PythonToolchain,
    controlled_python_environment,
)
from literate_ai.contracts import ContentIdentity, canonical_identity

from .python_lock import (
    SCHEMA,
    PythonWheelLock,
    _strict_object,
    parse_python_wheel_lock,
)
from .types import DependencyObservationError

_PROBE = """\
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from packaging.markers import default_environment
from packaging.tags import sys_tags
print(json.dumps({
    "schema": "literate-ai/python-wheel-target-probe@1",
    "runtime": os.path.realpath(sys.executable),
    "implementation": sys.implementation.name,
    "version_info": list(sys.version_info),
    "environment": default_environment(),
    "tags": sorted(set(str(tag) for tag in sys_tags())),
}, sort_keys=True, separators=(",", ":")))
"""
_MAX_HELPER_BYTES = 16 * 1024**2


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-target-invalid", message)


def _helper_sources() -> dict[str, bytes]:
    """Snapshot the pinned, flat pure-Python packaging runtime, not site-packages.

    The framework distribution binds packaging's version. Exact helper bytes are
    additionally retained in the probe identity; target packages are never loaded.
    """
    root = Path(packaging.__file__).parent
    files = {}
    total = 0
    for path in sorted(root.iterdir()):
        if path.name in {"__pycache__", "py.typed", "licenses"}:
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".py":
            _fail("Packaging probe helper has unsupported runtime contents")
        with path.open("rb") as stream:
            content = stream.read(_MAX_HELPER_BYTES + 1)
        total += len(content)
        if total > _MAX_HELPER_BYTES or len(files) >= 256:
            _fail("Packaging probe helper exceeds its bound")
        files[path.name] = content
    if not {"__init__.py", "markers.py", "tags.py"}.issubset(files):
        _fail("Packaging probe helper is incomplete")
    return files


@dataclass(frozen=True)
class PythonWheelTarget:
    toolchain_identity: str
    probe_identity: ContentIdentity
    environment: tuple[tuple[str, str], ...]
    tags: tuple[str, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/python-wheel-target@1",
                "toolchain_identity": self.toolchain_identity,
                "probe_identity": self.probe_identity.uri,
                "environment": dict(self.environment),
                "tags": list(self.tags),
            }
        )

    def require_lock(self, lock: PythonWheelLock) -> None:
        lock.require_target(dict(self.environment), self.tags)


def observe_python_wheel_target(
    toolchain: PythonToolchain,
    *,
    environment: Mapping[str, str] | None = None,
    temporary_root: Path | None = None,
) -> PythonWheelTarget:
    """Execute a bounded isolated probe under one already-selected toolchain.

    The result describes compatibility, not a package-manager installation or
    complete native runtime dependency closure. Both launcher and runtime are
    revalidated around the probe by the existing toolchain adapter.
    """
    if not isinstance(toolchain, PythonToolchain):
        raise TypeError("Wheel target observation requires a selected PythonToolchain")
    configured = controlled_python_environment(
        dict(os.environ if environment is None else environment)
    )
    try:
        toolchain.require_unchanged(configured)
        sources = _helper_sources()
        probe_identity = canonical_identity(
            {
                "driver": hashlib.sha256(_PROBE.encode()).hexdigest(),
                "packaging": [
                    [name, hashlib.sha256(content).hexdigest()]
                    for name, content in sorted(sources.items())
                ],
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="litai-pytarget-", dir=temporary_root
        ) as name:
            directory = Path(name)
            helper = directory / "packaging"
            helper.mkdir()
            for filename, content in sources.items():
                (helper / filename).write_bytes(content)
            result = run_bounded_process(
                (*toolchain.command, "-I", "-S", "-B", "-c", _PROBE, str(directory)),
                cwd=directory,
                environment=configured,
                timeout_seconds=15,
                stdout_limit_bytes=1024**2,
                stderr_limit_bytes=64 * 1024,
                error_prefix="dependencies.python_target",
            )
            # The helper should neither mutate itself nor create any hidden inputs.
            if (
                {path.name for path in helper.iterdir()} != set(sources)
                or any(
                    (helper / filename).is_symlink()
                    or (helper / filename).read_bytes() != content
                    for filename, content in sources.items()
                )
                or {path.name for path in directory.iterdir()} != {"packaging"}
            ):
                _fail("Packaging probe helper changed during execution")
        toolchain.require_unchanged(configured)
        if result.returncode != 0 or result.stderr:
            _fail("Selected interpreter did not complete the isolated wheel probe")
        raw = json.loads(
            result.stdout.decode("utf-8"), object_pairs_hook=_strict_object
        )
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "schema",
                "runtime",
                "implementation",
                "version_info",
                "environment",
                "tags",
            }
            or raw["schema"] != "literate-ai/python-wheel-target-probe@1"
        ):
            _fail("Selected interpreter returned an invalid wheel target record")
        if (raw["runtime"], raw["implementation"], raw["version_info"]) != (
            toolchain.runtime_executable,
            toolchain.implementation,
            list(toolchain.version_info),
        ):
            _fail("Wheel target probe does not match the selected toolchain")
        # Reuse the lock's strict environment and tag validation, with no packages.
        validated = parse_python_wheel_lock(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "environment": raw["environment"],
                    "tags": raw["tags"],
                    "requirements": [],
                    "packages": [],
                }
            )
        )
        return PythonWheelTarget(
            toolchain.identity, probe_identity, validated.environment, validated.tags
        )
    except (OSError, ValueError, UnicodeError, BuildError) as exc:
        raise DependencyObservationError(
            "dependencies.python-target-invalid",
            "Selected Python wheel target could not be observed",
        ) from exc

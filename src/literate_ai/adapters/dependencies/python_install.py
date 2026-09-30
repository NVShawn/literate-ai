"""Fixed offline wheel installation under an independently observed interpreter.

Lifecycle adapters must authorize the package-manager process before calling this
helper and retain its evidence with the artifact. It does not enable admission.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError, PythonToolchain
from literate_ai.contracts import ContentIdentity, canonical_identity

from .python_archive import _path
from .python_installed import (
    PythonInstallationObservation,
    PythonInstallerChanges,
    _snapshot,
    observe_python_installation,
)
from .python_lock import (
    SCHEMA,
    LockedPythonWheel,
    PythonWheelLock,
    _strict_object,
    parse_python_wheel_lock,
)
from .python_target import PythonWheelTarget, observe_python_wheel_target
from .python_wheelhouse import StagedPythonWheels, stage_python_wheels
from .types import DependencyObservationError

# Fixed profile: the digest is published by PyPI for this exact wheel.
PIP_INSTALLER = LockedPythonWheel(
    "pip",
    "26.2.1",
    "pip-26.2.1-py3-none-any.whl",
    "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e",
    ">=3.10",
    (),
)
_CALLABLE = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*"
    r"(?:\.[A-Za-z_]\w*)*(?:\s*\[[A-Za-z0-9_, -]+\])?"
)
_SCHEME = {
    "purelib": "site",
    "platlib": "site",
    "scripts": "scripts",
    "headers": "headers",
    "data": "data",
}


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-install-invalid", message)


def _requests(staged: StagedPythonWheels) -> list[dict]:
    requests = []
    for package in staged.lock.packages:
        wheel = staged.directory / package.filename
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        with zipfile.ZipFile(wheel) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(names) > 1:
                _fail("Wheel has ambiguous entry-point metadata")
            if names:
                if archive.getinfo(names[0]).file_size > 1024**2:
                    _fail("Wheel entry-point metadata exceeds its bound")
                parser.read_string(archive.read(names[0]).decode("utf-8"))
        groups = []
        for group in ("console_scripts", "gui_scripts"):
            values = dict(parser.items(group)) if parser.has_section(group) else {}
            if len(values) > 4096:
                _fail("Wheel entry-point count exceeds its bound")
            for name, value in values.items():
                _path(name)
                if "/" in name or not _CALLABLE.fullmatch(value):
                    _fail("Wheel entry point is not a portable name and callable")
            groups.append(values)
        if set(groups[0]) & set(groups[1]):
            _fail("Console and GUI entry points collide")
        requests.append(
            {
                "name": package.name,
                "wheel": str(wheel),
                "console": groups[0],
                "gui": groups[1],
            }
        )
    return requests


@dataclass(frozen=True)
class InstalledPythonWheels:
    directory: Path
    toolchain: PythonToolchain
    target: PythonWheelTarget
    installer_identity: ContentIdentity
    install_process_identity: ContentIdentity
    observation: PythonInstallationObservation
    package_schemes: Mapping[str, Mapping[str, str]]
    changes: Mapping[str, PythonInstallerChanges]
    staged: StagedPythonWheels
    environment: Mapping[str, str]

    def revalidate(self) -> None:
        target = observe_python_wheel_target(
            self.toolchain,
            environment=self.environment,
            temporary_root=self.directory.parent,
        )
        if target.identity != self.target.identity:
            _fail("Installed Python target changed after installation")
        observed = observe_python_installation(
            self.staged,
            self.directory,
            scheme=_SCHEME,
            package_schemes=self.package_schemes,
            changes=self.changes,
        )
        if observed != self.observation:
            _fail("Installed Python payload changed after installation")


@contextmanager
def install_python_wheels(
    toolchain: PythonToolchain,
    lock: PythonWheelLock,
    sources: Mapping[str, BinaryIO],
    *,
    installer_source: BinaryIO,
    temporary_root: Path | None = None,
) -> Iterator[InstalledPythonWheels]:
    """Install verified wheels into context-owned storage without a resolver.

    No network, ambient pip, target site-packages, dependency resolution, build
    scripts, source distributions or bytecode generation are used. The exact pip
    wheel API is intentionally version-pinned. Command wrappers are projected in
    a separate process before installation and checked byte-for-byte afterward.
    """
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment.update(
        {"PIP_CONFIG_FILE": os.devnull, "SOURCE_DATE_EPOCH": "315532800"}
    )
    target = observe_python_wheel_target(
        toolchain, environment=environment, temporary_root=temporary_root
    )
    target.require_lock(lock)
    installer_lock = parse_python_wheel_lock(
        json.dumps(
            {
                "schema": SCHEMA,
                "environment": dict(target.environment),
                "tags": list(target.tags),
                "requirements": ["pip==" + PIP_INSTALLER.version],
                "packages": [asdict(PIP_INSTALLER)],
            }
        )
    )
    try:
        with (
            stage_python_wheels(
                installer_lock,
                {"pip": installer_source},
                environment=dict(target.environment),
                tags=target.tags,
                temporary_root=temporary_root,
            ) as installer,
            stage_python_wheels(
                lock,
                sources,
                environment=dict(target.environment),
                tags=target.tags,
                temporary_root=temporary_root,
            ) as staged,
            tempfile.TemporaryDirectory(
                prefix="litai-pyinstall-", dir=temporary_root
            ) as temporary,
        ):
            directory = Path(temporary)
            driver = directory / "driver.py"
            driver_bytes = Path(__file__).with_name("python_pip_driver.py").read_bytes()
            driver.write_bytes(driver_bytes)
            installer_identity = canonical_identity(
                {
                    "profile": "literate-ai/pip-wheel-install@1",
                    "pip_wheel": PIP_INSTALLER.sha256,
                    "driver": hashlib.sha256(driver_bytes).hexdigest(),
                }
            )
            requests = _requests(staged)
            request_path = directory / "request.json"
            projection_root = directory / "projected"
            projection_root.mkdir()
            installed_root = directory / "installed"
            installed_root.mkdir()

            def run(mode: str, output: Path) -> tuple[dict, ContentIdentity]:
                request_path.write_text(
                    json.dumps(requests, sort_keys=True), encoding="utf-8"
                )
                request_bytes = request_path.read_bytes()
                toolchain.require_unchanged(environment)
                installer.revalidate()
                staged.revalidate()
                result = run_bounded_process(
                    (
                        *toolchain.command,
                        "-I",
                        "-S",
                        "-B",
                        str(driver),
                        mode,
                        str(installer.directory / PIP_INSTALLER.filename),
                        str(request_path),
                        str(output),
                    ),
                    cwd=directory,
                    environment=environment,
                    timeout_seconds=300,
                    stdout_limit_bytes=8 * 1024**2,
                    stderr_limit_bytes=1024**2,
                    error_prefix="dependencies.python_install",
                )
                toolchain.require_unchanged(environment)
                installer.revalidate()
                staged.revalidate()
                if (
                    driver.read_bytes() != driver_bytes
                    or request_path.read_bytes() != request_bytes
                ):
                    _fail("Python installer process input changed")
                if result.returncode != 0 or result.stderr:
                    _fail("Python installer process did not complete cleanly")
                raw = json.loads(
                    result.stdout.decode(), object_pairs_hook=_strict_object
                )
                if not isinstance(raw, dict) or set(raw) != {
                    p.name for p in lock.packages
                }:
                    _fail("Python installer returned a different package inventory")
                identity = canonical_identity(
                    {
                        "installer": installer_identity.uri,
                        "target": target.identity.uri,
                        "mode": mode,
                        "wheels": [[p.name, p.sha256] for p in lock.packages],
                        "result": raw,
                    }
                )
                return raw, identity

            projected, projection_identity = run("project", projection_root)
            schemes = {}
            changes = {}
            for request in requests:
                name = request["name"]
                raw = projected[name]
                if not isinstance(raw, dict) or set(raw) != {
                    "scheme",
                    "replaced",
                    "skipped",
                    "generated",
                }:
                    _fail("Python installer projection is malformed")
                if (
                    raw["scheme"] != {**_SCHEME, "headers": "headers/" + name}
                    or not isinstance(raw["replaced"], dict)
                    or any(
                        not isinstance(k, str) or not isinstance(v, str)
                        for k, v in raw["replaced"].items()
                    )
                    or any(
                        not isinstance(raw[key], list)
                        or any(not isinstance(path, str) for path in raw[key])
                        for key in ("skipped", "generated")
                    )
                ):
                    _fail("Python installer projection has unsupported fields")
                files = {item.path: item for item in _snapshot(projection_root / name)}
                expected_files = set(raw["replaced"].values()) | set(raw["generated"])
                if set(files) != expected_files:
                    _fail("Python installer projection has unexpected files")
                schemes[name] = raw["scheme"]
                changes[name] = PythonInstallerChanges(
                    canonical_identity(
                        {
                            "process": projection_identity.uri,
                            "package": name,
                            "files": [
                                [p.path, p.size, p.sha256] for p in files.values()
                            ],
                        }
                    ),
                    tuple(
                        (source, files[path])
                        for source, path in sorted(raw["replaced"].items())
                    ),
                    tuple(files[path] for path in raw["generated"]),
                    tuple(raw["skipped"]),
                )
                request["generated"] = raw["generated"]
            installed, process_identity = run("install", installed_root)
            if installed != projected:
                _fail("Python installer output plan differs from its projection")
            observation = observe_python_installation(
                staged,
                installed_root,
                scheme=_SCHEME,
                package_schemes=schemes,
                changes=changes,
            )
            installed_wheels = InstalledPythonWheels(
                installed_root,
                toolchain,
                target,
                installer_identity,
                process_identity,
                observation,
                schemes,
                changes,
                staged,
                environment,
            )
            yield installed_wheels
    except (
        OSError,
        ValueError,
        configparser.Error,
        zipfile.BadZipFile,
        BuildError,
    ) as exc:
        raise DependencyObservationError(
            "dependencies.python-install-invalid",
            "Controlled Python installation failed",
        ) from exc

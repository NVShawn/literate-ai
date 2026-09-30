"""Own and revalidate staged wheel bytes; no resolution, install or execution."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .python_archive import WheelPayload
from .python_lock import PythonWheelLock, verify_locked_wheel
from .types import DependencyObservationError

_MAX_WHEEL = 8 * 1024**3
_MAX_BUNDLE = 64 * 1024**3


def _fail(message: str) -> None:
    raise DependencyObservationError(
        "dependencies.python-wheel-custody-invalid", message
    )


def _regular(path: Path) -> os.stat_result:
    observed = path.lstat()
    # Windows junctions and other reparse points must not escape the staging root.
    if (
        not stat.S_ISREG(observed.st_mode)
        or getattr(observed, "st_file_attributes", 0) & 0x400
    ):
        _fail("Wheel staging member is not an owned regular file")
    if observed.st_nlink != 1:
        _fail("Wheel staging member has an external hard-link alias")
    return observed


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


@dataclass(frozen=True)
class StagedPythonWheels:
    """Context-lifetime evidence, not a reusable permission to install by path.

    The lifecycle must keep this directory private and revalidate at each phase
    boundary. These checks do not protect against a malicious same-user process
    changing files concurrently with an external installer.
    """

    directory: Path
    lock: PythonWheelLock
    payloads: tuple[tuple[str, tuple[WheelPayload, ...]], ...]
    directory_identity: tuple[int, int]

    def revalidate(self) -> None:
        try:
            observed = self.directory.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or getattr(observed, "st_file_attributes", 0) & 0x400
                or _identity(observed) != self.directory_identity
            ):
                _fail("Wheel staging directory custody changed")
            if {path.name for path in self.directory.iterdir()} != {
                package.filename for package in self.lock.packages
            }:
                _fail("Wheel staging inventory changed")
            payloads = []
            for package in self.lock.packages:
                path = self.directory / package.filename
                before = _regular(path)
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                fd = os.open(path, flags)
                with os.fdopen(fd, "rb") as stream:
                    if _identity(os.fstat(stream.fileno())) != _identity(before):
                        _fail("Wheel staging member changed while opening")
                    payloads.append(
                        (package.name, verify_locked_wheel(package, stream))
                    )
                    after = _regular(path)
                    if _identity(after) != _identity(before) or (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                        _fail("Wheel staging member changed during verification")
            if tuple(payloads) != self.payloads:
                _fail("Wheel staging payload evidence changed")
        except (OSError, ValueError) as exc:
            raise DependencyObservationError(
                "dependencies.python-wheel-custody-invalid",
                "Wheel staging bytes are unavailable",
            ) from exc


@contextmanager
def stage_python_wheels(
    lock: PythonWheelLock,
    sources: Mapping[str, BinaryIO],
    *,
    environment: Mapping[str, str],
    tags: tuple[str, ...],
    temporary_root: Path | None = None,
) -> Iterator[StagedPythonWheels]:
    """Copy exact inputs to a private temporary wheelhouse, verify, then yield.

    Caller streams remain open and may be changed after copying without changing
    the admitted snapshot. All temporary copies are removed on context exit,
    including partial-copy and verification failures. No source URL, package
    index, installer identity or installed closure is attested by this operation.
    """
    lock.require_target(environment, tags)
    if set(sources) != {package.name for package in lock.packages}:
        _fail("Wheel input inventory does not match the selected closure")
    names = [package.filename for package in lock.packages]
    if len(set(name.casefold() for name in names)) != len(names) or any(
        not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or ":" in name
        for name in names
    ):
        _fail("Wheel staging filenames must be unique portable basenames")
    with tempfile.TemporaryDirectory(
        prefix="litai-wheels-", dir=temporary_root
    ) as temporary:
        directory = Path(temporary)
        payloads = []
        total = 0
        try:
            for package in lock.packages:
                source = sources[package.name]
                source.seek(0)
                size = 0
                with (directory / package.filename).open("x+b") as destination:
                    while block := source.read(1024 * 1024):
                        size += len(block)
                        total += len(block)
                        if size > _MAX_WHEEL or total > _MAX_BUNDLE:
                            _fail("Wheel staging allocation exceeds its bound")
                        destination.write(block)
                    destination.flush()
                    payloads.append(
                        (package.name, verify_locked_wheel(package, destination))
                    )
            staged = StagedPythonWheels(
                directory, lock, tuple(payloads), _identity(directory.lstat())
            )
            staged.revalidate()
        except (OSError, ValueError) as exc:
            raise DependencyObservationError(
                "dependencies.python-wheel-custody-invalid",
                "Wheel staging failed while copying or verifying inputs",
            ) from exc
        yield staged

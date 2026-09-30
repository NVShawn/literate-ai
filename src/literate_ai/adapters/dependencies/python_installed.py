"""Observe an explicit Python installation tree without importing package code.

This verifies a payload projection, not target-process provenance or permission
to execute. The lifecycle must independently bind the scheme and wheel custody.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import posixpath
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath

from literate_ai.contracts import ContentIdentity, canonical_identity

from .python_archive import WheelPayload, _path
from .python_wheelhouse import StagedPythonWheels
from .types import DependencyObservationError, HostDependencyObservation

_SCHEMES = frozenset({"purelib", "platlib", "scripts", "headers", "data"})
_MAX_FILES = 200_000
_MAX_FILE_BYTES = 8 * 1024**3
_MAX_TREE_BYTES = 64 * 1024**3
_MAX_RECORD_BYTES = 32 * 1024**2


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-installed-invalid", message)


@dataclass(frozen=True)
class PythonInstallationObservation:
    """Actual installed file identities and package-to-import aliases."""

    files: tuple[WheelPayload, ...]
    graph: HostDependencyObservation
    tree_identity: ContentIdentity


@dataclass(frozen=True)
class PythonInstallerChanges:
    """Expected output of an independently run, identity-bound installer projector.

    This value is not authorization. Lifecycle consumers must bind the producing
    process to the exact installer, target and staged wheel identities.
    """

    process_identity: ContentIdentity
    replaced: tuple[tuple[str, WheelPayload], ...] = ()
    generated: tuple[WheelPayload, ...] = ()
    skipped: tuple[str, ...] = ()


def _scheme(raw: Mapping[str, str]) -> dict[str, PurePosixPath]:
    if set(raw) != _SCHEMES:
        _fail("Python installation requires all five explicit scheme roots")
    result = {}
    for key, path in raw.items():
        if not isinstance(path, str):
            _fail("Python installation scheme roots must be relative strings")
        if path != ".":
            _path(path)
        result[key] = PurePosixPath(path)
    return result


def _snapshot(root: Path) -> tuple[WheelPayload, ...]:
    observed = root.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or getattr(observed, "st_file_attributes", 0) & 0x400
    ):
        _fail("Python installation root must be a real directory")
    result = []
    nodes: dict[str, str] = {}
    pending = [root]
    total = 0
    count = 0
    while pending:
        directory = pending.pop()
        current = directory.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & 0x400
        ):
            _fail("Python installation directory custody changed")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_FILES:
                    _fail("Python installation inventory exceeds its bound")
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                _path(relative)
                if nodes.setdefault(relative.casefold(), relative) != relative:
                    _fail("Python installation paths have case aliases")
                # Windows DirEntry.stat omits file identity and link count.
                # Custody checks require full metadata, not enumeration hints.
                before = path.lstat()
                if getattr(before, "st_file_attributes", 0) & 0x400:
                    _fail("Python installation contains a reparse point")
                if stat.S_ISDIR(before.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    _fail("Python installation contains a link or special file")
                if before.st_size > _MAX_FILE_BYTES:
                    _fail("Python installation file exceeds its bound")
                size = 0
                digest = hashlib.sha256()
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0)
                )
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        _fail("Python installation file changed while opening")
                    while block := stream.read(1024 * 1024):
                        size += len(block)
                        total += len(block)
                        if size > _MAX_FILE_BYTES or total > _MAX_TREE_BYTES:
                            _fail("Python installation size exceeds its bound")
                        digest.update(block)
                after = path.lstat()
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or size != before.st_size:
                    _fail("Python installation file changed during observation")
                result.append(WheelPayload(relative, size, digest.hexdigest()))
    return tuple(sorted(result, key=lambda item: item.path))


def _read(root: Path, payload: WheelPayload, limit: int) -> bytes:
    if payload.size > limit:
        _fail("Python installed metadata exceeds its bound")
    with (root / payload.path).open("rb") as stream:
        content = stream.read(limit + 1)
    if (
        len(content) != payload.size
        or hashlib.sha256(content).hexdigest() != payload.sha256
    ):
        _fail("Python installed metadata changed during observation")
    return content


def _record(
    content: bytes,
    *,
    root: Path,
    base: PurePosixPath,
    record_path: str,
    owned: Mapping[str, WheelPayload],
) -> None:
    recorded = set()
    try:
        for row in csv.reader(
            io.StringIO(content.decode("utf-8"), newline=""), strict=True
        ):
            if len(row) != 3:
                _fail("Installed RECORD must have three columns")
            raw_path, digest, size = row
            # Relative parent segments are valid for scripts, but may never leave
            # the explicitly owned installation root. Absolute paths are excluded
            # from this relocatable profile even though general RECORD allows them.
            if (
                not raw_path
                or raw_path.startswith("/")
                or "\\" in raw_path
                or ":" in raw_path
            ):
                _fail("Installed RECORD path is not portable and relative")
            path = posixpath.normpath((base / raw_path).as_posix())
            _path(path)
            if path in recorded or path not in owned:
                _fail(
                    "Installed RECORD repeats a path or claims another package's file"
                )
            recorded.add(path)
            if path == record_path:
                if (digest, size) != ("", ""):
                    _fail("Installed RECORD must not hash itself")
                continue
            payload = owned[path]
            algorithm, separator, _ = digest.partition("=")
            if separator != "=" or algorithm not in {"sha256", "sha384", "sha512"}:
                _fail("Installed RECORD uses an unsupported secure hash")
            computed = bytes.fromhex(payload.sha256)
            if algorithm != "sha256":
                hasher = hashlib.new(algorithm)
                observed_sha256 = hashlib.sha256()
                observed_size = 0
                with (root / path).open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        observed_size += len(block)
                        if observed_size > payload.size:
                            _fail("Installed RECORD payload changed during hashing")
                        hasher.update(block)
                        observed_sha256.update(block)
                if (
                    observed_size != payload.size
                    or observed_sha256.hexdigest() != payload.sha256
                ):
                    _fail("Installed RECORD payload changed during hashing")
                computed = hasher.digest()
            expected = (
                algorithm
                + "="
                + base64.urlsafe_b64encode(computed).rstrip(b"=").decode("ascii")
            )
            if digest != expected or size != str(payload.size):
                _fail("Installed RECORD disagrees with observed file bytes")
    except (UnicodeError, csv.Error) as exc:
        raise DependencyObservationError(
            "dependencies.python-installed-invalid",
            "Installed RECORD is not valid UTF-8 CSV",
        ) from exc
    if recorded != set(owned):
        _fail("Installed RECORD does not cover the complete package payload")


def observe_python_installation(
    staged: StagedPythonWheels,
    root: Path,
    *,
    scheme: Mapping[str, str],
    package_schemes: Mapping[str, Mapping[str, str]] | None = None,
    changes: Mapping[str, PythonInstallerChanges] | None = None,
) -> PythonInstallationObservation:
    """Compare a selected install tree to the locked wheel closure.

    The tree is dedicated package payload, not an ambient interpreter prefix.
    All unmodified wheel members (including native files and dependency metadata)
    must be present. Only regenerated RECORD, literal pip INSTALLER and empty
    REQUESTED are currently permitted installer additions. Bytecode and rewritten
    or generated scripts require exact identity-bound projector output; they are
    not silently trusted from a self-asserted installed RECORD.
    """
    try:
        staged.revalidate()
        roots = _scheme(scheme)
        names = {package.name for package in staged.lock.packages}
        if package_schemes is not None and set(package_schemes) != names:
            _fail("Python package scheme inventory differs from the locked closure")
        if changes is not None and set(changes) != names:
            _fail("Python installer change inventory differs from the locked closure")
        files = _snapshot(root)
        actual = {item.path: item for item in files}
        claimed: set[str] = set()
        shareable: dict[str, tuple[str, str, int]] = {}
        owners: dict[str, list[str]] = {}
        components = []
        for package in staged.lock.packages:
            if package_schemes is not None:
                roots = _scheme(package_schemes[package.name])
            transformation = changes[package.name] if changes is not None else None
            replaced = dict(transformation.replaced) if transformation else {}
            generated = (
                {item.path: item for item in transformation.generated}
                if transformation
                else {}
            )
            skipped = set(transformation.skipped) if transformation else set()
            wheel_files = dict(staged.payloads)[package.name]
            source_names = {item.path for item in wheel_files}
            if (
                not (set(replaced) | skipped).issubset(source_names)
                or set(replaced) & skipped
            ):
                _fail("Installer changes do not correspond to distinct wheel members")
            if transformation and (
                len(replaced) != len(transformation.replaced)
                or len(generated) != len(transformation.generated)
                or len(skipped) != len(transformation.skipped)
                or not isinstance(transformation.process_identity, ContentIdentity)
            ):
                _fail("Installer projection is ambiguous or lacks process identity")
            metadata_path = next(
                item.path
                for item in wheel_files
                if item.path.endswith(".dist-info/METADATA")
            )
            dist = metadata_path.split("/")[0]
            with zipfile.ZipFile(staged.directory / package.filename) as archive:
                wheel = BytesParser(policy=compat32).parsebytes(
                    archive.read(dist + "/WHEEL")
                )
            base = roots["purelib" if wheel["Root-Is-Purelib"] == "true" else "platlib"]
            owned = {}
            aliases = set()
            for item in wheel_files:
                path = PurePosixPath(item.path)
                destination_base = base
                relative = path
                if path.parts[0].endswith(".data"):
                    destination_base = roots[path.parts[1]]
                    relative = PurePosixPath(*path.parts[2:])
                destination = (destination_base / relative).as_posix()
                if item.path in replaced or item.path in skipped:
                    if not (
                        len(path.parts) > 2
                        and path.parts[0].endswith(".data")
                        and path.parts[1] == "scripts"
                    ):
                        _fail("Installer may only transform wheel script members")
                if item.path in skipped:
                    if destination in actual and destination not in generated:
                        _fail("Suppressed wheel wrapper is still installed")
                    continue
                shared_signature = (item.path, item.sha256, item.size)
                ordinary = item.path not in replaced and not any(
                    part.endswith((".dist-info", ".data")) for part in path.parts
                )
                if destination in owned or (
                    destination in claimed
                    and (not ordinary or shareable.get(destination) != shared_signature)
                ):
                    _fail("Python wheel installation paths collide")
                payload = actual.get(destination)
                if payload is None:
                    _fail("Python installation is missing a locked wheel member")
                expected = replaced.get(item.path, item)
                if item.path in replaced and expected.path != destination:
                    _fail("Installer script projection has the wrong destination")
                if item.path != dist + "/RECORD" and (payload.sha256, payload.size) != (
                    expected.sha256,
                    expected.size,
                ):
                    _fail("Installed payload differs from the verified wheel")
                owned[destination] = payload
                if ordinary:
                    shareable[destination] = shared_signature
                if destination_base in (
                    roots["purelib"],
                    roots["platlib"],
                ) and relative.suffix in {".py", ".so", ".pyd"}:
                    candidate = (
                        relative.parts[0]
                        if len(relative.parts) > 1
                        else relative.name.split(".", 1)[0]
                    )
                    if candidate.isidentifier():
                        aliases.add(candidate)
            for destination, expected in generated.items():
                _path(destination)
                if PurePosixPath(destination).parent != roots["scripts"]:
                    _fail("Generated wrapper is outside the selected script directory")
                if destination in owned or destination in claimed:
                    _fail("Generated wrapper collides with another installed file")
                if actual.get(destination) != expected:
                    _fail(
                        "Generated wrapper differs from independently projected bytes"
                    )
                if os.name != "nt" and not (root / destination).stat().st_mode & 0o111:
                    _fail("Generated wrapper is not executable on this target")
                owned[destination] = expected
            for name, expected in (("INSTALLER", b"pip\n"), ("REQUESTED", b"")):
                destination = (base / dist / name).as_posix()
                if destination in actual:
                    if destination in owned or destination in claimed:
                        _fail("Wheel payload overlaps installer metadata")
                    if _read(root, actual[destination], 1024) != expected:
                        _fail("Unexpected installer metadata bytes")
                    owned[destination] = actual[destination]
            record_path = (base / dist / "RECORD").as_posix()
            _record(
                _read(root, actual[record_path], _MAX_RECORD_BYTES),
                root=root,
                base=base,
                record_path=record_path,
                owned=owned,
            )
            claimed.update(owned)
            for destination in owned:
                owners.setdefault(destination, []).append(package.name)
            tree = canonical_identity(
                {
                    "name": package.name,
                    "version": package.version,
                    "files": [
                        [item.path, item.sha256]
                        for item in sorted(owned.values(), key=lambda item: item.path)
                    ],
                }
            )
            components.append(
                {
                    "type": "library",
                    "bom-ref": "pkg:pypi/" + package.name,
                    "name": package.name,
                    "version": package.version,
                    "purl": f"pkg:pypi/{package.name}@{package.version}",
                    "hashes": [{"alg": "SHA-256", "content": tree.digest}],
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                        {
                            "name": "literate-ai:python-installed-tree-identity",
                            "value": tree.uri,
                        },
                        *(
                            {
                                "name": "literate-ai:python-top-level-import",
                                "value": alias,
                            }
                            for alias in sorted(aliases)
                        ),
                    ],
                }
            )
        if claimed != set(actual):
            _fail("Python installation contains files outside the locked closure")
        if _snapshot(root) != files:
            _fail("Python installation changed during observation")
        staged.revalidate()
        tree_identity = canonical_identity(
            {
                "files": [[item.path, item.sha256] for item in files],
                "owners": {
                    path: sorted(names) for path, names in sorted(owners.items())
                },
                "scheme": dict(sorted(scheme.items())),
                "package_schemes": package_schemes,
                "installer_projections": {
                    name: value.process_identity.uri
                    for name, value in sorted((changes or {}).items())
                },
            }
        )
        edges = tuple(
            (parent if parent == "@root" else "pkg:pypi/" + parent, "pkg:pypi/" + child)
            for parent, child in staged.lock.edges
        )
        return PythonInstallationObservation(
            files, HostDependencyObservation(tuple(components), edges), tree_identity
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DependencyObservationError(
            "dependencies.python-installed-invalid",
            "Python installation files are unavailable or invalid",
        ) from exc

"""Streaming, revalidated custody of explicitly selected retained input roots."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)

_CHUNK = 1024 * 1024


class RetainedProjectInputError(ValueError):
    """An input could not be captured under its reviewed identity."""


def _same_open_file(path_stat: os.stat_result, open_stat: os.stat_result) -> bool:
    """Compare a path ``lstat`` with the open handle's ``fstat``.

    On Windows, path and handle stats report file identity through different APIs
    (128-bit versus 64-bit file IDs), so only type, size and modification time are
    comparable there; path-to-path comparisons still use the full stamp.
    """

    if os.name == "nt":
        return (
            stat.S_IFMT(path_stat.st_mode) == stat.S_IFMT(open_stat.st_mode)
            and path_stat.st_size == open_stat.st_size
            and path_stat.st_mtime_ns == open_stat.st_mtime_ns
        )
    return _stamp(path_stat) == _stamp(open_stat)


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _root(root: Path) -> Path:
    root = root.absolute()
    if root != root.resolve(strict=True):
        raise RetainedProjectInputError("Retained root is redirected")
    require_safe_directory(root)
    return root


def _safe(root: Path, relative: str) -> Path:
    path = root / relative
    require_safe_directory(path.parent)
    if path_is_link_or_reparse(path) or path.resolve(strict=True) != path:
        raise RetainedProjectInputError("Retained input is redirected")
    return path


def _open_file(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if os.name == "nt":
        # Windows has no portable openat; reparse/ancestor checks bracket the read.
        return os.open(path, flags)
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        return os.open(path.name, flags, dir_fd=parent)
    finally:
        os.close(parent)


def _stream(
    root: Path, name: str, limit: int, output: BinaryIO | None = None
) -> tuple[int, int, str]:
    path = _safe(root, name)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise RetainedProjectInputError("Retained input type or size is inadmissible")
    descriptor = _open_file(path)
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(descriptor, "rb") as stream:
        if not _same_open_file(before, os.fstat(stream.fileno())) or _stamp(
            before
        ) != _stamp(_safe(root, name).lstat()):
            raise RetainedProjectInputError("Retained input changed before read")
        while data := stream.read(min(_CHUNK, limit - count + 1)):
            count += len(data)
            if count > limit:
                raise RetainedProjectInputError("Retained input exceeds byte bound")
            digest.update(data)
            if output is not None:
                output.write(data)
        after = os.fstat(stream.fileno())
    if (
        not _same_open_file(before, after)
        or _stamp(before) != _stamp(_safe(root, name).lstat())
        or count != before.st_size
    ):
        raise RetainedProjectInputError("Retained input changed during read")
    return count, 0o755 if before.st_mode & 0o111 else 0o644, digest.hexdigest()


def _inventory(
    root: Path, roots: tuple[str, ...], limit: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    names: list[str] = []
    directories: list[str] = []
    count = 0

    def visit(name: str) -> None:
        nonlocal count
        count += 1
        if count > limit:
            raise RetainedProjectInputError("Retained inputs exceed entry bound")
        canonical_relative_posix_paths((name,), label="retained entry")
        path = _safe(root, name)
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(name)
            # Do not materialize an unbounded scandir listing before checking bounds.
            if os.name != "nt":
                descriptor = _open_file(path)
                try:
                    if _stamp(os.fstat(descriptor)) != _stamp(metadata):
                        raise RetainedProjectInputError(
                            "Retained directory changed before inventory"
                        )
                    with os.scandir(descriptor) as children:
                        for child in children:
                            visit(name + "/" + child.name)
                finally:
                    os.close(descriptor)
            else:
                with os.scandir(path) as children:
                    for child in children:
                        visit(name + "/" + child.name)
            if _stamp(metadata) != _stamp(_safe(root, name).lstat()):
                raise RetainedProjectInputError(
                    "Retained directory changed during inventory"
                )
        elif stat.S_ISREG(metadata.st_mode):
            names.append(name)
        else:
            raise RetainedProjectInputError("Retained input contains a special file")

    for name in roots:
        visit(name)
    return tuple(sorted(names)), tuple(sorted(directories))


def discover_retained_project_inputs(
    root: Path, roots: Mapping[str, str], limits: RetainedProjectLimits
) -> RetainedProjectManifest:
    """Read a complete declared closure; the caller must review the result."""
    root = _root(root)
    selected = tuple(sorted(roots))
    canonical_relative_posix_paths(selected, label="retained roots")
    names, directories = _inventory(root, selected, limits.max_entries)
    members = []
    total = 0
    for name in names:
        size, mode, digest = _stream(
            root, name, min(limits.max_file_bytes, limits.max_total_bytes - total)
        )
        total += size
        role = next(
            role
            for prefix, role in roots.items()
            if name == prefix or name.startswith(prefix + "/")
        )
        members.append(RetainedProjectMember(name, size, mode, digest, role))
    manifest = RetainedProjectManifest(selected, tuple(members), limits, directories)
    verify_retained_project_inputs(root, manifest)
    return manifest


def verify_retained_project_inputs(
    root: Path, manifest: RetainedProjectManifest
) -> None:
    """Reject any extra, missing, redirected or changed input, including empty dirs."""
    root = _root(root)
    names, directories = _inventory(root, manifest.roots, manifest.limits.max_entries)
    if (
        names != tuple(m.path for m in manifest.members)
        or directories != manifest.directories
    ):
        raise RetainedProjectInputError("Retained input inventory changed")
    for member in manifest.members:
        if _stream(root, member.path, member.size) != (
            member.size,
            member.mode,
            member.sha256,
        ):
            raise RetainedProjectInputError(
                "Retained input bytes or executable mode changed"
            )
    if _inventory(root, manifest.roots, manifest.limits.max_entries) != (
        names,
        directories,
    ):
        raise RetainedProjectInputError(
            "Retained inventory changed during verification"
        )


def capture_retained_project_inputs(
    root: Path, manifest: RetainedProjectManifest, destination: Path
) -> Path:
    """Capture to a new private snapshot: ``blobs/<sha256>`` and ``tree/<path>``.

    Never reuse caller-controlled blobs or hardlink runnable files to CAS. All tree
    files are read-only, and content is rechecked before returning. Execution owners
    must still verify inputs after execution; permissions are not a trust boundary.
    """
    root = _root(root)
    destination = destination.absolute()
    require_safe_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise RetainedProjectInputError("Retained snapshot destination already exists")
    if destination == root or root in destination.parents:
        raise RetainedProjectInputError("Retained snapshot must be outside source root")
    verify_retained_project_inputs(root, manifest)
    temporary = Path(tempfile.mkdtemp(prefix=".retained-", dir=destination.parent))
    try:
        blobs = temporary / "blobs"
        tree = temporary / "tree"
        blobs.mkdir()
        tree.mkdir()
        for directory in manifest.directories:
            (tree / directory).mkdir(parents=True, exist_ok=True)
        for member in manifest.members:
            blob = blobs / member.sha256
            with tempfile.TemporaryFile(dir=temporary) as stream:
                if _stream(root, member.path, member.size, stream) != (
                    member.size,
                    member.mode,
                    member.sha256,
                ):
                    raise RetainedProjectInputError(
                        "Retained input changed while capturing"
                    )
                stream.seek(0)
                if not blob.exists():
                    with blob.open("xb") as target:
                        shutil.copyfileobj(stream, target, _CHUNK)
                    blob.chmod(0o444)
            target = tree / member.path
            target.parent.mkdir(parents=True, exist_ok=True)
            with blob.open("rb") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, _CHUNK)
            target.chmod(0o555 if member.mode == 0o755 else 0o444)
        verify_retained_project_inputs(root, manifest)
        verify_retained_project_inputs(tree, manifest)
        for digest, size in {m.sha256: m.size for m in manifest.members}.items():
            if _stream(temporary, "blobs/" + digest, size) != (size, 0o644, digest):
                raise RetainedProjectInputError(
                    "Retained CAS blob changed during capture"
                )
        # Atomic no-replacement reservation; no rename-over-existing-directory race.
        destination.mkdir(mode=0o700)
        try:
            for name in ("blobs", "tree"):
                (temporary / name).rename(destination / name)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return destination / "tree"
    finally:
        shutil.rmtree(temporary)

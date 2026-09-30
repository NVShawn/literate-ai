"""Exact package-tree custody after bounded directory-archive verification."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.directory_artifacts import DirectoryExportFile
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_bundle_delivery import _read_file
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.paths import canonical_relative_posix_paths


def _signature(node):
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
        node.st_nlink,
    )


@dataclass(frozen=True, slots=True)
class RetainedPackageTree:
    root: Path
    files: tuple[DirectoryExportFile, ...]
    directories: tuple[tuple[str, tuple[int, int, int]], ...]
    nodes: tuple[tuple[str, tuple[int, ...]], ...]

    @classmethod
    def capture(cls, root: Path, files: tuple[DirectoryExportFile, ...]):
        directories, nodes = cls._observe(root, files)
        return cls(root, files, directories, nodes)

    @staticmethod
    def _observe(root, files):
        require_safe_directory(root)
        wanted_directories = {"."}
        expected = {item.path: item for item in files}
        if len(expected) != len(files) or not files:
            raise ValueError("retained.package.files-invalid")
        for item in files:
            wanted_directories.update(str(p) for p in PurePosixPath(item.path).parents)
        actual_directories, actual_files = set(), set()
        nodes, directories = [], []
        pending = [root]
        while pending:
            current = pending.pop()
            relative = current.relative_to(root).as_posix()
            if relative not in wanted_directories:
                raise ValueError("retained.package.unbound-directory")
            actual_directories.add(relative)
            directories.append((relative, directory_node(current)))
            with os.scandir(current) as entries:
                for entry in entries:
                    path = current / entry.name
                    relative = path.relative_to(root).as_posix()
                    # DirEntry.stat omits device/inode/link counts on Windows.
                    before = path.lstat()
                    if stat_is_link_or_reparse(before):
                        raise ValueError("retained.package.file-unsafe")
                    if stat.S_ISDIR(before.st_mode):
                        if relative not in wanted_directories:
                            raise ValueError("retained.package.unbound-directory")
                        pending.append(path)
                        continue
                    if relative not in expected or relative in actual_files:
                        raise ValueError("retained.package.unbound-file")
                    actual_files.add(relative)
                    item = expected[relative]
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise ValueError("retained.package.file-unsafe")
                    # Windows has no POSIX executable mode. Capture its physical mode
                    # and retain intended modes in the immutable archive records.
                    if os.name != "nt" and stat.S_IMODE(before.st_mode) != item.mode:
                        raise ValueError("retained.package.mode-changed")
                    reference = BlobRef(
                        hashlib.sha256(item.content).hexdigest(), len(item.content)
                    )
                    if _read_file(path, reference) != item.content:
                        raise ValueError("retained.package.bytes-changed")
                    if _signature(path.lstat()) != _signature(before):
                        raise ValueError("retained.package.file-changed")
                    nodes.append((relative, _signature(before)))
        if actual_files != set(expected) or actual_directories != wanted_directories:
            raise ValueError("retained.package.tree-incomplete")
        return tuple(sorted(directories)), tuple(sorted(nodes))

    def require_unchanged(self):
        directories, nodes = self._observe(self.root, self.files)
        if directories != self.directories or nodes != self.nodes:
            raise ValueError("retained.package.custody-changed")

    def relocated(self, root: Path):
        result = replace(self, root=root)
        result.require_unchanged()
        return result


def write_staged_package(root: Path, files: tuple[DirectoryExportFile, ...]):
    """Populate an owned empty private stage; never merge into a preexisting tree."""
    if (
        not isinstance(files, tuple)
        or not files
        or any(not isinstance(item, DirectoryExportFile) for item in files)
    ):
        raise ValueError("retained.package.files-invalid")
    canonical_relative_posix_paths((item.path for item in files), label="package files")
    owned = directory_node(root)
    if any(root.iterdir()):
        raise ValueError("retained.package.stage-not-empty")
    for item in files:
        if directory_node(root) != owned:
            raise ValueError("retained.package.stage-changed")
        path = root / item.path
        # Only canonical archive paths reach here, through read_directory_export.
        path.parent.mkdir(parents=True, exist_ok=True)
        require_safe_directory(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, item.mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(item.content)
            stream.flush()
            if os.name != "nt":
                os.fchmod(stream.fileno(), item.mode)
            os.fsync(stream.fileno())
    if directory_node(root) != owned:
        raise ValueError("retained.package.stage-changed")
    return RetainedPackageTree.capture(root, files)

"""Disposable source-test workspaces with exact original-file custody.

Test outputs are allowed only in the disposable copy. This is cooperative custody,
not containment of an authorized hostile process.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.source_index import generated_source_tree_identity
from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError

# Match the filesystem source cache's accepted source limits. Count directories
# separately so a file-count bound also bounds the initial tree traversal.
_MAX_FILES = 4096
_MAX_ENTRIES = 16384
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024


class SourceVerificationWorkspaceError(ValueError):
    """Source verification cannot retain exact safe candidate custody."""


def _closure():
    return PinnedInputClosure(
        maximum_files=_MAX_FILES,
        maximum_file_bytes=_MAX_FILE_BYTES,
        maximum_total_bytes=_MAX_TOTAL_BYTES,
    )


def _members(root):
    pending = [root]
    files, directories = {}, {}
    count = 0
    while pending:
        current = pending.pop()
        directories[current.relative_to(root).as_posix()] = directory_node(current)
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise SourceVerificationWorkspaceError("source entry limit")
                path = current / entry.name
                node = path.lstat()
                if stat_is_link_or_reparse(node):
                    raise SourceVerificationWorkspaceError("indirect source entry")
                if stat.S_ISDIR(node.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(node.st_mode) and node.st_nlink == 1:
                    if len(files) >= _MAX_FILES:
                        raise SourceVerificationWorkspaceError("source file limit")
                    files[path.relative_to(root).as_posix()] = stat.S_IMODE(
                        node.st_mode
                    )
                else:
                    raise SourceVerificationWorkspaceError("unsafe source entry")
    return files, directories


@dataclass(frozen=True)
class SourceVerificationWorkspace:
    root: Path
    original: Path
    original_closure: PinnedInputClosure
    copied_closure: PinnedInputClosure
    modes: dict[str, int]
    original_directories: dict
    copied_directories: dict

    def require_unchanged(self):
        try:
            self.original_closure.require_unchanged()
            if _members(self.original) != (self.modes, self.original_directories):
                raise SourceVerificationWorkspaceError(
                    "original source membership changed"
                )
            # Test-created files are not source members. Validate only the captured
            # paths in this copy, including all original ancestor directory nodes.
            for relative, expected in self.copied_directories.items():
                if directory_node(self.root / relative) != expected:
                    raise SourceVerificationWorkspaceError(
                        "verification directory changed"
                    )
            self.copied_closure.require_unchanged()
            for relative, mode in self.modes.items():
                node = (self.root / relative).lstat()
                if node.st_nlink != 1 or stat.S_IMODE(node.st_mode) != mode:
                    raise SourceVerificationWorkspaceError(
                        "verification source mode changed"
                    )
        except (
            OSError,
            ValueError,
            PinnedInputClosureError,
            UnsafeFilesystemPathError,
        ) as exc:
            raise SourceVerificationWorkspaceError(
                "source verification custody changed"
            ) from exc


@contextmanager
def source_verification_workspace(root: Path, expected_tree: ContentIdentity):
    """Run relative test commands on a copy; never publish their output files."""
    stage = None
    owned = None
    body_failed = False
    try:
        # Resolve system temp aliases only for our newly created workspace. Do not
        # resolve caller-selected source links before checking their ancestors.
        original = Path(root).absolute()
        modes, directories = _members(original)
        original_closure = _closure()
        content = {
            relative: original_closure.pin(
                original / relative, boundary=original, label=relative
            )
            for relative in sorted(modes)
        }
        if generated_source_tree_identity(content) != expected_tree.uri:
            raise SourceVerificationWorkspaceError("candidate source tree changed")
        original_closure.require_unchanged()
        if _members(original) != (modes, directories):
            raise SourceVerificationWorkspaceError("candidate changed during capture")
        stage = Path(tempfile.mkdtemp(prefix="litai-source-test-")).resolve(strict=True)
        owned = directory_node(stage)
        # Preserve empty directories and original executable permissions as well
        # as bytes. Never copy symlinks or borrow source hardlinks.
        for relative in sorted(directories):
            (stage / relative).mkdir(parents=True, exist_ok=True)
        copied_closure = _closure()
        for relative, payload in content.items():
            destination = stage / relative
            destination.write_bytes(payload)
            destination.chmod(modes[relative])
            copied_closure.pin(
                destination,
                boundary=stage,
                label=relative,
                expected_content=payload,
            )
        copied_modes, copied_directories = _members(stage)
        if copied_modes != modes:
            raise SourceVerificationWorkspaceError("source copy mode mismatch")
        workspace = SourceVerificationWorkspace(
            stage,
            original,
            original_closure,
            copied_closure,
            modes,
            directories,
            copied_directories,
        )
        workspace.require_unchanged()
        try:
            yield workspace
        except BaseException:
            body_failed = True
            raise
        workspace.require_unchanged()
    except (
        OSError,
        ValueError,
        PinnedInputClosureError,
        UnsafeFilesystemPathError,
    ) as exc:
        if body_failed:
            raise
        raise SourceVerificationWorkspaceError(
            "source candidate or verification copy is unavailable, unsafe, "
            "over its capture limits, or changed"
        ) from exc
    finally:
        if stage is not None and owned is not None:
            try:
                still_owned = directory_node(stage) == owned
            except (OSError, ValueError, UnsafeFilesystemPathError):
                still_owned = False
            if still_owned:

                def writable_cleanup(function, raw_path, error):
                    # Windows refuses unlink of read-only source copies. Change
                    # only a regular node still beneath this owned stage.
                    path = Path(raw_path)
                    if (
                        not isinstance(error[1], PermissionError)
                        or directory_node(stage) != owned
                        or not path.is_relative_to(stage)
                    ):
                        raise error[1]
                    directory_node(path.parent)
                    node = path.lstat()
                    if (
                        stat_is_link_or_reparse(node)
                        or (stat.S_ISREG(node.st_mode) and node.st_nlink != 1)
                        or not (
                            stat.S_ISREG(node.st_mode) or stat.S_ISDIR(node.st_mode)
                        )
                    ):
                        raise error[1]
                    path.chmod(
                        node.st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                    )
                    function(path)

                shutil.rmtree(stage, onerror=writable_cleanup)

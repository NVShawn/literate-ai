"""Add complete content-addressed packs without advancing live Git authority.

Published object files are retained on failure/rollback: a concurrent reader may
already depend on them. Cooperative keep markers protect them until context exit.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory

from ._repository_pack_capture import RepositoryObjectPack
from ._write_reservations import (
    WriteReservationSet,
    WriteReservationTarget,
    acquire_write_reservations,
)
from .repository_file_custody import _node
from .repository_orchestration import OrchestrationInventoryError


def _fail(suffix, message):
    raise OrchestrationInventoryError("refresh_objects_" + suffix, message) from None


def _key(path):
    require_safe_directory(path)
    node = path.lstat()
    return node.st_dev, node.st_ino, node.st_mode


def _matching(path, content):
    # Existing Git packs may be hardlinked by a local clone. They are read only;
    # no operation rewrites their bytes or chmods their shared inode.
    node = _node(path.parent, path.name, set(), max(1, len(content)), single_link=False)
    if node.kind != "file" or node.content != content:
        _fail("collision", "existing object file does not match the verified pack")
    return node


def _install_file(path, content, guard):
    guard()
    WriteReservationSet._no_alias(path)
    if os.path.lexists(path):
        _matching(path, content)
        return False
    temporary = path.parent / ("tmp_litai_" + secrets.token_hex(16))
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    written = 0
    identity = None
    try:
        try:
            named = temporary.lstat()
            if not _identity_matches(
                temporary, named, descriptor, os.fstat(descriptor)
            ):
                _fail("changed", "object staging file changed during creation")
            identity = (named.st_dev, named.st_ino)
            while written < len(content):
                count = os.write(
                    descriptor, memoryview(content)[written : written + 65536]
                )
                if count <= 0:
                    _fail("write_failed", "object write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        guard()
        current = _matching(temporary, content)
        if current.signature[:2] != identity:
            _fail("changed", "object staging file identity changed")
        try:
            # Source and destination share the same directory/filesystem. link
            # publishes complete bytes atomically and never replaces a winner.
            os.link(temporary, path, follow_symlinks=False)
            created = True
        except FileExistsError:
            created = False
        _matching(path, content)
        guard()
        return created
    finally:
        try:
            guard()
            current = _matching(temporary, content[:written])
            if current.signature[:2] != identity:
                _fail("changed", "object temporary ownership changed")
            temporary.unlink()
        except (OSError, UnsafeFilesystemPathError, OrchestrationInventoryError):
            _fail(
                "cleanup_incomplete",
                "changed object temporary retained for explicit recovery",
            )


@dataclass(frozen=True, slots=True)
class InstalledObjectPack:
    repository: str
    directory: Path
    objects: RepositoryObjectPack


class InstalledRefreshObjects:
    def __init__(self, stage, records, keep, guards):
        self._stage, self._keep, self._guards = stage, keep, guards
        self.records = records
        self._active = True
        self._terminal_verified = False

    def require_current(self):
        if not self._active:
            _fail("inactive", "installed object ownership is no longer live")
        self._stage.require_current()
        self.require_owned_objects_current()
        self._stage.require_current()

    def require_owned_objects_current(self):
        """Verify bounded no-follow object bytes while keep ownership is live."""
        if not self._active:
            _fail("inactive", "installed object ownership is no longer live")
        self._keep.verify_all()
        for guard in self._guards:
            guard()
        for record in self.records:
            prefix = "pack-" + record.objects.pack_id
            _matching(record.directory / (prefix + ".pack"), record.objects.pack)
            _matching(record.directory / (prefix + ".idx"), record.objects.index)


@contextmanager
def install_refresh_objects(stage):
    from .repository_refresh_staging import StagedRefresh

    if not isinstance(stage, StagedRefresh) or stage._objects is None:
        raise TypeError("installation requires live complete object staging")
    stage.require_current()
    prepared = stage._files._owner._prepared.refresh
    observations = {observed.root: observed for observed in prepared.children}
    targets, records, guards = [], [], []
    try:
        for path, captured in stage._objects.packs:
            pack = stage.read_entry("objects-pack", path, captured.objects.pack_id)
            index = stage.read_entry("objects-index", path, captured.objects.pack_id)
            objects_record = RepositoryObjectPack(
                captured.objects.commit,
                pack,
                index,
                captured.objects.policy,
            )
            if objects_record.pack_id != captured.objects.pack_id:
                _fail("changed", "staged object identity changed")
            observed = observations[prepared.repository.root / path]
            if _key(observed.common_directory) != observed.common_node:
                _fail("changed", "child Git common-directory custody changed")
            objects = observed.common_directory / "objects"
            objects_node = _key(objects)
            directory = objects / "pack"
            WriteReservationSet._no_alias(directory)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                require_safe_directory(directory)
            directory_node = _key(directory)

            def guard(
                observed=observed,
                objects=objects,
                objects_node=objects_node,
                directory=directory,
                directory_node=directory_node,
            ):
                if (
                    _key(observed.common_directory) != observed.common_node
                    or _key(objects) != objects_node
                    or _key(directory) != directory_node
                ):
                    _fail("changed", "child object directory custody changed")

            guard()
            guards.append(guard)
            keep_path = directory / ("pack-" + objects_record.pack_id + ".keep")
            targets.append(WriteReservationTarget(keep_path, directory, directory_node))
            records.append(InstalledObjectPack(path, directory, objects_record))
        stage.require_current()
        with acquire_write_reservations(tuple(targets)) as keep:
            for record, guard in zip(records, guards, strict=True):
                stage.require_current()
                keep.verify_all()
                prefix = "pack-" + record.objects.pack_id
                _install_file(
                    record.directory / (prefix + ".pack"), record.objects.pack, guard
                )
                _install_file(
                    record.directory / (prefix + ".idx"), record.objects.index, guard
                )
            installed = InstalledRefreshObjects(
                stage, tuple(records), keep, tuple(guards)
            )
            try:
                installed.require_current()
                yield installed
                if not installed._terminal_verified:
                    installed.require_current()
                    installed._terminal_verified = True
            finally:
                installed._active = False
        stage.require_current()
    except OrchestrationInventoryError:
        raise
    except (OSError, UnsafeFilesystemPathError):
        _fail(
            "write_failed", "object installation failed; immutable additions may remain"
        )

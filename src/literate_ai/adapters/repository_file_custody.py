"""Physical before-file custody and no-clobber preflight, never filesystem apply."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_tree import (
    RepositoryTreeCapturePolicy,
    RepositoryTreeChange,
    RepositoryTreeSnapshot,
    repository_tree_changes,
)

from .repository_locks import REPOSITORY_LOCK_PATH
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import _local_identity_wire

_RESERVED = (
    ".git",
    ".litai-locks",
    ".litai-cache-locks",
    ".literate.project.json.write.lock",
    ".literate/.repository-lock.write",
    REPOSITORY_LOCK_PATH,
)


def _fail(suffix, message):
    raise OrchestrationInventoryError("refresh_files_" + suffix, message) from None


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
class PhysicalNode:
    path: str
    kind: str
    signature: tuple[int, ...] | None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class DirectoryCustody:
    path: str
    signature: tuple[int, ...]
    members: tuple[tuple[bytes, tuple[int, ...]], ...]


@dataclass(frozen=True, slots=True)
class PreparedWorktreeChanges:
    root: Path
    root_node: tuple[int, int, int]
    previous: RepositoryTreeSnapshot
    prospective: RepositoryTreeSnapshot
    policy: RepositoryTreeCapturePolicy
    changes: tuple[RepositoryTreeChange, ...]
    nodes: tuple[PhysicalNode, ...]
    directories: tuple[DirectoryCustody, ...]

    @property
    def identity(self):
        return canonical_identity(
            _local_identity_wire(
                {
                    "root": str(self.root),
                    "root_node": self.root_node,
                    "previous": self.previous.identity,
                    "prospective": self.prospective.identity,
                    "policy": asdict(self.policy),
                    "nodes": [
                        {
                            "path": item.path,
                            "kind": item.kind,
                            "signature": item.signature,
                            "content": None
                            if item.content is None
                            else hashlib.sha256(item.content).hexdigest(),
                        }
                        for item in self.nodes
                    ],
                    "directories": [
                        {
                            "path": item.path,
                            "signature": item.signature,
                            "members": [
                                (name.hex(), signature)
                                for name, signature in item.members
                            ],
                        }
                        for item in self.directories
                    ],
                }
            )
        ).uri

    @property
    def physical_bytes(self):
        return sum(len(item.content or b"") for item in self.nodes)


def _guard(root, expected):
    require_safe_directory(root)
    node = root.lstat()
    if (node.st_dev, node.st_ino, node.st_mode) != expected:
        _fail("changed", "worktree root changed after observation")


def _reachable(root, relative, transitions):
    """Do not follow links; owned file-to-directory descendants are absent."""
    parent = root
    prefix = PurePosixPath()
    for part in PurePosixPath(relative).parts[:-1]:
        require_safe_directory(parent)
        parent = parent / part
        prefix /= part
        try:
            node = parent.lstat()
        except FileNotFoundError:
            return False
        if stat_is_link_or_reparse(node) or not stat.S_ISDIR(node.st_mode):
            if prefix.as_posix() in transitions:
                return False
            _fail("unsafe", "worktree path has a non-directory or linked ancestor")
    require_safe_directory(parent)
    return True


def _node(root, relative, transitions, maximum_bytes, *, single_link=True):
    if not _reachable(root, relative, transitions):
        return PhysicalNode(relative, "absent", None)
    path = root / relative
    try:
        before = path.lstat()
    except FileNotFoundError:
        return PhysicalNode(relative, "absent", None)
    signature = _signature(before)
    if stat.S_ISLNK(before.st_mode):
        content = os.readlink(os.fsencode(path))
        if len(content) > maximum_bytes or _signature(path.lstat()) != signature:
            _fail("changed", "link changed or exceeds its capture bound")
        return PhysicalNode(relative, "symlink", signature, content)
    if stat_is_link_or_reparse(before):
        _fail("unsafe", "worktree capture refuses reparse nodes")
    if stat.S_ISDIR(before.st_mode):
        return PhysicalNode(relative, "directory", signature)
    if not stat.S_ISREG(before.st_mode) or (single_link and before.st_nlink != 1):
        _fail("unsafe", "worktree capture requires direct single-link regular files")
    if before.st_size > maximum_bytes:
        _fail("limit", "physical file exceeds its capture bound")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (single_link and opened.st_nlink != 1)
            or not _identity_matches(path, before, descriptor, opened)
        ):
            _fail("changed", "physical file identity changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum_bytes + 1)
        require_safe_directory(path.parent)
        after = path.lstat()
        if (
            _signature(after) != signature
            or _signature(os.fstat(descriptor)) != _signature(opened)
            or not _identity_matches(path, after, descriptor, os.fstat(descriptor))
            or len(content) != before.st_size
        ):
            _fail("changed", "physical file changed during capture")
        return PhysicalNode(relative, "file", signature, content)
    finally:
        os.close(descriptor)


def _directory(root, relative, maximum_entries):
    path = root / relative
    require_safe_directory(path)
    before = _signature(path.lstat())
    members = []
    with os.scandir(path) as entries:
        for entry in entries:
            if len(members) >= maximum_entries:
                _fail("limit", "affected directory inventory exceeds its entry bound")
            # DirEntry.stat may use stale enumeration metadata; on Windows it
            # also omits inode/device/link count. Custody needs a fresh lstat.
            members.append((os.fsencode(entry.name), _signature(os.lstat(entry.path))))
    require_safe_directory(path)
    if _signature(path.lstat()) != before:
        _fail("changed", "affected directory changed during inventory")
    return DirectoryCustody(relative, before, tuple(sorted(members)))


def _observe_worktree_changes(
    root: Path,
    root_node: tuple[int, int, int],
    previous: RepositoryTreeSnapshot,
    prospective: RepositoryTreeSnapshot,
    *,
    policy: RepositoryTreeCapturePolicy | None = None,
) -> PreparedWorktreeChanges:
    """Observe physical inputs; Git/publication ownership is a separate guard."""
    policy = RepositoryTreeCapturePolicy() if policy is None else policy
    if not isinstance(policy, RepositoryTreeCapturePolicy):
        raise TypeError("file custody requires a typed capture policy")
    root = Path(root).absolute()
    try:
        _guard(root, root_node)
        changes = repository_tree_changes(previous, prospective)
        if len(changes) > policy.maximum_entries:
            _fail("limit", "worktree change count exceeds its bound")
        before = {entry.path: entry for entry in previous.entries}
        after = {entry.path: entry for entry in prospective.entries}
        transitions = {
            change.path
            for change in changes
            if change.previous is not None
            and change.previous.mode not in {"040000", "160000"}
            and change.prospective is not None
            and change.prospective.mode == "040000"
        }
        for change in changes:
            folded = change.path.casefold()
            if any(
                folded == path
                or folded.startswith(path + "/")
                or path.startswith(folded + "/")
                for path in _RESERVED
            ):
                _fail("reserved", "prospective changes overlap transaction metadata")
            if any(
                entry is not None and entry.mode == "160000"
                for entry in (change.previous, change.prospective)
            ):
                _fail(
                    "nested_ownership",
                    "nested Gitlink transitions require recursive ownership",
                )
        nodes = []
        total = 0
        # Complete directory membership is transaction custody. Capturing only
        # ancestors of changed paths lets an unrelated concurrent addition escape
        # the final commit proof.
        directories = (
            {
                ".",
                *(entry.path for entry in previous.entries if entry.mode == "040000"),
            }
            if changes
            else set()
        )
        for change in changes:
            directories.update(
                parent.as_posix() for parent in PurePosixPath(change.path).parents
            )
            node = _node(
                root,
                change.path,
                transitions,
                min(
                    policy.maximum_blob_bytes,
                    max(1, policy.maximum_total_blob_bytes - total),
                ),
            )
            total += len(node.content or b"")
            if total > policy.maximum_total_blob_bytes:
                _fail(
                    "limit", "physical before-file bytes exceed their aggregate bound"
                )
            if change.previous is None:
                if node.kind != "absent":
                    _fail(
                        "collision",
                        "prospective addition collides with an existing entry",
                    )
            else:
                allowed = {
                    "040000": {"directory", "absent"},
                    "120000": {"symlink", "file"},
                    "100644": {"file"},
                    "100755": {"file"},
                }[change.previous.mode]
                if node.kind not in allowed:
                    _fail(
                        "changed",
                        "physical before-file type does not match the observed tree",
                    )
                if node.kind == "directory":
                    directories.add(change.path)
            nodes.append(node)
        inventories = []
        member_count = 0
        for relative in sorted(directories):
            node = _node(root, relative, transitions, policy.maximum_blob_bytes)
            if node.kind != "directory":
                continue
            inventory = _directory(
                root, relative, max(1, policy.maximum_entries - member_count)
            )
            member_count += len(inventory.members)
            if member_count > policy.maximum_entries:
                _fail(
                    "limit", "affected directory entries exceed their aggregate bound"
                )
            inventories.append(inventory)
            retiring = (
                relative in before
                and before[relative].mode == "040000"
                and (relative not in after or after[relative].mode != "040000")
            )
            intended = {
                PurePosixPath(change.path).name
                for change in changes
                if change.prospective is not None
                and PurePosixPath(change.path).parent.as_posix() == relative
            }
            for raw_name, _ in inventory.members:
                name = os.fsdecode(raw_name)
                folded = unicodedata.normalize("NFC", name.casefold())
                if any(
                    name != value
                    and folded == unicodedata.normalize("NFC", value.casefold())
                    for value in intended
                ):
                    _fail(
                        "alias", "prospective path aliases an existing directory entry"
                    )
                member = (PurePosixPath(relative) / name).as_posix()
                if retiring and member not in before:
                    _fail(
                        "collision", "directory retirement would affect a foreign entry"
                    )
        _guard(root, root_node)
        return PreparedWorktreeChanges(
            root,
            root_node,
            previous,
            prospective,
            policy,
            changes,
            tuple(nodes),
            tuple(inventories),
        )
    except OrchestrationInventoryError:
        raise
    except (OSError, ValueError, TypeError, UnsafeFilesystemPathError):
        _fail("unsafe", "physical worktree custody is unavailable or unsafe")


def prepare_worktree_changes(
    root: Path,
    root_node: tuple[int, int, int],
    previous: RepositoryTreeSnapshot,
    prospective: RepositoryTreeSnapshot,
    *,
    policy: RepositoryTreeCapturePolicy | None = None,
) -> PreparedWorktreeChanges:
    prepared = _observe_worktree_changes(
        root, root_node, previous, prospective, policy=policy
    )
    require_worktree_changes_unchanged(prepared)
    return prepared


def require_worktree_changes_unchanged(prepared: PreparedWorktreeChanges) -> None:
    if not isinstance(prepared, PreparedWorktreeChanges):
        raise TypeError("file revalidation requires prepared physical custody")
    fresh = _observe_worktree_changes(
        prepared.root,
        prepared.root_node,
        prepared.previous,
        prepared.prospective,
        policy=prepared.policy,
    )
    if fresh != prepared:
        _fail("changed", "physical custody changed after preflight")

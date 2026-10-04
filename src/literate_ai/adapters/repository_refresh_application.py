"""Live-only repository refresh application with conservative in-process rollback.

The saved stage is recovery evidence, never replay authority. Application requires
the still-live publication owner, reservations, exact staged bytes, verified object
packs and complete metadata transitions.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_identity

from .repository_file_custody import PhysicalNode
from .repository_lfs import require_hydrated_lfs_unchanged
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import require_nested_refresh_child_unchanged

_TERMINAL = frozenset({"committed", "rolled_back"})
_WORKTREE_INTERNALS = frozenset(
    {
        ".git",
        ".litai-locks",
        ".litai-cache-locks",
        ".literate.project.json.write.lock",
        ".literate/.repository-lock.write",
    }
)


def _is_windows() -> bool:
    return os.name == "nt"


def _requires_directory_transition(change) -> bool:
    previous = change.previous is not None and change.previous.mode == "040000"
    prospective = change.prospective is not None and change.prospective.mode == "040000"
    return previous != prospective


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError(
        "refresh_application_" + suffix, message
    ) from None


@dataclass(frozen=True, slots=True)
class RepositoryRefreshApplicationAuthority:
    """A complete live transaction selection, not serializable replay authority."""

    physical_custody: str
    object_custody: str
    metadata_identity: str

    @property
    def identity(self) -> str:
        return canonical_identity(
            {
                "physical_custody": self.physical_custody,
                "object_custody": self.object_custody,
                "metadata_identity": self.metadata_identity,
            }
        ).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/refresh-application-authority@1",
            "identity": self.identity,
            "physical_custody": self.physical_custody,
            "object_custody": self.object_custody,
            "metadata_identity": self.metadata_identity,
            "apply_supported": True,
            "writes": False,
            "execution": False,
        }


@dataclass(slots=True)
class AppliedRepositoryRefresh:
    state: str
    authority_identity: str
    changed: bool
    source_writes: bool = False
    cleanup_retained: tuple[str, ...] = ()

    def retain_cleanup(self, label: str) -> None:
        if label not in self.cleanup_retained:
            self.cleanup_retained = (*self.cleanup_retained, label)

    @property
    def authority_changed(self) -> bool:
        return self.changed

    @property
    def filesystem_writes(self) -> bool:
        return self.changed or self.source_writes or bool(self.cleanup_retained)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/refresh-application-result@1",
            "state": self.state,
            "authority_identity": self.authority_identity,
            "apply_supported": True,
            "changed": self.changed,
            "authority_changed": self.authority_changed,
            "filesystem_writes": self.filesystem_writes,
            "source_writes": self.source_writes,
            "cleanup_retained": list(self.cleanup_retained),
            "writes": self.filesystem_writes,
            "execution": False,
        }


@dataclass(frozen=True, slots=True)
class _State:
    kind: str
    content: bytes | None = None
    permissions: int | None = None


@dataclass(frozen=True, slots=True)
class _ObservedState:
    state: _State
    signature: tuple[int, ...] | None


@dataclass(slots=True)
class _OwnedNode:
    path: Path
    observed: _ObservedState
    descriptor: int | None


@dataclass(frozen=True, slots=True)
class _Undo:
    path: Path
    before: _State
    after: _State
    label: str


def _signature(node) -> tuple[int, ...]:
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
        node.st_nlink,
    )


def _observe(path: Path, expected: _State | None = None) -> _ObservedState:
    require_safe_directory(path.parent)
    try:
        node = path.lstat()
    except FileNotFoundError:
        return _ObservedState(_State("absent"), None)
    signature = _signature(node)
    if stat.S_ISLNK(node.st_mode):
        content = os.readlink(os.fsencode(path))
        if (
            expected is not None
            and expected.content is not None
            and len(content) > len(expected.content)
        ):
            return _ObservedState(_State("foreign"), signature)
        if _signature(path.lstat()) != signature:
            return _ObservedState(_State("foreign"), signature)
        return _ObservedState(_State("symlink", content), signature)
    if stat_is_link_or_reparse(node):
        return _ObservedState(_State("foreign"), signature)
    if stat.S_ISDIR(node.st_mode):
        return _ObservedState(
            _State(
                "directory",
                permissions=None if os.name == "nt" else stat.S_IMODE(node.st_mode),
            ),
            signature,
        )
    if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1:
        return _ObservedState(_State("foreign"), signature)
    maximum = None
    if expected is not None and expected.content is not None:
        maximum = len(expected.content)
        if node.st_size > maximum:
            return _ObservedState(_State("foreign"), signature)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if maximum is not None and opened.st_size > maximum:
            return _ObservedState(_State("foreign"), _signature(opened))
        chunks = []
        remaining = opened.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not _identity_matches(path, after, descriptor, opened_after)
            or _signature(opened) != _signature(opened_after)
            or len(content) != opened.st_size
        ):
            return _ObservedState(_State("foreign"), _signature(opened_after))
        return _ObservedState(
            _State(
                "file",
                content,
                None if os.name == "nt" else stat.S_IMODE(opened.st_mode),
            ),
            _signature(opened),
        )
    finally:
        os.close(descriptor)


def _state(path: Path, expected: _State | None = None) -> _State:
    return _observe(path, expected).state


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary(path: Path) -> Path:
    return path.parent / (".litai-refresh-" + secrets.token_hex(16))


def _require_hardlink_capability(stage) -> None:
    refresh = stage._files._owner._prepared.refresh
    previous = {
        item.path: item.commit for item in refresh.authority.previous.repositories
    }
    if all(
        previous[target.path] == target.commit
        for target in refresh.authority.request.targets
    ) and all(
        plan.previous == plan.prospective and not plan.changes
        for _path, plan in stage._files.plans
    ):
        return
    candidates = []
    source_roots = {plan.root for _path, plan in stage._files.plans}
    for observed in (
        refresh.root_git,
        *(item for item in refresh.children if item.root in source_roots),
    ):
        candidates.extend(
            (
                observed.root / ".litai-locks",
                observed.git_directory,
                observed.common_directory,
            )
        )
    selected = {}
    for directory in candidates:
        require_safe_directory(directory)
        selected.setdefault(directory.lstat().st_dev, directory)
    for directory in selected.values():
        source = directory / (".litai-link-source-" + secrets.token_hex(16))
        linked = directory / (".litai-link-target-" + secrets.token_hex(16))
        descriptor = -1
        signature = None
        try:
            descriptor = os.open(
                source,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.write(descriptor, b"literate-ai hardlink capability\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            signature = _signature(source.lstat())
            os.link(source, linked, follow_symlinks=False)
            if (
                source.lstat().st_ino != linked.lstat().st_ino
                or source.lstat().st_dev != linked.lstat().st_dev
            ):
                _fail(
                    "hardlink_unsupported",
                    "filesystem hardlink capability did not preserve identity",
                )
        except OrchestrationInventoryError:
            raise
        except OSError:
            _fail(
                "hardlink_unsupported",
                "filesystem cannot provide required no-clobber hardlinks",
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for path in (linked, source):
                try:
                    if os.path.lexists(path):
                        current = _signature(path.lstat())
                        if signature is None or current[:2] != signature[:2]:
                            _fail(
                                "hardlink_unsupported",
                                "hardlink capability probe custody changed",
                            )
                        path.unlink()
                except FileNotFoundError:
                    pass


def _before_materialize(_path: Path, _expected: _ObservedState) -> None:
    """Deterministic race-injection seam immediately before pathname mutation."""


def _before_discard(_owned: _OwnedNode) -> None:
    """Deterministic open-descriptor race seam before owned inode discard."""


def _before_terminal_journal(_application) -> None:
    """Deterministic race seam before committed terminal validation."""


def _moved_matches(current: _ObservedState, expected: _ObservedState) -> bool:
    if current.state != expected.state or current.signature is None:
        return False
    if expected.signature is None:
        return False
    # POSIX rename may advance ctime; every identity/content/mode/size/mtime/link
    # field that rename itself does not alter must remain exact.
    return (
        current.signature[:5] == expected.signature[:5]
        and current.signature[6:] == expected.signature[6:]
    )


def _remove_owned(path: Path, observed: _ObservedState) -> _OwnedNode:
    custody = _temporary(path)
    _before_materialize(path, observed)
    os.rename(path, custody)
    moved = _observe(custody, observed.state)
    if not _moved_matches(moved, observed):
        try:
            if moved.state.kind == "directory":
                os.rename(custody, path)
            else:
                os.link(custody, path, follow_symlinks=False)
                custody.unlink()
        except OSError:
            _fail(
                "foreign_change",
                "foreign replacement retained under live refresh custody",
            )
        _fail("foreign_change", "live refresh path changed before replacement")
    descriptor = None
    if moved.state.kind in {"file", "directory"}:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if moved.state.kind == "directory":
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(custody, flags)
        opened = os.fstat(descriptor)
        named = custody.lstat()
        if not _identity_matches(custody, named, descriptor, opened):
            os.close(descriptor)
            _fail("foreign_change", "renamed live node custody changed while opening")
    return _OwnedNode(custody, moved, descriptor)


def _owned_current(owned: _OwnedNode) -> bool:
    current = _observe(owned.path, owned.observed.state)
    if current != owned.observed:
        return False
    if owned.descriptor is None:
        return True
    try:
        named = owned.path.lstat()
        opened = os.fstat(owned.descriptor)
        return (
            _identity_matches(owned.path, named, owned.descriptor, opened)
            and _signature(opened) == owned.observed.signature
        )
    except OSError:
        return False


def _close_owned(owned: _OwnedNode) -> None:
    if owned.descriptor is not None:
        os.close(owned.descriptor)
        owned.descriptor = None


def _discard_owned(owned: _OwnedNode) -> None:
    if not _owned_current(owned):
        _close_owned(owned)
        _fail("foreign_change", "renamed live node changed before discard")
    _before_discard(owned)
    if not _owned_current(owned):
        _close_owned(owned)
        _fail("foreign_change", "renamed live node changed before discard")
    if _is_windows():
        # CRT descriptors do not share delete access. Close only after the final
        # descriptor-backed proof, then revalidate the named custody before the
        # pathname mutation that Windows otherwise rejects with ERROR_SHARING_VIOLATION.
        _close_owned(owned)
        if not _owned_current(owned):
            _fail("foreign_change", "renamed live node changed before discard")
    try:
        if owned.observed.state.kind == "directory":
            owned.path.rmdir()
        else:
            owned.path.unlink()
    finally:
        _close_owned(owned)


def _restore_owned(owned: _OwnedNode, path: Path) -> None:
    if os.path.lexists(path):
        _discard_owned(owned)
        return
    if not _owned_current(owned):
        _close_owned(owned)
        _fail("foreign_change", "renamed live node changed before restoration")
    if _is_windows():
        _close_owned(owned)
        if not _owned_current(owned):
            _fail("foreign_change", "renamed live node changed before restoration")
    if owned.observed.state.kind == "directory":
        os.rename(owned.path, path)
    else:
        os.link(owned.path, path, follow_symlinks=False)
        owned.path.unlink()
    _close_owned(owned)


def _publish_temporary(temporary: Path, path: Path) -> None:
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        _fail("foreign_change", "concurrent writer won live refresh publication")
    temporary.unlink()


def _materialize(
    path: Path,
    desired: _State,
    *,
    expected: _ObservedState | None = None,
    retain_custody: list[_OwnedNode] | None = None,
) -> None:
    require_safe_directory(path.parent)
    observed = _observe(path, expected.state if expected is not None else None)
    if expected is not None and observed != expected:
        _fail("foreign_change", "live refresh path changed before materialization")
    expected = observed
    custody: _OwnedNode | None = None
    if observed.state.kind != "absent":
        custody = _remove_owned(path, observed)
    else:
        _before_materialize(path, observed)
    if desired.kind == "absent":
        if custody is not None:
            if retain_custody is None:
                _discard_owned(custody)
            else:
                retain_custody.append(custody)
                custody = None
        _sync_directory(path.parent)
        return
    if desired.kind == "directory":
        try:
            path.mkdir(mode=desired.permissions or 0o755)
            if os.name != "nt":
                path.chmod(desired.permissions or 0o755)
        except BaseException:
            if custody is not None:
                _restore_owned(custody, path)
            raise
        if custody is not None:
            if retain_custody is None:
                _discard_owned(custody)
            else:
                retain_custody.append(custody)
                custody = None
        _sync_directory(path.parent)
        return
    temporary = _temporary(path)
    published = False
    try:
        if desired.kind == "symlink":
            os.symlink(desired.content or b"", os.fsencode(temporary))
        elif desired.kind == "file":
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                desired.permissions or 0o644,
            )
            try:
                content = desired.content or b""
                written = 0
                while written < len(content):
                    count = os.write(
                        descriptor, memoryview(content)[written : written + 65536]
                    )
                    if count <= 0:
                        _fail("write_failed", "live write made no progress")
                    written += count
                if os.name != "nt":
                    os.fchmod(descriptor, desired.permissions or 0o644)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            _fail("state_invalid", "unsupported live repository node state")
        _publish_temporary(temporary, path)
        published = True
        if custody is not None:
            if retain_custody is None:
                _discard_owned(custody)
            else:
                retain_custody.append(custody)
                custody = None
        _sync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()
        if custody is not None and os.path.lexists(custody.path):
            if published:
                _discard_owned(custody)
            else:
                _restore_owned(custody, path)


def _physical_before(node: PhysicalNode, content: bytes | None) -> _State:
    if node.kind == "absent":
        return _State("absent")
    if node.kind == "directory" and node.signature is not None:
        return _State(
            "directory",
            permissions=None if os.name == "nt" else stat.S_IMODE(node.signature[2]),
        )
    if node.kind == "symlink":
        return _State("symlink", content)
    if node.kind == "file" and node.signature is not None:
        return _State(
            "file",
            content,
            None if os.name == "nt" else stat.S_IMODE(node.signature[2]),
        )
    _fail("state_invalid", "staged physical before-state is unsupported")


def _tree_after(entry, content: bytes | None) -> _State:
    if entry is None:
        return _State("absent")
    if entry.mode == "040000":
        return _State("directory", permissions=None if os.name == "nt" else 0o755)
    if entry.mode == "120000":
        return _State("symlink", content)
    if entry.mode == "100644":
        return _State("file", content, None if os.name == "nt" else 0o644)
    if entry.mode == "100755":
        return _State("file", content, None if os.name == "nt" else 0o755)
    _fail("state_invalid", "unsupported prospective worktree mode")


def _metadata_state(content: bytes | None, before) -> _State:
    if content is None:
        return _State("absent")
    permissions = (
        None
        if os.name == "nt"
        else stat.S_IMODE(before.signature[2])
        if before.signature is not None
        else 0o644
    )
    return _State("file", content, permissions)


def _physical_tree_paths(
    root: Path,
    maximum_entries: int,
    opaque_directories: frozenset[str],
    ignored: frozenset[Path],
) -> set[str]:
    paths: set[str] = set()
    pending = [(root, PurePosixPath())]
    while pending:
        directory, relative = pending.pop()
        require_safe_directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                if Path(entry.path) in ignored:
                    continue
                path = (relative / entry.name).as_posix()
                if path in _WORKTREE_INTERNALS:
                    continue
                paths.add(path)
                if len(paths) > maximum_entries:
                    _fail(
                        "tree_changed",
                        "live worktree membership exceeds its reviewed bound",
                    )
                node = os.lstat(entry.path)
                if (
                    path not in opaque_directories
                    and stat.S_ISDIR(node.st_mode)
                    and not stat_is_link_or_reparse(node)
                ):
                    pending.append((Path(entry.path), relative / entry.name))
    return paths


def _entry_state(entry, physical_mode: int | None) -> _State:
    if entry.mode == "040000":
        return _State("directory", permissions=physical_mode)
    if entry.mode == "120000":
        return _State("symlink", entry.content)
    if entry.mode == "100644":
        return _State("file", entry.content, physical_mode)
    if entry.mode == "100755":
        return _State("file", entry.content, physical_mode)
    _fail("tree_changed", "nested Gitlinks have no live source ownership")


def _affected_paths(plan) -> set[str]:
    result = set()
    for change in plan.changes:
        path = PurePosixPath(change.path)
        result.add(path.as_posix())
        result.update(parent.as_posix() for parent in path.parents)
    return result


def _require_worktree_state(application, plan, *, prospective: bool) -> None:
    snapshot = plan.prospective if prospective else plan.previous
    observations = {
        item.root: item for item in application.owner._prepared.refresh.children
    }
    nested = {}
    for entry in snapshot.entries:
        if entry.mode != "160000":
            continue
        observed = observations.get(plan.root / entry.path)
        if observed is None:
            _fail("tree_changed", "nested Gitlink custody is unavailable")
        nested[entry.path] = observed
    expected = {entry.path: entry for entry in snapshot.entries}
    root = plan.root.lstat()
    if (root.st_dev, root.st_ino, root.st_mode) != plan.root_node:
        _fail("tree_changed", "live worktree root custody changed")
    paths = _physical_tree_paths(
        plan.root,
        plan.policy.maximum_entries,
        frozenset(nested),
        frozenset(item.path for item in application._retained_custody),
    )
    if paths != set(expected):
        unexpected = sorted(paths - set(expected))[:3]
        missing = sorted(set(expected) - paths)[:3]
        _fail(
            "tree_changed",
            "live worktree directory membership changed "
            f"(unexpected={unexpected!r}, missing={missing!r})",
        )
    changes = {change.path: change for change in plan.changes}
    directory_modes = {
        item.path: None if os.name == "nt" else stat.S_IMODE(item.signature[2])
        for item in plan.directories
    }
    member_modes = {
        (PurePosixPath(directory.path) / os.fsdecode(name)).as_posix(): stat.S_IMODE(
            signature[2]
        )
        for directory in plan.directories
        for name, signature in directory.members
    }
    direct = observations[plan.root]
    hydrated = {
        item.path: item for item in direct.hydrated_lfs if item.path not in changes
    }
    for path, entry in expected.items():
        if entry.mode == "160000":
            observed = nested[path]
            current = (plan.root / path).lstat()
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino, current.st_mode)
                != observed.root_node
            ):
                _fail("tree_changed", "opaque nested Gitlink custody changed")
            require_nested_refresh_child_unchanged(
                observed, reservations=application.reservations
            )
            continue
        if path in hydrated:
            try:
                require_hydrated_lfs_unchanged(plan.root, hydrated[path])
            except (OSError, UnsafeFilesystemPathError):
                _fail("tree_changed", "admitted hydrated LFS payload changed")
            continue
        mode = directory_modes.get(path)
        change = changes.get(path)
        if os.name != "nt" and plan.changes and entry.mode in {"100644", "100755"}:
            # Git records executable intent, whereas preparation admitted the
            # checkout's exact permissions (including umask/shared-repo bits).
            mode = (
                (0o755 if entry.mode == "100755" else 0o644)
                if prospective and change is not None
                else member_modes[path]
            )
        if (
            prospective
            and entry.mode == "040000"
            and change is not None
            and (change.previous is None or change.previous.mode != "040000")
        ):
            mode = None if os.name == "nt" else 0o755
        expected_state = _entry_state(entry, mode)
        if not plan.changes and os.name != "nt" and entry.mode != "120000":
            # A no-op physical plan intentionally has no directory inventory.
            # Admit its exact permissions once at application entry, then retain
            # that expectation through commit/rollback and terminal validation.
            live_path = plan.root / path
            admitted = application._noop_states.get(live_path)
            if admitted is None:
                observed = _state(live_path, expected_state)
                if entry.mode in {"100644", "100755"} and (
                    observed.permissions is None
                    or bool(observed.permissions & 0o100) != (entry.mode == "100755")
                ):
                    _fail("tree_changed", "live executable intent changed")
                admitted = _State(
                    expected_state.kind, expected_state.content, observed.permissions
                )
                if observed != admitted:
                    _fail("tree_changed", "live tracked worktree state changed")
                application._noop_states[live_path] = admitted
            expected_state = admitted
        if _state(plan.root / path, expected_state) != expected_state:
            _fail("tree_changed", "live tracked worktree state changed")

    # Unchanged members retain exact prepared node custody, not merely matching
    # bytes. Changed paths and their ancestors legitimately acquire new mtimes or
    # inodes during application/rollback.
    affected = _affected_paths(plan)
    for directory in plan.directories:
        if directory.path not in affected:
            if _signature((plan.root / directory.path).lstat()) != directory.signature:
                _fail("tree_changed", "unchanged directory custody changed")
        for raw_name, signature in directory.members:
            member = (PurePosixPath(directory.path) / os.fsdecode(raw_name)).as_posix()
            if member in affected or member in _WORKTREE_INTERNALS:
                continue
            if _signature((plan.root / member).lstat()) != signature:
                _fail("tree_changed", "unchanged worktree member custody changed")


class _LiveRefreshApplication:
    def __init__(self, stage, authority):
        self.stage = stage
        self.authority = authority
        self.owner = stage._files._owner
        self.reservations = self.owner._reservations
        self.state = "applying"
        self._undos: list[_Undo] = []
        self._targets: dict[Path, tuple[_State, _State, str]] = {}
        self._installed_objects = None
        self._retained_custody: list[_OwnedNode] = []
        self._noop_states: dict[Path, _State] = {}
        self._result: AppliedRepositoryRefresh | None = None

    @property
    def changed_repositories(self) -> frozenset[str]:
        refresh = self.owner._prepared.refresh
        previous = {
            item.path: item.commit for item in refresh.authority.previous.repositories
        }
        return frozenset(
            target.path
            for target in refresh.authority.request.targets
            if previous[target.path] != target.commit
        )

    @property
    def source_writes(self) -> bool:
        return any(
            plan.previous != plan.prospective or bool(plan.changes)
            for _path, plan in self.stage._files.plans
        )

    def _register(self, path: Path, before: _State, after: _State, label: str) -> None:
        previous = self._targets.get(path)
        value = (before, after, label)
        if previous is not None:
            if previous == value:
                return
            if previous[1] == before and previous[2] == label:
                self._targets[path] = (previous[0], after, label)
                return
            _fail("target_conflict", "transaction paths have conflicting states")
        self._targets[path] = value

    def require_coherent(self) -> None:
        self.reservations.verify_all()
        self.owner.require_application_inputs(self)
        for path, (before, after, _label) in self._targets.items():
            if _state(path) not in {before, after}:
                _fail(
                    "foreign_change",
                    "live refresh path differs from both before and prospective state",
                )
        self.reservations.verify_all()
        self.owner.require_application_inputs(self)

    def require_worktrees(self, *, prospective: bool) -> None:
        self.reservations.verify_all()
        for _repository, plan in self.stage._files.plans:
            _require_worktree_state(self, plan, prospective=prospective)
        self.reservations.verify_all()

    def require_terminal(self) -> None:
        if self.state not in _TERMINAL:
            _fail("incomplete", "refresh application has no proven terminal state")
        expected_index = 1 if self.state == "committed" else 0
        self.reservations.verify_all()
        if self.state == "committed":
            self.owner.require_application_inputs(self)
        for path, states in self._targets.items():
            if _state(path, states[expected_index]) != states[expected_index]:
                _fail(
                    "terminal_changed",
                    "terminal repository refresh state changed or is incomplete",
                )
        self.require_worktrees(prospective=self.state == "committed")
        if self.source_writes:
            if self._installed_objects is None:
                _fail("objects_changed", "installed object ownership is unavailable")
            if self._installed_objects._active:
                self._installed_objects.require_owned_objects_current()
            elif not self._installed_objects._terminal_verified:
                _fail("objects_changed", "terminal object proof is unavailable")
        self.reservations.verify_all()

    def transition(self, path: Path, before: _State, after: _State, label: str) -> None:
        self._register(path, before, after, label)
        if before == after:
            return
        observed = _observe(path, before)
        if observed.state != before:
            _fail("foreign_change", f"{label} changed before application")
        undo = _Undo(path, before, after, label)
        self._undos.append(undo)
        _materialize(
            path,
            after,
            expected=observed,
            retain_custody=self._retained_custody,
        )
        if _state(path, after) != after:
            _fail("write_failed", f"{label} did not reach its prospective state")

    def rollback(self) -> None:
        failures = []
        for undo in reversed(self._undos):
            observed = _observe(undo.path, undo.after)
            current = observed.state
            if current == undo.before:
                continue
            if current != undo.after:
                failures.append(undo.label)
                continue
            try:
                _materialize(
                    undo.path,
                    undo.before,
                    expected=observed,
                    retain_custody=self._retained_custody,
                )
                if _state(undo.path, undo.before) != undo.before:
                    failures.append(undo.label)
            except (OSError, OrchestrationInventoryError):
                failures.append(undo.label)
        if failures:
            _fail(
                "rollback_incomplete",
                "foreign or unavailable paths were preserved during rollback: "
                + ", ".join(failures[:3]),
            )

    def discard_replaced_custody(self) -> None:
        while self._retained_custody:
            owned = self._retained_custody[-1]
            _discard_owned(owned)
            self._retained_custody.pop()

    def retain_cleanup(self, label: str) -> None:
        if self.state != "committed" or self._result is None:
            _fail("state_invalid", "cleanup retention requires a committed result")
        self._result.retain_cleanup(label)


def prepare_refresh_application(stage) -> RepositoryRefreshApplicationAuthority:
    """Admit only the complete, still-live public-ready transaction machinery."""
    from .repository_refresh_staging import StagedRefresh

    if not isinstance(stage, StagedRefresh):
        raise TypeError("refresh application requires live owned staging")
    stage.require_current()
    if stage._objects is None or not stage.metadata:
        _fail(
            "incomplete",
            "application requires verified object packs and staged metadata",
        )
    expected = {"lock", "manifest"}
    if not expected.issubset({item.kind for item in stage.metadata}):
        _fail("incomplete", "application metadata is missing terminal root transitions")
    if _is_windows() and any(
        any(
            entry is not None and entry.mode == "120000"
            for entry in (change.previous, change.prospective)
        )
        for _repository, plan in stage._files.plans
        for change in plan.changes
    ):
        _fail(
            "platform_unsupported",
            "symlink refresh transitions are unsupported on Windows",
        )
    if _is_windows() and any(
        _requires_directory_transition(change)
        for _repository, plan in stage._files.plans
        for change in plan.changes
    ):
        _fail(
            "platform_unsupported",
            "directory refresh transitions are unsupported on Windows",
        )
    _require_hardlink_capability(stage)
    return RepositoryRefreshApplicationAuthority(
        stage._files.identity,
        stage._objects.identity,
        canonical_identity([item.to_dict() for item in stage.metadata]).uri,
    )


def _apply_worktree(application: _LiveRefreshApplication, repository, plan) -> None:
    stage = application.stage
    nodes = {node.path: node for node in plan.nodes}
    changes = {change.path: change for change in plan.changes}
    before = {}
    after = {}
    for path, change in changes.items():
        node = nodes[path]
        before_content = (
            stage.read_entry("before-file", repository, path)
            if node.content is not None
            else None
        )
        prospective_content = (
            stage.read_entry("prospective-file", repository, path)
            if change.prospective is not None and change.prospective.content is not None
            else None
        )
        before[path] = _physical_before(node, before_content)
        after[path] = _tree_after(change.prospective, prospective_content)

    incompatible = [
        path
        for path in changes
        if before[path].kind != after[path].kind and before[path].kind != "absent"
    ]
    removed = [
        path
        for path in changes
        if after[path].kind == "absent" and path not in incompatible
    ]
    for path in sorted((*incompatible, *removed), key=lambda p: (-p.count("/"), p)):
        application.transition(
            plan.root / path, before[path], _State("absent"), f"{repository}:{path}"
        )
    for path in sorted(
        (
            path
            for path in changes
            if after[path].kind == "directory" and before[path].kind != "directory"
        ),
        key=lambda p: (p.count("/"), p),
    ):
        application.transition(
            plan.root / path, _State("absent"), after[path], f"{repository}:{path}"
        )
    for path in sorted(
        path for path in changes if after[path].kind in {"file", "symlink"}
    ):
        initial = _State("absent") if path in incompatible else before[path]
        application.transition(
            plan.root / path, initial, after[path], f"{repository}:{path}"
        )


def _apply_metadata(application, transition) -> None:
    stage = application.stage
    before_content = (
        stage.read_entry(
            "metadata-before-" + transition.kind,
            transition.repository,
            transition.name,
        )
        if transition.before.content is not None
        else None
    )
    after_content = (
        stage.read_entry(
            "metadata-prospective-" + transition.kind,
            transition.repository,
            transition.name,
        )
        if transition.prospective is not None
        else None
    )
    application.transition(
        transition.before.path,
        _metadata_state(before_content, transition.before),
        _metadata_state(after_content, transition.before),
        f"{transition.repository}:{transition.name}",
    )


def _apply_live_changes(application: _LiveRefreshApplication) -> None:
    stage = application.stage
    refresh = stage._files._owner._prepared.refresh
    observations = {item.root: item for item in refresh.children}
    metadata = tuple(stage.metadata)

    for repository, plan in stage._files.plans:
        _apply_worktree(application, repository, plan)
        observation = observations[plan.root]
        application.transition(
            observation.index.path,
            _State(
                "file",
                stage.read_entry("before-index", repository, "index"),
                (
                    None
                    if os.name == "nt"
                    else stat.S_IMODE(observation.index.signature[2])
                ),
            ),
            _State(
                "file",
                stage.read_entry("prospective-index", repository, "index"),
                (
                    None
                    if os.name == "nt"
                    else stat.S_IMODE(observation.index.signature[2])
                ),
            ),
            f"{repository}:index",
        )
        for transition in metadata:
            if transition.repository == repository and transition.kind in {
                "head",
                "ref",
                "packed-refs",
            }:
                _apply_metadata(application, transition)
        for transition in metadata:
            if transition.repository == repository and transition.kind == "reflog":
                _apply_metadata(application, transition)

    application.transition(
        refresh.root_git.index.path,
        _State(
            "file",
            stage.read_entry("before-index", ".", "index"),
            (
                None
                if os.name == "nt"
                else stat.S_IMODE(refresh.root_git.index.signature[2])
            ),
        ),
        _State(
            "file",
            stage.read_entry("prospective-index", ".", "index"),
            (
                None
                if os.name == "nt"
                else stat.S_IMODE(refresh.root_git.index.signature[2])
            ),
        ),
        ".:index",
    )
    lock = next(item for item in metadata if item.kind == "lock")
    manifest = next(item for item in metadata if item.kind == "manifest")
    _apply_metadata(application, lock)
    application.require_worktrees(prospective=True)
    _apply_metadata(application, manifest)


def apply_repository_refresh(stage) -> AppliedRepositoryRefresh:
    """Apply one complete live stage; rollback conservatively on every failure."""
    authority = prepare_refresh_application(stage)
    stage.arm_recovery()
    application = _LiveRefreshApplication(stage, authority)
    stage.bind_application(application)
    application.owner.bind_application(application)
    terminal_recorded = False

    def commit() -> AppliedRepositoryRefresh:
        nonlocal terminal_recorded
        application.require_coherent()
        application.owner.renew_application_publication(application)
        application.require_coherent()
        _before_terminal_journal(application)
        application.state = "committed"
        application.require_terminal()
        application.discard_replaced_custody()
        application.require_terminal()
        transfer = application.reservations.prepare_committed_directory_transfer(
            tuple(
                path
                for path, (before, after, _label) in application._targets.items()
                if before.kind == "absent" and after.kind != "absent"
            )
        )
        if application._installed_objects is not None:
            application._installed_objects.require_owned_objects_current()
        stage.record_terminal("committed")
        if application._installed_objects is not None:
            application._installed_objects._terminal_verified = True
        terminal_recorded = True
        application.reservations.commit_directory_transfer(transfer)
        application._result = AppliedRepositoryRefresh(
            "committed",
            authority.identity,
            bool(application.changed_repositories),
            application.source_writes,
        )
        return application._result

    def execute() -> AppliedRepositoryRefresh:
        try:
            application.require_worktrees(prospective=False)
            _apply_live_changes(application)
            return commit()
        except BaseException as error:
            if terminal_recorded or stage._application_state == "committed":
                stage.retain_terminal_failure()
                raise
            try:
                application.rollback()
                application.state = "rolled_back"
                application.require_terminal()
                application.discard_replaced_custody()
                application.require_terminal()
                stage.record_terminal("rolled_back")
                if application._installed_objects is not None:
                    application._installed_objects._terminal_verified = True
            except BaseException as rollback_error:
                application.state = "applying"
                raise OrchestrationInventoryError(
                    "refresh_application_rollback_incomplete",
                    "refresh failed ("
                    + str(error)
                    + ") and conservative rollback retained recovery staging: "
                    + str(rollback_error),
                ) from rollback_error
            raise error

    if application.source_writes:
        try:
            with stage.installed_objects() as installed:
                application._installed_objects = installed
                return execute()
        except BaseException:
            if stage._application_state == "committed":
                stage.retain_terminal_failure()
            raise
    return execute()


apply_staged_refresh = apply_repository_refresh

__all__ = [
    "AppliedRepositoryRefresh",
    "RepositoryRefreshApplicationAuthority",
    "apply_repository_refresh",
    "apply_staged_refresh",
    "prepare_refresh_application",
]

"""Streaming, revalidated custody of explicitly selected retained input roots."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
    RetainedProjectTreePolicy,
)

_CHUNK = 1024 * 1024


class RetainedProjectInputError(ValueError):
    """An input could not be captured under its reviewed identity."""


class _DirectoryAccess:
    """Bounded, transaction-local handles; current names are checked on every use.

    Handles are never evidence by themselves: root and relative directory
    identities must still match the no-follow lookup through their current parent.
    Cached lineages are checked again before successful transaction completion.
    """

    def __init__(self, root: Path, budget: int):
        self.root = root
        self._root_parts = root.parts
        self._last_path_parts = None
        self._last_relative_parts = None
        self._absolute = []
        self._cached = OrderedDict()
        self._flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self._flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(root.anchor, self._flags)
            try:
                identity = self._identity(os.fstat(descriptor))
            except BaseException:
                os.close(descriptor)
                raise
            self._absolute.append((root.anchor, descriptor, identity))
            for name in root.parts[1:]:
                parent = self._absolute[-1][1]
                current = self._identity(
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                )
                child = self._open_directory(parent, name, current)
                self._absolute.append((name, child, current))
            self._capacity = budget - len(self._absolute)
            self._root_current()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(metadata):
        if not stat.S_ISDIR(metadata.st_mode):
            raise RetainedProjectInputError("Retained ancestor is redirected")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        )

    @staticmethod
    def _observed_identity(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        )

    @staticmethod
    def _root_change(*, reason, depth, name, expected, actual=None):
        def encoded(value):
            return "unavailable" if value is None else ":".join(map(str, value))

        return RetainedProjectInputError(
            "Retained root ancestor changed "
            f"(reason={reason}; depth={depth}; name={name!r}; "
            f"expected={encoded(expected)}; actual={encoded(actual)})"
        )

    def _require_root_identity(self, *, depth, name, expected, parent=None):
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as error:
            raise self._root_change(
                reason="missing", depth=depth, name=name, expected=expected
            ) from error
        try:
            actual = self._identity(current)
        except RetainedProjectInputError as error:
            raise self._root_change(
                reason="redirected",
                depth=depth,
                name=name,
                expected=expected,
                actual=self._observed_identity(current),
            ) from error
        if actual != expected:
            raise self._root_change(
                reason="identity",
                depth=depth,
                name=name,
                expected=expected,
                actual=actual,
            )

    def _open_directory(self, parent, name, expected):
        descriptor = os.open(name, self._flags, dir_fd=parent)
        try:
            if self._identity(os.fstat(descriptor)) != expected:
                raise RetainedProjectInputError("Retained ancestor changed during open")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _root_current(self):
        first = self._absolute[0]
        self._require_root_identity(depth=0, name=first[0], expected=first[2])
        for depth, (parent, child) in enumerate(
            zip(self._absolute, self._absolute[1:], strict=False), start=1
        ):
            self._require_root_identity(
                depth=depth,
                name=child[0],
                expected=child[2],
                parent=parent[1],
            )

    def directory(self, parts):
        self._root_current()
        descriptor = self._absolute[-1][1]
        lineage = []
        for index, name in enumerate(parts):
            key = parts[: index + 1]
            current = self._identity(
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            )
            lineage.append(current)
            expected = tuple(lineage)
            cached = self._cached.pop(key, None)
            if cached is not None:
                if cached[1] != expected:
                    os.close(cached[0])
                    raise RetainedProjectInputError(
                        "Retained directory lineage changed"
                    )
                self._cached[key] = cached
            else:
                while len(self._cached) >= self._capacity:
                    oldest = next(
                        k for k, v in self._cached.items() if v[0] != descriptor
                    )
                    os.close(self._cached.pop(oldest)[0])
                child = self._open_directory(descriptor, name, current)
                self._cached[key] = (child, expected)
            descriptor = self._cached[key][0]
        return descriptor, tuple(lineage)

    def parent(self, path):
        # This cache contains only immutable lexical parsing, never filesystem
        # observations. Python 3.12 relative_to walks parents; avoid repeating
        # that walk and portable-path parsing around each read of the same file.
        path_parts = path.parts
        if path_parts != self._last_path_parts:
            if path_parts[: len(self._root_parts)] != self._root_parts:
                raise RetainedProjectInputError("Retained entry is outside its root")
            parts = path_parts[len(self._root_parts) :]
            canonical_relative_posix_paths(("/".join(parts),), label="retained entry")
            self._last_path_parts = path_parts
            self._last_relative_parts = parts
        parts = self._last_relative_parts
        descriptor, _ = self.directory(parts[:-1])
        return descriptor

    def metadata(self, path):
        return os.stat(path.name, dir_fd=self.parent(path), follow_symlinks=False)

    def require_current(self):
        previous = tuple((parts, value[1]) for parts, value in self._cached.items())
        for parts, expected in previous:
            if self.directory(parts)[1] != expected:
                raise RetainedProjectInputError("Retained directory lineage changed")
        self._root_current()

    def close(self):
        for descriptor, _ in self._cached.values():
            os.close(descriptor)
        self._cached.clear()
        for _, descriptor, _ in reversed(self._absolute):
            os.close(descriptor)
        self._absolute.clear()


@contextmanager
def _directory_access(root):
    if os.name == "nt":
        yield None
        return
    import resource

    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    budget = 32 if soft == resource.RLIM_INFINITY else min(32, max(0, (soft - 32) // 4))
    if len(root.parts) + 2 > budget:
        yield None
        return
    try:
        access = _DirectoryAccess(root, budget)
    except OSError as error:
        if error.errno not in (errno.EMFILE, errno.ENFILE):
            raise
        yield None
        return
    try:
        yield access
        access.require_current()
    finally:
        access.close()


def _remove_tree(path: Path) -> None:
    """Remove a capture tree, including read-only blobs Windows refuses to unlink."""

    def writable_retry(function, name, _error):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable_retry)
    else:
        shutil.rmtree(path, onerror=writable_retry)


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


@contextmanager
def _parent_descriptor(path: Path, access=None):
    if access is not None:
        yield access.parent(path)
        return
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        yield parent
    finally:
        os.close(parent)


def _open_file(path: Path, access=None) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if os.name == "nt":
        # Windows has no portable openat; reparse/ancestor checks bracket the read.
        return os.open(path, flags)
    with _parent_descriptor(path, access) as parent:
        return os.open(path.name, flags, dir_fd=parent)


def _link_bytes(root: Path, name: str, limit: int, access=None) -> bytes:
    if os.name == "nt":
        raise RetainedProjectInputError("POSIX symlink custody requires a POSIX worker")
    path = root / name
    if access is None:
        require_safe_directory(path.parent)
        before = path.lstat()
    else:
        before = access.metadata(path)
    if not stat.S_ISLNK(before.st_mode) or before.st_size > limit:
        raise RetainedProjectInputError("Retained symlink type or size is inadmissible")
    with _parent_descriptor(path, access) as parent:
        if _stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _stamp(
            before
        ):
            raise RetainedProjectInputError("Retained symlink changed before read")
        data = os.readlink(path.name, dir_fd=parent).encode("utf-8")
        if _stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _stamp(
            before
        ):
            raise RetainedProjectInputError("Retained symlink changed during read")
    if access is None:
        require_safe_directory(path.parent)
        current = path.lstat()
    else:
        current = access.metadata(path)
    if len(data) > min(limit, 4096) or _stamp(current) != _stamp(before):
        raise RetainedProjectInputError(
            "Retained symlink changed or exceeds byte bound"
        )
    return data


def _stream(
    root: Path,
    name: str,
    limit: int,
    output: BinaryIO | None = None,
    *,
    access=None,
    windows_executable: bool = False,
) -> tuple[int, int, str]:
    path = root / name
    if access is None:
        require_safe_directory(path.parent)
        metadata = path.lstat()
    else:
        metadata = access.metadata(path)
    if stat.S_ISLNK(metadata.st_mode):
        data = _link_bytes(root, name, limit, access)
        if output is not None:
            output.write(data)
        return len(data), 0o120000, hashlib.sha256(data).hexdigest()
    if access is None:
        path = _safe(root, name)
        before = path.lstat()
    else:
        before = metadata
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise RetainedProjectInputError("Retained input type or size is inadmissible")
    descriptor = _open_file(path, access)
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(descriptor, "rb") as stream:
        if not _same_open_file(before, os.fstat(stream.fileno())) or _stamp(
            before
        ) != _stamp(
            _safe(root, name).lstat() if access is None else access.metadata(path)
        ):
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
        or _stamp(before)
        != _stamp(
            _safe(root, name).lstat() if access is None else access.metadata(path)
        )
        or count != before.st_size
    ):
        raise RetainedProjectInputError("Retained input changed during read")
    executable = bool(before.st_mode & 0o111)
    if os.name == "nt":
        # NTFS has no POSIX execute bit. Discovery supplies reviewed intent;
        # verification reuses only the sealed manifest selection.
        executable = windows_executable
    return count, 0o755 if executable else 0o644, digest.hexdigest()


def _inventory(
    root: Path,
    roots: tuple[str, ...],
    limit: int,
    *,
    allow_internal_symlinks: bool = False,
    access=None,
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
        path = root / name
        if access is None:
            require_safe_directory(path.parent)
            metadata = path.lstat()
        else:
            metadata = access.metadata(path)
        if stat.S_ISLNK(metadata.st_mode) and allow_internal_symlinks:
            names.append(name)
            return  # The link is an input; never traverse its target.
        if access is None:
            path = _safe(root, name)
        elif stat.S_ISLNK(metadata.st_mode):
            raise RetainedProjectInputError("Retained input is redirected")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(name)
            # Do not materialize an unbounded scandir listing before checking bounds.
            if os.name != "nt":
                descriptor = _open_file(path, access)
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
            current = (
                _safe(root, name).lstat() if access is None else access.metadata(path)
            )
            if _stamp(metadata) != _stamp(current):
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


def _hardlink_topology(root, names, access):
    """Observe complete physical groups without following symbolic inputs."""
    if os.name == "nt":
        raise RetainedProjectInputError(
            "Faithful retained topology requires a POSIX worker"
        )
    groups = {}
    stamps = {}
    for name in names:
        value = (
            access.metadata(root / name)
            if access is not None
            else _safe(root, name).lstat()
        )
        if stat.S_ISREG(value.st_mode):
            groups.setdefault((value.st_dev, value.st_ino), []).append(name)
            stamps[name] = (*_stamp(value), value.st_nlink)
    aliases = {}
    for group in groups.values():
        first = min(group)
        if any(stamps[name][-1] != len(group) for name in group):
            raise RetainedProjectInputError("Retained hard link group is incomplete")
        aliases.update((name, first) for name in group if name != first)
    return aliases, stamps


def _require_case_sensitive(root: Path) -> None:
    probe = root / ".case-probe"
    probe.mkdir()
    try:
        (probe / "Aa").write_bytes(b"")
        try:
            with (probe / "aa").open("xb"):
                pass
        except FileExistsError as error:
            raise ValueError(
                "Input requires a case-sensitive destination filesystem"
            ) from error
        if (probe / "Aa").stat().st_ino == (probe / "aa").stat().st_ino:
            raise ValueError("Input requires distinct case-sensitive directory entries")
    finally:
        _remove_tree(probe)


def discover_retained_project_inputs(
    root: Path,
    roots: Mapping[str, str],
    limits: RetainedProjectLimits,
    *,
    allow_internal_symlinks: bool = False,
    tree_policy: RetainedProjectTreePolicy | None = None,
    executable_paths: tuple[str, ...] = (),
) -> RetainedProjectManifest:
    """Read a complete declared closure; the caller must review the result."""
    root = _root(root)
    if tree_policy is not None and not isinstance(
        tree_policy, RetainedProjectTreePolicy
    ):
        raise TypeError("Typed retained tree policy required")
    if type(allow_internal_symlinks) is not bool:
        raise TypeError(
            "Retained symlink admission requires explicit boolean selection"
        )
    if type(executable_paths) is not tuple:
        raise TypeError("Retained executable paths require an explicit tuple")
    canonical_relative_posix_paths(executable_paths, label="retained executables")
    if executable_paths != tuple(sorted(set(executable_paths))):
        raise RetainedProjectInputError(
            "Retained executable paths must be sorted and unique"
        )
    selected = tuple(sorted(roots))
    if tree_policy is None or not tree_policy.case_sensitive:
        canonical_relative_posix_paths(selected, label="retained roots")
    else:
        for name in selected:
            canonical_relative_posix_paths((name,), label="retained roots")
    with _directory_access(root) as access:
        names, directories = _inventory(
            root,
            selected,
            limits.max_entries,
            allow_internal_symlinks=allow_internal_symlinks,
            access=access,
        )
        executable_set = set(executable_paths)
        if not executable_set <= set(names):
            raise RetainedProjectInputError(
                "Retained executable path is absent from the selected inputs"
            )
        topology = (
            _hardlink_topology(root, names, access) if tree_policy is not None else None
        )
        members = []
        total = 0
        physical = {}
        for name in names:
            representative = (
                topology[0].get(name, name) if topology is not None else name
            )
            if representative in physical:
                size, mode, digest = physical[representative]
                if size > min(limits.max_file_bytes, limits.max_total_bytes - total):
                    raise RetainedProjectInputError("Retained inputs exceed byte bound")
            else:
                size, mode, digest = _stream(
                    root,
                    name,
                    min(limits.max_file_bytes, limits.max_total_bytes - total),
                    access=access,
                    windows_executable=name in executable_set,
                )
                physical[name] = (size, mode, digest)
            total += size
            role = next(
                role
                for prefix, role in roots.items()
                if name == prefix or name.startswith(prefix + "/")
            )
            target = None
            if mode == 0o120000:
                if name in executable_set:
                    raise RetainedProjectInputError(
                        "Retained executable path must name an ordinary file"
                    )
                target = _link_bytes(root, name, size, access).decode("utf-8")
            elif name in executable_set and mode != 0o755:
                raise RetainedProjectInputError(
                    "Retained executable path lacks native execute permission"
                )
            members.append(
                RetainedProjectMember(
                    name,
                    size,
                    mode,
                    digest,
                    role,
                    target,
                    topology[0].get(name) if topology is not None else None,
                )
            )
        if topology is not None and _hardlink_topology(root, names, access) != topology:
            raise RetainedProjectInputError(
                "Retained hard link topology changed during discovery"
            )
        manifest = RetainedProjectManifest(
            selected, tuple(members), limits, directories, tree_policy=tree_policy
        )
        _verify_inputs(root, manifest, access)
        return manifest


def verify_retained_project_inputs(
    root: Path, manifest: RetainedProjectManifest
) -> None:
    """Reject any extra, missing, redirected or changed input, including empty dirs."""
    root = _root(root)
    with _directory_access(root) as access:
        _verify_inputs(root, manifest, access)


def _verify_inputs(root, manifest, access):
    allow_links = any(m.symlink_target is not None for m in manifest.members)
    names, directories = _inventory(
        root,
        manifest.roots,
        manifest.limits.max_entries,
        allow_internal_symlinks=allow_links,
        access=access,
    )
    if (
        names != tuple(m.path for m in manifest.members)
        or directories != manifest.directories
    ):
        raise RetainedProjectInputError("Retained input inventory changed")
    topology = None
    if manifest.tree_policy is not None:
        topology = _hardlink_topology(root, names, access)
        expected = {
            m.path: m.hardlink_target
            for m in manifest.members
            if m.hardlink_target is not None
        }
        if topology[0] != expected:
            raise RetainedProjectInputError("Retained hard link topology changed")
    for member in manifest.members:
        if member.hardlink_target is not None:
            # The complete inode group shares its verified representative bytes.
            continue
        if _stream(
            root,
            member.path,
            member.size,
            access=access,
            windows_executable=member.mode == 0o755,
        ) != (
            member.size,
            member.mode,
            member.sha256,
        ):
            raise RetainedProjectInputError(
                "Retained input bytes or executable mode changed"
            )
    if _inventory(
        root,
        manifest.roots,
        manifest.limits.max_entries,
        allow_internal_symlinks=allow_links,
        access=access,
    ) != (
        names,
        directories,
    ):
        raise RetainedProjectInputError(
            "Retained inventory changed during verification"
        )

    if topology is not None and _hardlink_topology(root, names, access) != topology:
        raise RetainedProjectInputError(
            "Retained hard link topology changed during verification"
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
        if manifest.tree_policy is not None and manifest.tree_policy.case_sensitive:
            _require_case_sensitive(tree)
        for directory in manifest.directories:
            (tree / directory).mkdir(parents=True, exist_ok=True)
        with _directory_access(root) as access:
            for member in manifest.members:
                if member.hardlink_target is not None:
                    continue  # A canonical representative supplies the CAS bytes.
                blob = blobs / member.sha256
                with tempfile.TemporaryFile(dir=temporary) as stream:
                    if _stream(
                        root,
                        member.path,
                        member.size,
                        stream,
                        access=access,
                        windows_executable=member.mode == 0o755,
                    ) != (
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
                if (
                    member.symlink_target is not None
                    or member.hardlink_target is not None
                ):
                    continue  # Materialize links after every ordinary input is copied.
                with blob.open("rb") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, _CHUNK)
                target.chmod(0o555 if member.mode == 0o755 else 0o444)
        for member in manifest.members:
            if member.hardlink_target is not None:
                os.link(tree / member.hardlink_target, tree / member.path)
        by_path = {member.path: member for member in manifest.members}
        destinations: dict[str, str] = {}
        for member in manifest.members:
            current = member.path
            chain = []
            while (
                current in by_path
                and by_path[current].symlink_target is not None
                and current not in destinations
            ):
                chain.append(current)
                current = by_path[current].symlink_destination
            terminal = destinations.get(current, current)
            destinations.update((name, terminal) for name in chain)
        for member in manifest.members:
            if member.symlink_target is not None:
                (tree / member.path).symlink_to(
                    member.symlink_target,
                    target_is_directory=destinations[member.path]
                    in manifest.directories,
                )
        verify_retained_project_inputs(root, manifest)
        verify_retained_project_inputs(tree, manifest)
        with _directory_access(temporary) as access:
            for digest, size in {m.sha256: m.size for m in manifest.members}.items():
                if _stream(temporary, "blobs/" + digest, size, access=access) != (
                    size,
                    0o644,
                    digest,
                ):
                    raise RetainedProjectInputError(
                        "Retained CAS blob changed during capture"
                    )
        # Atomic no-replacement reservation; no rename-over-existing-directory race.
        destination.mkdir(mode=0o700)
        try:
            for name in ("blobs", "tree"):
                (temporary / name).rename(destination / name)
        except BaseException:
            _remove_tree(destination)
            raise
        return destination / "tree"
    finally:
        _remove_tree(temporary)

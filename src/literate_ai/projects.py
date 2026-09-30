"""Filesystem adapter for explicit Literate AI project roots and catalogs."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import _is_windows, _windows_identity_matches
from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.contracts import (
    CANONICAL_PROJECT_PROFILE,
    ContentIdentity,
    ProjectDefinition,
    SourceCacheRootKind,
    canonical_identity,
)

# Windows ``msvcrt.locking`` has no blocking-with-clean-error mode: ``LK_LOCK``
# retries internally for ~10s and then raises a bare ``OSError`` that surfaces as
# ``manifest lock failed`` even under ordinary contention. We instead poll
# ``LK_NBLCK`` -- which fails fast with EACCES/EDEADLOCK when another holder owns
# the byte -- on a bounded retry loop so transient contention is retried while a
# genuine failure still raises promptly. POSIX keeps its blocking ``flock`` path.
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.01

PROJECT_FILENAME = "literate.project.json"
DEFAULT_MAXIMUM_PROJECT_CONFIGURATION_BYTES = 1024 * 1024
DEFAULT_MAXIMUM_PINNED_INPUT_FILES = 1024
DEFAULT_MAXIMUM_PINNED_INPUT_BYTES = 2 * 1024 * 1024
DEFAULT_MAXIMUM_PINNED_INPUT_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAXIMUM_DOCUMENTATION_FILES = 1_024
DEFAULT_MAXIMUM_DOCUMENTATION_ENTRIES = 4_096
# Rich literate assets such as editable slide decks must fit without turning the
# documentation graph into an unbounded binary store. Sixteen MiB admits normal
# presentation/PDF artifacts while the independent 64 MiB catalog ceiling remains.
DEFAULT_MAXIMUM_DOCUMENTATION_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAXIMUM_DOCUMENTATION_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAXIMUM_DOCUMENTATION_PATH_BYTES = 1_024
DEFAULT_MAXIMUM_DOCUMENTATION_DEPTH = 32
CANONICAL_AGENT_SHIMS = {
    "AGENTS.md": "SKILL.md",
    "CLAUDE.md": "SKILL.md",
    ".cursor/rules/literate-ai.mdc": "SKILL.md",
    "agents/openai.yaml": "$literate-ai",
}


class ProjectError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def parse_project_configuration(content: bytes) -> ProjectDefinition:
    try:
        return ProjectDefinition.from_dict(json.loads(content.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProjectError(
            "project.manifest_invalid", "project manifest is invalid"
        ) from exc


def serialize_project_configuration(definition: ProjectDefinition) -> bytes:
    if not isinstance(definition, ProjectDefinition):
        raise TypeError("project configuration must be a ProjectDefinition")
    return (
        json.dumps(definition.to_dict(), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProjectConfigurationSnapshot:
    root: Path
    definition: ProjectDefinition
    content: bytes

    @property
    def content_identity(self) -> ContentIdentity:
        return ContentIdentity.parse_uri(
            f"sha256:{hashlib.sha256(self.content).hexdigest()}"
        )


class ProjectConfigurationStore:
    """Atomic project manifest storage using exact-byte compare-and-swap."""

    def __init__(
        self,
        root: Path,
        *,
        maximum_bytes: int = DEFAULT_MAXIMUM_PROJECT_CONFIGURATION_BYTES,
    ) -> None:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise ValueError("project configuration byte limit must be positive")
        self.root = Path(root)
        self.maximum_bytes = maximum_bytes

    @property
    def path(self) -> Path:
        return self.root / PROJECT_FILENAME

    def _root(self, *, create: bool = False) -> Path:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ProjectError(
                "project.root_unavailable", "project root is unavailable"
            ) from exc
        if self.root.is_symlink() or not root.is_dir():
            raise ProjectError("project.root_unavailable", "project root is unsafe")
        return root

    def read(self) -> ProjectConfigurationSnapshot:
        root = self._root()
        path = root / PROJECT_FILENAME
        content = self._read_manifest(path)
        return ProjectConfigurationSnapshot(
            root, parse_project_configuration(content), content
        )

    @classmethod
    def discover(cls, start: Path) -> ProjectConfigurationSnapshot | None:
        try:
            resolved = Path(start).resolve(strict=True)
        except OSError:
            resolved = Path(start).resolve()
        candidate = resolved.parent if resolved.is_file() else resolved
        for root in (candidate, *candidate.parents):
            path = root / PROJECT_FILENAME
            if path.is_file() and not path.is_symlink():
                return cls(root).read()
        return None

    def create(self, definition: ProjectDefinition) -> ProjectConfigurationSnapshot:
        content = serialize_project_configuration(definition)
        self._require_bounded(content)
        root = self._root(create=True)
        with self._lock(root):
            path = root / PROJECT_FILENAME
            if path.exists() or path.is_symlink():
                raise ProjectError(
                    "project.manifest_exists", "project manifest already exists"
                )
            self._replace(path, content)
        return ProjectConfigurationSnapshot(root, definition, content)

    def update(
        self,
        expected: ProjectConfigurationSnapshot,
        definition: ProjectDefinition,
    ) -> ProjectConfigurationSnapshot:
        if not isinstance(expected, ProjectConfigurationSnapshot):
            raise TypeError("expected must be a ProjectConfigurationSnapshot")
        content = serialize_project_configuration(definition)
        self._require_bounded(content)
        root = self._root()
        if expected.root != root:
            raise ProjectError(
                "project.manifest_stale", "project snapshot belongs to another root"
            )
        with self._lock(root):
            path = root / PROJECT_FILENAME
            current = self._read_manifest(path)
            if current != expected.content:
                raise ProjectError(
                    "project.manifest_stale",
                    "project manifest changed since it was read",
                )
            if content != current:
                self._replace(path, content)
        return ProjectConfigurationSnapshot(root, definition, content)

    def _read_manifest(self, path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode):
                raise OSError("manifest is a symbolic link")
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ProjectError(
                "project.manifest_unavailable",
                f"project root must contain {PROJECT_FILENAME}",
            ) from exc
        try:
            opened = os.fstat(descriptor)
            observed = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or not self._same_file(opened, before)
                or not self._same_file(opened, observed)
                or opened.st_size > self.maximum_bytes
            ):
                raise ProjectError(
                    "project.manifest_unavailable",
                    "project manifest is unsafe or exceeds the byte limit",
                )
            content = bytearray()
            while len(content) <= self.maximum_bytes:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, self.maximum_bytes + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = path.lstat()
            final = os.fstat(descriptor)
            if (
                len(content) > self.maximum_bytes
                or not self._same_file(opened, final)
                or not self._same_file(opened, after)
            ):
                raise ProjectError(
                    "project.manifest_unavailable",
                    "project manifest changed while it was read",
                )
            return bytes(content)
        except ProjectError:
            raise
        except OSError as exc:
            raise ProjectError(
                "project.manifest_unavailable", "project manifest is unavailable"
            ) from exc
        finally:
            os.close(descriptor)

    def _require_bounded(self, content: bytes) -> None:
        if len(content) > self.maximum_bytes:
            raise ProjectError(
                "project.manifest_too_large",
                "project manifest exceeds the byte limit",
            )

    @staticmethod
    def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
        return os.path.samestat(left, right)

    @staticmethod
    def _lock_identity_matches(
        path: Path,
        path_metadata: os.stat_result,
        descriptor: int,
        descriptor_metadata: os.stat_result,
    ) -> bool:
        """Return whether ``path`` and the held descriptor name the same file.

        On POSIX this is the exact ``(st_dev, st_ino)`` compare-and-swap check the
        lock relied on before. On Windows ``os.open``/``os.fstat`` frequently
        report ``st_ino == 0`` (CPython issue #76): the bare ``st_ino`` compare is
        then either a no-op that treats a swapped lock file as safe or a flake
        that aborts on an unchanged file. When Windows cannot supply a reliable
        inode we fall back to ``GetFileInformationByHandle`` file-index/volume
        identity -- reopening the path without following reparse points and
        comparing it against the descriptor's own identity -- which is
        Microsoft's documented reliable per-volume file identity. Symlink and
        regular-file checks are enforced by the callers regardless.
        """

        if _is_windows() and (
            path_metadata.st_ino == 0 or descriptor_metadata.st_ino == 0
        ):
            return _windows_identity_matches(path, descriptor)
        return (path_metadata.st_dev, path_metadata.st_ino) == (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        )

    @staticmethod
    def _acquire_exclusive(descriptor: int) -> None:
        """Take an exclusive advisory lock on byte 0, blocking until acquired.

        POSIX uses a single blocking ``flock(LOCK_EX)`` -- byte-for-byte the
        original behavior. Windows has no blocking mode that raises a clean errno,
        so we poll non-blocking ``LK_NBLCK`` on a bounded deadline: transient
        contention (EACCES/EAGAIN/EDEADLOCK) is retried, and any other error --
        or the deadline -- propagates as ``OSError`` for the caller to surface as
        ``project.manifest_lock``.
        """

        if os.name != "nt":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        import msvcrt

        contention = {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EDEADLOCK", errno.EDEADLK),
        }
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                # EDEADLOCK is what ``msvcrt.locking`` reports when the byte is
                # already locked by another holder; EACCES/EAGAIN cover the same
                # contention on other configurations.
                if exc.errno not in contention:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_SECONDS)

    @staticmethod
    def _release_exclusive(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    @contextmanager
    def _lock(self, root: Path) -> Iterator[None]:
        path = root / f".{PROJECT_FILENAME}.write.lock"
        descriptor = -1
        locked = False
        opened: os.stat_result | None = None
        try:
            descriptor, locked, opened = self._open_and_acquire(path)
            yield
        except ProjectError:
            raise
        except OSError as exc:
            raise ProjectError("project.manifest_lock", "manifest lock failed") from exc
        finally:
            if descriptor != -1:
                if locked:
                    try:
                        self._release_exclusive(descriptor)
                    except OSError:
                        pass
                os.close(descriptor)
            if opened is not None:
                self._remove_idle_lock(path, opened)

    def _open_and_acquire(self, path: Path) -> tuple[int, bool, os.stat_result]:
        """Open the lock file, take the exclusive lock, and verify identity.

        Returns ``(descriptor, locked, opened)`` with the exclusive lock held.

        A concurrent holder's ``_remove_idle_lock`` may unlink the lock file
        after this process has already opened it but before -- on POSIX -- this
        process wins the *blocking* ``flock``. When that happens the descriptor
        we hold names an inode that the path no longer resolves to (or the path
        has vanished entirely). That is a benign lost race for the *lock*, not a
        malicious swap and not a lost compare-and-swap: the correct response is
        to release, reopen the freshly recreated lock file, and retry -- never to
        surface ``project.manifest_lock`` to a caller that simply queued behind a
        prior writer. Retrying keeps the writer serialized so it can go on to
        read the changed manifest and fail its CAS with ``project.manifest_stale``
        (the loser contract). Every attempt re-verifies symlink/regular-file and
        descriptor-vs-path identity, so the swap and reparse protections are
        unchanged.
        """

        # WINDOWS-LOCK-001: converge on the byte-for-byte open flags of the
        # proven-on-Windows ``_cache_lock.exclusive_cache_lock`` path
        # (``O_RDWR | O_CREAT | O_BINARY | O_NOFOLLOW``). Notably this drops
        # ``O_CLOEXEC``: that flag is meaningless for a lock descriptor held only
        # within this process, it is absent from the lock implementation that
        # passes on Windows CI, and it is one of only two things the failing
        # manifest path did that the working cache path did not.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                descriptor = os.open(path, flags, 0o600)
            except PermissionError as exc:
                # Windows delete-pending race: another writer's idle-lock cleanup
                # unlinked the file but a handle is still closing, so O_CREAT open
                # transiently fails EACCES until the delete completes. Retry within
                # the deadline. POSIX has no such state; raise there immediately.
                if os.name == "nt" and time.monotonic() < deadline:
                    time.sleep(_LOCK_RETRY_SECONDS)
                    continue
                raise ProjectError(
                    "project.manifest_lock", "manifest lock is unsafe"
                ) from exc
            except OSError as exc:
                raise ProjectError(
                    "project.manifest_lock", "manifest lock is unsafe"
                ) from exc
            locked = False
            retry = False
            try:
                opened = os.fstat(descriptor)
                observed = path.lstat()
            except FileNotFoundError:
                # An idle-lock cleanup can unlink the inode after this process
                # opened it but before it starts waiting on the lock. No lock is
                # held yet, so discard the orphaned descriptor and retry the normal
                # sequence. A path that exists but names another inode remains a
                # hard failure in the identity check below.
                os.close(descriptor)
                if time.monotonic() >= deadline:
                    raise ProjectError(
                        "project.manifest_lock",
                        "manifest lock changed before it was acquired",
                    ) from None
                time.sleep(_LOCK_RETRY_SECONDS)
                continue
            except BaseException:
                os.close(descriptor)
                raise
            try:
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_ISLNK(observed.st_mode)
                    or not self._lock_identity_matches(
                        path, observed, descriptor, opened
                    )
                ):
                    raise ProjectError(
                        "project.manifest_lock", "manifest lock is unsafe"
                    )
                # Windows ``msvcrt.locking`` locks a byte range that must lie
                # within the file: locking byte [0, 1) of a 0-byte lock file
                # returns EACCES *permanently* (not transient contention), so the
                # retry loop would spin until the deadline and then raise
                # ``manifest_lock``. Ensure the file holds its placeholder byte
                # BEFORE acquiring the lock. This is safe on a freshly created,
                # not-yet-locked file: nobody can hold a byte-range lock on a byte
                # that does not exist yet, so the write can never collide with
                # another holder. When the file already has content (another
                # holder wrote the placeholder first) we skip the write.
                #
                # WINDOWS-LOCK-001: mirror the proven ``_cache_lock`` path exactly
                # -- write the byte, then ``lseek`` back to 0, and NOTHING ELSE.
                # The failing manifest path additionally called ``os.fsync`` here,
                # which is ``_commit()``/``FlushFileBuffers`` on Windows. That was
                # one of only two behaviours the manifest lock added over the
                # cache lock that passes on Windows CI, and it is unnecessary: the
                # placeholder byte only has to exist in the file so the 1-byte
                # region is lockable -- it is advisory-lock scratch, not durable
                # state, so there is no reason to force it to disk before locking.
                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                # POSIX blocks here on ``flock(LOCK_EX)`` so a concurrent loser
                # waits for the current writer, then loses the compare-and-swap
                # cleanly. This must remain blocking (not timeout-bounded) on
                # POSIX so the loser surfaces ``manifest_stale``, never
                # ``manifest_lock``.
                self._acquire_exclusive(descriptor)
                locked = True
                try:
                    observed = path.lstat()
                except FileNotFoundError:
                    # The lock file we held was unlinked by a prior holder's idle
                    # cleanup while we blocked on ``flock`` -- ``_remove_idle_lock``
                    # only ever *unlinks* the lock inode, never replaces it with a
                    # different regular file. A vanished path is therefore a benign
                    # lost race for the *lock*, not a malicious swap and not a
                    # compare-and-swap loss: release, reopen the recreated lock
                    # file, and retry so the writer stays serialized and can go on
                    # to fail its manifest CAS with ``manifest_stale``. A path that
                    # still exists but names a *different* inode is a genuine swap
                    # and is rejected below.
                    if time.monotonic() >= deadline:
                        raise ProjectError(
                            "project.manifest_lock",
                            "manifest lock changed while it was acquired",
                        ) from None
                    retry = True
                    observed = None
                opened = os.fstat(descriptor)
                if observed is not None and (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_ISLNK(observed.st_mode)
                    or not self._lock_identity_matches(
                        path, observed, descriptor, opened
                    )
                ):
                    raise ProjectError(
                        "project.manifest_lock",
                        "manifest lock changed while it was acquired",
                    )
                if not retry:
                    return descriptor, locked, opened
            except BaseException:
                if locked:
                    try:
                        self._release_exclusive(descriptor)
                    except OSError:
                        pass
                os.close(descriptor)
                raise
            # Benign lost race: clean up this attempt, back off, and retry.
            if locked:
                try:
                    self._release_exclusive(descriptor)
                except OSError:
                    pass
            os.close(descriptor)
            if retry:
                time.sleep(_LOCK_RETRY_SECONDS)

    def _remove_idle_lock(self, path: Path, expected: os.stat_result) -> None:
        """Remove only the same unlocked inode when no waiter has acquired it."""

        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        locked = False
        try:
            opened = os.fstat(descriptor)
            observed = path.lstat()
            # ``expected`` is a stat_result captured earlier; on Windows its
            # ``st_ino`` may be 0, so a bare ``samestat`` against it would treat
            # any lock file as "the same" one. Only trust the historical
            # stat-to-stat identity when the inode is reliable; the live
            # path-vs-descriptor identity is verified through the reliable helper.
            expected_still_matches = (
                self._same_file(expected, opened)
                if not (_is_windows() and expected.st_ino == 0)
                else self._lock_identity_matches(path, observed, descriptor, opened)
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or not expected_still_matches
                or not self._lock_identity_matches(path, observed, descriptor, opened)
            ):
                return
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    return
            else:
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return
            locked = True
            observed = path.lstat()
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(observed.st_mode) or not self._lock_identity_matches(
                path, observed, descriptor, opened
            ):
                return
            if os.name != "nt":
                path.unlink()
        except OSError:
            return
        finally:
            if locked:
                try:
                    self._release_exclusive(descriptor)
                except OSError:
                    pass
            os.close(descriptor)
        if os.name == "nt" and locked:
            # On Windows the file must be unlinked after the descriptor closes and
            # the byte lock is released. Re-verify the path still names the inode
            # we just held (never a symlink) before removing it.
            try:
                observed = path.lstat()
                if not stat.S_ISLNK(observed.st_mode) and (
                    self._same_file(expected, observed)
                    if not (_is_windows() and expected.st_ino == 0)
                    else True
                ):
                    path.unlink()
            except OSError:
                pass

    @staticmethod
    def _replace(path: Path, content: bytes) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            # Fsync the parent directory so the rename is durable. Windows does
            # not permit FlushFileBuffers on a directory handle (it raises
            # PermissionError/EACCES), and NTFS metadata durability does not
            # require it, so this is POSIX-only.
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class PinnedInputClosureError(ProjectError):
    """A generation-authority file is unsafe, unbounded, or changed."""


@dataclass(frozen=True, slots=True)
class _PinnedInput:
    path: Path
    boundary: Path
    content: bytes
    labels: tuple[str, ...]

    @property
    def content_identity(self) -> str:
        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"


class PinnedInputClosure:
    """Bounded exact files whose bytes authorize one generation operation.

    Entries retain their original bytes and boundary. Revalidation rejects mutation,
    disappearance, non-regular replacements, and a symlink in any path component.
    Logical labels make the closure identity portable without publishing host paths.
    """

    def __init__(
        self,
        *,
        maximum_files: int = DEFAULT_MAXIMUM_PINNED_INPUT_FILES,
        maximum_file_bytes: int = DEFAULT_MAXIMUM_PINNED_INPUT_BYTES,
        maximum_total_bytes: int = DEFAULT_MAXIMUM_PINNED_INPUT_TOTAL_BYTES,
    ) -> None:
        if (
            maximum_files < 1
            or maximum_file_bytes < 1
            or maximum_total_bytes < maximum_file_bytes
        ):
            raise ValueError("pinned-input closure limits must be positive and ordered")
        self.maximum_files = maximum_files
        self.maximum_file_bytes = maximum_file_bytes
        self.maximum_total_bytes = maximum_total_bytes
        self._entries: dict[tuple[Path, Path], _PinnedInput] = {}
        self._labels: dict[str, tuple[Path, Path, bytes]] = {}

    def pin(
        self,
        path: Path,
        *,
        boundary: Path,
        label: str,
        expected_content: bytes | None = None,
        expected_identity: str | ContentIdentity | None = None,
    ) -> bytes:
        """Capture one regular file and optionally bind bytes already consumed."""

        if not label or len(label.encode("utf-8")) > 1024:
            raise PinnedInputClosureError(
                "inputs.closure_label_invalid",
                "pinned-input labels must be non-empty and at most 1024 bytes",
            )
        configured_boundary, resolved_boundary = self._boundary(boundary)
        resolved_path, content = self._read(
            path, resolved_boundary, configured_boundary=configured_boundary
        )
        if expected_content is not None and content != expected_content:
            raise PinnedInputClosureError(
                "inputs.closure_capture_drift",
                f"generation authority changed while it was captured: {label}",
            )
        if expected_identity is not None:
            identity = (
                expected_identity.uri
                if isinstance(expected_identity, ContentIdentity)
                else expected_identity
            )
            try:
                parsed = ContentIdentity.parse_uri(identity)
            except (TypeError, ValueError) as exc:
                raise PinnedInputClosureError(
                    "inputs.closure_identity_invalid",
                    f"generation authority has an invalid identity: {label}",
                ) from exc
            if hashlib.sha256(content).hexdigest() != parsed.digest:
                raise PinnedInputClosureError(
                    "inputs.closure_identity_changed",
                    f"generation authority identity changed: {label}",
                )
        self._admit(_PinnedInput(resolved_path, resolved_boundary, content, (label,)))
        return content

    def include(self, other: PinnedInputClosure) -> None:
        """Merge an independently captured authority set without rereading it."""

        if other is self:
            return
        other.require_unchanged()
        for entry in other._entries.values():
            self._admit(entry)

    def fork(self) -> PinnedInputClosure:
        """Create an independent closure initialized with this exact authority set."""

        copied = PinnedInputClosure(
            maximum_files=self.maximum_files,
            maximum_file_bytes=self.maximum_file_bytes,
            maximum_total_bytes=self.maximum_total_bytes,
        )
        for entry in self._entries.values():
            copied._admit(entry)
        return copied

    @property
    def identity(self) -> str:
        return canonical_identity(self._identity_document()).uri

    @property
    def file_count(self) -> int:
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        return sum(len(item.content) for item in self._entries.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/pinned-input-closure@1",
            "identity": self.identity,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "inputs": self._identity_document()["inputs"],
        }

    def require_unchanged(self) -> None:
        for entry in self._entries.values():
            _resolved, content = self._read(entry.path, entry.boundary)
            if content != entry.content:
                raise PinnedInputClosureError(
                    "inputs.closure_changed",
                    "generation authority changed during the operation: "
                    + ", ".join(entry.labels),
                )

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/pinned-input-closure@1",
            "inputs": [
                {
                    "labels": list(item.labels),
                    "content_identity": item.content_identity,
                    "bytes": len(item.content),
                }
                for item in sorted(
                    self._entries.values(), key=lambda value: value.labels
                )
            ],
        }

    def _admit(self, entry: _PinnedInput) -> None:
        key = (entry.path, entry.boundary)
        existing = self._entries.get(key)
        labels = set(entry.labels)
        if existing is not None:
            if existing.content != entry.content:
                raise PinnedInputClosureError(
                    "inputs.closure_capture_drift",
                    "one generation authority path was captured with different bytes",
                )
            labels.update(existing.labels)
        candidate = _PinnedInput(
            entry.path,
            entry.boundary,
            entry.content,
            tuple(sorted(labels)),
        )
        for label in candidate.labels:
            claimed = self._labels.get(label)
            claim = (candidate.path, candidate.boundary, candidate.content)
            if claimed is not None and claimed != claim:
                raise PinnedInputClosureError(
                    "inputs.closure_label_conflict",
                    f"generation authority label is ambiguous: {label}",
                )
        prospective_files = len(self._entries) + (existing is None)
        prospective_bytes = self.total_bytes + (
            len(candidate.content) if existing is None else 0
        )
        if len(candidate.content) > self.maximum_file_bytes:
            raise PinnedInputClosureError(
                "inputs.closure_file_size_limit",
                "generation authority exceeds the pinned-input per-file limit",
            )
        if prospective_files > self.maximum_files:
            raise PinnedInputClosureError(
                "inputs.closure_file_limit",
                "generation authority exceeds the pinned-input file limit",
            )
        if prospective_bytes > self.maximum_total_bytes:
            raise PinnedInputClosureError(
                "inputs.closure_total_size_limit",
                "generation authority exceeds the pinned-input total-size limit",
            )
        self._entries[key] = candidate
        for label in candidate.labels:
            self._labels[label] = (
                candidate.path,
                candidate.boundary,
                candidate.content,
            )

    def _boundary(self, boundary: Path) -> tuple[Path, Path]:
        configured = Path(os.path.abspath(boundary))
        if configured.is_symlink():
            raise PinnedInputClosureError(
                "inputs.closure_boundary_invalid",
                "generation-authority boundary cannot be a symbolic link",
            )
        try:
            resolved = configured.resolve(strict=True)
        except OSError as exc:
            raise PinnedInputClosureError(
                "inputs.closure_boundary_invalid",
                "generation-authority boundary is unavailable",
            ) from exc
        if not resolved.is_dir():
            raise PinnedInputClosureError(
                "inputs.closure_boundary_invalid",
                "generation-authority boundary must be a directory",
            )
        return configured, resolved

    def _read(
        self,
        path: Path,
        boundary: Path,
        *,
        configured_boundary: Path | None = None,
    ) -> tuple[Path, bytes]:
        configured = Path(os.path.abspath(path))
        lexical_boundary = configured_boundary or boundary
        if configured.is_relative_to(lexical_boundary):
            current = lexical_boundary
            for part in configured.relative_to(lexical_boundary).parts:
                current /= part
                if current.is_symlink():
                    raise PinnedInputClosureError(
                        "inputs.closure_symlink",
                        "generation authority cannot traverse a symbolic link",
                    )
        if configured.is_symlink():
            raise PinnedInputClosureError(
                "inputs.closure_symlink",
                "generation authority cannot traverse a symbolic link",
            )
        try:
            resolved = configured.resolve(strict=True)
        except OSError as exc:
            raise PinnedInputClosureError(
                "inputs.closure_unavailable",
                f"generation authority is unavailable: {configured}",
            ) from exc
        if not resolved.is_relative_to(boundary):
            raise PinnedInputClosureError(
                "inputs.closure_path_escape",
                "generation authority escapes its declared boundary",
            )
        relative = resolved.relative_to(boundary)
        current = boundary
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise PinnedInputClosureError(
                    "inputs.closure_symlink",
                    "generation authority cannot traverse a symbolic link",
                )
        if not resolved.is_file():
            raise PinnedInputClosureError(
                "inputs.closure_not_regular",
                "generation authority must be a regular file",
            )
        try:
            with resolved.open("rb") as stream:
                content = stream.read(self.maximum_file_bytes + 1)
        except OSError as exc:
            raise PinnedInputClosureError(
                "inputs.closure_unavailable",
                f"generation authority could not be read: {configured}",
            ) from exc
        if len(content) > self.maximum_file_bytes:
            raise PinnedInputClosureError(
                "inputs.closure_file_size_limit",
                "generation authority exceeds the pinned-input per-file limit",
            )
        if resolved.is_symlink() or resolved.resolve(strict=True) != resolved:
            raise PinnedInputClosureError(
                "inputs.closure_replaced",
                "generation authority changed while it was read",
            )
        return resolved, content


@dataclass(frozen=True, slots=True)
class LoadedProject:
    root: Path
    definition: ProjectDefinition

    def roots(self, catalog: str) -> tuple[Path, ...]:
        try:
            configured = getattr(self.definition, f"{catalog}_roots")
        except AttributeError as exc:
            raise ValueError(f"unknown project catalog: {catalog}") from exc
        return tuple(self.root.joinpath(*Path(item).parts) for item in configured)

    @property
    def agent_skill(self) -> Path:
        return self.root.joinpath(*Path(self.definition.agent_skill).parts)

    def resolve_file(self, base: Path, uri: str, *, label: str) -> Path:
        configured = base / uri
        if configured.is_symlink():
            raise ProjectError(
                "project.reference_symlink",
                f"{label} cannot be a symbolic link",
            )
        try:
            path = configured.resolve(strict=True)
        except OSError as exc:
            raise ProjectError(
                "project.reference_unavailable", f"{label} is unavailable"
            ) from exc
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ProjectError(
                "project.reference_escape",
                f"{label} must be a regular file inside the project root",
            )
        return path

    def flavor_selectors_for(
        self, component: Path, explicit: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """Return persisted Component selectors followed by CLI overrides."""

        resolved = component.resolve(strict=True)
        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ProjectError(
                "project.component_outside_root",
                "Component Flavor selectors require a Component inside the project",
            ) from exc
        configured = self.definition.component_flavor_selectors.get(relative, ())
        return (*configured, *explicit)


def load_project(root: Path) -> LoadedProject:
    snapshot = ProjectConfigurationStore(root).read()
    return LoadedProject(snapshot.root, snapshot.definition)


def discover_project(start: Path) -> LoadedProject | None:
    snapshot = ProjectConfigurationStore.discover(start)
    if snapshot is None:
        return None
    return LoadedProject(snapshot.root, snapshot.definition)


def project_boundary(start: Path, *, legacy: Path) -> Path:
    project = discover_project(start)
    return project.root if project is not None else legacy.resolve(strict=True)


def project_skill_catalog(project: LoadedProject) -> AgentSkillCatalog:
    """Return the one validated skill-taxonomy authority for a project."""

    roots = project.roots("skill")
    if not roots:
        raise ProjectError("project.skill_invalid", "project declares no skill roots")
    try:
        return AgentSkillCatalog.discover(
            project.root,
            catalog_roots=roots,
            root_manifests=(project.agent_skill,),
            validate_dependencies=True,
            validate_references=True,
        )
    except AgentSkillCatalogError as exc:
        raise ProjectError(exc.code, exc.message) from exc


def _skill_manifest_paths(
    project: LoadedProject,
    direction: str,
    *,
    validate_catalog: bool = True,
) -> tuple[Path, ...]:
    """Return manifests admitted beneath one declared skill-catalog direction."""

    roots = project.roots("skill")
    if not roots:
        return ()
    prefixes = tuple((root / direction).resolve() for root in roots)
    if validate_catalog:
        manifests = tuple(
            skill.manifest_path
            for skill in project_skill_catalog(project).skills
            if skill.manifest_path is not None
        )
    else:
        try:
            manifests = AgentSkillCatalog.discover_manifest_paths(
                project.root,
                catalog_roots=roots,
                root_manifests=(project.agent_skill,),
            )
        except AgentSkillCatalogError as exc:
            code = (
                "project.skill_ambiguous"
                if exc.code == "agent_skill.authority_ambiguous"
                else "project.skill_invalid"
            )
            raise ProjectError(code, exc.message) from exc
    return tuple(
        sorted(
            manifest
            for manifest in manifests
            if any(manifest.is_relative_to(prefix) for prefix in prefixes)
        )
    )


def specification_to_source_skill_paths(
    project: LoadedProject, *, validate_catalog: bool = True
) -> tuple[Path, ...]:
    """Return exact files admitted by declared forward-generation skill roots."""

    return _skill_manifest_paths(
        project,
        "specification-to-source",
        validate_catalog=validate_catalog,
    )


def source_to_specification_skill_paths(
    project: LoadedProject, *, validate_catalog: bool = True
) -> tuple[Path, ...]:
    """Return exact files admitted by declared inverse-authoring skill roots."""

    return _skill_manifest_paths(
        project,
        "source-to-specification",
        validate_catalog=validate_catalog,
    )


def documentation_files(project: LoadedProject) -> tuple[Path, ...]:
    """Return every bounded regular file admitted by documentation roots."""

    configured_roots = project.roots("documentation")
    resolved_roots: list[Path] = []
    for configured_root in configured_roots:
        current = project.root
        try:
            relative_root = configured_root.relative_to(project.root)
        except ValueError as exc:
            raise ProjectError(
                "project.documentation_invalid",
                "declared documentation root escapes the project root",
            ) from exc
        for part in relative_root.parts:
            current /= part
            if current.is_symlink():
                raise ProjectError(
                    "project.documentation_invalid",
                    "declared documentation roots cannot traverse symbolic links",
                )
        if not configured_root.is_dir():
            raise ProjectError(
                "project.documentation_invalid",
                "declared documentation roots must be regular directories",
            )
        root = configured_root.resolve(strict=True)
        if not root.is_relative_to(project.root):
            raise ProjectError(
                "project.documentation_invalid",
                "declared documentation root escapes the project root",
            )
        if any(
            root == previous
            or root.is_relative_to(previous)
            or previous.is_relative_to(root)
            for previous in resolved_roots
        ):
            raise ProjectError(
                "project.documentation_invalid",
                "declared documentation roots cannot overlap",
            )
        resolved_roots.append(root)

    paths: set[Path] = set()
    entries = 0
    total_bytes = 0
    for configured_root, root in zip(configured_roots, resolved_roots, strict=True):
        for configured in configured_root.rglob("*"):
            entries += 1
            if entries > DEFAULT_MAXIMUM_DOCUMENTATION_ENTRIES:
                raise ProjectError(
                    "project.documentation_limit",
                    "declared documentation contains too many filesystem entries",
                )
            if configured.is_symlink():
                raise ProjectError(
                    "project.documentation_invalid",
                    "declared documentation cannot contain symbolic links",
                )
            relative = configured.relative_to(configured_root)
            if (
                len(relative.as_posix().encode("utf-8"))
                > DEFAULT_MAXIMUM_DOCUMENTATION_PATH_BYTES
                or len(relative.parts) > DEFAULT_MAXIMUM_DOCUMENTATION_DEPTH
            ):
                raise ProjectError(
                    "project.documentation_limit",
                    "declared documentation path exceeds portable limits",
                )
            if configured.is_dir():
                continue
            if not configured.is_file():
                raise ProjectError(
                    "project.documentation_invalid",
                    "declared documentation entries must be regular files",
                )
            resolved = configured.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_relative_to(
                project.root
            ):
                raise ProjectError(
                    "project.documentation_invalid",
                    "declared documentation file escapes its documentation root",
                )
            size = resolved.stat().st_size
            if size > DEFAULT_MAXIMUM_DOCUMENTATION_FILE_BYTES:
                raise ProjectError(
                    "project.documentation_limit",
                    "declared documentation file exceeds the per-file byte limit",
                )
            total_bytes += size
            if total_bytes > DEFAULT_MAXIMUM_DOCUMENTATION_TOTAL_BYTES:
                raise ProjectError(
                    "project.documentation_limit",
                    "declared documentation exceeds the total byte limit",
                )
            paths.add(resolved)
            if len(paths) > DEFAULT_MAXIMUM_DOCUMENTATION_FILES:
                raise ProjectError(
                    "project.documentation_limit",
                    "declared documentation contains too many files",
                )
    return tuple(sorted(paths))


def documentation_paths(project: LoadedProject) -> tuple[Path, ...]:
    """Return Markdown documents admitted by declared documentation roots."""

    return tuple(
        path for path in documentation_files(project) if path.suffix.casefold() == ".md"
    )


def documentation_asset_paths(project: LoadedProject) -> tuple[Path, ...]:
    """Return non-Markdown assets admitted by declared documentation roots."""

    return tuple(
        path for path in documentation_files(project) if path.suffix.casefold() != ".md"
    )


def _require_disjoint_source_cache_roots(
    project: LoadedProject, authority_paths: dict[str, Path]
) -> None:
    """Keep a committable cache away from every authority, onboarding, and receipt path.

    A project-relative source-cache root may be tracked in Git, which is the point. It
    must never sit on top of a declared catalog, the onboarding skill, or the test
    receipt: derived source landing inside an authority root would be read as authority
    on the next scan, and a cache write could silently overwrite a specification. The
    contract has always stated this constraint; nothing enforced it.
    """

    configuration = project.definition.source_cache
    if configuration is None:
        return
    protected = dict(authority_paths)
    receipt = project.definition.test_receipt
    if receipt:
        protected["test_receipt"] = project.root.joinpath(*Path(receipt).parts)
    for target in configuration.targets:
        if target.root_kind is not SourceCacheRootKind.PROJECT_RELATIVE:
            continue
        root = project.root.joinpath(*Path(target.root_reference).parts)
        if root == project.root:
            raise ProjectError(
                "project.source_cache_overlaps_authority",
                f"source-cache target {target.target_id!r} names the project root",
            )
        for label, path in protected.items():
            if root == path or root.is_relative_to(path) or path.is_relative_to(root):
                raise ProjectError(
                    "project.source_cache_overlaps_authority",
                    f"source-cache target {target.target_id!r} overlaps {label}; a "
                    "committable cache must not sit on an authority path",
                )


def project_owns_agent_shim(definition: ProjectDefinition, relative: str) -> bool:
    """Optional provider shims inside independent children are not root authority."""
    binding = definition.repository_orchestration
    if binding is None:
        return True
    path = relative.casefold()
    return not any(
        path == pin.path.casefold() or path.startswith(pin.path.casefold() + "/")
        for pin in binding.repositories
    )


def validate_project_structure(project: LoadedProject) -> dict[str, object]:
    if project.definition.profile != CANONICAL_PROJECT_PROFILE:
        raise ProjectError(
            "project.profile_unsupported",
            f"unsupported project profile: {project.definition.profile}",
        )
    source_intelligence = project.definition.source_intelligence
    paths = {"agent_skill": project.agent_skill}
    for catalog in (
        "component",
        "flavor",
        "skill",
        "mcp",
        "workflow",
        "routing",
        "documentation",
    ):
        for index, path in enumerate(project.roots(catalog)):
            paths[f"{catalog}_roots[{index}]"] = path
    for label, path in paths.items():
        expected = path.is_file() if label == "agent_skill" else path.is_dir()
        if path.is_symlink() or not expected:
            raise ProjectError(
                "project.layout_incomplete",
                f"declared project path is unavailable: {label}",
            )
        if not path.resolve(strict=True).is_relative_to(project.root):
            raise ProjectError(
                "project.layout_escape",
                f"declared project path escapes the project root: {label}",
            )
    _require_disjoint_source_cache_roots(project, paths)
    for relative, marker in CANONICAL_AGENT_SHIMS.items():
        if not project_owns_agent_shim(project.definition, relative):
            continue
        path = project.root.joinpath(*Path(relative).parts)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ProjectError(
                "project.agent_shim_invalid",
                f"optional agent shim must be a regular file: {relative}",
            )
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(project.root):
            raise ProjectError(
                "project.agent_shim_invalid",
                f"optional agent shim escapes the project root: {relative}",
            )
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProjectError(
                "project.agent_shim_invalid",
                f"optional agent shim is unreadable: {relative}",
            ) from exc
        if marker not in content:
            raise ProjectError(
                "project.agent_shim_invalid",
                "optional agent shim does not delegate to the provider-neutral "
                f"onboarding skill: {relative}",
            )
    return {
        "project_identity": project.definition.identity.uri,
        "project_id": project.definition.project_id,
        "profile": project.definition.profile,
        "agent_skill": project.definition.agent_skill,
        "source_intelligence": {
            "state": "configured",
            "policy": source_intelligence.to_dict(),
        },
        "default_flavor_selectors": list(project.definition.default_flavor_selectors),
        "catalogs": {
            catalog: [
                path.relative_to(project.root).as_posix()
                for path in project.roots(catalog)
            ]
            for catalog in (
                "component",
                "flavor",
                "skill",
                "workflow",
                "routing",
                "documentation",
            )
        },
    }


__all__ = [
    "CANONICAL_AGENT_SHIMS",
    "DEFAULT_MAXIMUM_PINNED_INPUT_BYTES",
    "DEFAULT_MAXIMUM_PINNED_INPUT_FILES",
    "DEFAULT_MAXIMUM_PINNED_INPUT_TOTAL_BYTES",
    "LoadedProject",
    "PROJECT_FILENAME",
    "PinnedInputClosure",
    "PinnedInputClosureError",
    "ProjectError",
    "discover_project",
    "documentation_asset_paths",
    "documentation_files",
    "documentation_paths",
    "load_project",
    "project_boundary",
    "source_to_specification_skill_paths",
    "specification_to_source_skill_paths",
    "validate_project_structure",
]

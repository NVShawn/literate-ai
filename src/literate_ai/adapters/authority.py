"""Git-friendly, append-only storage for Component authority projections."""

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
from errno import EEXIST
from pathlib import Path

from literate_ai._cache_lock import _is_windows, _windows_identity_matches
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.application.source_promotion import (
    qualify_locked_source_promotion,
)
from literate_ai.contracts.authority import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.source_to_specification.promotion_materialization import (
    VerifiedSourcePromotionEvidence,
)


class AuthorityProjectionStoreError(RuntimeError):
    pass


# Windows ``msvcrt.locking`` has no blocking-with-clean-error mode: ``LK_LOCK``
# retries internally for ~10s and then raises a bare ``OSError``. We poll
# ``LK_NBLCK`` on a bounded deadline instead so transient contention is retried
# while a genuine failure raises promptly. POSIX keeps its blocking ``flock``.
_APPEND_LOCK_TIMEOUT_SECONDS = 30.0
_APPEND_LOCK_RETRY_SECONDS = 0.01


def _append_lock_identity_matches(
    path: Path,
    path_metadata: os.stat_result,
    descriptor: int,
    descriptor_metadata: os.stat_result,
) -> bool:
    """Whether ``path`` and the held descriptor name the same file.

    POSIX uses the exact ``(st_dev, st_ino)`` compare. On Windows ``os.fstat``
    frequently reports ``st_ino == 0`` (CPython issue #76), so the bare inode
    compare is unreliable; fall back to ``GetFileInformationByHandle`` identity.
    """

    if _is_windows() and (path_metadata.st_ino == 0 or descriptor_metadata.st_ino == 0):
        return _windows_identity_matches(path, descriptor)
    return (path_metadata.st_dev, path_metadata.st_ino) == (
        descriptor_metadata.st_dev,
        descriptor_metadata.st_ino,
    )


def _acquire_exclusive(descriptor: int) -> None:
    """Take an exclusive advisory lock on byte 0, blocking until acquired.

    POSIX uses a single blocking ``flock(LOCK_EX)``. Windows polls non-blocking
    ``LK_NBLCK`` on a bounded deadline; contention (EACCES/EAGAIN/EDEADLOCK) is
    retried, any other error or the deadline propagates as ``OSError``.
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
    deadline = time.monotonic() + _APPEND_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in contention or time.monotonic() >= deadline:
                raise
            time.sleep(_APPEND_LOCK_RETRY_SECONDS)


def _release_exclusive(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


class FileAuthorityProjectionStore:
    CURRENT_SCHEMA = "literate-ai/component-authority-current@1"

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve(strict=True)

    def append(self, projection: ComponentAuthorityProjection) -> Path:
        return self._append(projection, qualification_authorized=False)

    def append_qualified(
        self,
        authority: LockedGenerationAuthority,
        evidence: VerifiedSourcePromotionEvidence,
    ) -> Path:
        """Persist a qualified successor only through current lock admission."""

        projection = qualify_locked_source_promotion(authority, evidence)
        return self._append(projection, qualification_authorized=True)

    def _append(
        self,
        projection: ComponentAuthorityProjection,
        *,
        qualification_authorized: bool,
    ) -> Path:
        root = self._component_root(projection.component_coordinate, create=True)
        with self._append_lock(root):
            current = self._read_current(root, required=False)
            prior = None if current is None else current.identity
            if projection.prior_projection_identity != prior:
                raise AuthorityProjectionStoreError(
                    "projection does not extend the current immutable history"
                )
            if current is None:
                if projection.prior_projection_identity is not None:
                    raise AuthorityProjectionStoreError(
                        "initial projection must not name a predecessor"
                    )
            else:
                try:
                    projection.require_successor_of(current)
                except ValueError as exc:
                    raise AuthorityProjectionStoreError(
                        "projection is not a legal authority transition"
                    ) from exc
            if (
                projection.state
                is ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
                and not qualification_authorized
            ):
                raise AuthorityProjectionStoreError(
                    "qualification-v2-required: qualified authority must pass "
                    "locked lifecycle admission"
                )

            projections = self._direct_directory(root, "projections", create=False)
            path = projections / f"{projection.identity.digest}.json"
            content = canonical_json_bytes(projection.to_dict()) + b"\n"
            if path.exists() or path.is_symlink():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_bytes() != content
                ):
                    raise AuthorityProjectionStoreError(
                        "projection identity is already occupied by different bytes"
                    )
            else:
                self._atomic_write(path, content, exclusive=True)

            # Keep the pointer update a compare-and-swap even while holding the
            # cooperative process lock.  This detects a writer that ignored the lock
            # instead of silently replacing its branch.
            observed = self._read_current(root, required=False)
            observed_identity = None if observed is None else observed.identity
            if observed_identity != prior:
                raise AuthorityProjectionStoreError(
                    "current authority projection changed during append"
                )
            pointer = {
                "schema": self.CURRENT_SCHEMA,
                "component_coordinate": projection.component_coordinate,
                "projection_identity": projection.identity.uri,
            }
            self._atomic_write(
                root / "current.json", canonical_json_bytes(pointer) + b"\n"
            )
            return path

    def current(self, component_coordinate: str) -> ComponentAuthorityProjection:
        result = self._read_current(
            self._component_root(component_coordinate, create=False), required=True
        )
        assert result is not None
        return result

    def history(
        self, component_coordinate: str
    ) -> tuple[ComponentAuthorityProjection, ...]:
        current = self.current(component_coordinate)
        root = self._component_root(component_coordinate, create=False)
        reverse: list[ComponentAuthorityProjection] = []
        seen = set()
        while True:
            if current.identity in seen:
                raise AuthorityProjectionStoreError(
                    "projection history contains a cycle"
                )
            seen.add(current.identity)
            reverse.append(current)
            if current.prior_projection_identity is None:
                break
            successor = current
            current = self._read_projection(
                root, current.prior_projection_identity.digest
            )
            try:
                successor.require_successor_of(current)
            except ValueError as exc:
                raise AuthorityProjectionStoreError(
                    "projection history contains an illegal transition"
                ) from exc
        reverse.reverse()
        return tuple(reverse)

    def coordinates(self) -> tuple[str, ...]:
        try:
            base = self._authority_base(create=False)
        except FileNotFoundError:
            return ()
        values = []
        for path in sorted(base.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise AuthorityProjectionStoreError(
                    "authority store contains an unsafe entry"
                )
            current = self._read_current(path, required=True)
            assert current is not None
            values.append(current.component_coordinate)
        return tuple(values)

    def _component_root(self, coordinate: str, *, create: bool) -> Path:
        digest = hashlib.sha256(coordinate.encode("utf-8")).hexdigest()
        try:
            base = self._authority_base(create=create)
            root = self._direct_directory(base, digest, create=create)
            self._direct_directory(root, "projections", create=create)
        except FileNotFoundError as exc:
            raise AuthorityProjectionStoreError(
                "authority projection is missing"
            ) from exc
        return root

    def _authority_base(self, *, create: bool) -> Path:
        current = self.project_root
        for name in ("provenance", "component-authority"):
            current = self._direct_directory(current, name, create=create)
        return current

    @staticmethod
    def _direct_directory(parent: Path, name: str, *, create: bool) -> Path:
        path = parent / name
        created = False
        if create:
            try:
                path.mkdir()
                created = True
            except FileExistsError:
                pass
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise AuthorityProjectionStoreError(
                "authority path could not be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AuthorityProjectionStoreError("authority path is unsafe")
        if created:
            FileAuthorityProjectionStore._fsync_directory(parent)
        return path

    @contextmanager
    def _append_lock(self, root: Path) -> Iterator[None]:
        path = root / ".append.lock"
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise AuthorityProjectionStoreError(
                "authority append lock is unsafe or unavailable"
            ) from exc
        locked = False
        try:
            opened = os.fstat(descriptor)
            observed = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or not _append_lock_identity_matches(path, observed, descriptor, opened)
            ):
                raise AuthorityProjectionStoreError("authority append lock is unsafe")
            # Windows msvcrt.locking locks a byte range that must exist in the
            # file: locking byte 0 of a 0-byte file returns EACCES permanently.
            # Write the placeholder byte on the fresh, not-yet-locked file first
            # (it cannot collide with a holder that does not exist yet), then take
            # the exclusive lock. Skip the write when the file already has content.
            if opened.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
            _acquire_exclusive(descriptor)
            locked = True
            yield
        except AuthorityProjectionStoreError:
            raise
        except OSError as exc:
            raise AuthorityProjectionStoreError("authority append lock failed") from exc
        finally:
            if locked:
                try:
                    _release_exclusive(descriptor)
                except OSError:
                    pass
            os.close(descriptor)

    def _read_current(
        self, root: Path, *, required: bool
    ) -> ComponentAuthorityProjection | None:
        path = root / "current.json"
        if not path.exists():
            if required:
                raise AuthorityProjectionStoreError("authority projection is missing")
            return None
        if path.is_symlink() or not path.is_file():
            raise AuthorityProjectionStoreError("current projection pointer is unsafe")
        try:
            value = json.loads(path.read_bytes())
            if (
                set(value) != {"schema", "component_coordinate", "projection_identity"}
                or value["schema"] != self.CURRENT_SCHEMA
            ):
                raise ValueError
            digest = value["projection_identity"].removeprefix("sha256:")
            projection = self._read_projection(root, digest)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise AuthorityProjectionStoreError(
                "current projection pointer is invalid"
            ) from exc
        if projection.component_coordinate != value["component_coordinate"]:
            raise AuthorityProjectionStoreError(
                "current pointer coordinate does not match projection"
            )
        return projection

    def _read_projection(self, root: Path, digest: str) -> ComponentAuthorityProjection:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AuthorityProjectionStoreError("projection identity is invalid")
        projections = self._direct_directory(root, "projections", create=False)
        path = projections / f"{digest}.json"
        if path.is_symlink() or not path.is_file():
            raise AuthorityProjectionStoreError(
                "projection content is missing or unsafe"
            )
        try:
            projection = ComponentAuthorityProjection.from_dict(
                json.loads(path.read_bytes())
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AuthorityProjectionStoreError(
                "projection content is invalid"
            ) from exc
        if projection.identity.digest != digest:
            raise AuthorityProjectionStoreError(
                "projection content does not match its identity"
            )
        return projection

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, exclusive: bool = False) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                try:
                    os.link(temporary, path)
                except OSError as exc:
                    if exc.errno == EEXIST:
                        raise AuthorityProjectionStoreError(
                            "refusing to overwrite immutable projection"
                        ) from exc
                    raise
                os.unlink(temporary)
            else:
                os.replace(temporary, path)
            FileAuthorityProjectionStore._fsync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["AuthorityProjectionStoreError", "FileAuthorityProjectionStore"]

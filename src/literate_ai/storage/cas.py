"""Immutable, content-addressed filesystem storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts.blobs import BlobRef

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUFFER_SIZE = 1024 * 1024


class StorageError(RuntimeError):
    """Base error for stable storage failures."""


class StorageSafetyError(StorageError):
    """A managed path is missing, symbolic, or outside its storage root."""


class BlobNotFoundError(StorageError):
    """A requested immutable blob is not present."""


class BlobIntegrityError(StorageError):
    """Stored content does not match its declared identity."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for portable identities."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _native_cas_path(path: Path) -> str | Path:
    """Use the Win32 extended namespace consistently at the CAS blob I/O boundary."""
    if os.name != "nt":
        return path
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if not path.is_absolute():
        raise StorageSafetyError("CAS native paths must be absolute")
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


class FileSystemCAS:
    """A filesystem CAS that never overwrites an existing blob identity."""

    def __init__(self, root: str | Path, *, create: bool = True) -> None:
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise StorageSafetyError("CAS root must not be a symbolic link")
        if create:
            configured.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not configured.is_dir():
            raise StorageSafetyError("read-only CAS root must already exist")
        self.root = configured.resolve(strict=True)
        if not self.root.is_dir():
            raise StorageSafetyError("CAS root must be a directory")
        self.blob_root = Path(_native_cas_path(self.root / "blobs" / "sha256"))
        if create:
            self.blob_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not self.blob_root.is_dir():
            raise StorageSafetyError("read-only CAS blob root must already exist")
        self._require_safe_directory(self.blob_root)

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> BlobRef:
        """Store bytes once and return their immutable identity."""

        digest = hashlib.sha256(content).hexdigest()
        reference = BlobRef(
            digest=digest,
            size=len(content),
            media_type=media_type,
        )
        self._store_stream(reference, _BytesReader(content))
        return reference

    def put_file(
        self,
        source: str | Path,
        *,
        media_type: str = "application/octet-stream",
    ) -> BlobRef:
        """Store a regular file without following a symbolic link."""

        path = Path(source)
        if path.is_symlink():
            raise StorageSafetyError("CAS input must be a regular non-symbolic file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StorageSafetyError(
                "CAS input must be a readable non-symbolic file"
            ) from exc
        input_file = os.fdopen(descriptor, "rb")
        if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
            input_file.close()
            raise StorageSafetyError("CAS input must be a regular file")
        digest = hashlib.sha256()
        size = 0
        with input_file:
            while chunk := input_file.read(_BUFFER_SIZE):
                digest.update(chunk)
                size += len(chunk)
            reference = BlobRef(
                digest=digest.hexdigest(),
                size=size,
                media_type=media_type,
            )
            input_file.seek(0)
            self._store_stream(reference, input_file)
        return reference

    def put_manifest(
        self,
        value: Mapping[str, Any],
        *,
        media_type: str = "application/vnd.literate-ai.manifest+json",
    ) -> BlobRef:
        return self.put_bytes(canonical_json_bytes(value), media_type=media_type)

    def contains(self, reference: BlobRef, *, verify: bool = True) -> bool:
        path = self.path_for(reference)
        if path.is_symlink() or not path.is_file():
            return False
        if not verify:
            return path.stat().st_size == reference.size
        try:
            self.verify(reference)
        except BlobIntegrityError:
            return False
        return True

    def verify(self, reference: BlobRef) -> None:
        self._read_verified(reference, collect=False)

    def get_bytes(self, reference: BlobRef) -> bytes:
        """Return bytes verified from the same no-follow regular-file descriptor."""

        content = self._read_verified(reference, collect=True)
        assert content is not None
        return content

    def copy_to(self, reference: BlobRef, destination: str | Path) -> None:
        """Copy one verified blob to a new file with bounded, same-handle reads."""
        target = Path(destination).absolute()
        require_safe_directory(target.parent)
        # Exclusive creation never truncates an existing file or follows its link.
        with target.open("x+b") as output:
            owned = os.fstat(output.fileno())
            try:
                self._read_verified(reference, collect=False, sink=output)
                output.flush()
                output.seek(0)
                digest = hashlib.sha256()
                size = 0
                while chunk := output.read(
                    min(_BUFFER_SIZE, reference.size - size + 1)
                ):
                    size += len(chunk)
                    if size > reference.size:
                        raise BlobIntegrityError(
                            "copied blob exceeded its expected size"
                        )
                    digest.update(chunk)
                if size != reference.size or digest.hexdigest() != reference.digest:
                    raise BlobIntegrityError("copied blob failed verification")
                require_safe_directory(target.parent)
                current = target.lstat()
                if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                    raise StorageSafetyError("copied blob destination was replaced")
            except BaseException:
                output.close()
                # Never unlink a path replaced by another writer after our creation.
                try:
                    current = target.lstat()
                    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        target.unlink()
                except FileNotFoundError:
                    pass
                raise

    def get_manifest(self, reference: BlobRef) -> dict[str, Any]:
        try:
            value = json.loads(self.get_bytes(reference))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlobIntegrityError(
                f"manifest {reference.identity} is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(value, dict):
            raise BlobIntegrityError("manifest root must be a JSON object")
        return value

    def path_for(self, reference: BlobRef) -> Path:
        # BlobRef validation makes each interpolated segment non-traversable.
        path = self.blob_root / reference.digest[:2] / reference.digest
        if not path.parent.resolve().is_relative_to(self.blob_root):
            raise StorageSafetyError("blob path escaped the CAS root")
        return path

    def _read_verified(
        self, reference: BlobRef, *, collect: bool, sink: BinaryIO | None = None
    ) -> bytes | None:
        """Hash and optionally retain one immutable blob through a single open file."""

        path = self.path_for(reference)
        if path.is_symlink():
            raise StorageSafetyError("CAS blob must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise BlobNotFoundError(
                f"blob {reference.identity} is not present"
            ) from exc
        except OSError as exc:
            raise StorageSafetyError("CAS blob could not be opened safely") from exc
        digest = hashlib.sha256()
        size = 0
        chunks: list[bytes] | None = [] if collect else None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageSafetyError("CAS blob must be a regular file")
            if metadata.st_size != reference.size:
                raise BlobIntegrityError(
                    f"blob {reference.identity} failed size verification"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as input_file:
                # The file can grow after fstat. Read at most one byte beyond the
                # admitted size so growth is rejected without collecting it all.
                while chunk := input_file.read(
                    min(_BUFFER_SIZE, reference.size - size + 1)
                ):
                    size += len(chunk)
                    if size > reference.size:
                        raise BlobIntegrityError(
                            f"blob {reference.identity} exceeded its expected size"
                        )
                    digest.update(chunk)
                    if sink is not None:
                        sink.write(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
        finally:
            os.close(descriptor)
        if size != reference.size or digest.hexdigest() != reference.digest:
            raise BlobIntegrityError(f"blob {reference.identity} failed verification")
        return None if chunks is None else b"".join(chunks)

    def iter_refs(self) -> Iterator[BlobRef]:
        """Yield verified identities for all regular blobs in the store."""

        for prefix in sorted(self.blob_root.iterdir()):
            if prefix.is_symlink() or not prefix.is_dir():
                raise StorageSafetyError("CAS prefix must be a regular directory")
            for path in sorted(prefix.iterdir()):
                if path.is_symlink() or not path.is_file():
                    raise StorageSafetyError("CAS blob entry must be a regular file")
                if not _SHA256.fullmatch(path.name):
                    raise StorageSafetyError("CAS contains an invalid blob filename")
                yield BlobRef(
                    digest=path.name,
                    size=path.stat().st_size,
                )

    def _store_stream(self, reference: BlobRef, source: BinaryIO) -> None:
        destination = self.path_for(reference)
        prefix = destination.parent
        if prefix.exists() and prefix.is_symlink():
            raise StorageSafetyError("CAS prefix must not be a symbolic link")
        prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_safe_directory(prefix)
        if destination.exists() or destination.is_symlink():
            self.verify(reference)
            return

        fd, temp_name = tempfile.mkstemp(prefix=".blob-", dir=prefix)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := source.read(_BUFFER_SIZE):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o600)
            if size != reference.size or digest.hexdigest() != reference.digest:
                raise BlobIntegrityError("input changed while it was stored")
            try:
                # Linking is an atomic create-if-absent operation. Unlike replace(),
                # it cannot overwrite an immutable identity won by another process.
                os.link(_native_cas_path(temp_path), _native_cas_path(destination))
            except FileExistsError:
                self.verify(reference)
            else:
                self._fsync_directory(prefix)
        finally:
            temp_path.unlink(missing_ok=True)

    def _require_safe_directory(self, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise StorageSafetyError("managed CAS path must be a regular directory")
        resolved = path.resolve(strict=True)
        native_root = Path(_native_cas_path(self.root))
        if resolved != native_root and not resolved.is_relative_to(native_root):
            raise StorageSafetyError("managed CAS path escaped its root")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            # Windows provides durable file flushes but the CRT cannot open a
            # directory descriptor for fsync.
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _BytesReader:
    """Tiny BinaryIO-compatible reader without copying through BytesIO."""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        end = len(self._content) if size < 0 else self._offset + size
        value = self._content[self._offset : end]
        self._offset += len(value)
        return value

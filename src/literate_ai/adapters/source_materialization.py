"""Capture one exact current project tree for bounded remote materialization."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.cache.filesystem import (
    FileSystemSourceCache,
    SourceCacheError,
    _native_filesystem_path,
)
from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    ContentIdentity,
    ContentReference,
    ExecutionDispatchRequest,
    ExecutionSourceMaterialization,
    ExecutionSourceMaterializationKind,
    HashAlgorithm,
)
from literate_ai.remote_source_guard import source_paths_identity, source_tree_identity

_EXCLUDED_NAMES = frozenset(
    {
        ".codegraph",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "_build",
        "node_modules",
    }
)


class SourceMaterializationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class CapturedSourceArchive:
    path: Path
    materialization: ExecutionSourceMaterialization
    accepted_source_cache_path: Path | None = None


def discover_accepted_source_provider_binding(
    project_root: Path,
) -> tuple[str, ContentIdentity] | None:
    """Recover one portable provider binding from valid transported membership."""

    from literate_ai.cache_directories import resolve_cache_directories

    root = Path(project_root).resolve(strict=True)
    cache_root = resolve_cache_directories(root).build_dir / "accepted-source-cache"
    if not cache_root.is_dir() or cache_root.is_symlink():
        return None
    try:
        cache = FileSystemSourceCache(
            "dispatch-provider-discovery", cache_root, writable=False
        )
        entries = cache.verified_published_entries()
    except (OSError, SourceCacheError) as exc:
        raise SourceMaterializationError(
            "execution.accepted_source_cache_invalid",
            "accepted-source provider binding requires valid cache membership",
        ) from exc
    bindings: set[tuple[str, ContentIdentity]] = set()
    for entry in entries:
        cache_key = (
            entry.derivation.cache_key
            if isinstance(entry, AcceptedSourceCacheEntry)
            else entry.cache_key
        )
        bindings.add(
            (
                cache_key.model_binding.provider_id,
                cache_key.coding_cli_tool_binding_identity,
            )
        )
    if not bindings:
        return None
    if len(bindings) != 1:
        raise SourceMaterializationError(
            "execution.accepted_source_provider_ambiguous",
            "accepted-source cache contains more than one provider/tool binding",
        )
    return next(iter(bindings))


def _excluded_directory_paths(root: Path) -> tuple[str, ...]:
    from literate_ai.cache_directories import resolve_cache_directories

    directories = resolve_cache_directories(root)
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in (directories.build_dir, directories.obj_dir)
            if path.is_relative_to(root)
        )
    )


def _is_excluded_path(path: str, excluded_directories: tuple[str, ...]) -> bool:
    return any(
        path == directory or path.startswith(f"{directory}/")
        for directory in excluded_directories
    )


def _git_paths(
    root: Path, excluded_directories: tuple[str, ...] = ()
) -> tuple[str, ...] | None:
    try:
        completed = run_with_tree_kill(
            (
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ),
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
            },
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        values = tuple(
            item.decode("utf-8") for item in completed.stdout.split(b"\0") if item
        )
    except UnicodeDecodeError as exc:
        raise SourceMaterializationError(
            "execution.source_path_invalid", "Git reported a non-UTF-8 source path"
        ) from exc
    return tuple(
        item
        for item in values
        if not any(part in _EXCLUDED_NAMES for part in item.split("/"))
        and not _is_excluded_path(item, excluded_directories)
    )


def _filesystem_paths(
    root: Path, excluded_directories: tuple[str, ...] = ()
) -> tuple[str, ...]:
    paths: list[str] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        retained: list[str] = []
        for name in sorted(directories):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if name in _EXCLUDED_NAMES or _is_excluded_path(
                relative, excluded_directories
            ):
                continue
            if path.is_symlink():
                paths.append(path.relative_to(root).as_posix())
            else:
                retained.append(name)
        directories[:] = retained
        paths.extend(
            (base / name).relative_to(root).as_posix() for name in sorted(files)
        )
    return tuple(sorted(paths))


def _archive_bytes(root: Path, paths: tuple[str, ...]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        top = tarfile.TarInfo("literate-ai")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        top.mtime = 0
        archive.addfile(top)
        for relative in paths:
            source = root.joinpath(*relative.split("/"))
            metadata = source.lstat()
            member = tarfile.TarInfo(f"literate-ai/{relative}")
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            if stat.S_ISLNK(metadata.st_mode):
                member.type = tarfile.SYMTYPE
                member.mode = 0o777
                member.linkname = os.readlink(source)
                archive.addfile(member)
            elif stat.S_ISREG(metadata.st_mode):
                content = source.read_bytes()
                after = source.lstat()
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise SourceMaterializationError(
                        "execution.source_changed",
                        f"source changed while captured: {relative}",
                    )
                member.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            else:
                raise SourceMaterializationError(
                    "execution.source_entry_invalid",
                    f"source entry is not a regular file or symlink: {relative}",
                )
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as stream:
        stream.write(raw.getvalue())
    return output.getvalue()


class _BoundedArchiveWriter:
    """Hash compressed archive bytes while enforcing the transport limit."""

    def __init__(self, stream, maximum: int) -> None:
        self.stream = stream
        self.maximum = maximum
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, content: bytes) -> int:
        if self.size + len(content) > self.maximum:
            raise SourceMaterializationError(
                "execution.source_archive_oversized",
                "source archive exceeds its configured transport limit",
            )
        written = self.stream.write(content)
        if written != len(content):
            raise OSError("source archive write was incomplete")
        self.size += written
        self.digest.update(content)
        return written

    def flush(self) -> None:
        self.stream.flush()

    def tell(self) -> int:
        return self.size


def write_bounded_source_archive(
    root: Path,
    paths: tuple[str, ...],
    destination: Path,
    *,
    max_source_bytes: int,
    max_archive_bytes: int,
    max_entries: int,
) -> tuple[ContentIdentity, ContentIdentity, int]:
    """Stream the canonical source archive to a new bounded regular file."""

    created = False
    limits = (max_source_bytes, max_archive_bytes, max_entries)
    if any(type(value) is not int or value < 1 for value in limits):
        raise SourceMaterializationError(
            "execution.source_archive_limit_invalid",
            "source archive limits must be positive integers",
        )
    if not 1 <= len(paths) <= max_entries:
        raise SourceMaterializationError(
            "execution.source_entry_limit",
            "source projection exceeds its configured entry limit",
        )
    try:
        selected_root = Path(root).resolve(strict=True)
        manifest = ContentIdentity.parse_uri(
            source_paths_identity(selected_root, paths)
        )
        total = 0
        for relative in paths:
            source = selected_root.joinpath(*relative.split("/"))
            metadata = source.lstat()
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
            elif stat.S_ISLNK(metadata.st_mode):
                total += len(os.fsencode(os.readlink(source)))
            else:
                raise OSError
            if total > max_source_bytes:
                raise SourceMaterializationError(
                    "execution.source_projection_oversized",
                    "source projection exceeds its configured content limit",
                )
        selected_destination = Path(destination)
        if (
            selected_destination.exists()
            or selected_destination.is_symlink()
            or not selected_destination.parent.is_dir()
        ):
            raise OSError
        with tempfile.TemporaryFile() as raw:
            with tarfile.open(
                fileobj=raw, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                top = tarfile.TarInfo("literate-ai")
                top.type = tarfile.DIRTYPE
                top.mode = 0o755
                top.mtime = 0
                archive.addfile(top)
                for relative in paths:
                    source = selected_root.joinpath(*relative.split("/"))
                    metadata = source.lstat()
                    member = tarfile.TarInfo(f"literate-ai/{relative}")
                    member.mtime = 0
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    if stat.S_ISLNK(metadata.st_mode):
                        member.type = tarfile.SYMTYPE
                        member.mode = 0o777
                        member.linkname = os.readlink(source)
                        archive.addfile(member)
                    elif stat.S_ISREG(metadata.st_mode):
                        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(source, flags)
                        try:
                            opened = os.fstat(descriptor)
                            if not stat.S_ISREG(opened.st_mode) or (
                                opened.st_dev,
                                opened.st_ino,
                            ) != (metadata.st_dev, metadata.st_ino):
                                raise OSError
                            member.mode = 0o755 if opened.st_mode & 0o111 else 0o644
                            member.size = opened.st_size
                            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                                archive.addfile(member, stream)
                        finally:
                            os.close(descriptor)
                    else:
                        raise OSError
                    after = source.lstat()
                    if (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise SourceMaterializationError(
                            "execution.source_changed",
                            f"source changed while captured: {relative}",
                        )
            raw.seek(0)
            with selected_destination.open("xb") as output:
                created = True
                bounded = _BoundedArchiveWriter(output, max_archive_bytes)
                with gzip.GzipFile(
                    fileobj=bounded, mode="wb", mtime=0, filename=""
                ) as compressed:
                    shutil.copyfileobj(raw, compressed, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        after_manifest = ContentIdentity.parse_uri(
            source_paths_identity(selected_root, paths)
        )
        if after_manifest != manifest:
            raise SourceMaterializationError(
                "execution.source_changed",
                "source projection changed while captured",
            )
        return (
            manifest,
            ContentIdentity(HashAlgorithm.SHA256, bounded.digest.hexdigest()),
            bounded.size,
        )
    except SourceMaterializationError:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise SourceMaterializationError(
            "execution.source_capture_failed",
            "source projection could not be captured",
        ) from exc


def _cache_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories if not (current_path / name).is_symlink()
        )
        for name in sorted(files):
            candidate = current_path / name
            if candidate.is_symlink():
                raise SourceMaterializationError(
                    "execution.accepted_source_cache_capture_failed",
                    "accepted-source-cache must not contain symlinks",
                )
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceMaterializationError(
                    "execution.accepted_source_cache_capture_failed",
                    "accepted-source-cache must contain only regular files",
                )
            paths.append(candidate.relative_to(root).as_posix())
    return tuple(sorted(paths))


def _cache_archive_bytes(root: Path, paths: tuple[str, ...]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative in paths:
            source = root.joinpath(*relative.split("/"))
            content = source.read_bytes()
            member = tarfile.TarInfo(relative)
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mode = 0o644
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as stream:
        stream.write(raw.getvalue())
    return output.getvalue()


def capture_accepted_source_cache_archive(
    project_root: Path,
    *,
    directory: Path,
) -> ContentReference | None:
    """Stage the project's writable accepted-source-cache tree for remote restore.

    Every entry inside is independently content-addressed by the cache system
    itself (a hash embedded in its own path), so staging the whole tree rather
    than a computed subset is both simple and safe: a worker restoring more
    admitted membership than one request strictly needs is never a correctness
    or security problem, only a transport-size one.
    """

    from literate_ai.cache_directories import resolve_cache_directories

    directories = resolve_cache_directories(Path(project_root).resolve(strict=True))
    cache_root = Path(
        _native_filesystem_path(directories.build_dir / "accepted-source-cache")
    )
    if not cache_root.is_dir() or cache_root.is_symlink():
        return None
    paths = _cache_paths(cache_root)
    if not paths:
        return None
    content = _cache_archive_bytes(cache_root, paths)
    path = directory / "accepted-source-cache.tar.gz"
    if path.exists() or path.is_symlink():
        raise SourceMaterializationError(
            "execution.source_staging_invalid",
            "accepted-source-cache staging archive must not already exist",
        )
    path.write_bytes(content)
    identity = ContentIdentity(
        HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
    )
    return ContentReference(
        "accepted-source-cache-archive",
        "staged:accepted-source-cache.tar.gz",
        identity,
    )


def capture_source_archive(
    project_root: Path,
    request: ExecutionDispatchRequest,
    *,
    directory: Path | None = None,
) -> CapturedSourceArchive:
    """Capture current authored project bytes and bind both tree and transport."""

    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise SourceMaterializationError(
            "execution.source_root_invalid", "project source root is unavailable"
        ) from exc
    if not root.is_dir() or root.is_symlink():
        raise SourceMaterializationError(
            "execution.source_root_invalid",
            "project source root must be a non-symlink directory",
        )
    excluded_directories = _excluded_directory_paths(root)
    paths = _git_paths(root, excluded_directories)
    if paths is None:
        paths = _filesystem_paths(root, excluded_directories)
    try:
        before = (
            source_paths_identity(root, paths) if paths else source_tree_identity(root)
        )
        content = _archive_bytes(root, paths)
        after = (
            source_paths_identity(root, paths) if paths else source_tree_identity(root)
        )
    except (OSError, ValueError) as exc:
        raise SourceMaterializationError(
            "execution.source_capture_failed", "project source could not be captured"
        ) from exc
    if before != after:
        raise SourceMaterializationError(
            "execution.source_changed", "project source changed while captured"
        )
    owner = (
        Path(directory).resolve(strict=True)
        if directory is not None
        else Path(tempfile.mkdtemp(prefix="litai-source-materialization-")).resolve()
    )
    if not owner.is_dir() or owner.is_symlink():
        raise SourceMaterializationError(
            "execution.source_staging_invalid",
            "source staging directory must be a non-symlink directory",
        )
    path = owner / "source.tar.gz"
    if path.exists() or path.is_symlink():
        raise SourceMaterializationError(
            "execution.source_staging_invalid",
            "source staging archive must not already exist",
        )
    path.write_bytes(content)
    transport_identity = ContentIdentity(
        HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
    )
    accepted_source_cache_reference: ContentReference | None = None
    accepted_source_cache_path: Path | None = None
    if request.accepted_source_only:
        accepted_source_cache_reference = capture_accepted_source_cache_archive(
            root, directory=owner
        )
        if accepted_source_cache_reference is not None:
            accepted_source_cache_path = owner / "accepted-source-cache.tar.gz"
    materialization = ExecutionSourceMaterialization(
        ExecutionSourceMaterializationKind.ARCHIVE,
        request.identity,
        ContentIdentity.parse_uri(before),
        archive_reference=ContentReference(
            "source-archive", "staged:source.tar.gz", transport_identity
        ),
        accepted_source_cache_reference=accepted_source_cache_reference,
    )
    return CapturedSourceArchive(path, materialization, accepted_source_cache_path)


__all__ = [
    "CapturedSourceArchive",
    "SourceMaterializationError",
    "capture_accepted_source_cache_archive",
    "capture_source_archive",
    "discover_accepted_source_provider_binding",
    "write_bounded_source_archive",
]

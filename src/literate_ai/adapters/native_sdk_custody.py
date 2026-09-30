"""Capture and relocate verified original-source SDK bytes without loading code."""

from __future__ import annotations

import shutil
import stat
import tempfile
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts.executable_components.commands import LibraryImportSurface
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_sdks import NativeSdkFile, NativeSdkSnapshot
from literate_ai.storage.cas import FileSystemCAS


def _files(root: Path) -> tuple[tuple[str, int, int, int, int, int], ...]:
    require_safe_directory(root)
    result = []
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("SDK trees must materialize symlinks before capture")
        if stat.S_ISDIR(info.st_mode):
            require_safe_directory(path)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SDK trees may contain only regular files")
        if len(result) >= 16384:
            raise ValueError("SDK tree exceeds the file limit")
        result.append(
            (
                path.relative_to(root).as_posix(),
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_mode & 0o111,
            )
        )
    require_safe_directory(root)
    return tuple(sorted(result))


def capture_native_sdk(
    root: Path,
    *,
    store: FileSystemCAS,
    source_lock_identity: ContentIdentity,
    recipe_identity: ContentIdentity,
    target_identity: ContentIdentity,
    license_identity: ContentIdentity,
    import_surface: LibraryImportSurface,
    import_root: str,
    native_libraries: tuple[str, ...],
) -> NativeSdkSnapshot:
    """Capture actual SDK files; declared input identities remain unverified claims."""
    root = Path(root).absolute()
    before = _files(root)
    files = []
    for name, _device, _inode, _size, _modified, executable in before:
        path = root.joinpath(*name.split("/"))
        require_safe_directory(path.parent)
        files.append(NativeSdkFile(name, store.put_file(path), bool(executable)))
    if before != _files(root):
        raise ValueError("SDK tree changed during capture")
    # Re-read the source as well as the CAS: a stable path inventory is not enough.
    for item in files:
        if store.put_file(root.joinpath(*item.path.split("/"))) != item.blob:
            raise ValueError("SDK bytes changed during capture")
        store.verify(item.blob)
    if before != _files(root):
        raise ValueError("SDK tree changed during capture")
    return NativeSdkSnapshot(
        source_lock_identity,
        recipe_identity,
        target_identity,
        license_identity,
        import_surface,
        import_root,
        native_libraries,
        tuple(files),
    )


def materialize_native_sdk(
    snapshot: NativeSdkSnapshot,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    parent: Path,
) -> Path:
    """Return a fresh verified SDK directory; this grants no execution admission."""
    if (
        not isinstance(snapshot, NativeSdkSnapshot)
        or snapshot.identity != expected_identity
    ):
        raise ValueError("SDK snapshot differs from the requested exact identity")
    parent = Path(parent).absolute()
    require_safe_directory(parent)
    for item in snapshot.files:
        store.verify(item.blob)
    staging = Path(tempfile.mkdtemp(prefix=".native-sdk-", dir=parent))
    try:
        for item in snapshot.files:
            store.verify(item.blob)
            path = staging.joinpath(*item.path.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            require_safe_directory(path.parent)
            store.copy_to(item.blob, path)
            if store.put_file(path) != item.blob:
                raise ValueError("SDK bytes changed during materialization")
            path.chmod(0o755 if item.executable else 0o644)
        if tuple(item[0] for item in _files(staging)) != tuple(
            item.path for item in snapshot.files
        ):
            raise ValueError("SDK materialization contains an unexpected file")
        return staging
    except BaseException:
        shutil.rmtree(staging)
        raise

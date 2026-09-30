"""Verify exact remote source material and supervise one bounded worker process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# NOTE: this module is shipped standalone to remote workers (see
# `_linux_command`/`_windows_command` in `scripts/fanout_samples.py`), which
# invoke it as a bare script (`python3 .../remote_source_guard.py ...`)
# without `PYTHONPATH=src`. It must stay import-clean of the rest of the
# `literate_ai` package (stdlib only) -- so `_windows_validated_taskkill`
# below intentionally duplicates the resolution logic in
# `literate_ai.adapters._processes.windows_taskkill_executable` rather than
# importing it. Keep the two in sync if either changes.

_EXCLUDED_ROOTS = {".codegraph", ".git", "_build"}
_EXCLUDED_TREE_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_MANIFEST_SCHEMA = "literate-ai/canonical-source-manifest@1"
_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class SourceGuardError(RuntimeError):
    """Exact source verification or supervision failed."""


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _manifest_identity(entries: list[dict[str, Any]]) -> str:
    content = json.dumps(
        {"schema": _MANIFEST_SCHEMA, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _digest(content)


def _portable_source_path(raw: str) -> tuple[str, ...]:
    """Accept only one canonical path spelling with the same meaning on every host."""

    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceGuardError("source path is not UTF-8") from error
    parsed = PurePosixPath(raw)
    parts = parsed.parts
    if (
        not raw
        or "\\" in raw
        or parsed.is_absolute()
        or parsed.as_posix() != raw
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or unicodedata.normalize("NFC", raw) != raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise SourceGuardError(f"source path is not portable and canonical: {raw!r}")
    for part in parts:
        if (
            part.endswith((" ", "."))
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise SourceGuardError(
                f"source path is not portable and canonical: {raw!r}"
            )
    return parts


def _validated_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate portable uniqueness and prevent file/symlink parent aliasing."""

    by_key: dict[str, str] = {}
    prefixes_by_key: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for entry in entries:
        path = entry.get("path")
        kind = entry.get("kind")
        if not isinstance(path, str) or kind not in {"file", "symlink"}:
            raise SourceGuardError("source manifest entry is invalid")
        _portable_source_path(path)
        key = path.casefold()
        if key in by_key:
            raise SourceGuardError(
                "source paths collide on a case-insensitive host: "
                f"{by_key[key]!r}, {path!r}"
            )
        by_key[key] = path
        kinds[path] = kind
        parts = PurePosixPath(path).parts
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            prefix_key = prefix.casefold()
            previous = prefixes_by_key.get(prefix_key)
            if previous is not None and previous != prefix:
                raise SourceGuardError(
                    "source path prefixes collide on a case-insensitive host: "
                    f"{previous!r}, {prefix!r}"
                )
            prefixes_by_key[prefix_key] = prefix
    for path in kinds:
        parts = PurePosixPath(path).parts
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if parent in kinds:
                raise SourceGuardError(
                    f"source entry is beneath a file or symlink: {path!r}"
                )
    return sorted(entries, key=lambda item: item["path"])


def _symlink_target_identity(path: str, target: bytes) -> str:
    """Validate a portable, repository-confined symlink and bind its exact bytes."""

    try:
        target_text = target.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceGuardError(f"source symlink target is not UTF-8: {path}") from error
    posix_target = PurePosixPath(target_text)
    windows_target = PureWindowsPath(target_text)
    if (
        not target_text
        or "\\" in target_text
        or ":" in target_text
        or posix_target.is_absolute()
        or windows_target.is_absolute()
        or bool(windows_target.drive)
        or unicodedata.normalize("NFC", target_text) != target_text
        or any(
            ord(character) < 32 or ord(character) == 127 for character in target_text
        )
    ):
        raise SourceGuardError(
            f"source symlink target is not portable and repository-relative: {path}"
        )

    depth = len(PurePosixPath(path).parent.parts)
    for part in posix_target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                raise SourceGuardError(
                    f"source symlink target escapes the repository: {path}"
                )
        else:
            if (
                part.endswith((" ", "."))
                or any(character in '<>:"|?*' for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            ):
                raise SourceGuardError(f"source symlink target is not portable: {path}")
            depth += 1
    return _digest(target)


def _excluded(relative: Path, *, operational_exclusions: bool) -> bool:
    if relative.parts[0] == ".git":
        return True
    return operational_exclusions and (
        relative.parts[0] in _EXCLUDED_ROOTS
        or any(part in _EXCLUDED_TREE_NAMES for part in relative.parts)
    )


def source_tree_entries(
    root: Path, *, operational_exclusions: bool = True
) -> list[dict[str, Any]]:
    """Return a stable manifest of regular files and symlinks beneath ``root``."""

    root = root.resolve(strict=True)
    entries: list[dict[str, Any]] = []
    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory = Path(current)
        retained: list[str] = []
        names: list[str] = []
        for name in sorted(directory_names):
            path = directory / name
            relative = path.relative_to(root)
            if _excluded(relative, operational_exclusions=operational_exclusions):
                continue
            if path.is_symlink():
                names.append(name)
            else:
                retained.append(name)
        directory_names[:] = retained
        names.extend(sorted(file_names))
        for name in names:
            path = directory / name
            relative = path.relative_to(root)
            if _excluded(relative, operational_exclusions=operational_exclusions):
                continue
            metadata = path.lstat()
            path_text = relative.as_posix()
            _portable_source_path(path_text)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.fsencode(os.readlink(path))
                after = path.lstat()
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
                    raise SourceGuardError(
                        f"source symlink changed during capture: {path_text}"
                    )
                entries.append(
                    {
                        "kind": "symlink",
                        "path": path_text,
                        "target_identity": _symlink_target_identity(path_text, target),
                    }
                )
            elif stat.S_ISREG(metadata.st_mode):
                content = path.read_bytes()
                after = path.lstat()
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
                    raise SourceGuardError(
                        f"source file changed during capture: {path_text}"
                    )
                entries.append(
                    {
                        "content_identity": _digest(content),
                        "executable": bool(metadata.st_mode & 0o111),
                        "kind": "file",
                        "path": path_text,
                        "size": len(content),
                    }
                )
            else:
                raise SourceGuardError(
                    f"source tree contains unsupported entry: {path_text}"
                )
    return _validated_entries(entries)


def source_tree_identity(root: Path, *, operational_exclusions: bool = True) -> str:
    return _manifest_identity(
        source_tree_entries(root, operational_exclusions=operational_exclusions)
    )


def source_path_entries(root: Path, paths: Sequence[str]) -> list[dict[str, Any]]:
    """Return a stable manifest for an explicit, canonical set of source paths."""

    root = root.resolve(strict=True)
    if len(paths) != len(set(paths)):
        raise SourceGuardError("selected source paths are not unique")
    entries: list[dict[str, Any]] = []
    for raw in sorted(paths):
        parts = _portable_source_path(raw)
        relative = Path(*parts)
        parent = root
        for part in relative.parts[:-1]:
            parent /= part
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SourceGuardError(
                    f"selected source path has an unsafe parent: {raw}"
                )
        path = root.joinpath(*relative.parts)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(os.readlink(path))
            after = path.lstat()
            if (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise SourceGuardError(f"source symlink changed during capture: {raw}")
            entries.append(
                {
                    "kind": "symlink",
                    "path": raw,
                    "target_identity": _symlink_target_identity(raw, target),
                }
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceGuardError(f"selected source path is not a file: {raw}")
        digest = hashlib.sha256()
        size = 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while content := stream.read(1024 * 1024):
                    size += len(content)
                    digest.update(content)
            if size != opened.st_size:
                raise OSError
        except OSError as error:
            raise SourceGuardError(
                f"selected source path could not be read safely: {raw}"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        after = path.lstat()
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
            raise SourceGuardError(f"source file changed during capture: {raw}")
        entries.append(
            {
                "content_identity": f"sha256:{digest.hexdigest()}",
                "executable": bool(metadata.st_mode & 0o111),
                "kind": "file",
                "path": raw,
                "size": size,
            }
        )
    return _validated_entries(entries)


def source_paths_identity(root: Path, paths: Sequence[str]) -> str:
    return _manifest_identity(source_path_entries(root, paths))


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _git(root: Path, *arguments: str, text: bool = False):
    return subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "-c",
            "core.symlinks=true",
            "-C",
            str(root),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=text,
        env=_git_environment(),
    ).stdout


def git_tree_entries(root: Path, revision: str) -> list[dict[str, Any]]:
    """Read one commit's exact regular-file/symlink manifest from Git objects."""

    output = _git(root, "ls-tree", "-rz", "--full-tree", revision)
    entries: list[dict[str, Any]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise SourceGuardError("git ls-tree returned a malformed record")
        mode, object_type, object_id = metadata.decode("ascii").split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceGuardError("Git source path is not UTF-8") from error
        _portable_source_path(path)
        if object_type == "commit" and mode == "160000":
            raise SourceGuardError(
                f"Git submodules require explicit recursive source authority: {path}"
            )
        if object_type != "blob" or mode not in {"100644", "100755", "120000"}:
            raise SourceGuardError(f"unsupported Git tree entry: {path}")
        content = _git(root, "cat-file", "blob", object_id)
        if mode == "120000":
            entries.append(
                {
                    "kind": "symlink",
                    "path": path,
                    "target_identity": _symlink_target_identity(path, content),
                }
            )
        else:
            entries.append(
                {
                    "content_identity": _digest(content),
                    "executable": mode == "100755",
                    "kind": "file",
                    "path": path,
                    "size": len(content),
                }
            )
    return _validated_entries(entries)


def git_tree_identity(root: Path, revision: str) -> str:
    return _manifest_identity(git_tree_entries(root, revision))


def _verify_materialized_entries(
    root: Path,
    expected_entries: list[dict[str, Any]],
    *,
    executable_mode_supported: bool | None = None,
) -> None:
    actual_entries = source_tree_entries(root, operational_exclusions=False)
    actual_by_path = {item["path"]: item for item in actual_entries}
    expected_by_path = {item["path"]: item for item in expected_entries}
    mode_supported = (
        os.name != "nt"
        if executable_mode_supported is None
        else executable_mode_supported
    )
    if set(actual_by_path) != set(expected_by_path):
        raise SourceGuardError("materialized source paths do not match authority")
    for path, expected_entry in expected_by_path.items():
        comparable = dict(actual_by_path[path])
        if not mode_supported and comparable.get("kind") == "file":
            comparable["executable"] = expected_entry["executable"]
        if comparable != expected_entry:
            raise SourceGuardError(
                f"materialized source content does not match authority: {path}"
            )


def verify_materialized_source(
    root: Path,
    expected_entries: Sequence[dict[str, Any]],
    *,
    executable_mode_supported: bool | None = None,
) -> str:
    """Verify bytes against transport authority while retaining executable intent."""

    entries = _validated_entries([dict(entry) for entry in expected_entries])
    _verify_materialized_entries(
        root,
        entries,
        executable_mode_supported=executable_mode_supported,
    )
    return _manifest_identity(entries)


def _materialize_entries(
    destination: Path,
    entries: list[dict[str, Any]],
    payloads: dict[str, bytes],
) -> None:
    """Write one already-validated manifest without following source links."""

    entries = _validated_entries(entries)
    expected_paths = {entry["path"] for entry in entries}
    if set(payloads) != expected_paths:
        raise SourceGuardError("source payloads do not match their manifest")
    if destination.exists() or destination.is_symlink():
        raise SourceGuardError("materialization destination must not exist")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise SourceGuardError("materialization parent is unavailable") from error
    if not parent.is_dir():
        raise SourceGuardError("materialization parent must be a directory")

    for entry in entries:
        path = entry["path"]
        payload = payloads[path]
        if entry["kind"] == "file":
            if (
                len(payload) != entry["size"]
                or _digest(payload) != entry["content_identity"]
            ):
                raise SourceGuardError(f"source file payload is invalid: {path}")
        elif _symlink_target_identity(path, payload) != entry["target_identity"]:
            raise SourceGuardError(f"source symlink payload is invalid: {path}")

    destination.mkdir()
    try:
        directories = {
            parts[:length]
            for entry in entries
            for parts in (_portable_source_path(entry["path"]),)
            for length in range(1, len(parts))
        }
        for parts in sorted(directories, key=lambda item: (len(item), item)):
            destination.joinpath(*parts).mkdir(exist_ok=True)
        for entry in entries:
            if entry["kind"] != "file":
                continue
            path = destination.joinpath(*_portable_source_path(entry["path"]))
            path.write_bytes(payloads[entry["path"]])
            path.chmod(0o755 if entry["executable"] else 0o644)
        for entry in entries:
            if entry["kind"] != "symlink":
                continue
            path = destination.joinpath(*_portable_source_path(entry["path"]))
            target = payloads[entry["path"]].decode("utf-8")
            target_path = path.parent.joinpath(*PurePosixPath(target).parts)
            try:
                path.symlink_to(target, target_is_directory=target_path.is_dir())
            except OSError as error:
                raise SourceGuardError(
                    f"host cannot materialize authoritative symlink: {entry['path']}"
                ) from error
        _verify_materialized_entries(destination, entries)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def extract_source_archive_with_manifest(
    archive_path: Path,
    destination: Path,
    expected: str,
    *,
    max_archive_bytes: int | None = None,
    max_entries: int | None = None,
    max_total_bytes: int | None = None,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Materialize an archive and retain its platform-neutral accepted manifest."""

    if destination.exists() or destination.is_symlink():
        raise SourceGuardError("archive destination must not exist")
    for name, value in (
        ("archive", max_archive_bytes),
        ("entry", max_entries),
        ("content", max_total_bytes),
    ):
        if value is not None and (type(value) is not int or value < 1):
            raise SourceGuardError(f"source archive {name} limit is invalid")
    if max_archive_bytes is not None:
        try:
            if (
                archive_path.is_symlink()
                or not archive_path.is_file()
                or archive_path.stat().st_size > max_archive_bytes
            ):
                raise OSError
        except OSError as error:
            raise SourceGuardError(
                "source archive exceeds its transport bound"
            ) from error
    entries: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    member_kinds: dict[str, str] = {}
    member_keys: dict[str, str] = {}
    member_count = 0
    total_bytes = 0
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (OSError, tarfile.TarError) as error:
        raise SourceGuardError("source archive is unreadable") from error
    with archive:
        for member in archive:
            parts = _portable_source_path(member.name)
            if parts[0] != "literate-ai":
                raise SourceGuardError("archive member is outside its canonical root")
            if len(parts) == 1:
                if not member.isdir():
                    raise SourceGuardError("archive root must be a directory")
                continue
            member_count += 1
            if max_entries is not None and member_count > max_entries:
                raise SourceGuardError("source archive exceeds its entry bound")
            relative = "/".join(parts[1:])
            _portable_source_path(relative)
            key = relative.casefold()
            if key in member_keys:
                raise SourceGuardError(
                    "archive contains duplicate or case-colliding members"
                )
            member_keys[key] = relative
            if member.isdir():
                member_kinds[relative] = "directory"
                continue
            if member.isreg():
                if member.size < 0:
                    raise SourceGuardError(f"archive file size is invalid: {relative}")
                total_bytes += member.size
                if max_total_bytes is not None and total_bytes > max_total_bytes:
                    raise SourceGuardError("source archive exceeds its content bound")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceGuardError(f"archive file has no payload: {relative}")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise SourceGuardError(f"archive file is truncated: {relative}")
                entries.append(
                    {
                        "content_identity": _digest(content),
                        "executable": bool(member.mode & 0o111),
                        "kind": "file",
                        "path": relative,
                        "size": len(content),
                    }
                )
                payloads[relative] = content
                member_kinds[relative] = "file"
                continue
            if member.issym():
                try:
                    target = member.linkname.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise SourceGuardError(
                        f"archive symlink target is not UTF-8: {relative}"
                    ) from error
                total_bytes += len(target)
                if max_total_bytes is not None and total_bytes > max_total_bytes:
                    raise SourceGuardError("source archive exceeds its content bound")
                entries.append(
                    {
                        "kind": "symlink",
                        "path": relative,
                        "target_identity": _symlink_target_identity(relative, target),
                    }
                )
                payloads[relative] = target
                member_kinds[relative] = "symlink"
                continue
            raise SourceGuardError(f"archive member type is unsupported: {relative}")

    for path in member_kinds:
        parts = PurePosixPath(path).parts
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if member_kinds.get(parent) in {"file", "symlink"}:
                raise SourceGuardError(
                    f"archive member is beneath a file or symlink: {path}"
                )
    prefix_keys: dict[str, str] = {}
    for path in member_kinds:
        parts = PurePosixPath(path).parts
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = prefix.casefold()
            previous = prefix_keys.get(key)
            if previous is not None and previous != prefix:
                raise SourceGuardError(
                    "archive member prefixes collide on a case-insensitive host"
                )
            prefix_keys[key] = prefix
    entries = _validated_entries(entries)
    identity = _manifest_identity(entries)
    if identity != expected:
        raise SourceGuardError("archive source identity does not match authority")
    try:
        destination.parent.resolve(strict=True)
    except OSError as error:
        raise SourceGuardError("archive destination parent is unavailable") from error
    destination.mkdir()
    try:
        _materialize_entries(destination / "literate-ai", entries, payloads)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return identity, tuple(dict(entry) for entry in entries)


def extract_source_archive(archive_path: Path, destination: Path, expected: str) -> str:
    """Safely materialize one authenticated ``literate-ai/`` source archive."""

    identity, _entries = extract_source_archive_with_manifest(
        archive_path, destination, expected
    )
    return identity


def extract_accepted_source_cache_archive(
    archive_path: Path, destination: Path
) -> None:
    """Safely materialize a staged accepted-source-cache tree, files only.

    Unlike a project source archive, no separate manifest identity gates this
    archive: every cache path already carries its own sha256 in its filename
    (content-addressed entries/keys/CAS objects), and the archive's own bytes are
    already bound to ``ExecutionSourceMaterialization.accepted_source_cache_reference``
    before this is called, so a second manifest scheme would only duplicate that
    check. This function's job is purely a safe extraction: reject traversal,
    symlinks, and case-collisions.
    """

    if destination.exists() or destination.is_symlink():
        raise SourceGuardError("cache materialization destination must not exist")
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (OSError, tarfile.TarError) as error:
        raise SourceGuardError("accepted-source-cache archive is unreadable") from error
    entries: list[tuple[str, bytes]] = []
    keys: set[str] = set()
    with archive:
        for member in archive.getmembers():
            relative = "/".join(_portable_source_path(member.name))
            key = relative.casefold()
            if key in keys:
                raise SourceGuardError(
                    "accepted-source-cache archive contains duplicate or "
                    "case-colliding members"
                )
            keys.add(key)
            if not member.isreg():
                raise SourceGuardError(
                    "accepted-source-cache archive member is not a regular file: "
                    f"{relative}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise SourceGuardError(
                    f"accepted-source-cache archive file has no payload: {relative}"
                )
            content = stream.read()
            if len(content) != member.size:
                raise SourceGuardError(
                    f"accepted-source-cache archive file is truncated: {relative}"
                )
            entries.append((relative, content))
    for relative, _content in entries:
        parts = PurePosixPath(relative).parts
        for length in range(1, len(parts)):
            if "/".join(parts[:length]).casefold() in keys:
                raise SourceGuardError(
                    "accepted-source-cache archive member is beneath a file: "
                    f"{relative}"
                )
    try:
        destination.parent.resolve(strict=True)
    except OSError as error:
        raise SourceGuardError(
            "accepted-source-cache destination parent is unavailable"
        ) from error
    destination.mkdir()
    try:
        directories = {
            parts[:length]
            for relative, _content in entries
            for parts in (_portable_source_path(relative),)
            for length in range(1, len(parts))
        }
        for parts in sorted(directories, key=lambda item: (len(item), item)):
            destination.joinpath(*parts).mkdir(exist_ok=True)
        for relative, content in entries:
            path = destination.joinpath(*_portable_source_path(relative))
            path.write_bytes(content)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def materialize_git_tree(
    repository: Path, revision: str, destination: Path, expected: str
) -> str:
    """Materialize exact Git blobs directly, without archive export semantics."""

    if destination.exists() or destination.is_symlink():
        raise SourceGuardError("Git materialization destination must not exist")
    entries = git_tree_entries(repository, revision)
    identity = _manifest_identity(entries)
    if identity != expected:
        raise SourceGuardError("Git object manifest identity does not match authority")
    payloads: dict[str, bytes] = {}
    for entry in entries:
        path = entry["path"]
        content = _git(repository, "show", f"{revision}:{path}")
        if entry["kind"] == "file":
            if (
                len(content) != entry["size"]
                or _digest(content) != entry["content_identity"]
            ):
                raise SourceGuardError(
                    f"Git blob changed during materialization: {path}"
                )
        elif _symlink_target_identity(path, content) != entry["target_identity"]:
            raise SourceGuardError(
                f"Git symlink changed during materialization: {path}"
            )
        payloads[path] = content
    _materialize_entries(destination, entries, payloads)
    return identity


def verify_materialized_git_tree(root: Path, revision: str, expected: str) -> str:
    expected_entries = git_tree_entries(root, revision)
    identity = _manifest_identity(expected_entries)
    if identity != expected:
        raise SourceGuardError("Git object manifest identity does not match authority")

    _verify_materialized_entries(root, expected_entries)
    return identity


def _write_status(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _windows_validated_taskkill(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve the real System32 `taskkill.exe` without consulting PATH.

    Duplicates `literate_ai.adapters._processes.windows_taskkill_executable`
    (this module cannot import it -- see the module-level note above). A
    project-local `taskkill.exe` placed earlier on PATH must never be
    executed; only a validated, symlink-free path under `%SystemRoot%\\
    System32`, derived solely from the `SystemRoot` environment variable, is
    acceptable.
    """

    selected = os.environ if environment is None else environment
    configured_roots = {
        value
        for key, value in selected.items()
        if key.casefold() == "systemroot" and isinstance(value, str) and value
    }
    if len(configured_roots) != 1:
        return None
    configured_root = Path(configured_roots.pop())
    if not configured_root.is_absolute() or configured_root.is_symlink():
        return None
    try:
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            return None
        system32 = root / "System32"
        if system32.is_symlink() or not system32.is_dir():
            return None
        resolved_system32 = system32.resolve(strict=True)
        if resolved_system32 != system32:
            return None
        candidate = resolved_system32 / "taskkill.exe"
        if candidate.is_symlink() or not candidate.is_file():
            return None
        executable = candidate.resolve(strict=True)
    except OSError:
        return None
    if (
        executable != candidate
        or executable.parent != resolved_system32
        or not os.access(executable, os.X_OK)
    ):
        return None
    return executable


def supervise(
    timeout_seconds: int,
    status_path: Path,
    command: Sequence[str],
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    if timeout_seconds < 1 or not command:
        raise SourceGuardError("supervisor requires a positive timeout and command")
    selected_platform = os.name if platform_name is None else platform_name
    selected_environment = os.environ if environment is None else environment
    windows = selected_platform == "nt"
    process = subprocess.Popen(
        list(command),
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if windows else 0
        ),
        start_new_session=not windows,
    )
    _write_status(
        status_path,
        {"schema": "literate-ai/remote-worker-status@1", "state": "running"},
    )
    try:
        returncode = process.wait(timeout=timeout_seconds)
        state = "passed" if returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        state = "timed-out"
        if windows:
            # Resolve a validated, non-PATH `taskkill.exe` (#74): a
            # project-local decoy earlier on PATH must never be executed to
            # kill a timed-out worker tree.
            executable = _windows_validated_taskkill(selected_environment)
            if executable is not None:
                try:
                    subprocess.run(
                        [str(executable), "/PID", str(process.pid), "/T", "/F"],
                        cwd=executable.parent,
                        env={"SystemRoot": str(executable.parent.parent)},
                        check=False,
                        capture_output=True,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if windows:
                try:
                    process.kill()
                except OSError:
                    pass
            else:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        returncode = 124
    _write_status(
        status_path,
        {
            "returncode": returncode,
            "schema": "literate-ai/remote-worker-status@1",
            "state": state,
            "termination_confirmed": process.poll() is not None,
        },
    )
    return returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tree = commands.add_parser("verify-tree")
    tree.add_argument("--root", type=Path, required=True)
    tree.add_argument("--expected", required=True)
    git = commands.add_parser("verify-git")
    git.add_argument("--root", type=Path, required=True)
    git.add_argument("--revision", required=True)
    git.add_argument("--expected", required=True)
    extract = commands.add_parser("extract-archive")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    extract.add_argument("--expected", required=True)
    materialize = commands.add_parser("materialize-git")
    materialize.add_argument("--repository", type=Path, required=True)
    materialize.add_argument("--revision", required=True)
    materialize.add_argument("--destination", type=Path, required=True)
    materialize.add_argument("--expected", required=True)
    supervisor = commands.add_parser("supervise")
    supervisor.add_argument("--timeout-seconds", type=int, required=True)
    supervisor.add_argument("--status", type=Path, required=True)
    supervisor.add_argument("worker", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify-tree":
        identity = source_tree_identity(args.root)
        if identity != args.expected:
            raise SystemExit("materialized source tree identity mismatch")
        print(f"LITAI_SOURCE_TREE_IDENTITY={identity}")
        return 0
    if args.command == "verify-git":
        identity = verify_materialized_git_tree(args.root, args.revision, args.expected)
        print(f"LITAI_SOURCE_TREE_IDENTITY={identity}")
        return 0
    if args.command == "extract-archive":
        identity = extract_source_archive(args.archive, args.destination, args.expected)
        print(f"LITAI_SOURCE_TREE_IDENTITY={identity}")
        return 0
    if args.command == "materialize-git":
        identity = materialize_git_tree(
            args.repository,
            args.revision,
            args.destination,
            args.expected,
        )
        print(f"LITAI_SOURCE_TREE_IDENTITY={identity}")
        return 0
    worker = list(args.worker)
    if worker[:1] == ["--"]:
        worker.pop(0)
    return supervise(args.timeout_seconds, args.status, worker)


if __name__ == "__main__":
    raise SystemExit(main())

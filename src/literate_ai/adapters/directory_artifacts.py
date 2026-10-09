"""One canonical directory-export encoding for Standard and retained products."""

from __future__ import annotations

import hashlib
import io
import stat
import struct
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.paths import canonical_relative_posix_paths


@dataclass(frozen=True, slots=True)
class DirectoryExportFile:
    """Validated archive bytes and mode; integrity is not qualification."""

    path: str
    content: bytes
    mode: int


def read_directory_export(
    content: bytes,
    expected: BlobRef,
    *,
    max_bytes: int,
    max_entries: int,
) -> tuple[DirectoryExportFile, ...]:
    """Read a bounded canonical directory ZIP without filesystem writes.

    Callers must bound transport before allocating ``content`` and separately
    admit qualification evidence. This reader does not grant execution authority.
    ZIP64, compression, comments and extra fields are outside this bounded format.
    """

    for limit in (max_bytes, max_entries):
        if type(limit) is not int or limit < 1:
            raise ValueError("directory export limits must be positive integers")
    if not isinstance(content, bytes) or not isinstance(expected, BlobRef):
        raise ValueError("directory export requires bytes and a typed blob reference")
    if (
        len(content) > max_bytes
        or len(content) != expected.size
        or hashlib.sha256(content).hexdigest() != expected.digest
    ):
        raise ValueError(
            "directory export exceeds its bound or differs from pinned bytes"
        )
    if len(content) < 22:
        raise ValueError("directory export is truncated")
    signature, disk, start_disk, disk_count, count, size, start, comment = (
        struct.unpack_from("<4s4H2IH", content, len(content) - 22)
    )
    if (
        signature != b"PK\x05\x06"
        or disk != 0
        or start_disk != 0
        or disk_count != count
        or not 1 <= count <= min(max_entries, 65534)
        or comment != 0
        or start + size != len(content) - 22
    ):
        raise ValueError("directory export has an unsupported ZIP directory")
    # ZipFile ignores the declared entry count. Walk bounded central records
    # before it allocates ZipInfo objects, rejecting a forged small count.
    cursor = start
    for _ in range(count):
        if cursor + 46 > start + size or content[cursor : cursor + 4] != b"PK\x01\x02":
            raise ValueError("directory export has a malformed ZIP directory")
        name_size, extra_size, comment_size = struct.unpack_from(
            "<3H", content, cursor + 28
        )
        if not 1 <= name_size <= 4096 or extra_size or comment_size:
            raise ValueError("directory export has unsupported ZIP member metadata")
        cursor += 46 + name_size
    if cursor != start + size:
        raise ValueError("directory export entry count differs from its directory")
    result = []
    canonical = io.BytesIO()
    try:
        with (
            zipfile.ZipFile(io.BytesIO(content)) as archive,
            zipfile.ZipFile(canonical, "w", compression=zipfile.ZIP_STORED) as rebuilt,
        ):
            members = archive.infolist()
            names = [member.filename for member in members]
            if any(
                not member.filename or member.orig_filename != member.filename
                for member in members
            ):
                raise ValueError("directory export has an empty or altered member path")
            paths = canonical_relative_posix_paths(
                names, label="directory export member"
            )
            prefixes: dict[str, str] = {}
            for path in paths:
                for depth in range(1, len(path.parts) + 1):
                    prefix = "/".join(path.parts[:depth])
                    alias = unicodedata.normalize("NFC", prefix.casefold())
                    if prefixes.setdefault(alias, prefix) != prefix:
                        raise ValueError("directory export has aliased directory paths")
            if len(members) != count or names != sorted(names):
                raise ValueError("directory export members are not in canonical order")
            total = 0
            for member in members:
                mode = member.external_attr >> 16
                total += member.file_size
                if (
                    total > max_bytes
                    or member.compress_type != zipfile.ZIP_STORED
                    or member.compress_size != member.file_size
                    or member.flag_bits & ~0x800
                    or not stat.S_ISREG(mode)
                    or mode & ~(stat.S_IFREG | 0o777)
                ):
                    raise ValueError(
                        "directory export member has unsafe type, mode or size"
                    )
                data = archive.read(member)
                info = zipfile.ZipInfo(member.filename, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = mode << 16
                rebuilt.writestr(info, data)
                result.append(
                    DirectoryExportFile(member.filename, data, stat.S_IMODE(mode))
                )
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError("directory export has malformed ZIP content") from exc
    if canonical.getvalue() != content:
        raise ValueError("directory export does not use canonical ZIP bytes")
    return tuple(result)


def encode_directory_export(
    files: tuple[DirectoryExportFile, ...], *, max_bytes: int, max_entries: int
) -> bytes:
    """Encode immutable retained files with the existing canonical ZIP format.

    Bound the complete archive (including headers and encoded names) before
    allocation. The shared reader checks all path aliases and format invariants;
    no filesystem access or package execution occurs.
    """

    if any(type(limit) is not int or limit < 1 for limit in (max_bytes, max_entries)):
        raise ValueError("directory export limits must be positive integers")
    if not isinstance(files, tuple) or not 1 <= len(files) <= min(max_entries, 65534):
        raise ValueError("directory export exceeds its entry bound")
    size = 22
    for item in files:
        if (
            not isinstance(item, DirectoryExportFile)
            or not isinstance(item.path, str)
            or not isinstance(item.content, bytes)
            or type(item.mode) is not int
            or not 0 <= item.mode <= 0o777
        ):
            raise ValueError("directory export requires immutable regular files")
        encoded_name = item.path.encode("utf-8")
        if not 1 <= len(encoded_name) <= 4096:
            raise ValueError("directory export has an unsupported member name")
        size += 76 + 2 * len(encoded_name) + len(item.content)
        if size > max_bytes or size >= 2**31:
            raise ValueError("directory export exceeds its byte bound")
    canonical_relative_posix_paths(
        [item.path for item in files], label="directory export member"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False
    ) as archive:
        for item in sorted(files, key=lambda item: item.path):
            info = zipfile.ZipInfo(item.path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | item.mode) << 16
            archive.writestr(info, item.content)
    content = stream.getvalue()
    if len(content) != size:
        raise ValueError("directory export differs from its bounded encoded size")
    reference = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
    read_directory_export(
        content, reference, max_bytes=max_bytes, max_entries=max_entries
    )
    return content


def directory_export_bytes(root: Path) -> bytes:
    """Preserve the Standard directory blob format, including file mode bits."""

    if path_is_link_or_reparse(root):
        raise ValueError("artifact exports cannot contain links")
    if not root.is_dir():
        raise ValueError("directory artifact export is empty")
    files = []
    for path in sorted(
        root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()
    ):
        if path_is_link_or_reparse(path):
            raise ValueError("artifact exports cannot contain links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("artifact exports must contain only regular files")
        files.append(path)
    if not files:
        raise ValueError("directory artifact export is empty")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            information = zipfile.ZipInfo(
                path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
            )
            information.compress_type = zipfile.ZIP_STORED
            information.create_system = 3
            information.external_attr = (
                stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)
            ) << 16
            archive.writestr(information, path.read_bytes())
    return stream.getvalue()


def require_library_package(product: LibraryArtifactProduct, artifact: Path) -> None:
    """Require the retained package bytes to match the accepted export blob."""

    if not isinstance(product, LibraryArtifactProduct):
        raise ValueError("library package requires typed product authority")
    content = directory_export_bytes(artifact)
    blob = product.artifact_export.blob
    if len(content) != blob.size or hashlib.sha256(content).hexdigest() != blob.digest:
        raise ValueError("library package differs from the accepted export blob")


def require_transported_library_package(
    product: LibraryArtifactProduct,
    package: Path,
    artifact: Path,
    *,
    executable_by_path: Mapping[str, bool] | None = None,
) -> None:
    """Verify sealed ZIP custody against the transported tree without extracting it.

    Evidence transport retains executable bits, not all POSIX permissions. The ZIP
    preserves the exact accepted modes while every member must match the separately
    verified file evidence. Never import or execute the package during verification.
    """

    content = package.read_bytes()
    blob = product.artifact_export.blob
    if len(content) != blob.size or hashlib.sha256(content).hexdigest() != blob.digest:
        raise ValueError("transported library package differs from the accepted blob")
    files = sorted(path for path in artifact.rglob("*") if path.is_file())
    if executable_by_path is not None and set(executable_by_path) != {
        path.relative_to(artifact).as_posix() for path in files
    }:
        raise ValueError("library file mode evidence differs from transported artifact")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if [item.filename for item in members] != [
            path.relative_to(artifact).as_posix() for path in files
        ]:
            raise ValueError("library package members differ from transported artifact")
        for member, path in zip(members, files, strict=True):
            mode = member.external_attr >> 16
            executable = (
                bool(path.stat().st_mode & 0o111)
                if executable_by_path is None
                else executable_by_path[member.filename]
            )
            if (
                member.compress_type != zipfile.ZIP_STORED
                or member.flag_bits & 1
                or not stat.S_ISREG(mode)
                or member.file_size != path.stat().st_size
                or bool(mode & 0o111) != executable
                or archive.read(member) != path.read_bytes()
            ):
                raise ValueError(
                    "library package member differs from transported artifact"
                )

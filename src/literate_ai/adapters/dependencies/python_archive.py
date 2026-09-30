"""Bounded wheel payload inspection, without extraction or package execution."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32

from packaging.utils import parse_wheel_filename

from .types import DependencyObservationError

_MAX_MEMBER = 8 * 1024**3
_MAX_EXPANDED = 32 * 1024**3
_MAX_RECORD = 32 * 1024**2
_MAX_WHEEL_METADATA = 1024**2
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", re.I)
_SCHEMES = frozenset({"purelib", "platlib", "headers", "scripts", "data"})


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-wheel-invalid", message)


@dataclass(frozen=True)
class WheelPayload:
    path: str
    size: int
    sha256: str


def _path(value: str, *, directory: bool = False) -> str:
    path = value[:-1] if directory and value.endswith("/") else value
    if not path or len(path) > 4096 or unicodedata.normalize("NFC", path) != path:
        _fail("Wheel member path is empty, oversized or noncanonical")
    for part in path.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or _RESERVED.fullmatch(part)
            or any(ord(c) < 32 or ord(c) == 127 or c in '<>:"\\|?*' for c in part)
        ):
            _fail("Wheel member path is not portable and relative")
    return path


def _inventory(archive: zipfile.ZipFile, directory: str) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > 100_000:
        _fail("Wheel member count exceeds its bound")
    nodes: dict[str, tuple[str, bool]] = {}
    files = {}
    explicit = set()
    expanded = 0
    data_directory = directory.removesuffix(".dist-info") + ".data"
    for entry in entries:
        path = _path(entry.filename, directory=entry.is_dir())
        if entry.orig_filename != entry.filename or path.casefold() in explicit:
            _fail("Wheel member inventory has duplicate or truncated paths")
        explicit.add(path.casefold())
        mode = stat.S_IFMT(entry.external_attr >> 16)
        expected_mode = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
        if mode not in (0, expected_mode) or entry.flag_bits & 1:
            _fail("Wheel contains a special file or encrypted member")
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            _fail("Wheel compression method is unsupported")
        if entry.file_size > _MAX_MEMBER or (entry.is_dir() and entry.file_size):
            _fail("Wheel member exceeds its expanded size bound")
        expanded += entry.file_size
        if expanded > _MAX_EXPANDED:
            _fail("Wheel expanded size exceeds its bound")
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            is_dir = index < len(parts) or entry.is_dir()
            prior = nodes.setdefault(prefix.casefold(), (prefix, is_dir))
            if prior != (prefix, is_dir):
                _fail("Wheel paths alias or collide with a directory")
        if parts[0].endswith(".dist-info") and parts[0] != directory:
            _fail("Wheel contains another distribution metadata directory")
        if parts[0].endswith(".data"):
            if parts[0] != data_directory or (
                len(parts) > 1 and parts[1] not in _SCHEMES
            ):
                _fail("Wheel contains an unknown installation scheme")
            if not entry.is_dir() and len(parts) < 3:
                _fail("Wheel data member has no installation-relative path")
        if not entry.is_dir():
            files[path] = entry
    return files


def _read(
    archive: zipfile.ZipFile, files: dict[str, zipfile.ZipInfo], path: str, limit: int
) -> bytes:
    entry = files.get(path)
    if entry is None or entry.file_size > limit:
        _fail("Wheel is missing a bounded required metadata member")
    with archive.open(entry) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        _fail("Wheel metadata exceeds its bound")
    return content


def _wheel_metadata(content: bytes, filename: str) -> None:
    metadata = BytesParser(policy=compat32).parsebytes(content)
    if metadata.defects or any(
        len(metadata.get_all(key, [])) != 1
        for key in ("Wheel-Version", "Root-Is-Purelib")
    ):
        _fail("WHEEL metadata is malformed or ambiguous")
    if metadata["Wheel-Version"] != "1.0":
        _fail("Only wheel format 1.0 is supported by this profile")
    if metadata["Root-Is-Purelib"] not in {"true", "false"}:
        _fail("WHEEL lacks a valid root installation scheme")
    _, _, build, tags = parse_wheel_filename(filename)
    actual_tags = metadata.get_all("Tag", [])
    if len(set(actual_tags)) != len(actual_tags) or set(actual_tags) != set(
        map(str, tags)
    ):
        _fail("WHEEL tags disagree with its filename")
    expected_build = [str(build[0]) + build[1]] if build else []
    if metadata.get_all("Build", []) != expected_build:
        _fail("WHEEL build tag disagrees with its filename")


def inspect_wheel_payload(
    archive: zipfile.ZipFile, *, directory: str, filename: str
) -> tuple[WheelPayload, ...]:
    """Verify RECORD against every regular member; retain actual SHA-256 values.

    Signature sidecars are covered by the locked outer archive digest, not
    authenticated here. This is not an installer or installed-tree evidence.
    """
    files = _inventory(archive, directory)
    _wheel_metadata(
        _read(archive, files, directory + "/WHEEL", _MAX_WHEEL_METADATA), filename
    )
    record_path = directory + "/RECORD"
    signatures = {record_path + ".jws", record_path + ".p7s"}
    content = _read(archive, files, record_path, _MAX_RECORD)
    records: dict[str, tuple[str, str]] = {}
    try:
        for row in csv.reader(
            io.StringIO(content.decode("utf-8"), newline=""), strict=True
        ):
            if len(row) != 3:
                _fail("Wheel RECORD must have three columns")
            path, digest, size = row
            _path(path)
            if path in records or len(records) >= 100_000:
                _fail("Wheel RECORD paths are repeated or oversized")
            records[path] = (digest, size)
    except (UnicodeError, csv.Error) as exc:
        raise DependencyObservationError(
            "dependencies.python-wheel-invalid", "Wheel RECORD is not valid UTF-8 CSV"
        ) from exc
    if set(records) != set(files) - signatures or records.get(record_path) != ("", ""):
        _fail("Wheel RECORD does not exactly cover the archive payload")
    result = []
    for path, entry in sorted(files.items()):
        expected_digest = None
        algorithm = "sha256"
        if path not in signatures and path != record_path:
            digest, size = records[path]
            algorithm, separator, expected_digest = digest.partition("=")
            if separator != "=" or algorithm not in {"sha256", "sha384", "sha512"}:
                _fail("Wheel RECORD uses an unsupported or missing secure hash")
            if size != str(entry.file_size):
                _fail("Wheel RECORD size differs from its payload")
        declared_hash = hashlib.new(algorithm)
        actual_hash = hashlib.sha256()
        size = 0
        with archive.open(entry) as stream:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > entry.file_size or size > _MAX_MEMBER:
                    _fail("Wheel payload exceeds its declared size")
                declared_hash.update(block)
                actual_hash.update(block)
        if size != entry.file_size:
            _fail("Wheel payload is truncated")
        if expected_digest is not None:
            encoded = (
                base64.urlsafe_b64encode(declared_hash.digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            if encoded != expected_digest:
                _fail("Wheel RECORD hash differs from its payload")
        result.append(WheelPayload(path, size, actual_hash.hexdigest()))
    return tuple(result)

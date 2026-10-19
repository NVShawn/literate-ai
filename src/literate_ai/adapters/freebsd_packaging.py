"""Exact FreeBSD pkg projection, native construction and independent inspection."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.freebsd_package_elf import inspect_freebsd_package_elf
from literate_ai.adapters.packaging import (
    logical_package_files,
    package_result_for,
    validate_materialized_package_root,
)
from literate_ai.adapters.retained_project_inputs import _stream
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts import (
    BlobRef,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components.packages import (
    PackagedFile,
    PackageFileKind,
    PackageKind,
)
from literate_ai.contracts.paths import canonical_relative_posix_paths

_EPOCH = 946684800
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,127}\Z")
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+,_-]{0,127}\Z")
_ABI = re.compile(r"FreeBSD:[1-9][0-9]*:(?:amd64|aarch64)\Z")
_MAX_METADATA = 16 * 1024 * 1024
_MAX_MEMBERS = 100_000


def _xz_codec():
    try:
        import lzma
    except ImportError as exc:
        raise PackagingError(
            "native pkg inspection requires Python's lzma codec"
        ) from exc
    return lzma


def _absolute(path):
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise PackagingError("FreeBSD payload requires canonical absolute file paths")
    try:
        canonical_relative_posix_paths((path[1:],), label="FreeBSD installed file")
    except ValueError as exc:
        raise PackagingError(str(exc)) from exc
    return path


def _origin(value):
    if (
        not isinstance(value, str)
        or len(value.split("/")) != 2
        or any(_NAME.fullmatch(p) is None or p in {".", ".."} for p in value.split("/"))
    ):
        raise PackagingError("FreeBSD package origin is invalid")


def _document(content):
    if not isinstance(content, bytes) or len(content) > _MAX_METADATA:
        raise PackagingError("FreeBSD metadata exceeds its byte bound")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PackagingError("FreeBSD metadata contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        raise PackagingError("FreeBSD metadata is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PackagingError("FreeBSD metadata requires an object")
    return value


def freebsd_runtime_dependencies(plan, resolved_sbom):
    """Use only explicit accepted coordinates; never query ambient packages."""
    references = [v.blob for v in plan.inputs if v.role == "cyclonedx-resolved-sbom"]
    if len(references) != 1 or (
        len(resolved_sbom),
        hashlib.sha256(resolved_sbom).hexdigest(),
    ) != (references[0].size, references[0].digest):
        raise PackagingError("FreeBSD dependency BOM differs from accepted inputs")
    document = _document(resolved_sbom)
    components = document.get("components")
    if not isinstance(components, list):
        raise PackagingError("FreeBSD dependency BOM has no component inventory")
    coordinates = {}
    for component in components:
        if not isinstance(component, dict):
            raise PackagingError("FreeBSD dependency component is invalid")
        properties = {}
        raw = component.get("properties", [])
        if not isinstance(raw, list):
            raise PackagingError("FreeBSD dependency properties are invalid")
        for prop in raw:
            if (
                not isinstance(prop, dict)
                or not isinstance(prop.get("name"), str)
                or not isinstance(prop.get("value"), str)
            ):
                raise PackagingError("FreeBSD dependency property is invalid")
            properties.setdefault(prop["name"], []).append(prop["value"])
        native = [
            properties.get("literate-ai:freebsd-package-" + key, [])
            for key in ("name", "origin", "version")
        ]
        if all(len(v) == 1 for v in native):
            coordinate = tuple(v[0] for v in native)
            for requirement in properties.get(
                "literate-ai:runtime-requirement-identity", []
            ):
                coordinates.setdefault(requirement, set()).add(coordinate)
    result = {}
    for requirement in plan.runtime_requirements:
        if requirement.supplied_by_package:
            continue
        selected = coordinates.get(requirement.requirement_identity.uri, set())
        if len(selected) != 1:
            raise PackagingError(
                "FreeBSD runtime requirement has no unique accepted coordinate"
            )
        name, origin, version = next(iter(selected))
        if _NAME.fullmatch(name) is None or _VERSION.fullmatch(version) is None:
            raise PackagingError("FreeBSD dependency name/version is invalid")
        _origin(origin)
        if name in result and result[name] != (origin, version):
            raise PackagingError("FreeBSD dependency coordinates conflict")
        result[name] = (origin, version)
    return tuple((name, *result[name]) for name in sorted(result))


@dataclass(frozen=True, slots=True)
class FreeBsdPayloadEntry:
    input_path: str
    installed_path: str
    mode: int

    def __post_init__(self):
        canonical_relative_posix_paths(
            (self.input_path,), label="FreeBSD package input"
        )
        _absolute(self.installed_path)
        if type(self.mode) is not int or self.mode not in {0o644, 0o755}:
            raise PackagingError("FreeBSD payload mode is unsupported")

    def to_dict(self):
        return {
            "input_path": self.input_path,
            "installed_path": self.installed_path,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class FreeBsdPackageProjection:
    source_package_plan_identity: ContentIdentity
    source_packager_identity: ContentIdentity
    package: str
    version: str
    origin: str
    abi: str
    architecture: str
    prefix: str
    dependencies: tuple[tuple[str, str, str], ...]
    payload: tuple[FreeBsdPayloadEntry, ...]
    tool_identity: ContentIdentity
    maximum_archive_bytes: int = 256 * 1024 * 1024
    source_date_epoch: int = _EPOCH

    SCHEMA = "literate-ai/freebsd-pkg-projection@1"

    def __post_init__(self):
        for value in (
            self.source_package_plan_identity,
            self.source_packager_identity,
            self.tool_identity,
        ):
            if not isinstance(value, ContentIdentity):
                raise PackagingError("FreeBSD projection identities must be typed")
        if (
            not isinstance(self.package, str)
            or _NAME.fullmatch(self.package) is None
            or not isinstance(self.version, str)
            or _VERSION.fullmatch(self.version) is None
        ):
            raise PackagingError("FreeBSD package name/version is invalid")
        _origin(self.origin)
        if (
            not isinstance(self.abi, str)
            or _ABI.fullmatch(self.abi) is None
            or not isinstance(self.architecture, str)
            or re.fullmatch(r"[A-Za-z0-9:._-]{1,96}", self.architecture) is None
        ):
            raise PackagingError("FreeBSD package ABI is unsupported")
        if self.prefix != "/":
            _absolute(self.prefix)
        if (
            type(self.dependencies) is not tuple
            or self.dependencies != tuple(sorted(set(self.dependencies)))
            or len(self.dependencies) > 1024
        ):
            raise PackagingError("FreeBSD dependencies must be canonical")
        names = set()
        for name, origin, version in self.dependencies:
            if (
                _NAME.fullmatch(name) is None
                or _VERSION.fullmatch(version) is None
                or name.casefold() in names
            ):
                raise PackagingError("FreeBSD dependency coordinates are invalid")
            _origin(origin)
            names.add(name.casefold())
        if (
            type(self.payload) is not tuple
            or not 1 <= len(self.payload) <= _MAX_MEMBERS
            or any(not isinstance(v, FreeBsdPayloadEntry) for v in self.payload)
            or self.payload
            != tuple(sorted(self.payload, key=lambda v: v.installed_path))
        ):
            raise PackagingError("FreeBSD payload must be canonical")
        canonical_relative_posix_paths(
            (v.input_path for v in self.payload), label="FreeBSD input paths"
        )
        canonical_relative_posix_paths(
            (v.installed_path[1:] for v in self.payload),
            label="FreeBSD installed paths",
        )
        if (
            type(self.maximum_archive_bytes) is not int
            or not 1 <= self.maximum_archive_bytes <= 16 * 1024**3
            or type(self.source_date_epoch) is not int
            or self.source_date_epoch != _EPOCH
        ):
            raise PackagingError("FreeBSD package limits/timestamp are invalid")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "source_package_plan_identity": self.source_package_plan_identity.uri,
            "source_packager_identity": self.source_packager_identity.uri,
            "package": self.package,
            "version": self.version,
            "origin": self.origin,
            "abi": self.abi,
            "architecture": self.architecture,
            "prefix": self.prefix,
            "dependencies": [list(v) for v in self.dependencies],
            "payload": [v.to_dict() for v in self.payload],
            "tool_identity": self.tool_identity.uri,
            "maximum_archive_bytes": self.maximum_archive_bytes,
            "source_date_epoch": self.source_date_epoch,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        fields = {
            "schema",
            "source_package_plan_identity",
            "source_packager_identity",
            "package",
            "version",
            "origin",
            "abi",
            "architecture",
            "prefix",
            "dependencies",
            "payload",
            "tool_identity",
            "maximum_archive_bytes",
            "source_date_epoch",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != cls.SCHEMA
            or not isinstance(value["payload"], list)
            or len(value["payload"]) > _MAX_MEMBERS
            or not isinstance(value["dependencies"], list)
            or len(value["dependencies"]) > 1024
        ):
            raise PackagingError("FreeBSD projection document is invalid")
        entries = []
        for item in value["payload"]:
            if not isinstance(item, dict) or set(item) != {
                "input_path",
                "installed_path",
                "mode",
            }:
                raise PackagingError("FreeBSD projection payload record is invalid")
            entries.append(FreeBsdPayloadEntry(**item))
        if any(
            not isinstance(v, list)
            or len(v) != 3
            or any(not isinstance(p, str) for p in v)
            for v in value["dependencies"]
        ):
            raise PackagingError("FreeBSD dependency records are invalid")
        return cls(
            ContentIdentity.parse_uri(value["source_package_plan_identity"]),
            ContentIdentity.parse_uri(value["source_packager_identity"]),
            *(
                value[n]
                for n in (
                    "package",
                    "version",
                    "origin",
                    "abi",
                    "architecture",
                    "prefix",
                )
            ),
            tuple(tuple(v) for v in value["dependencies"]),
            tuple(entries),
            ContentIdentity.parse_uri(value["tool_identity"]),
            value["maximum_archive_bytes"],
            value["source_date_epoch"],
        )

    @property
    def manifest(self):
        return {
            "name": self.package,
            "version": self.version,
            "origin": self.origin,
            "abi": self.abi,
            "arch": self.architecture,
            "prefix": self.prefix,
            "comment": "Exact accepted Literate AI package",
            "desc": (
                "Accepted payload with specification and source/resolved provenance."
            ),
            "maintainer": "literate-ai-maintainers@users.noreply.github.com",
            "www": "https://github.com/jordanhubbard/literate-ai",
            "deps": {n: {"origin": o, "version": v} for n, o, v in self.dependencies},
            "annotations": {
                "literate-ai:package-plan": self.source_package_plan_identity.uri,
                "literate-ai:projection": self.identity.uri,
            },
        }

    @property
    def plist(self):
        lines = ["@owner root", "@group wheel"]
        for item in self.payload:
            lines.extend((f"@mode {item.mode:04o}", item.installed_path))
        return ("\n".join(lines) + "\n").encode()


def project_freebsd_package(
    plan,
    *,
    package,
    version,
    origin,
    target_abi,
    worker_abi,
    worker_architecture,
    resolved_sbom,
    tool_identity,
    prefix="/usr/local",
    installed_paths=None,
):
    if target_abi != worker_abi:
        raise PackagingError("FreeBSD target and worker ABIs contradict")
    roles = {v.role for v in plan.inputs}
    if (
        not {
            "component-specification",
            "cyclonedx-source-sbom",
            "cyclonedx-resolved-sbom",
        }
        <= roles
    ):
        raise PackagingError("FreeBSD packaging requires specification and both BOMs")
    if installed_paths is not None and (
        not isinstance(installed_paths, dict)
        or set(installed_paths) != {v.path for v in plan.inputs}
    ):
        raise PackagingError("FreeBSD explicit projection must cover exact inputs")
    entrypoints = {v.path: v.name for v in plan.entrypoints}
    root = prefix.rstrip("/")
    payload = []
    for item in plan.inputs:
        if installed_paths is not None:
            destination = installed_paths[item.path]
        elif item.path in entrypoints:
            destination = f"{root}/bin/{entrypoints[item.path]}"
        elif item.role in {
            "component-specification",
            "cyclonedx-source-sbom",
            "cyclonedx-resolved-sbom",
        }:
            destination = f"{root}/share/doc/{package}/literate-ai/{item.path}"
        else:
            destination = f"{root}/lib/{package}/{item.path}"
        payload.append(
            FreeBsdPayloadEntry(
                item.path,
                destination,
                0o755 if item.executable or item.path in entrypoints else 0o644,
            )
        )
    return FreeBsdPackageProjection(
        plan.identity,
        plan.packager_identity,
        package,
        version,
        origin,
        target_abi,
        worker_architecture,
        prefix,
        freebsd_runtime_dependencies(plan, resolved_sbom),
        tuple(sorted(payload, key=lambda v: v.installed_path)),
        tool_identity,
    )


def _tool_file(path):
    node = path.lstat()
    if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1:
        raise PackagingError("FreeBSD pkg tool must be a regular executable")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True, slots=True)
class FreeBsdPkgToolBinding:
    executable: Path
    executable_digest: str
    version: str
    abi: str
    architecture: str

    @property
    def command(self):
        return (
            str(self.executable),
            "-C",
            os.devnull,
            "-o",
            "PKG_ENABLE_PLUGINS=false",
        )

    @property
    def environment(self):
        return {
            "PATH": str(self.executable.parent),
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
        }

    @classmethod
    def discover(cls, command="pkg-static"):
        if platform.system().casefold() != "freebsd":
            raise PackagingError("native pkg discovery requires FreeBSD")
        _xz_codec()
        selected = shutil.which(command)
        if selected is None:
            raise PackagingError(
                "native pkg-static is unavailable; "
                "no bootstrap or installation is authorized"
            )
        executable = Path(selected).resolve(strict=True)
        result = cls(executable, _tool_file(executable), "", "", "")

        version, abi, architecture = (
            result._probe(("-v",)),
            result._probe(("config", "ABI")),
            result._probe(("config", "ALTABI")),
        )
        if _ABI.fullmatch(abi) is None:
            raise PackagingError("native pkg reports an unsupported FreeBSD ABI")
        result = cls(executable, result.executable_digest, version, abi, architecture)
        result.require_unchanged()
        return result

    def _probe(self, argv):
        value = run_bounded_process(
            (*self.command, *argv),
            cwd=None,
            environment=self.environment,
            timeout_seconds=15,
            stdout_limit_bytes=4096,
            stderr_limit_bytes=4096,
            error_prefix="freebsd.pkg.probe",
            terminate_descendants=True,
        )
        text = value.stdout.decode("utf-8", errors="strict").strip()
        if value.returncode or not text or len(text) > 128 or "\n" in text:
            raise PackagingError("native pkg version/ABI probe failed")
        return text

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/freebsd-pkg-tool@1",
                "executable_sha256": self.executable_digest,
                "version": self.version,
                "abi": self.abi,
                "architecture": self.architecture,
                "configuration": "empty",
                "plugins": False,
            }
        )

    def require_unchanged(self):
        if _tool_file(self.executable) != self.executable_digest:
            raise PackagingError("native pkg executable changed")
        if (self._probe(("config", "ABI")), self._probe(("config", "ALTABI"))) != (
            self.abi,
            self.architecture,
        ):
            raise PackagingError("native pkg worker ABI changed")


def _zbase32(content):
    # pkg's file-checksum encoding consumes low bits first, not RFC 4648 order.
    alphabet = "ybndrfg8ejkmcpqxot1uwisza345h769"
    accumulator = bits = 0
    result = []
    for byte in content:
        accumulator |= byte << bits
        bits += 8
        while bits >= 5:
            result.append(alphabet[accumulator & 31])
            accumulator >>= 5
            bits -= 5
    if bits:
        result.append(alphabet[accumulator & 31])
    return "".join(result)


def _native_hasher(checksum):
    if not isinstance(checksum, str):
        raise PackagingError("native pkg file checksum is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", checksum):
        return hashlib.sha256(), lambda value: value.hex()
    kind, separator, _digest = checksum.partition("$")
    choices = {
        "0": hashlib.sha256,
        "1": hashlib.sha256,
        "2": hashlib.blake2b,
        "5": hashlib.blake2s,
    }
    if not separator or kind not in choices:
        raise PackagingError("native pkg file checksum algorithm is unsupported")
    encode = (lambda value: value.hex()) if kind == "1" else _zbase32
    return choices[kind](), lambda value: kind + "$" + encode(value)


def _archive_path(name):
    if name in {".", "./", "/"}:
        return "/"
    if name.endswith("/"):
        name = name[:-1]
    if name.startswith("./"):
        name = name[2:]
    if name.startswith("/"):
        name = name[1:]
    _absolute("/" + name)
    return "/" + name


def _manifest_files(document):
    values = document.get("files")
    if not isinstance(values, dict) or len(values) > _MAX_MEMBERS:
        raise PackagingError("FreeBSD manifest file inventory is invalid")
    result = {}
    for name, value in values.items():
        path = unquote(name, errors="strict")
        _absolute(path)
        if path in result:
            raise PackagingError("FreeBSD native manifest aliases file paths")
        result[path] = value
    return result


def _require_single_xz_frame(content, maximum_expanded_bytes):
    lzma = _xz_codec()
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=128 * 1024**2)
    offset = total = 0
    try:
        while not decoder.eof:
            if decoder.needs_input:
                block = content[offset : offset + 65536]
                if not block:
                    raise PackagingError("FreeBSD XZ frame is truncated")
                offset += len(block)
            else:
                block = b""
            total += len(decoder.decompress(block, max_length=1024 * 1024))
            if total > maximum_expanded_bytes:
                raise PackagingError("FreeBSD XZ frame exceeds accepted size bound")
        if decoder.unused_data or offset != len(content):
            raise PackagingError("FreeBSD package has trailing compressed data")
    except lzma.LZMAError as exc:
        raise PackagingError(
            "FreeBSD XZ compression is invalid or over budget"
        ) from exc


def _require_tar_metadata(member, *, numeric_owner, timestamp):
    # PAX records are not ordinary members. libarchive can interpret attributes
    # that tarfile leaves only in pax_headers, so inspecting uid/mode alone does
    # not cover the metadata a native package consumer might apply.
    standard = {
        "path": member.name,
        "size": str(member.size),
        "mtime": str(timestamp),
        "uid": str(numeric_owner),
        "gid": str(numeric_owner),
        "uname": "root",
        "gname": "wheel",
    }
    if any(
        key not in standard or value != standard[key]
        for key, value in member.pax_headers.items()
    ):
        raise PackagingError("FreeBSD archive contains unplanned extended tar metadata")


class FreeBsdPackageAdapter:
    """Construct actual native packages, inspect without installation or execution."""

    def __init__(self, projection, tool):
        if not isinstance(projection, FreeBsdPackageProjection) or not isinstance(
            tool, FreeBsdPkgToolBinding
        ):
            raise TypeError("FreeBSD packaging requires typed projection and tool")
        if (
            projection.tool_identity != tool.identity
            or projection.abi != tool.abi
            or projection.architecture != tool.architecture
        ):
            raise PackagingError("FreeBSD projection binds another native tool/ABI")
        self.projection, self.tool = projection, tool
        self._created = {}

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/freebsd-pkg-packager@1",
                "projection_identity": self.projection.identity.uri,
                "compression": "txz",
                "compression_threads": 1,
                "compression_level": 9,
            }
        )

    def _require_plan(self, plan):
        source = replace(
            plan, packager_identity=self.projection.source_packager_identity
        )
        if (
            source.identity != self.projection.source_package_plan_identity
            or plan.packager_identity != self.packager_identity
            or plan.package_kind is not PackageKind.ARCHIVE
        ):
            raise PackagingError("FreeBSD package plan differs from exact projection")
        expected = {v.input_path for v in self.projection.payload}
        if expected != {v.path for v in plan.inputs}:
            raise PackagingError("FreeBSD projection does not cover exact inputs")
        inputs = {v.path: v for v in plan.inputs}
        entrypoints = {v.path for v in plan.entrypoints}
        if any(
            v.mode
            != (
                0o755
                if inputs[v.input_path].executable or v.input_path in entrypoints
                else 0o644
            )
            for v in self.projection.payload
        ):
            raise PackagingError("FreeBSD projection changes accepted executable modes")

    def package(self, plan, *, materialized_root, object_root):
        if platform.system().casefold() != "freebsd":
            raise PackagingError("native pkg construction requires a FreeBSD worker")
        self._require_plan(plan)
        root, materialized = validate_materialized_package_root(plan, materialized_root)
        output_root = Path(object_root).absolute()
        directory_node(output_root)
        with tempfile.TemporaryDirectory(prefix="freebsd-pkg-", dir=output_root) as raw:
            staging = Path(raw)
            payload, output, repos = (
                staging / "root",
                staging / "output",
                staging / "repos",
            )
            payload.mkdir()
            output.mkdir()
            repos.mkdir()
            projected = {v.input_path: v for v in self.projection.payload}
            for item in plan.inputs:
                source = materialized[item.path]
                if not source.is_file():
                    raise PackagingError(
                        "FreeBSD pkg requires expanded regular-file inputs"
                    )
                entry = projected[item.path]
                destination = payload / entry.installed_path[1:]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(entry.mode)
                if _stream(payload, entry.installed_path[1:], item.blob.size) != (
                    item.blob.size,
                    entry.mode,
                    item.blob.digest,
                ):
                    raise PackagingError(
                        "FreeBSD staged payload differs from accepted bytes"
                    )
            for path in (*sorted(payload.rglob("*"), reverse=True), payload):
                if path.is_dir():
                    path.chmod(0o755)
                os.utime(
                    path,
                    (self.projection.source_date_epoch,) * 2,
                    follow_symlinks=False,
                )
            metadata = staging / "metadata"
            metadata.mkdir()
            manifest, plist = metadata / "+MANIFEST", staging / "plist"
            manifest.write_bytes(canonical_json_bytes(self.projection.manifest))
            plist.write_bytes(self.projection.plist)
            self.tool.require_unchanged()
            completed = run_bounded_process(
                (
                    *self.tool.command,
                    "-R",
                    str(repos),
                    "create",
                    "-m",
                    str(metadata),
                    "-p",
                    str(plist),
                    "-r",
                    str(payload),
                    "-o",
                    str(output),
                    "-f",
                    "txz",
                    "-l",
                    "9",
                    "-T",
                    "1",
                    "-t",
                    str(self.projection.source_date_epoch),
                ),
                cwd=staging,
                environment=self.tool.environment,
                timeout_seconds=300,
                stdout_limit_bytes=1024 * 1024,
                stderr_limit_bytes=1024 * 1024,
                error_prefix="freebsd.pkg.create",
                terminate_descendants=True,
            )
            self.tool.require_unchanged()
            if completed.returncode:
                detail = completed.stderr[-4000:].decode("utf-8", errors="replace")
                raise PackagingError("native pkg construction failed: " + detail)
            filename = f"{self.projection.package}-{self.projection.version}.pkg"
            if tuple(sorted(v.name for v in output.iterdir())) != (filename,):
                raise PackagingError(
                    "native pkg produced an unexpected output inventory"
                )
            buffer = io.BytesIO()
            size, _mode, digest = _stream(
                output, filename, self.projection.maximum_archive_bytes, output=buffer
            )
            content = buffer.getvalue()
            blob = BlobRef(digest, size, media_type="application/vnd.freebsd.pkg")
            self._created[blob.identity] = content
            artifact = PackagedFile(
                filename,
                "freebsd-pkg",
                PackageFileKind.PACKAGE_OUTPUT,
                plan.identity,
                plan.target_identity,
                blob,
                False,
            )
            result = package_result_for(
                plan, files=logical_package_files(plan), artifacts=(artifact,)
            )
            validate_materialized_package_root(plan, root)

            def read(reference):
                if reference == blob:
                    return content
                for item in plan.inputs:
                    if item.blob == reference:
                        return materialized[item.path].read_bytes()
                raise PackagingError("FreeBSD verification requested an unknown blob")

            verify_package_result(plan, result, read_blob=read)
            self.verify_bytes(plan, result, content)
            return result

    def read_created_blob(self, reference):
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError(
                "native pkg bytes were not created by this adapter"
            ) from exc

    def verify_bytes(self, plan, result, content):
        self._require_plan(plan)
        expected_result = package_result_for(
            plan, files=logical_package_files(plan), artifacts=result.artifacts
        )
        if result != expected_result:
            raise PackagingError("FreeBSD result differs from accepted package plan")
        if (
            result.packager_identity != self.packager_identity
            or result.package_kind is not PackageKind.ARCHIVE
            or len(result.artifacts) != 1
        ):
            raise PackagingError("FreeBSD result differs from exact packager")
        artifact = result.artifacts[0]
        if (
            artifact.kind is not PackageFileKind.PACKAGE_OUTPUT
            or artifact.source_identity != plan.identity
            or artifact.target_identity != plan.target_identity
            or artifact.executable
            or artifact.role != "freebsd-pkg"
            or artifact.path
            != f"{self.projection.package}-{self.projection.version}.pkg"
            or not isinstance(content, bytes)
            or not len(content) <= self.projection.maximum_archive_bytes
            or (len(content), hashlib.sha256(content).hexdigest())
            != (artifact.blob.size, artifact.blob.digest)
        ):
            raise PackagingError("FreeBSD archive bytes differ from result")
        files = {v.path: v for v in result.files}
        expected = {v.installed_path: v for v in self.projection.payload}
        if set(files) != {v.input_path for v in expected.values()}:
            raise PackagingError("FreeBSD result input inventory differs")
        _require_single_xz_frame(
            content,
            sum(v.blob.size for v in files.values())
            + 2 * _MAX_METADATA
            + 4096 * (len(expected) + 2)
            + 10240,
        )
        manifests, seen, regular = {}, set(), set()
        manifest_files = {}
        required_libraries, provided_libraries = set(), set()
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as archive:
                for index, member in enumerate(archive):
                    if index >= _MAX_MEMBERS + 2:
                        raise PackagingError("FreeBSD archive exceeds member bound")
                    path = _archive_path(member.name)
                    is_manifest = path in {"/+MANIFEST", "/+COMPACT_MANIFEST"}
                    # pkg 2.8's packing owner encodes metadata as zero-time root
                    # entries and payloads with reproducible nobody numeric IDs.
                    # Installation resolves mandatory root/wheel names instead.
                    numeric_owner = 0 if is_manifest else 65534
                    timestamp = 0 if is_manifest else self.projection.source_date_epoch
                    _require_tar_metadata(
                        member, numeric_owner=numeric_owner, timestamp=timestamp
                    )
                    if path in seen:
                        raise PackagingError("FreeBSD archive has duplicate members")
                    seen.add(path)
                    if (
                        member.uid != numeric_owner
                        or member.gid != numeric_owner
                        or member.uname != "root"
                        or member.gname != "wheel"
                        or member.mtime != timestamp
                        or not (member.isfile() or member.isdir())
                        or member.sparse is not None
                    ):
                        raise PackagingError(
                            "FreeBSD archive contains unsafe metadata/type"
                        )
                    if path in {"/+MANIFEST", "/+COMPACT_MANIFEST"}:
                        if (
                            not member.isfile()
                            or member.size > _MAX_METADATA
                            or member.mode != 0o644
                        ):
                            raise PackagingError(
                                "FreeBSD native manifest metadata differs"
                            )
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise PackagingError("FreeBSD native manifest is missing")
                        manifests[path] = _document(stream.read(_MAX_METADATA + 1))
                        if path == "/+MANIFEST":
                            manifest_files = _manifest_files(manifests[path])
                        continue
                    if member.isdir():
                        if member.mode != 0o755 or not any(
                            v.startswith(path.rstrip("/") + "/") for v in expected
                        ):
                            raise PackagingError(
                                "FreeBSD archive contains undeclared directories"
                            )
                        continue
                    if path not in expected or "/+MANIFEST" not in manifests:
                        raise PackagingError(
                            "FreeBSD archive contains undeclared/unbound files"
                        )
                    regular.add(path)
                    projected = expected[path]
                    logical = files[projected.input_path]
                    if (
                        member.size != logical.blob.size
                        or member.mode != projected.mode
                    ):
                        raise PackagingError("FreeBSD payload size/mode differs")
                    record = manifest_files.get(path)
                    if not isinstance(record, dict) or set(record) - {
                        "sum",
                        "uname",
                        "gname",
                        "perm",
                        "mtime",
                    }:
                        raise PackagingError(
                            "FreeBSD native file metadata is unsupported"
                        )
                    if (
                        record.get("uname") != "root"
                        or record.get("gname") != "wheel"
                        or record.get("perm")
                        not in {f"{projected.mode:04o}", f"0{projected.mode:04o}"}
                        or record.get("mtime") != self.projection.source_date_epoch
                    ):
                        raise PackagingError("FreeBSD manifest file metadata differs")
                    native, encode = _native_hasher(record.get("sum"))
                    accepted = hashlib.sha256()
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise PackagingError("FreeBSD payload stream is missing")
                    size = 0
                    while block := stream.read(1024 * 1024):
                        size += len(block)
                        if size > logical.blob.size:
                            raise PackagingError(
                                "FreeBSD payload exceeds accepted size"
                            )
                        native.update(block)
                        accepted.update(block)
                    if (
                        size != logical.blob.size
                        or accepted.hexdigest() != logical.blob.digest
                        or encode(native.digest()) != record["sum"]
                    ):
                        raise PackagingError(
                            "FreeBSD payload or native checksum differs"
                        )
                    required, provided = inspect_freebsd_package_elf(
                        stream, size, self.projection.abi
                    )
                    required_libraries.update(required)
                    provided_libraries.update(provided)
                archive.fileobj.seek(archive.offset)
                padding = 0
                while tail := archive.fileobj.read(1024 * 1024):
                    padding += len(tail)
                    if any(tail):
                        raise PackagingError("FreeBSD package has trailing tar data")
                if padding < 1024 or padding % 512:
                    raise PackagingError("FreeBSD tar padding is invalid")
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            if isinstance(exc, PackagingError):
                raise
            raise PackagingError("FreeBSD native package archive is invalid") from exc
        if regular != set(expected) or set(manifests) != {
            "/+MANIFEST",
            "/+COMPACT_MANIFEST",
        }:
            raise PackagingError("FreeBSD archive inventory is incomplete")
        full, compact = manifests["/+MANIFEST"], manifests["/+COMPACT_MANIFEST"]
        expected_metadata = self.projection.manifest
        for name, value in expected_metadata.items():
            if (
                full.get(name, {} if name == "deps" else None) != value
                or compact.get(name, {} if name == "deps" else None) != value
            ):
                raise PackagingError("FreeBSD native package intent differs")
        if set(full) - set(expected_metadata) - {
            "files",
            "flatsize",
            "licenselogic",
            "shlibs_required",
            "shlibs_provided",
        } or set(compact) - set(expected_metadata) - {
            "flatsize",
            "licenselogic",
            "shlibs_required",
            "shlibs_provided",
        }:
            raise PackagingError("FreeBSD native manifest has undeclared fields/hooks")
        for name, expected_libraries in (
            ("shlibs_required", required_libraries - provided_libraries),
            ("shlibs_provided", provided_libraries),
        ):
            expected_libraries = sorted(expected_libraries)
            for manifest in (full, compact):
                actual = manifest.get(name, [])
                if (
                    not isinstance(actual, list)
                    or any(not isinstance(value, str) for value in actual)
                    or sorted(actual) != expected_libraries
                ):
                    raise PackagingError(
                        "FreeBSD native shared libraries differ "
                        "from accepted ELF payload"
                    )
        if (
            full.get("flatsize") != sum(v.blob.size for v in files.values())
            or compact.get("flatsize") != full.get("flatsize")
            or full.get("licenselogic", "single") != "single"
            or compact.get("licenselogic", "single") != "single"
        ):
            raise PackagingError(
                "FreeBSD native manifest size/license metadata differs"
            )
        if set(manifest_files) != set(expected):
            raise PackagingError("FreeBSD native manifest file inventory differs")

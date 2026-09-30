"""Exact Debian projection, bound ``dpkg-deb`` construction, and verification."""

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.packaging import (
    logical_package_files,
    materialized_package_input_bytes,
    package_result_for,
    validate_materialized_package_root,
)
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageKind,
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

_DEBIAN_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
_DEBIAN_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+:~-]{0,127}$")
_DEBIAN_COORDINATE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]*(?: \((?:<<|<=|=|>=|>>|<|>) "
    r"[0-9A-Za-z.+:~-]+\))?$"
)
_OUTPUT_LIMIT = 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_FIXED_EPOCH = 946684800  # 2000-01-01, accepted by gzip and old dpkg releases.


def debian_architecture(target_architecture: str, worker_architecture: str) -> str:
    """Map equal accepted target/worker architectures to a Debian architecture."""

    aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    target = aliases.get(target_architecture.casefold())
    worker = aliases.get(worker_architecture.casefold())
    if target is None or worker is None:
        raise PackagingError("Debian packaging requires x86_64/amd64 or arm64")
    if target != worker:
        raise PackagingError("Debian target and worker architectures contradict")
    return target


def _properties(component: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    raw = component.get("properties", [])
    if not isinstance(raw, list):
        raise PackagingError("resolved CycloneDX component properties are invalid")
    for value in raw:
        if not isinstance(value, dict):
            raise PackagingError("resolved CycloneDX component property is invalid")
        name = value.get("name")
        item = value.get("value")
        if not isinstance(name, str) or not isinstance(item, str) or not item:
            raise PackagingError("resolved CycloneDX component property is invalid")
        result.setdefault(name, []).append(item)
    return {name: tuple(values) for name, values in result.items()}


def debian_runtime_dependencies(
    plan: PackagePlan, resolved_sbom: bytes
) -> tuple[str, ...]:
    """Resolve external requirements only from explicit accepted BOM coordinates."""

    try:
        document = json.loads(resolved_sbom)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagingError("resolved CycloneDX SBOM is not valid UTF-8 JSON") from exc
    components = document.get("components") if isinstance(document, dict) else None
    if not isinstance(components, list):
        raise PackagingError("resolved CycloneDX SBOM has no component inventory")
    by_requirement: dict[str, list[str]] = {}
    for candidate in components:
        if not isinstance(candidate, dict):
            raise PackagingError("resolved CycloneDX inventory is invalid")
        properties = _properties(candidate)
        requirements = properties.get("literate-ai:runtime-requirement-identity", ())
        coordinates = properties.get("literate-ai:debian-package", ())
        for requirement in requirements:
            by_requirement.setdefault(requirement, []).extend(coordinates)
    resolved: list[str] = []
    for requirement in plan.runtime_requirements:
        if requirement.supplied_by_package:
            continue
        coordinates = tuple(
            dict.fromkeys(by_requirement.get(requirement.requirement_identity.uri, ()))
        )
        if (
            len(coordinates) != 1
            or _DEBIAN_COORDINATE.fullmatch(coordinates[0]) is None
        ):
            raise PackagingError(
                "external runtime requirement has no unique accepted Debian "
                "coordinate: "
                f"{requirement.requirement_id}"
            )
        resolved.append(coordinates[0])
    if len({item.casefold() for item in resolved}) != len(resolved):
        raise PackagingError("Debian runtime dependency coordinates collide")
    return tuple(sorted(resolved))


@dataclass(frozen=True, slots=True)
class DebianPayloadEntry:
    input_path: str
    installed_path: str
    mode: int

    def __post_init__(self) -> None:
        source = PurePosixPath(self.input_path)
        target = PurePosixPath(self.installed_path)
        if (
            not self.input_path
            or source.is_absolute()
            or ".." in source.parts
            or not target.is_absolute()
            or ".." in target.parts
            or target.as_posix() != self.installed_path
        ):
            raise PackagingError("Debian projection contains an unsafe path")
        if self.mode not in {0o644, 0o755}:
            raise PackagingError("Debian projection contains an unsupported file mode")

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "installed_path": self.installed_path,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class DebianPackageProjection:
    source_package_plan_identity: ContentIdentity
    source_packager_identity: ContentIdentity
    package: str
    version: str
    architecture: str
    dependencies: tuple[str, ...]
    payload: tuple[DebianPayloadEntry, ...]
    tool_identity: ContentIdentity
    source_date_epoch: int = _FIXED_EPOCH

    def __post_init__(self) -> None:
        if len(self.package) < 2 or _DEBIAN_NAME.fullmatch(self.package) is None:
            raise PackagingError("Debian package name is invalid")
        if _DEBIAN_VERSION.fullmatch(self.version) is None:
            raise PackagingError("Debian package version is invalid")
        if self.architecture not in {"amd64", "arm64"}:
            raise PackagingError("Debian package architecture is unsupported")
        if tuple(sorted(self.dependencies)) != self.dependencies:
            raise PackagingError("Debian dependencies must be canonical")
        if any(
            _DEBIAN_COORDINATE.fullmatch(item) is None for item in self.dependencies
        ):
            raise PackagingError("Debian dependency coordinate is invalid")
        if (
            not isinstance(self.source_package_plan_identity, ContentIdentity)
            or not isinstance(self.source_packager_identity, ContentIdentity)
            or not isinstance(self.tool_identity, ContentIdentity)
        ):
            raise PackagingError("Debian projection identities must be typed")
        if (
            tuple(sorted(self.payload, key=lambda item: item.installed_path))
            != self.payload
        ):
            raise PackagingError("Debian payload must be canonical")
        sources = [item.input_path.casefold() for item in self.payload]
        targets = [item.installed_path.casefold() for item in self.payload]
        if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
            raise PackagingError("Debian projection paths collide")
        if self.source_date_epoch != _FIXED_EPOCH:
            raise PackagingError("Debian reproducibility epoch differs")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/debian-package-projection@1",
            "source_package_plan_identity": self.source_package_plan_identity.to_dict(),
            "source_packager_identity": self.source_packager_identity.to_dict(),
            "package": self.package,
            "version": self.version,
            "architecture": self.architecture,
            "dependencies": list(self.dependencies),
            "payload": [item.to_dict() for item in self.payload],
            "tool_identity": self.tool_identity.to_dict(),
            "source_date_epoch": self.source_date_epoch,
            "reproducibility": {
                "compression": "gzip",
                "compression_level": 9,
                "uniform_compression": True,
                "root_owner_group": True,
                "locale": "C",
                "timezone": "UTC",
                "umask": "0022",
            },
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "DebianPackageProjection"
    ) -> DebianPackageProjection:
        if not isinstance(value, dict):
            raise PackagingError(f"{path} must be an object")
        expected = {
            "schema",
            "source_package_plan_identity",
            "source_packager_identity",
            "package",
            "version",
            "architecture",
            "dependencies",
            "payload",
            "tool_identity",
            "source_date_epoch",
            "reproducibility",
        }
        if set(value) != expected or value.get("schema") != (
            "literate-ai/debian-package-projection@1"
        ):
            raise PackagingError(f"{path} has invalid fields or schema")
        dependencies = value.get("dependencies")
        payload = value.get("payload")
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise PackagingError(f"{path}.dependencies must be strings")
        if not isinstance(payload, list):
            raise PackagingError(f"{path}.payload must be an array")
        entries: list[DebianPayloadEntry] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict) or set(item) != {
                "input_path",
                "installed_path",
                "mode",
            }:
                raise PackagingError(f"{path}.payload[{index}] is invalid")
            input_path = item.get("input_path")
            installed_path = item.get("installed_path")
            mode = item.get("mode")
            if (
                not isinstance(input_path, str)
                or not isinstance(installed_path, str)
                or not isinstance(mode, int)
                or isinstance(mode, bool)
            ):
                raise PackagingError(f"{path}.payload[{index}] is invalid")
            entries.append(DebianPayloadEntry(input_path, installed_path, mode))
        package = value.get("package")
        version = value.get("version")
        architecture = value.get("architecture")
        source_date_epoch = value.get("source_date_epoch")
        if (
            not isinstance(package, str)
            or not isinstance(version, str)
            or not isinstance(architecture, str)
            or not isinstance(source_date_epoch, int)
            or isinstance(source_date_epoch, bool)
        ):
            raise PackagingError(f"{path} has invalid scalar fields")
        projection = cls(
            ContentIdentity.from_dict(
                value["source_package_plan_identity"],
                path=f"{path}.source_package_plan_identity",
            ),
            ContentIdentity.from_dict(
                value["source_packager_identity"],
                path=f"{path}.source_packager_identity",
            ),
            package,
            version,
            architecture,
            tuple(dependencies),
            tuple(entries),
            ContentIdentity.from_dict(
                value["tool_identity"], path=f"{path}.tool_identity"
            ),
            source_date_epoch,
        )
        if projection.to_dict() != value:
            raise PackagingError(f"{path} is not canonical")
        return projection

    @property
    def control(self) -> bytes:
        fields = [
            f"Package: {self.package}",
            f"Version: {self.version}",
            f"Architecture: {self.architecture}",
            "Maintainer: Literate AI "
            "<literate-ai-maintainers@users.noreply.github.com>",
        ]
        if self.dependencies:
            fields.append(f"Depends: {', '.join(self.dependencies)}")
        fields.append("Description: Exact accepted Literate AI Component package")
        return ("\n".join(fields) + "\n").encode()


def project_debian_package(
    plan: PackagePlan,
    *,
    package: str,
    version: str,
    target_architecture: str,
    worker_architecture: str,
    resolved_sbom: bytes,
    tool_identity: ContentIdentity,
) -> DebianPackageProjection:
    """Project exact logical inputs into one target-specific Debian filesystem."""

    entrypoints = {item.path: item.name for item in plan.entrypoints}
    payload: list[DebianPayloadEntry] = []
    for item in plan.inputs:
        if item.path in entrypoints:
            installed = f"/usr/bin/{entrypoints[item.path]}"
            mode = 0o755
        elif item.path == "specification/component.md":
            installed = f"/usr/share/doc/{package}/literate-ai/component.md"
            mode = 0o644
        elif item.path == "sbom/resolved.cdx.json":
            installed = f"/usr/share/doc/{package}/literate-ai/resolved.cdx.json"
            mode = 0o644
        else:
            installed = f"/usr/lib/{package}/{item.path}"
            mode = 0o755 if item.executable else 0o644
        payload.append(DebianPayloadEntry(item.path, installed, mode))
    return DebianPackageProjection(
        plan.identity,
        plan.packager_identity,
        package,
        version,
        debian_architecture(target_architecture, worker_architecture),
        debian_runtime_dependencies(plan, resolved_sbom),
        tuple(sorted(payload, key=lambda item: item.installed_path)),
        tool_identity,
    )


def _executable_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class DpkgDebToolBinding:
    command: tuple[str, ...]
    executable: Path
    executable_identity: str
    version: str

    @classmethod
    def discover(cls, command: Sequence[str] = ("dpkg-deb",)) -> DpkgDebToolBinding:
        values = tuple(command)
        if not values or any(not isinstance(item, str) or not item for item in values):
            raise PackagingError("dpkg-deb command must be a non-empty argv sequence")
        selected = shutil.which(values[0])
        if selected is None:
            raise PackagingError("dpkg-deb is unavailable on the selected worker")
        executable = Path(selected).resolve(strict=True)
        resolved = (str(executable), *values[1:])
        completed = run_with_tree_kill([*resolved, "--version"], text=True, timeout=15)
        output = (completed.stdout + completed.stderr).strip()
        if (
            completed.returncode != 0
            or not output
            or len(output.encode()) > _OUTPUT_LIMIT
        ):
            raise PackagingError("dpkg-deb version probe failed")
        return cls(resolved, executable, _executable_digest(executable), output)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/dpkg-deb-tool-binding@1",
                "executable_identity": self.executable_identity,
                "version": self.version,
                "arguments": list(self.command[1:]),
            }
        )

    def require_unchanged(self) -> None:
        try:
            current = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise PackagingError("dpkg-deb executable became unavailable") from exc
        if (
            current != self.executable
            or _executable_digest(current) != self.executable_identity
        ):
            raise PackagingError(
                "dpkg-deb executable changed during package construction"
            )


def _parse_ar(content: bytes) -> dict[str, bytes]:
    if not content.startswith(b"!<arch>\n"):
        raise PackagingError("Debian package is not an ar archive")
    offset = 8
    members: dict[str, bytes] = {}
    while offset < len(content):
        if offset + 60 > len(content):
            raise PackagingError("Debian ar header is truncated")
        header = content[offset : offset + 60]
        offset += 60
        if header[58:60] != b"`\n":
            raise PackagingError("Debian ar header is invalid")
        try:
            name = header[:16].decode("ascii").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackagingError("Debian ar member metadata is invalid") from exc
        if not name or name in members or size < 0 or offset + size > len(content):
            raise PackagingError("Debian ar members are invalid")
        members[name] = content[offset : offset + size]
        offset += size + (size % 2)
    if offset != len(content):
        raise PackagingError("Debian ar archive has trailing bytes")
    return members


def _tar_members(content: bytes, label: str) -> tuple[tarfile.TarInfo, ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            members = tuple(archive.getmembers())
    except (tarfile.TarError, OSError) as exc:
        raise PackagingError(f"Debian {label} archive is invalid") from exc
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise PackagingError(f"Debian {label} archive contains too many members")
    return members


def _archive_member_path(name: str, *, label: str) -> str:
    raw = PurePosixPath(name)
    if raw.is_absolute() or ".." in raw.parts or "\\" in name:
        raise PackagingError(f"Debian {label} archive contains an unsafe path")
    parts = tuple(part for part in raw.parts if part != ".")
    if not parts:
        return "/"
    normalized = PurePosixPath(*parts).as_posix()
    if normalized.startswith("/"):
        raise PackagingError(f"Debian {label} archive contains an unsafe path")
    return "/" + normalized


def _set_reproducible_timestamp(path: Path, epoch: int) -> None:
    """Set one private staging path without following a replaced link."""

    if path.is_symlink():
        raise PackagingError("Debian staging tree contains a symbolic link")
    times = (epoch, epoch)
    try:
        os.utime(path, times, follow_symlinks=False)
    except NotImplementedError as exc:
        # Windows does not implement follow_symlinks=False for os.utime. This tree
        # is private adapter staging, and the link check above retains the refusal
        # boundary before using the platform's ordinary timestamp operation.
        if path.is_symlink():
            raise PackagingError(
                "Debian staging tree contains a symbolic link"
            ) from exc
        os.utime(path, times)


class DebianPackageAdapter:
    """Construct and independently inspect a real target-specific ``.deb``."""

    def __init__(self, projection: DebianPackageProjection, tool: DpkgDebToolBinding):
        if projection.tool_identity != tool.identity:
            raise PackagingError("Debian projection binds another dpkg-deb tool")
        self.projection = projection
        self.tool = tool
        self._created: dict[str, bytes] = {}

    @property
    def packager_identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/debian-packager@1",
                "projection_identity": self.projection.identity.uri,
            }
        )

    def package(
        self, plan: PackagePlan, *, materialized_root: Path, object_root: Path
    ) -> PackageResult:
        if platform.system().casefold() != "linux":
            raise PackagingError("local Debian construction requires a Linux worker")
        if plan.package_kind is not PackageKind.ARCHIVE:
            raise PackagingError("Debian adapter requires the archive package kind")
        source_plan = replace(
            plan, packager_identity=self.projection.source_packager_identity
        )
        if source_plan.identity != self.projection.source_package_plan_identity:
            raise PackagingError("Debian projection binds another source package plan")
        if plan.packager_identity != self.packager_identity:
            raise PackagingError("Debian packager identity differs from the plan")
        _root, materialized = validate_materialized_package_root(
            plan, materialized_root
        )
        projected = {item.input_path: item for item in self.projection.payload}
        if set(projected) != {item.path for item in plan.inputs}:
            raise PackagingError("Debian projection does not cover exact plan inputs")
        output_root = Path(object_root)
        if output_root.is_symlink():
            raise PackagingError("Debian object root cannot be a symbolic link")
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="debian-build-", dir=output_root
        ) as raw:
            staging = Path(raw)
            package_root = staging / "root"
            control_root = package_root / "DEBIAN"
            control_root.mkdir(parents=True)
            control = control_root / "control"
            control.write_bytes(self.projection.control)
            control.chmod(0o644)
            for item in plan.inputs:
                source = materialized[item.path]
                if not source.is_file():
                    raise PackagingError(
                        "first Debian adapter requires regular-file package inputs"
                    )
                projected_item = projected[item.path]
                destination = package_root.joinpath(
                    *PurePosixPath(projected_item.installed_path).parts[1:]
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(projected_item.mode)
            for path in sorted(package_root.rglob("*"), reverse=True):
                _set_reproducible_timestamp(path, self.projection.source_date_epoch)
            output = staging / (
                f"{self.projection.package}_{self.projection.version}_"
                f"{self.projection.architecture}.deb"
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "LC_ALL": "C",
                    "LANG": "C",
                    "TZ": "UTC",
                    "SOURCE_DATE_EPOCH": str(self.projection.source_date_epoch),
                }
            )
            self.tool.require_unchanged()
            completed = run_with_tree_kill(
                [
                    *self.tool.command,
                    "--root-owner-group",
                    "--uniform-compression",
                    "-Zgzip",
                    "-z9",
                    "--build",
                    str(package_root),
                    str(output),
                ],
                cwd=staging,
                env=environment,
                text=True,
                timeout=180,
            )
            self.tool.require_unchanged()
            if len((completed.stdout + completed.stderr).encode()) > _OUTPUT_LIMIT:
                raise PackagingError("dpkg-deb output exceeded its bounded limit")
            if completed.returncode != 0:
                raise PackagingError(
                    "dpkg-deb construction failed: "
                    + (completed.stdout + completed.stderr)[-4000:]
                )
            if not output.is_file() or output.is_symlink():
                raise PackagingError("dpkg-deb did not create the exact output")
            content = output.read_bytes()
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/vnd.debian.binary-package",
        )
        self._created[blob.identity] = content
        artifact = logical_package_files(plan)[0]
        artifact = type(artifact)(
            output.name,
            "debian-package",
            PackageFileKind.PACKAGE_OUTPUT,
            plan.identity,
            plan.target_identity,
            blob,
            False,
        )
        result = package_result_for(
            plan, files=logical_package_files(plan), artifacts=(artifact,)
        )

        def reader(reference: BlobRef) -> bytes:
            if reference == blob:
                return content
            item = next(
                candidate for candidate in plan.inputs if candidate.blob == reference
            )
            return materialized_package_input_bytes(materialized[item.path])

        verify_package_result(plan, result, read_blob=reader)
        self.verify_bytes(result, content)
        return result

    def verify_bytes(self, result: PackageResult, content: bytes) -> None:
        if (
            result.packager_identity != self.packager_identity
            or len(result.artifacts) != 1
        ):
            raise PackagingError("Debian result differs from exact packager authority")
        artifact = result.artifacts[0]
        if (
            len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError("Debian bytes differ from the package result")
        members = _parse_ar(content)
        if members.get("debian-binary") != b"2.0\n":
            raise PackagingError("Debian archive format marker differs")
        control_names = [name for name in members if name.startswith("control.tar")]
        data_names = [name for name in members if name.startswith("data.tar")]
        if (
            set(members) != {"debian-binary", *control_names, *data_names}
            or len(control_names) != 1
            or len(data_names) != 1
        ):
            raise PackagingError("Debian archive members are incomplete or undeclared")
        control_members = _tar_members(members[control_names[0]], "control")
        control_paths = {
            _archive_member_path(item.name, label="control"): item
            for item in control_members
        }
        regular_control = [item for item in control_members if item.isfile()]
        if len(control_paths) != len(control_members) or set(control_paths) - {
            "/",
            "/control",
        }:
            raise PackagingError("Debian control archive contains undeclared members")
        if (
            len(regular_control) != 1
            or _archive_member_path(regular_control[0].name, label="control")
            != "/control"
            or any(
                not (item.isfile() or item.isdir())
                or item.uid != 0
                or item.gid != 0
                or item.mtime != self.projection.source_date_epoch
                for item in control_members
            )
        ):
            raise PackagingError("Debian control archive contains an unsafe member")
        with tarfile.open(
            fileobj=io.BytesIO(members[control_names[0]]), mode="r:*"
        ) as archive:
            stream = archive.extractfile(regular_control[0])
            if stream is None or stream.read() != self.projection.control:
                raise PackagingError("Debian control fields differ")
        data_members = _tar_members(members[data_names[0]], "payload")
        regular = {}
        directories = {}
        for item in data_members:
            path = _archive_member_path(item.name, label="payload")
            if (
                item.issym()
                or item.islnk()
                or not (item.isfile() or item.isdir())
                or item.uid != 0
                or item.gid != 0
                or item.mtime != self.projection.source_date_epoch
            ):
                raise PackagingError("Debian payload contains an unsafe member")
            if item.isfile():
                if path in regular:
                    raise PackagingError("Debian payload contains duplicate paths")
                regular[path] = item
            else:
                if path in directories:
                    raise PackagingError("Debian payload contains duplicate paths")
                directories[path] = item
        expected = {item.installed_path: item for item in self.projection.payload}
        if set(regular) != set(expected):
            raise PackagingError("Debian payload paths differ from the projection")
        expected_directories = {"/"}
        for path in expected:
            parent = PurePosixPath(path).parent
            while parent.as_posix() != "/":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if set(directories) != expected_directories or any(
            stat.S_IMODE(item.mode) != 0o755 for item in directories.values()
        ):
            raise PackagingError("Debian payload directories differ from projection")
        files = {item.path: item for item in result.files}
        with tarfile.open(
            fileobj=io.BytesIO(members[data_names[0]]), mode="r:*"
        ) as archive:
            for path, projected in expected.items():
                member = regular[path]
                stream = archive.extractfile(member)
                logical = files[projected.input_path]
                if (
                    stream is None
                    or member.uid != 0
                    or member.gid != 0
                    or stat.S_IMODE(member.mode) != projected.mode
                ):
                    raise PackagingError("Debian payload metadata differs")
                body = stream.read()
                if (
                    len(body) != logical.blob.size
                    or hashlib.sha256(body).hexdigest() != logical.blob.digest
                ):
                    raise PackagingError("Debian payload content digest differs")

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError("Debian blob was not created by this adapter") from exc


__all__ = [
    "DebianPackageAdapter",
    "DebianPackageProjection",
    "DebianPayloadEntry",
    "DpkgDebToolBinding",
    "debian_architecture",
    "debian_runtime_dependencies",
    "project_debian_package",
]

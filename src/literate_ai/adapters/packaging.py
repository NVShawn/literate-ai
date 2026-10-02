"""Deterministic package adapters over exact package plans and blob readers."""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import re
import stat
import struct
import sys
import tarfile
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path

from literate_ai.application.packaging import (
    PackageBlobReader,
    PackagingError,
    verify_package_result,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.packages import (
    PackagedFile,
    PackageFileKind,
    PackageInput,
    PackageKind,
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.versioning import SemanticVersion

_WHEEL_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WHEEL_TAG = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+$")
_NPM_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def native_archive_package_plan(
    base: PackagePlan,
    *,
    packager_identity,
    specification: bytes,
    resolved_sbom: bytes,
    resolved_sbom_source_identity,
) -> PackagePlan:
    """Add exact specification and resolved-SBOM resources to a native plan."""

    resources = []
    for path, role, content, source_identity, media_type in (
        (
            "specification/component.md",
            "component-specification",
            specification,
            base.root_component_revision,
            "text/markdown",
        ),
        (
            "sbom/resolved.cdx.json",
            "cyclonedx-resolved-sbom",
            resolved_sbom,
            resolved_sbom_source_identity,
            "application/vnd.cyclonedx+json",
        ),
    ):
        resources.append(
            PackageInput(
                path,
                role,
                PackageFileKind.RESOURCE,
                source_identity,
                base.target_identity,
                BlobRef(
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    media_type=media_type,
                ),
            )
        )
    if {item.path for item in base.inputs}.intersection(
        item.path for item in resources
    ):
        raise PackagingError(
            "accepted package already occupies reserved package resource paths"
        )
    return replace(
        base,
        package_kind=PackageKind.ARCHIVE,
        packager_identity=packager_identity,
        inputs=tuple(sorted((*base.inputs, *resources), key=lambda item: item.path)),
    )


def lifecycle_archive_package_plan(
    base: PackagePlan,
    *,
    packager_identity,
    specification: bytes,
    source_sbom: bytes,
    source_sbom_identity,
    resolved_sbom: bytes,
    resolved_sbom_source_identity,
) -> PackagePlan:
    """Bind both CycloneDX lifecycle documents into one native archive plan."""

    plan = native_archive_package_plan(
        base,
        packager_identity=packager_identity,
        specification=specification,
        resolved_sbom=resolved_sbom,
        resolved_sbom_source_identity=resolved_sbom_source_identity,
    )
    path = "sbom/source.cdx.json"
    if any(item.path == path for item in plan.inputs):
        raise PackagingError("accepted package already occupies source SBOM path")
    source = PackageInput(
        path,
        "cyclonedx-source-sbom",
        PackageFileKind.RESOURCE,
        source_sbom_identity,
        plan.target_identity,
        BlobRef(
            hashlib.sha256(source_sbom).hexdigest(),
            len(source_sbom),
            media_type="application/vnd.cyclonedx+json",
        ),
    )
    return replace(
        plan,
        inputs=tuple(sorted((*plan.inputs, source), key=lambda item: item.path)),
    )


# Preserve the public npm helper while sharing the exact resource projection.
npm_archive_package_plan = lifecycle_archive_package_plan


_NATIVE_METADATA_PROVIDERS = frozenset({"apt", "brew", "winget", "chocolatey"})


def native_metadata_resource(
    provider: str, distribution: str, version: str
) -> tuple[str, bytes]:
    """Return the provider-native control file path and deterministic bytes."""

    if provider == "apt":
        path = "DEBIAN/control"
        body = (
            f"Package: {distribution}\n"
            f"Version: {version}\n"
            "Architecture: all\n"
            "Maintainer: Literate AI "
            "<literate-ai-maintainers@users.noreply.github.com>\n"
            "Description: Flavor-selected native package\n"
        )
    elif provider == "brew":
        path = f"Formula/{distribution}.rb"
        class_name = distribution.replace("-", "").title().replace("_", "")
        body = (
            f"class {class_name} < Formula\n"
            f'  desc "Flavor-selected native package"\n'
            f'  homepage "https://github.com/jordanhubbard/literate-ai"\n'
            f'  version "{version}"\n'
            "end\n"
        )
    elif provider == "winget":
        path = "manifest.yaml"
        body = (
            f"PackageIdentifier: {distribution}\n"
            f"PackageVersion: {version}\n"
            "PackageLocale: en-US\n"
        )
    elif provider == "chocolatey":
        path = f"{distribution}.nuspec"
        body = (
            '<?xml version="1.0"?>\n'
            "<package>\n"
            "  <metadata>\n"
            f"    <id>{distribution}</id>\n"
            f"    <version>{version}</version>\n"
            "  </metadata>\n"
            "</package>\n"
        )
    else:
        raise PackagingError(f"native metadata is not implemented for {provider}")
    return path, body.encode("utf-8")


def native_metadata_archive_plan(
    base: PackagePlan,
    *,
    packager_identity,
    specification: bytes,
    resolved_sbom: bytes,
    resolved_sbom_source_identity,
    provider: str,
    distribution: str,
    version: str,
) -> tuple[PackagePlan, str, bytes]:
    """Extend a native archive plan with one provider control file."""

    plan = native_archive_package_plan(
        base,
        packager_identity=packager_identity,
        specification=specification,
        resolved_sbom=resolved_sbom,
        resolved_sbom_source_identity=resolved_sbom_source_identity,
    )
    path, content = native_metadata_resource(provider, distribution, version)
    extra = PackageInput(
        path,
        "native-package-metadata",
        PackageFileKind.RESOURCE,
        plan.identity,
        plan.target_identity,
        BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="text/plain",
        ),
    )
    if any(item.path == path for item in plan.inputs):
        raise PackagingError("accepted package already occupies native metadata path")
    return (
        replace(
            plan,
            inputs=tuple(sorted((*plan.inputs, extra), key=lambda item: item.path)),
        ),
        path,
        content,
    )


def logical_package_files(plan: PackagePlan) -> tuple[PackagedFile, ...]:
    executable_paths = {item.path for item in plan.entrypoints}
    return tuple(
        PackagedFile(
            item.path,
            item.role,
            item.kind,
            item.source_identity,
            item.target_identity,
            item.blob,
            item.executable or item.path in executable_paths,
        )
        for item in plan.inputs
    )


def package_result_for(
    plan: PackagePlan,
    *,
    files: tuple[PackagedFile, ...],
    artifacts: tuple[PackagedFile, ...],
) -> PackageResult:
    return PackageResult(
        package_plan_identity=plan.identity,
        root_component_revision=plan.root_component_revision,
        component_lock_identity=plan.component_lock_identity,
        target_identity=plan.target_identity,
        artifact_graph_identity=plan.artifact_graph_identity,
        package_kind=plan.package_kind,
        packager_identity=plan.packager_identity,
        files=files,
        artifacts=artifacts,
        entrypoints=plan.entrypoints,
        runtime_requirements=plan.runtime_requirements,
        native_library_root=plan.native_library_root,
        native_library_layout=plan.native_library_layout,
    )


class DirectoryPackageAdapter:
    """Retain the exact release-relative file tree as package artifacts."""

    _KINDS = frozenset(
        {
            PackageKind.DIRECTORY,
            PackageKind.RUNTIME_BUNDLE,
            PackageKind.STANDALONE_EXECUTABLE,
        }
    )

    def package(
        self, plan: PackagePlan, *, read_blob: PackageBlobReader
    ) -> PackageResult:
        if plan.package_kind not in self._KINDS:
            raise PackagingError(
                f"directory adapter cannot produce {plan.package_kind.value}"
            )
        files = logical_package_files(plan)
        result = package_result_for(plan, files=files, artifacts=files)
        return verify_package_result(plan, result, read_blob=read_blob)


class DeterministicZipPackageAdapter:
    """Produce a byte-stable ZIP while retaining its exact logical file closure."""

    def __init__(self) -> None:
        self._created: dict[str, bytes] = {}

    def package(
        self, plan: PackagePlan, *, read_blob: PackageBlobReader
    ) -> PackageResult:
        if plan.package_kind is not PackageKind.ARCHIVE:
            raise PackagingError("ZIP adapter requires the archive package kind")
        files = logical_package_files(plan)
        stream = io.BytesIO()
        with zipfile.ZipFile(
            stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for item in files:
                content = read_blob(item.blob)
                if not isinstance(content, bytes):
                    raise PackagingError("package blob reader must return bytes")
                information = zipfile.ZipInfo(
                    item.path, date_time=(1980, 1, 1, 0, 0, 0)
                )
                information.compress_type = zipfile.ZIP_DEFLATED
                information.create_system = 3
                information.external_attr = (
                    0o100755 if item.executable else 0o100644
                ) << 16
                archive.writestr(information, content, compresslevel=9)
        content = stream.getvalue()
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/zip",
        )
        self._created[blob.identity] = content
        artifact = PackagedFile(
            f"package-{plan.identity.digest}.zip",
            "archive",
            PackageFileKind.PACKAGE_OUTPUT,
            plan.identity,
            plan.target_identity,
            blob,
            False,
        )

        def combined_reader(reference: BlobRef) -> bytes:
            created = self._created.get(reference.identity)
            return read_blob(reference) if created is None else created

        result = package_result_for(plan, files=files, artifacts=(artifact,))
        return verify_package_result(plan, result, read_blob=combined_reader)

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError("ZIP blob was not created by this adapter") from exc


def materialized_package_input_bytes(path: Path) -> bytes:
    """Return the immutable blob bytes represented by one materialized input."""

    if path.is_symlink():
        raise PackagingError("materialized package input cannot be a symbolic link")
    if path.is_file():
        return path.read_bytes()
    if not path.is_dir():
        raise PackagingError("materialized package input is unavailable")
    files = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise PackagingError("materialized package input cannot contain links")
        if child.is_dir():
            continue
        if not child.is_file():
            raise PackagingError(
                "materialized package input must contain regular files"
            )
        files.append(child)
    if not files:
        raise PackagingError("materialized package directory cannot be empty")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for child in files:
            information = zipfile.ZipInfo(
                child.relative_to(path).as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            information.compress_type = zipfile.ZIP_STORED
            information.create_system = 3
            information.external_attr = (
                stat.S_IFREG | stat.S_IMODE(child.stat().st_mode)
            ) << 16
            archive.writestr(information, child.read_bytes())
    return stream.getvalue()


def validate_materialized_package_root(
    plan: PackagePlan, materialized_root: Path
) -> tuple[Path, dict[str, Path]]:
    """Recheck exact package inputs and reject undeclared materialized files."""

    supplied_root = Path(materialized_root)
    if supplied_root.is_symlink():
        raise PackagingError("materialized package root must not be a link")
    root = supplied_root.resolve(strict=True)
    if not root.is_dir():
        raise PackagingError("materialized package root must be a directory")
    expected = {item.path: item for item in plan.inputs}
    materialized: dict[str, Path] = {}
    for relative, item in expected.items():
        path = root.joinpath(*Path(relative).parts)
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PackagingError(
                f"materialized package input escaped its root: {relative}"
            ) from exc
        content = materialized_package_input_bytes(path)
        if (
            len(content) != item.blob.size
            or hashlib.sha256(content).hexdigest() != item.blob.digest
        ):
            raise PackagingError(
                f"materialized package input differs from plan: {relative}"
            )
        materialized[relative] = path

    covered_files = {
        child.relative_to(root).as_posix()
        for source in materialized.values()
        for child in ((source,) if source.is_file() else tuple(source.rglob("*")))
        if child.is_file()
    }
    actual_files = set()
    for child in root.rglob("*"):
        if child.is_symlink():
            raise PackagingError("materialized package root cannot contain links")
        if child.is_dir():
            continue
        if not child.is_file():
            raise PackagingError("materialized package root must contain regular files")
        actual_files.add(child.relative_to(root).as_posix())
    if actual_files != covered_files:
        raise PackagingError("materialized package root contains undeclared files")
    return root, materialized


def _tar_file(
    path: str, content: bytes, *, executable: bool = False
) -> tarfile.TarInfo:
    information = tarfile.TarInfo(path)
    information.size = len(content)
    information.mode = 0o755 if executable else 0o644
    information.uid = 0
    information.gid = 0
    information.uname = ""
    information.gname = ""
    information.mtime = 0
    return information


class NativeZipPackageAdapter(DeterministicZipPackageAdapter):
    """Expose deterministic ZIP construction through native package custody."""

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/zip-packager@1",
                "compression": "deflate-9",
                "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                "python_runtime": list(sys.version_info[:3]),
                "zipfile_sha256": hashlib.sha256(
                    Path(zipfile.__file__).read_bytes()
                ).hexdigest(),
                "timestamp": [1980, 1, 1, 0, 0, 0],
                "regular_file_modes": [0o644, 0o755],
            }
        )

    def package(self, plan: PackagePlan, *, materialized_root: Path) -> PackageResult:
        if plan.packager_identity != self.packager_identity:
            raise PackagingError("ZIP plan differs from exact packager authority")
        _root, materialized = validate_materialized_package_root(
            plan, materialized_root
        )

        def read_blob(reference: BlobRef) -> bytes:
            item = next((item for item in plan.inputs if item.blob == reference), None)
            if item is None:
                raise PackagingError("ZIP verification requested an unknown blob")
            return materialized_package_input_bytes(materialized[item.path])

        result = super().package(plan, read_blob=read_blob)
        self.verify_bytes(result, self.read_created_blob(result.artifacts[0].blob))
        return result

    def verify_bytes(self, result: PackageResult, content: bytes) -> None:
        """Inspect archive bytes without extracting, executing or publishing."""

        if result.packager_identity != self.packager_identity:
            raise PackagingError("ZIP result differs from exact packager authority")
        if result.package_kind is not PackageKind.ARCHIVE or len(result.artifacts) != 1:
            raise PackagingError("ZIP result must contain one archive")
        artifact = result.artifacts[0]
        if artifact.path != f"package-{result.package_plan_identity.digest}.zip":
            raise PackagingError("ZIP filename differs from exact package plan")
        if (
            not isinstance(content, bytes)
            or len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError("ZIP bytes differ from the package result")
        if content[:4] != b"PK\x03\x04" or content[-22:-18] != b"PK\x05\x06":
            raise PackagingError("ZIP archive has non-canonical framing")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                names = [item.filename for item in members]
                expected = {item.path: item for item in result.files}
                if (
                    names != sorted(expected)
                    or len(expected) != len(result.files)
                    or archive.comment
                ):
                    raise PackagingError(
                        "ZIP members differ from exact canonical closure"
                    )
                for member in members:
                    item = expected[member.filename]
                    mode = (0o100755 if item.executable else 0o100644) << 16
                    large_values = []
                    if (
                        member.file_size > zipfile.ZIP64_LIMIT
                        or member.compress_size > zipfile.ZIP64_LIMIT
                    ):
                        large_values.extend([member.file_size, member.compress_size])
                    if member.header_offset > zipfile.ZIP64_LIMIT:
                        large_values.append(member.header_offset)
                    canonical_extra = (
                        struct.pack("<HH", 1, 8 * len(large_values))
                        + struct.pack("<" + "Q" * len(large_values), *large_values)
                        if large_values
                        else b""
                    )
                    if (
                        member.date_time != (1980, 1, 1, 0, 0, 0)
                        or member.create_system != 3
                        or member.external_attr != mode
                        or member.compress_type != zipfile.ZIP_DEFLATED
                        or member.comment
                        or member.extra != canonical_extra
                        or member.flag_bits & ~0x800
                        or member.file_size != item.blob.size
                    ):
                        raise PackagingError("ZIP member metadata differs from plan")
                    body = archive.read(member)
                    if hashlib.sha256(body).hexdigest() != item.blob.digest:
                        raise PackagingError("ZIP member content digest differs")
        except (
            zipfile.BadZipFile,
            OSError,
            ValueError,
            RuntimeError,
            zlib.error,
        ) as exc:
            if isinstance(exc, PackagingError):
                raise
            raise PackagingError(
                "ZIP package is not a valid canonical archive"
            ) from exc


class NpmPackageAdapter:
    """Construct and independently verify one deterministic native npm tarball."""

    def __init__(self, name: str, version: str) -> None:
        if len(name) > 214 or not _NPM_COMPONENT.fullmatch(name):
            raise ValueError("npm package name is invalid")
        try:
            SemanticVersion.parse(version)
        except ValueError as exc:
            raise ValueError("npm package version is invalid") from exc
        self.name = name
        self.version = version
        self._created: dict[str, bytes] = {}

    @property
    def package_json(self) -> bytes:
        return (
            json.dumps(
                {
                    "description": "Exact accepted Literate AI Component package",
                    "files": ["literate-ai"],
                    "name": self.name,
                    "version": self.version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/npm-packager@1",
                "name": self.name,
                "version": self.version,
                "package_json_sha256": hashlib.sha256(self.package_json).hexdigest(),
                "payload_root": "package/literate-ai",
                "archive": "tar+gzip",
            }
        )

    def package(self, plan: PackagePlan, *, materialized_root: Path) -> PackageResult:
        if plan.package_kind is not PackageKind.ARCHIVE:
            raise PackagingError("npm adapter requires the archive package kind")
        if plan.packager_identity != self.packager_identity:
            raise PackagingError("npm metadata differs from exact packager authority")
        if any(item.path == "PACKAGE_PLAN.json" for item in plan.inputs):
            raise PackagingError("npm payload collides with package plan metadata")
        _root, materialized = validate_materialized_package_root(
            plan, materialized_root
        )
        entries: dict[str, tuple[bytes, bool]] = {
            "package/package.json": (self.package_json, False),
            "package/literate-ai/PACKAGE_PLAN.json": (
                json.dumps(
                    plan.to_dict(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
                False,
            ),
        }
        for item in plan.inputs:
            entries[f"package/literate-ai/{item.path}"] = (
                materialized_package_input_bytes(materialized[item.path]),
                item.executable
                or item.path in {entry.path for entry in plan.entrypoints},
            )
        tar_stream = io.BytesIO()
        with tarfile.open(
            fileobj=tar_stream, mode="w:", format=tarfile.PAX_FORMAT
        ) as archive:
            for path in sorted(entries):
                entry, executable = entries[path]
                archive.addfile(
                    _tar_file(path, entry, executable=executable), io.BytesIO(entry)
                )
        stream = io.BytesIO()
        with gzip.GzipFile(
            filename="", mode="wb", compresslevel=9, fileobj=stream, mtime=0
        ) as compressed:
            compressed.write(tar_stream.getvalue())
        content = stream.getvalue()
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/gzip",
        )
        self._created[blob.identity] = content
        artifact = PackagedFile(
            f"{self.name}-{self.version}.tgz",
            "npm-package",
            PackageFileKind.PACKAGE_OUTPUT,
            plan.identity,
            plan.target_identity,
            blob,
            False,
        )
        result = package_result_for(
            plan, files=logical_package_files(plan), artifacts=(artifact,)
        )

        def read_blob(reference: BlobRef) -> bytes:
            if reference == blob:
                return content
            item = next(
                (candidate for candidate in plan.inputs if candidate.blob == reference),
                None,
            )
            if item is None:
                raise PackagingError("npm verification requested an unknown blob")
            return materialized_package_input_bytes(materialized[item.path])

        verify_package_result(plan, result, read_blob=read_blob)
        self.verify_bytes(result, content)
        return result

    def verify_bytes(self, result: PackageResult, content: bytes) -> None:
        """Verify npm bytes without installing, publishing, or construction custody."""

        if result.packager_identity != self.packager_identity:
            raise PackagingError("npm result differs from exact packager authority")
        if len(result.artifacts) != 1:
            raise PackagingError("npm result must contain one native package")
        artifact = result.artifacts[0]
        if artifact.path != f"{self.name}-{self.version}.tgz":
            raise PackagingError("npm filename differs from exact packager authority")
        if (
            not isinstance(content, bytes)
            or len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError("npm bytes differ from the package result")
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                members = archive.getmembers()
                names = [item.name for item in members]
                if names != sorted(names) or len(names) != len(set(names)):
                    raise PackagingError("npm entries are not unique and canonical")
                if any(
                    not item.isfile()
                    or item.uid != 0
                    or item.gid != 0
                    or item.mtime != 0
                    or item.uname
                    or item.gname
                    for item in members
                ):
                    raise PackagingError("npm entries have non-canonical metadata")
                by_name = {item.name: item for item in members}
                plan_path = "package/literate-ai/PACKAGE_PLAN.json"
                metadata_path = "package/package.json"
                if plan_path not in by_name or metadata_path not in by_name:
                    raise PackagingError("npm package metadata is incomplete")
                if (
                    by_name[plan_path].mode != 0o644
                    or by_name[metadata_path].mode != 0o644
                ):
                    raise PackagingError("npm package metadata mode differs")
                metadata = archive.extractfile(by_name[metadata_path])
                plan_stream = archive.extractfile(by_name[plan_path])
                if metadata is None or metadata.read() != self.package_json:
                    raise PackagingError("npm package metadata drifted")
                if plan_stream is None:
                    raise PackagingError("npm package plan is unreadable")
                package_plan = PackagePlan.from_dict(json.loads(plan_stream.read()))
                if package_plan.identity != result.package_plan_identity:
                    raise PackagingError("npm package plan identity drifted")
                expected = {
                    metadata_path,
                    plan_path,
                    *(f"package/literate-ai/{item.path}" for item in result.files),
                }
                if set(names) != expected:
                    raise PackagingError("npm archive contains undeclared contents")
                entrypoints = {item.path for item in result.entrypoints}
                for item in result.files:
                    path = f"package/literate-ai/{item.path}"
                    stream = archive.extractfile(by_name[path])
                    if stream is None:
                        raise PackagingError("npm payload entry is unreadable")
                    body = stream.read()
                    if (
                        len(body) != item.blob.size
                        or hashlib.sha256(body).hexdigest() != item.blob.digest
                    ):
                        raise PackagingError("npm payload content digest differs")
                    expected_mode = (
                        0o755 if item.executable or item.path in entrypoints else 0o644
                    )
                    if by_name[path].mode != expected_mode:
                        raise PackagingError("npm payload executable mode differs")
        except (tarfile.TarError, OSError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, PackagingError):
                raise
            raise PackagingError(
                "npm package is not a valid canonical tarball"
            ) from exc

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError("npm blob was not created by this adapter") from exc


def _wheel_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _wheel_info(path: str, *, executable: bool = False) -> zipfile.ZipInfo:
    information = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    information.compress_type = zipfile.ZIP_DEFLATED
    information.create_system = 3
    information.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    return information


class WheelPackageAdapter:
    """Construct and independently verify one deterministic native wheel."""

    def __init__(self, distribution: str, version: str, tag: str = "py3-none-any"):
        if not _WHEEL_COMPONENT.fullmatch(distribution):
            raise ValueError("wheel distribution name is invalid")
        if not _WHEEL_COMPONENT.fullmatch(version):
            raise ValueError("wheel version is invalid")
        if not _WHEEL_TAG.fullmatch(tag):
            raise ValueError("wheel compatibility tag is invalid")
        self.distribution = distribution.replace("-", "_")
        self.version = version.replace("-", "_")
        self.tag = tag
        self._created: dict[str, bytes] = {}

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/wheel-packager@1",
                "distribution": self.distribution,
                "version": self.version,
                "tag": self.tag,
                "wheel_version": "1.0",
            }
        )

    def package(self, plan: PackagePlan, *, materialized_root: Path) -> PackageResult:
        if plan.package_kind is not PackageKind.ARCHIVE:
            raise PackagingError("wheel adapter requires the archive package kind")
        if plan.packager_identity != self.packager_identity:
            raise PackagingError("wheel metadata differs from exact packager authority")
        _root, materialized = validate_materialized_package_root(
            plan, materialized_root
        )

        dist_info = f"{self.distribution}-{self.version}.dist-info"
        data_root = f"{self.distribution}-{self.version}.data/data/literate-ai"
        entries: dict[str, tuple[bytes, bool]] = {}
        for relative, source in materialized.items():
            if source.is_file():
                entries[f"{data_root}/{relative}"] = (
                    source.read_bytes(),
                    relative in {item.path for item in plan.entrypoints},
                )
                continue
            for child in sorted(source.rglob("*")):
                if child.is_dir():
                    continue
                entries[
                    f"{data_root}/{relative}/{child.relative_to(source).as_posix()}"
                ] = (child.read_bytes(), bool(child.stat().st_mode & 0o111))
        entries[f"{data_root}/PACKAGE_PLAN.json"] = (
            json.dumps(plan.to_dict(), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            False,
        )
        entries[f"{dist_info}/METADATA"] = (
            (
                "Metadata-Version: 2.4\n"
                f"Name: {self.distribution}\n"
                f"Version: {self.version}\n"
                "Summary: Exact accepted Literate AI Component package\n\n"
            ).encode(),
            False,
        )
        entries[f"{dist_info}/WHEEL"] = (
            (
                "Wheel-Version: 1.0\n"
                "Generator: literate-ai\n"
                "Root-Is-Purelib: false\n"
                f"Tag: {self.tag}\n"
            ).encode(),
            False,
        )
        record_path = f"{dist_info}/RECORD"
        record_stream = io.StringIO(newline="")
        writer = csv.writer(record_stream, lineterminator="\n")
        for path in sorted(entries):
            content, _executable = entries[path]
            writer.writerow((path, _wheel_hash(content), str(len(content))))
        writer.writerow((record_path, "", ""))
        entries[record_path] = (record_stream.getvalue().encode("utf-8"), False)

        stream = io.BytesIO()
        with zipfile.ZipFile(
            stream, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(entries):
                content, executable = entries[path]
                archive.writestr(
                    _wheel_info(path, executable=executable), content, compresslevel=9
                )
        content = stream.getvalue()
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/vnd.pypa.wheel+zip",
        )
        self._created[blob.identity] = content
        artifact = PackagedFile(
            f"{self.distribution}-{self.version}-{self.tag}.whl",
            "python-wheel",
            PackageFileKind.PACKAGE_OUTPUT,
            plan.identity,
            plan.target_identity,
            blob,
            False,
        )
        files = logical_package_files(plan)
        result = package_result_for(plan, files=files, artifacts=(artifact,))

        def read_blob(reference: BlobRef) -> bytes:
            if reference == blob:
                return content
            item = next(
                (candidate for candidate in plan.inputs if candidate.blob == reference),
                None,
            )
            if item is None:
                raise PackagingError("wheel verification requested an unknown blob")
            return materialized_package_input_bytes(materialized[item.path])

        verify_package_result(plan, result, read_blob=read_blob)
        self.verify(result)
        return result

    def verify(self, result: PackageResult) -> None:
        if len(result.artifacts) != 1:
            raise PackagingError("wheel result must contain one native package")
        artifact = result.artifacts[0]
        try:
            content = self._created[artifact.blob.identity]
        except KeyError as exc:
            raise PackagingError("wheel bytes are not in adapter custody") from exc
        self.verify_bytes(result, content)

    def verify_bytes(self, result: PackageResult, content: bytes) -> None:
        """Verify wheel bytes without relying on construction-process custody."""

        if result.packager_identity != self.packager_identity:
            raise PackagingError("wheel result differs from exact packager authority")
        if len(result.artifacts) != 1:
            raise PackagingError("wheel result must contain one native package")
        artifact = result.artifacts[0]
        if artifact.path != f"{self.distribution}-{self.version}-{self.tag}.whl":
            raise PackagingError("wheel filename differs from exact packager authority")
        if (
            not isinstance(content, bytes)
            or len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError("wheel bytes differ from the package result")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                if names != sorted(names) or len(names) != len(set(names)):
                    raise PackagingError("wheel entries are not unique and canonical")
                dist_info = f"{self.distribution}-{self.version}.dist-info"
                record_path = f"{dist_info}/RECORD"
                wheel_path = f"{dist_info}/WHEEL"
                if record_path not in names or wheel_path not in names:
                    raise PackagingError("wheel metadata is incomplete")
                if f"Tag: {self.tag}\n" not in archive.read(wheel_path).decode("utf-8"):
                    raise PackagingError("wheel compatibility tag differs")
                rows = list(
                    csv.reader(io.StringIO(archive.read(record_path).decode("utf-8")))
                )
                if len(rows) != len(names):
                    raise PackagingError("wheel RECORD does not cover every entry")
                by_path = {row[0]: row[1:] for row in rows if len(row) == 3}
                if len(by_path) != len(rows) or set(by_path) != set(names):
                    raise PackagingError("wheel RECORD paths are invalid")
                for name in names:
                    digest, size = by_path[name]
                    if name == record_path:
                        if digest or size:
                            raise PackagingError("wheel RECORD must self-record empty")
                        continue
                    entry = archive.read(name)
                    if digest != _wheel_hash(entry) or size != str(len(entry)):
                        raise PackagingError("wheel RECORD content digest differs")
        except zipfile.BadZipFile as exc:
            raise PackagingError("wheel package is not a valid ZIP archive") from exc

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError("wheel blob was not created by this adapter") from exc


class NativeMetadataArchiveAdapter:
    """Deterministic ZIP plus one provider-native metadata file. No publication."""

    def __init__(self, provider: str, distribution: str, version: str) -> None:
        if provider not in _NATIVE_METADATA_PROVIDERS:
            raise PackagingError(f"native metadata is not implemented for {provider}")
        self.provider = provider
        self.distribution = distribution
        self.version = version
        self._created: dict[str, bytes] = {}
        self._metadata_path, self._metadata_bytes = native_metadata_resource(
            provider, distribution, version
        )

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/native-metadata-archive-packager@1",
                "provider": self.provider,
                "distribution": self.distribution,
                "version": self.version,
            }
        )

    def package(self, plan: PackagePlan, *, materialized_root: Path) -> PackageResult:
        if plan.packager_identity != self.packager_identity:
            raise PackagingError(
                "native metadata differs from exact packager authority"
            )
        _root, materialized = validate_materialized_package_root(
            plan, materialized_root
        )
        adapter = DeterministicZipPackageAdapter()

        def read_blob(reference: BlobRef) -> bytes:
            item = next(
                candidate for candidate in plan.inputs if candidate.blob == reference
            )
            return materialized_package_input_bytes(materialized[item.path])

        result = adapter.package(plan, read_blob=read_blob)
        self._created = dict(adapter._created)
        return result

    def verify_bytes(self, result: PackageResult, content: bytes) -> None:
        if result.packager_identity != self.packager_identity:
            raise PackagingError("native metadata packager identity drifted")
        if len(result.artifacts) != 1:
            raise PackagingError("native metadata result must contain one package")
        artifact = result.artifacts[0]
        if (
            not isinstance(content, bytes)
            or len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError(
                "native metadata package bytes differ from the exact result"
            )
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                if self._metadata_path not in names:
                    raise PackagingError(
                        f"native metadata package is missing {self._metadata_path}"
                    )
                if archive.read(self._metadata_path) != self._metadata_bytes:
                    raise PackagingError("native metadata control file drifted")
                for required in (
                    "specification/component.md",
                    "sbom/resolved.cdx.json",
                ):
                    if required not in names:
                        raise PackagingError(
                            f"native metadata package is missing {required}"
                        )
        except zipfile.BadZipFile as exc:
            raise PackagingError("native metadata package is not a valid ZIP") from exc

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError(
                "native metadata blob was not created by this adapter"
            ) from exc


__all__ = [
    "DeterministicZipPackageAdapter",
    "DirectoryPackageAdapter",
    "NativeMetadataArchiveAdapter",
    "NativeZipPackageAdapter",
    "NpmPackageAdapter",
    "WheelPackageAdapter",
    "lifecycle_archive_package_plan",
    "logical_package_files",
    "materialized_package_input_bytes",
    "native_archive_package_plan",
    "native_metadata_archive_plan",
    "native_metadata_resource",
    "npm_archive_package_plan",
    "package_result_for",
    "validate_materialized_package_root",
]

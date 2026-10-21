"""Explicit native pkg construction from an exact link-preserving package tree.

This provider does not admit a producer or select a lifecycle/default CLI. Its
caller must retain those owners; verification never installs or runs payloads.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai.adapters import freebsd_packaging as native
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.freebsd_package_elf import inspect_freebsd_package_elf
from literate_ai.adapters.retained_project_inputs import _stream
from literate_ai.adapters.tree_packaging import (
    _BoundedTarInfo,
    _BoundedTarReader,
    materialize_tree_package_plan,
    verify_tree_package_stage,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import (
    BlobRef,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components.packages import RuntimeRequirementKind
from literate_ai.contracts.tree_packages import (
    TreePackageMember,
    TreePackagePlan,
    project_tree_package_members,
)


class _NativeTarReader(_BoundedTarReader):
    def seekable(self):
        return True

    def read(self, size=-1):
        # ELF inspection and native manifests permit bounded 16 MiB records;
        # _BoundedTarInfo separately caps PAX allocation before decoding.
        if not 0 <= size <= native._MAX_METADATA + 1:
            raise PackagingError("Native tree pkg read exceeds allocation bound")
        return self.source.read(size)


def _dependencies(plan, content):
    # Environment/device obligations stay bound by the complete original plan.
    # are not pkg coordinates and cannot be satisfied by inventing package names.
    selected = replace(
        plan.base,
        runtime_requirements=tuple(
            item
            for item in plan.base.runtime_requirements
            if item.kind
            not in {RuntimeRequirementKind.ENVIRONMENT, RuntimeRequirementKind.DEVICE}
        ),
    )
    return native.freebsd_runtime_dependencies(selected, content)


@dataclass(frozen=True, slots=True)
class FreeBsdTreePackageProjection:
    plan: TreePackagePlan
    package: str
    version: str
    origin: str
    abi: str
    architecture: str
    prefix: str
    dependencies: tuple[tuple[str, str, str], ...]
    tool_identity: ContentIdentity
    maximum_archive_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if not isinstance(self.plan, TreePackagePlan) or not isinstance(
            self.tool_identity, ContentIdentity
        ):
            raise TypeError("Native tree pkg requires typed plan and tool identity")
        if (
            not isinstance(self.package, str)
            or native._NAME.fullmatch(self.package) is None
            or not isinstance(self.version, str)
            or native._VERSION.fullmatch(self.version) is None
        ):
            raise PackagingError("Native tree pkg name/version is invalid")
        native._origin(self.origin)
        native._absolute(self.prefix)
        if (
            not isinstance(self.abi, str)
            or native._ABI.fullmatch(self.abi) is None
            or not isinstance(self.architecture, str)
            or re.fullmatch(r"[A-Za-z0-9:._-]{1,96}", self.architecture) is None
        ):
            raise PackagingError("Native tree pkg ABI is invalid")
        if (
            type(self.maximum_archive_bytes) is not int
            or not 1 <= self.maximum_archive_bytes <= 16 * 1024**3
        ):
            raise PackagingError("Native tree pkg archive bound is invalid")
        if (
            type(self.dependencies) is not tuple
            or len(self.dependencies) > 1024
            or self.dependencies != tuple(sorted(set(self.dependencies)))
        ):
            raise PackagingError("Native tree pkg dependencies are not canonical")
        names = set()
        for name, origin, version in self.dependencies:
            if (
                native._NAME.fullmatch(name) is None
                or native._VERSION.fullmatch(version) is None
                or name.casefold() in names
            ):
                raise PackagingError("Native tree pkg dependency is invalid")
            native._origin(origin)
            names.add(name.casefold())
        roles = {item.role for item in self.plan.base.inputs}
        if (
            not {
                "component-specification",
                "cyclonedx-source-sbom",
                "cyclonedx-resolved-sbom",
            }
            <= roles
        ):
            raise PackagingError("Native tree pkg requires specification and both BOMs")
        if len(self.members) > native._MAX_MEMBERS:
            raise PackagingError("Native tree pkg inventory exceeds provider bound")

    @property
    def members(self):
        entries = (
            TreePackageMember("", "directory", 0o755),
            *project_tree_package_members(self.plan.base, self.plan.trees),
        )
        return {
            self.prefix + ("/" + item.path if item.path else ""): item
            for item in entries
        }

    def to_dict(self):
        return {
            "schema": "literate-ai/freebsd-tree-pkg-projection@1",
            "tree_package_plan": self.plan.to_dict(),
            "package": self.package,
            "version": self.version,
            "origin": self.origin,
            "abi": self.abi,
            "architecture": self.architecture,
            "prefix": self.prefix,
            "dependencies": [list(item) for item in self.dependencies],
            "tool_identity": self.tool_identity.uri,
            "maximum_archive_bytes": self.maximum_archive_bytes,
            "source_date_epoch": native._EPOCH,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def manifest(self):
        return {
            "name": self.package,
            "version": self.version,
            "origin": self.origin,
            "abi": self.abi,
            "arch": self.architecture,
            "prefix": self.prefix,
            "comment": "Exact accepted Literate AI package tree",
            "desc": "Linked payload with specification and source/resolved provenance.",
            "maintainer": "literate-ai-maintainers@users.noreply.github.com",
            "www": "https://github.com/jordanhubbard/literate-ai",
            "deps": {
                name: {"origin": origin, "version": version}
                for name, origin, version in self.dependencies
            },
            "annotations": {
                "literate-ai:tree-package-plan": self.plan.identity.uri,
                "literate-ai:projection": self.identity.uri,
            },
        }

    @property
    def plist(self):
        lines = ["@owner root", "@group wheel"]
        for path, item in self.members.items():
            lines.extend(
                (
                    f"@mode {item.mode:04o}",
                    ("@dir " if item.kind == "directory" else "") + path,
                )
            )
        return ("\n".join(lines) + "\n").encode()


def project_freebsd_tree_package(
    plan, *, package, version, origin, tool, target_abi, resolved_sbom, prefix
):
    if not isinstance(plan, TreePackagePlan) or not isinstance(
        tool, native.FreeBsdPkgToolBinding
    ):
        raise TypeError(
            "Native tree pkg requires explicit typed tree plan and native tool"
        )
    if target_abi != tool.abi:
        raise PackagingError("Native tree pkg target and worker ABIs contradict")
    return FreeBsdTreePackageProjection(
        plan,
        package,
        version,
        origin,
        target_abi,
        tool.architecture,
        prefix,
        _dependencies(plan, resolved_sbom),
        tool.identity,
    )


@dataclass(frozen=True, slots=True)
class FreeBsdTreePackageResult:
    package_plan_identity: ContentIdentity
    packager_identity: ContentIdentity
    archive: BlobRef

    def __post_init__(self):
        if (
            not isinstance(self.package_plan_identity, ContentIdentity)
            or not isinstance(self.packager_identity, ContentIdentity)
            or not isinstance(self.archive, BlobRef)
            or self.archive.media_type != "application/vnd.freebsd.pkg"
        ):
            raise TypeError(
                "Native tree pkg result requires exact native archive authority"
            )

    def to_dict(self):
        return {
            "schema": "literate-ai/freebsd-tree-pkg-result@1",
            "package_plan_identity": self.package_plan_identity.to_dict(),
            "packager_identity": self.packager_identity.to_dict(),
            "archive": self.archive.to_dict(),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


class FreeBsdTreePackageAdapter:
    def __init__(self, projection, tool):
        if not isinstance(projection, FreeBsdTreePackageProjection) or not isinstance(
            tool, native.FreeBsdPkgToolBinding
        ):
            raise TypeError("Native tree pkg requires typed projection and tool")
        if (projection.tool_identity, projection.abi, projection.architecture) != (
            tool.identity,
            tool.abi,
            tool.architecture,
        ):
            raise PackagingError("Native tree pkg projection binds another tool/ABI")
        self.projection, self.tool = projection, tool

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/freebsd-tree-pkg-packager@1",
                "projection_identity": self.projection.identity.uri,
                "compression": "txz",
                "compression_threads": 1,
                "compression_level": 9,
            }
        )

    def _require_plan(self, plan, store):
        if not isinstance(plan, TreePackagePlan) or plan != self.projection.plan:
            raise PackagingError("Native tree pkg differs from exact selected plan")
        bom = [
            item for item in plan.base.inputs if item.role == "cyclonedx-resolved-sbom"
        ]
        if (
            len(bom) != 1
            or _dependencies(plan, store.get_bytes(bom[0].blob))
            != self.projection.dependencies
        ):
            raise PackagingError("Native tree pkg dependency projection differs")

    def package(self, plan, *, store, object_root):
        if platform.system().casefold() != "freebsd":
            raise PackagingError("Native tree pkg construction requires FreeBSD")
        self._require_plan(plan, store)
        parent = Path(object_root).absolute()
        directory_node(parent)
        with tempfile.TemporaryDirectory(prefix="freebsd-tree-pkg-", dir=parent) as raw:
            work = Path(raw)
            root = materialize_tree_package_plan(
                plan, expected_identity=plan.identity, store=store, parent=work
            )
            owned = directory_node(root)
            payload, output, repos, metadata = (
                work / name for name in ("root", "output", "repos", "metadata")
            )
            for directory in (payload, output, repos, metadata):
                directory.mkdir()
            installed = payload / self.projection.prefix[1:]
            installed.parent.mkdir(parents=True, exist_ok=True)
            root.rename(installed)  # Preserve hard-link groups; never recopy aliases.
            if directory_node(installed) != owned:
                raise PackagingError("Native tree pkg staging ownership changed")
            installed.chmod(0o755)
            owned = directory_node(installed)
            for path in self.projection.members:
                os.utime(
                    payload / path[1:],
                    (native._EPOCH, native._EPOCH),
                    follow_symlinks=False,
                )
            verify_tree_package_stage(plan, installed)
            (metadata / "+MANIFEST").write_bytes(
                canonical_json_bytes(self.projection.manifest)
            )
            plist = work / "plist"
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
                    str(native._EPOCH),
                ),
                cwd=work,
                environment=self.tool.environment,
                timeout_seconds=300,
                stdout_limit_bytes=1024**2,
                stderr_limit_bytes=1024**2,
                error_prefix="freebsd.tree-pkg.create",
                terminate_descendants=True,
            )
            self.tool.require_unchanged()
            if completed.returncode:
                raise PackagingError(
                    "Native tree pkg construction failed: "
                    + completed.stderr[-4000:].decode(errors="replace")
                )
            verify_tree_package_stage(plan, installed)
            if directory_node(installed) != owned:
                raise PackagingError("Native tree pkg staging ownership changed")
            filename = f"{self.projection.package}-{self.projection.version}.pkg"
            if tuple(item.name for item in output.iterdir()) != (filename,):
                raise PackagingError("Native tree pkg output inventory differs")
            buffer = io.BytesIO()
            _stream(
                output, filename, self.projection.maximum_archive_bytes, output=buffer
            )
            content = buffer.getvalue()
            blob = store.put_bytes(content, media_type="application/vnd.freebsd.pkg")
            result = FreeBsdTreePackageResult(plan.identity, self.identity, blob)
            self.verify(plan, result, store=store, object_root=work)
            return result

    def verify(self, plan, result, *, store, object_root):
        self._require_plan(plan, store)
        if not isinstance(result, FreeBsdTreePackageResult) or (
            result.package_plan_identity,
            result.packager_identity,
        ) != (plan.identity, self.identity):
            raise PackagingError("Native tree pkg result binds another plan/provider")
        content = store.get_bytes(result.archive)
        if len(content) > self.projection.maximum_archive_bytes or (
            len(content),
            hashlib.sha256(content).hexdigest(),
        ) != (result.archive.size, result.archive.digest):
            raise PackagingError("Native tree pkg archive differs from result")
        expected = self.projection.members
        expanded_limit = (
            plan.limits.max_total_bytes
            + 2 * native._MAX_METADATA
            + len(expected) * 16384
            + 20480
        )
        native._require_single_xz_frame(content, expanded_limit)
        directory_node(Path(object_root).absolute())
        with tempfile.TemporaryFile(dir=object_root) as expanded:
            with native._xz_codec().LZMAFile(io.BytesIO(content)) as source:
                while block := source.read(1024**2):
                    if expanded.tell() + len(block) > expanded_limit:
                        raise PackagingError("Native tree pkg expansion exceeds bound")
                    expanded.write(block)
            expanded.seek(0)
            self._verify_tar(plan, expected, expanded)

    def _verify_tar(self, plan, expected, expanded):
        manifests, seen, records = {}, set(), {}
        required, provided = set(), set()
        try:
            with tarfile.open(
                fileobj=_NativeTarReader(expanded), mode="r:", tarinfo=_BoundedTarInfo
            ) as archive:
                for index, member in enumerate(archive):
                    if index >= len(expected) + 2:
                        raise PackagingError("Native tree pkg exceeds member bound")
                    path = native._archive_path(member.name)
                    metadata = path in {"/+MANIFEST", "/+COMPACT_MANIFEST"}
                    owner, epoch = (0, 0) if metadata else (65534, native._EPOCH)
                    # Native libarchive marks long UTF-8 names as BINARY under
                    # the C locale. Decoded paths/targets still must match the
                    # complete typed tree exactly; other attributes refuse.
                    standard = {
                        "path": member.name + ("/" if member.isdir() else ""),
                        "linkpath": member.linkname,
                        "size": str(member.size),
                        "mtime": str(epoch),
                        "uid": str(owner),
                        "gid": str(owner),
                        "uname": "root",
                        "gname": "wheel",
                        "hdrcharset": "BINARY",
                    }
                    if (
                        path in seen
                        or member.sparse is not None
                        or (
                            member.uid,
                            member.gid,
                            member.uname,
                            member.gname,
                            member.mtime,
                        )
                        != (owner, owner, "root", "wheel", epoch)
                        or any(
                            key not in standard or value != standard[key]
                            for key, value in member.pax_headers.items()
                        )
                    ):
                        raise PackagingError("Native tree pkg metadata/type differs")
                    seen.add(path)
                    if metadata:
                        if (
                            member.type != tarfile.REGTYPE
                            or member.mode != 0o644
                            or not 0 <= member.size <= native._MAX_METADATA
                        ):
                            raise PackagingError(
                                "Native tree pkg manifest exceeds bounds/type"
                            )
                        manifests[path] = native._document(
                            archive.extractfile(member).read(native._MAX_METADATA + 1)
                        )
                        if path == "/+MANIFEST":
                            records = native._manifest_files(manifests[path])
                        continue
                    if path not in expected or set(manifests) != {
                        "/+MANIFEST",
                        "/+COMPACT_MANIFEST",
                    }:
                        raise PackagingError(
                            "Native tree pkg has undeclared/unbound payload"
                        )
                    item = expected[path]
                    kind = {
                        "file": tarfile.REGTYPE,
                        "directory": tarfile.DIRTYPE,
                        "symlink": tarfile.SYMTYPE,
                        "hardlink": tarfile.LNKTYPE,
                    }[item.kind]
                    target = (
                        self.projection.prefix + "/" + item.target
                        if item.kind == "hardlink"
                        else item.target or ""
                    )
                    size = item.blob.size if item.kind == "file" else 0
                    if (member.type, member.mode, member.linkname, member.size) != (
                        kind,
                        item.mode,
                        target,
                        size,
                    ):
                        raise PackagingError(
                            "Native tree pkg payload topology/mode differs"
                        )
                    if item.kind == "directory":
                        continue
                    record = records.get(path)
                    allowed = {"sum", "uname", "gname", "perm", "mtime"} | (
                        {"symlink_target"} if item.kind == "symlink" else set()
                    )
                    if (
                        not isinstance(record, dict)
                        or set(record) != allowed
                        or (record["uname"], record["gname"], record["mtime"])
                        != ("root", "wheel", native._EPOCH)
                        or record["perm"]
                        not in {f"{item.mode:04o}", f"0{item.mode:04o}"}
                        or (
                            item.kind == "symlink"
                            and record["symlink_target"] != item.target
                        )
                    ):
                        raise PackagingError("Native tree pkg file manifest differs")
                    hasher, encode = native._native_hasher(record["sum"])
                    if item.kind == "symlink":
                        hasher.update(item.target.encode("utf-8"))
                    elif item.kind == "hardlink":
                        physical = expected.get(target)
                        if physical is None or physical.kind != "file":
                            raise PackagingError(
                                "Native tree pkg hardlink has no physical owner"
                            )
                        self._require_hardlink_checksum(
                            records[path], records.get(target)
                        )
                    else:
                        stream = archive.extractfile(member)
                        accepted = hashlib.sha256()
                        count = 0
                        while block := stream.read(1024**2):
                            count += len(block)
                            if count > item.blob.size:
                                raise PackagingError(
                                    "Native tree pkg file exceeds accepted size"
                                )
                            accepted.update(block)
                            hasher.update(block)
                        if (
                            count != item.blob.size
                            or accepted.hexdigest() != item.blob.digest
                        ):
                            raise PackagingError(
                                "Native tree pkg physical bytes differ"
                            )
                        needs, supplies = inspect_freebsd_package_elf(
                            stream, count, self.projection.abi
                        )
                        required.update(needs)
                        provided.update(supplies)
                    if (
                        item.kind != "hardlink"
                        and encode(hasher.digest()) != record["sum"]
                    ):
                        raise PackagingError("Native tree pkg native checksum differs")
                archive.fileobj.seek(archive.offset)
                padding = 0
                while block := archive.fileobj.read(1024**2):
                    padding += len(block)
                    if any(block):
                        raise PackagingError("Native tree pkg has trailing TAR data")
                if padding < 1024 or padding % 512:
                    raise PackagingError("Native tree pkg TAR padding is invalid")
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            if isinstance(exc, PackagingError):
                raise
            raise PackagingError("Native tree pkg archive is invalid") from exc
        if seen != set(expected) | {"/+MANIFEST", "/+COMPACT_MANIFEST"}:
            raise PackagingError("Native tree pkg archive inventory differs")
        full, compact = manifests["/+MANIFEST"], manifests["/+COMPACT_MANIFEST"]
        for document in (full, compact):
            for key, value in self.projection.manifest.items():
                if document.get(key, {} if key == "deps" else None) != value:
                    raise PackagingError("Native tree pkg manifest intent differs")
            allowed = set(self.projection.manifest) | {
                "flatsize",
                "licenselogic",
                "shlibs_required",
                "shlibs_provided",
            }
            if document is full:
                allowed |= {"files", "directories"}
            if (
                set(document) - allowed
                or document.get("licenselogic", "single") != "single"
            ):
                raise PackagingError(
                    "Native tree pkg manifest has undeclared fields/hooks"
                )
            flatsize = sum(
                item.blob.size
                if item.kind == "file"
                else len(item.target.encode("utf-8"))
                if item.kind == "symlink"
                else 0
                for item in expected.values()
            )
            if document.get("flatsize") != flatsize:
                raise PackagingError("Native tree pkg physical size differs")
            for key, names in (
                ("shlibs_required", required - provided),
                ("shlibs_provided", provided),
            ):
                if document.get(key, []) != sorted(names):
                    raise PackagingError(
                        "Native tree pkg shared-library inventory differs"
                    )
        if set(native._manifest_files(full)) != {
            path for path, item in expected.items() if item.kind != "directory"
        }:
            raise PackagingError("Native tree pkg file manifest inventory differs")
        directories = native._manifest_files({"files": full.get("directories")})
        if set(directories) != {
            path for path, item in expected.items() if item.kind == "directory"
        }:
            raise PackagingError("Native tree pkg directory manifest inventory differs")
        if any(
            not isinstance(record, dict)
            or set(record) != {"uname", "gname", "perm"}
            or record != {"uname": "root", "gname": "wheel", "perm": "0755"}
            for record in directories.values()
        ):
            raise PackagingError("Native tree pkg directory metadata differs")

    @staticmethod
    def _require_hardlink_checksum(alias_record, physical_record):
        # The regular member is independently hashed against its accepted blob.
        # Native manifests encode hardlinks with that same checksum, not a target.
        if not isinstance(physical_record, dict) or alias_record.get(
            "sum"
        ) != physical_record.get("sum"):
            raise PackagingError("Native tree pkg hardlink checksum differs from owner")

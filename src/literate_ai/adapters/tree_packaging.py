"""Deterministic link-preserving TAR construction and independent byte inspection."""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
import tempfile
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.package_tree_inputs import materialize_package_tree_input
from literate_ai.adapters.retained_project_inputs import (
    _directory_access,
    _hardlink_topology,
    _inventory,
    _link_bytes,
    _remove_tree,
    _require_case_sensitive,
    _stream,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.tree_packages import (
    TreePackagePlan,
    TreePackageResult,
    project_tree_package_members,
)

_EPOCH = 946684800
_CHUNK = 1024 * 1024


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        allowed = {
            tarfile.REGTYPE,
            tarfile.DIRTYPE,
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.XHDTYPE,
        }
        if self.type not in allowed:
            raise PackagingError("Tree TAR header has an unsupported member type")
        if self.type == tarfile.XHDTYPE:
            if self.size > _CHUNK or getattr(archive, "_tree_pax_active", False):
                raise PackagingError("Tree TAR metadata exceeds allocation bound")
            archive._tree_pax_active = True
            try:
                return super()._proc_member(archive)
            finally:
                archive._tree_pax_active = False
        return super()._proc_member(archive)


class _BoundedTarReader:
    """Reject oversized metadata reads before tarfile allocates from header claims."""

    def __init__(self, source):
        self.source = source
        self.size = os.fstat(source.fileno()).st_size

    def read(self, size=-1):
        if not 0 <= size <= _CHUNK:
            raise PackagingError("Tree TAR metadata/read exceeds allocation bound")
        return self.source.read(size)

    def tell(self):
        return self.source.tell()

    def seek(self, offset, whence=0):
        target = (
            offset
            if whence == 0
            else self.tell() + offset
            if whence == 1
            else self.size + offset
        )
        if not 0 <= target <= self.size:
            raise PackagingError("Tree TAR seek exceeds archive bounds")
        return self.source.seek(target)


def _top_names(root, access, maximum):
    descriptor = os.open(
        ".",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=access.directory(())[0],
    )
    try:
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= maximum:
                    raise PackagingError("Tree package exceeds top-level entry bound")
                names.append(entry.name)
        return tuple(sorted(names))
    finally:
        os.close(descriptor)


def verify_tree_package_stage(plan, root):
    """Check exact current inventory, physical bytes and complete inode groups."""
    if not isinstance(plan, TreePackagePlan):
        raise TypeError("Tree package stage requires explicit tree plan")
    if os.name == "nt":
        raise PackagingError("Tree package custody requires a POSIX worker")
    root = Path(root).absolute()
    require_safe_directory(root)
    members = project_tree_package_members(plan.base, plan.trees)
    roots = tuple(sorted({v.path.split("/", 1)[0] for v in members}))
    files = tuple(v.path for v in members if v.kind != "directory")
    directories = tuple(v.path for v in members if v.kind == "directory")
    with _directory_access(root) as access:

        def inventory():
            if _top_names(root, access, plan.limits.max_entries) != roots:
                raise PackagingError("Tree package top-level inventory changed")
            return _inventory(
                root,
                roots,
                plan.limits.max_entries,
                allow_internal_symlinks=True,
                access=access,
            )

        if inventory() != (files, directories):
            raise PackagingError("Tree package inventory changed")
        topology = _hardlink_topology(root, files, access)
        if topology[0] != {v.path: v.target for v in members if v.kind == "hardlink"}:
            raise PackagingError("Tree package hard-link topology changed")
        for value in members:
            node = access.metadata(root / value.path)
            if value.kind == "file":
                if not stat.S_ISREG(node.st_mode) or _stream(
                    root, value.path, value.blob.size, access=access
                ) != (value.blob.size, value.mode, value.blob.digest):
                    raise PackagingError("Tree package file bytes or mode changed")
            elif value.kind == "symlink":
                if not stat.S_ISLNK(node.st_mode) or _link_bytes(
                    root, value.path, 4096, access
                ) != value.target.encode("utf-8"):
                    raise PackagingError("Tree package symbolic target changed")
            elif value.kind == "directory":
                if (
                    not stat.S_ISDIR(node.st_mode)
                    or stat.S_IMODE(node.st_mode) != value.mode
                ):
                    raise PackagingError("Tree package directory mode changed")
            elif not stat.S_ISREG(node.st_mode):
                raise PackagingError("Tree package hard-link type changed")
        if (
            inventory() != (files, directories)
            or _hardlink_topology(root, files, access) != topology
        ):
            raise PackagingError("Tree package changed during verification")


def materialize_tree_package_plan(plan, *, expected_identity, store, parent):
    if not isinstance(plan, TreePackagePlan) or plan.identity != expected_identity:
        raise PackagingError("Tree package plan differs from expected identity")
    if os.name == "nt":
        raise PackagingError("Tree package custody requires a POSIX worker")
    require_safe_directory(parent)
    root = Path(tempfile.mkdtemp(prefix="tree-package-", dir=parent))
    owned = directory_node(root)
    try:
        if plan.case_sensitive:
            _require_case_sensitive(root)
        members = project_tree_package_members(plan.base, plan.trees)
        prefixes = tuple(v.prefix for v in plan.trees)
        for value in members:
            if value.kind == "directory" and not any(
                value.path == p or value.path.startswith(p + "/") for p in prefixes
            ):
                (root / value.path).mkdir(mode=value.mode, parents=True, exist_ok=True)
                (root / value.path).chmod(value.mode)
        executable = {v.path for v in plan.base.entrypoints}
        for value in plan.base.inputs:
            target = root / value.path
            store.copy_to(value.blob, target)
            target.chmod(
                0o755 if value.executable or value.path in executable else 0o644
            )
        for value in plan.trees:
            stage = materialize_package_tree_input(
                value,
                expected_identity=value.identity,
                store=store,
                limits=plan.limits,
                parent=root,
            )
            destination = root / value.prefix
            # Prefix ownership was checked; this complete root is still private.
            if destination.exists() or destination.is_symlink():
                raise PackagingError("Tree package prefix is already occupied")
            stage.rename(destination)
        for value in members:
            if value.kind == "directory":
                (root / value.path).chmod(value.mode)
        if directory_node(root) != owned:
            raise PackagingError("Tree package staging root changed")
        verify_tree_package_stage(plan, root)
        return root
    except BaseException:
        if root.exists() and directory_node(root) == owned:
            _remove_tree(root)
        raise


class TreeTarPackageAdapter:
    """A distinct TAR provider; bytes grant no installation or release access."""

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/tree-tar-packager@1",
                "format": "pax",
                "epoch": _EPOCH,
                "uid": 0,
                "gid": 0,
            }
        )

    def _require_plan(self, plan):
        if (
            not isinstance(plan, TreePackagePlan)
            or plan.base.packager_identity != self.identity
        ):
            raise PackagingError("Tree TAR requires its exact explicit provider plan")

    def package(self, plan, *, store, object_root):
        self._require_plan(plan)
        require_safe_directory(object_root)
        with tempfile.TemporaryDirectory(prefix="tree-tar-", dir=object_root) as temp:
            work = Path(temp)
            root = materialize_tree_package_plan(
                plan, expected_identity=plan.identity, store=store, parent=work
            )
            archive = work / "package.tar"
            with (
                _directory_access(root) as access,
                tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as output,
            ):
                for value in project_tree_package_members(plan.base, plan.trees):
                    info = tarfile.TarInfo(value.path)
                    info.mode, info.mtime = value.mode, _EPOCH
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    if value.kind == "file":
                        info.size = value.blob.size
                        with tempfile.TemporaryFile(dir=work) as stream:
                            if _stream(
                                root, value.path, info.size, stream, access=access
                            ) != (info.size, value.mode, value.blob.digest):
                                raise PackagingError(
                                    "Tree package input changed during TAR construction"
                                )
                            stream.seek(0)
                            output.addfile(info, stream)
                    else:
                        info.type = {
                            "directory": tarfile.DIRTYPE,
                            "symlink": tarfile.SYMTYPE,
                            "hardlink": tarfile.LNKTYPE,
                        }[value.kind]
                        info.linkname = value.target or ""
                        output.addfile(info)
            if archive.stat().st_size > plan.maximum_archive_bytes:
                raise PackagingError("Tree TAR archive exceeds byte bound")
            verify_tree_package_stage(plan, root)
            result = TreePackageResult(
                plan.identity,
                self.identity,
                store.put_file(archive, media_type="application/x-tar"),
            )
            self.verify(plan, result, store=store, object_root=work)
            return result

    def verify(self, plan, result, *, store, object_root):
        self._require_plan(plan)
        if (
            not isinstance(result, TreePackageResult)
            or result.package_plan_identity != plan.identity
            or result.packager_identity != self.identity
        ):
            raise PackagingError("Tree TAR result differs from exact plan/provider")
        if result.archive.size > plan.maximum_archive_bytes:
            raise PackagingError("Tree TAR archive exceeds byte bound")
        require_safe_directory(object_root)
        expected = project_tree_package_members(plan.base, plan.trees)
        with tempfile.TemporaryDirectory(
            prefix="tree-tar-verify-", dir=object_root
        ) as temp:
            path = Path(temp) / "archive.tar"
            store.copy_to(result.archive, path)
            seen = 0
            last_end = 0
            with (
                path.open("rb") as raw,
                tarfile.open(
                    fileobj=_BoundedTarReader(raw), mode="r:", tarinfo=_BoundedTarInfo
                ) as archive,
            ):
                for member in archive:
                    if seen >= len(expected):
                        raise PackagingError("Tree TAR contains unexpected members")
                    value = expected[seen]
                    seen += 1
                    actual_kind = {
                        tarfile.REGTYPE: "file",
                        tarfile.DIRTYPE: "directory",
                        tarfile.SYMTYPE: "symlink",
                        tarfile.LNKTYPE: "hardlink",
                    }.get(member.type, "unsafe")
                    if (
                        member.name,
                        actual_kind,
                        member.mode,
                        member.uid,
                        member.gid,
                        member.mtime,
                        member.uname,
                        member.gname,
                    ) != (value.path, value.kind, value.mode, 0, 0, _EPOCH, "", ""):
                        raise PackagingError(
                            "Tree TAR member metadata or topology differs"
                        )
                    if set(member.pax_headers) - {"path", "linkpath"}:
                        raise PackagingError("Tree TAR contains unbound PAX metadata")
                    if member.linkname != (value.target or ""):
                        raise PackagingError("Tree TAR link target differs")
                    size = value.blob.size if value.blob is not None else 0
                    if member.size != size:
                        raise PackagingError("Tree TAR member size differs")
                    if value.blob is not None:
                        digest = hashlib.sha256()
                        total = 0
                        with archive.extractfile(member) as content:
                            while chunk := content.read(_CHUNK):
                                total += len(chunk)
                                if total > size:
                                    raise PackagingError(
                                        "Tree TAR member exceeds byte bound"
                                    )
                                digest.update(chunk)
                        if total != size or digest.hexdigest() != value.blob.digest:
                            raise PackagingError("Tree TAR member bytes differ")
                    last_end = member.offset_data + ((member.size + 511) // 512) * 512
            if seen != len(expected):
                raise PackagingError("Tree TAR is missing declared members")
            expected_size = (
                (last_end + 1024 + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE
            ) * tarfile.RECORDSIZE
            if path.stat().st_size != expected_size:
                raise PackagingError("Tree TAR has an unbound end trailer")
            with path.open("rb") as stream:
                stream.seek(last_end)
                while chunk := stream.read(_CHUNK):
                    if any(chunk):
                        raise PackagingError("Tree TAR has a nonzero end trailer")
        return result

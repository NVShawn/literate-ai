"""Explicit tree package plans/results, separate from regular-file wire contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields
from .blobs import BlobRef
from .executable_components.packages import PackageKind, PackagePlan
from .identity import ContentIdentity, contract_identity
from .package_tree_inputs import PackageTreeInput, require_package_tree_namespace
from .retained_project import RetainedProjectLimits


@dataclass(frozen=True, slots=True)
class TreePackageMember:
    """Internal projection of exact file type; not transported admission evidence."""

    path: str
    kind: str
    mode: int
    blob: BlobRef | None = None
    target: str | None = None


def project_tree_package_members(base, trees):
    entries = {}
    executable = {value.path for value in base.entrypoints}
    for item in base.inputs:
        entries[item.path] = TreePackageMember(
            item.path,
            "file",
            0o755 if item.executable or item.path in executable else 0o644,
            item.blob,
        )
    for value in trees:
        entries[value.prefix] = TreePackageMember(value.prefix, "directory", 0o755)
        for directory in value.tree.directories:
            path = value.prefix + "/" + directory
            entries[path] = TreePackageMember(path, "directory", 0o755)
        files = {item.path: item for item in value.tree.files}
        for item in value.tree.files:
            path = value.prefix + "/" + item.path
            entries[path] = TreePackageMember(
                path, "file", 0o755 if item.executable else 0o644, item.blob
            )
        for alias in value.tree.aliases:
            path = value.prefix + "/" + alias.path
            if alias.kind == "hardlink":
                item = files[alias.target]
                entries[path] = TreePackageMember(
                    path,
                    "hardlink",
                    0o755 if item.executable else 0o644,
                    target=value.prefix + "/" + alias.target,
                )
            else:
                entries[path] = TreePackageMember(
                    path, "symlink", 0o777, target=alias.target
                )
    for path in tuple(entries):
        parent = path.rpartition("/")[0]
        while parent:
            if parent not in entries:
                entries[parent] = TreePackageMember(parent, "directory", 0o755)
            parent = parent.rpartition("/")[0]
    return tuple(entries[path] for path in sorted(entries))


@dataclass(frozen=True, slots=True)
class TreePackagePlan:
    base: PackagePlan
    trees: tuple[PackageTreeInput, ...]
    limits: RetainedProjectLimits
    case_sensitive: bool = False

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:tree-package-plan@1"

    def __post_init__(self):
        if not isinstance(self.base, PackagePlan) or not isinstance(
            self.limits, RetainedProjectLimits
        ):
            raise TypeError(
                "Tree package requires exact Component plan and typed limits"
            )
        if self.base.package_kind is not PackageKind.ARCHIVE:
            raise ValueError("Tree TAR package requires archive base plan")
        if (
            type(self.trees) is not tuple
            or not 1 <= len(self.trees) <= 1024
            or any(not isinstance(v, PackageTreeInput) for v in self.trees)
        ):
            raise TypeError("Tree package requires 1 to 1024 typed tree inputs")
        if tuple(v.prefix for v in self.trees) != tuple(
            sorted(v.prefix for v in self.trees)
        ):
            raise ValueError("Tree package inputs require canonical prefix order")
        require_package_tree_namespace(
            self.trees,
            tuple(v.path for v in self.base.inputs),
            target_identity=self.base.target_identity,
            case_sensitive=self.case_sensitive,
        )
        if len(self.base.inputs) + sum(
            len(v.tree.files) + len(v.tree.aliases) + len(v.tree.directories) + 1
            for v in self.trees
        ) > min(self.limits.max_entries, 1048576):
            raise ValueError("Tree package exceeds entry bound")
        members = project_tree_package_members(self.base, self.trees)
        sizes = [
            v.blob.size
            if v.blob is not None
            else len(v.target.encode("utf-8"))
            if v.target is not None and v.kind == "symlink"
            else 0
            for v in members
        ]
        if (
            len(members) > min(self.limits.max_entries, 1048576)
            or any(n > self.limits.max_file_bytes for n in sizes)
            or sum(sizes) > self.limits.max_total_bytes
        ):
            raise ValueError("Tree package exceeds combined custody bounds")

    @property
    def identity(self):
        return contract_identity(self)

    @property
    def maximum_archive_bytes(self):
        # Physical data plus bounded PAX/header/name overhead and final blocking.
        return self.limits.max_total_bytes + self.limits.max_entries * 16384 + 20480

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "base": self.base.to_dict(),
            "trees": [v.to_dict() for v in self.trees],
            "limits": self.limits.to_dict(),
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path="TreePackagePlan",
            schema_uri=cls.SCHEMA,
            required=frozenset({"base", "trees", "limits", "case_sensitive"}),
        )
        if not isinstance(data["trees"], list) or not 1 <= len(data["trees"]) <= 1024:
            raise ValueError("Tree package requires bounded tree input array")
        return cls(
            PackagePlan.from_dict(data["base"]),
            tuple(PackageTreeInput.from_dict(v) for v in data["trees"]),
            RetainedProjectLimits.from_dict(data["limits"]),
            data["case_sensitive"],
        )


@dataclass(frozen=True, slots=True)
class TreePackageResult:
    package_plan_identity: ContentIdentity
    packager_identity: ContentIdentity
    archive: BlobRef

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:tree-package-result@1"

    def __post_init__(self):
        if (
            any(
                not isinstance(v, ContentIdentity)
                for v in (self.package_plan_identity, self.packager_identity)
            )
            or not isinstance(self.archive, BlobRef)
            or self.archive.media_type != "application/x-tar"
        ):
            raise TypeError("Tree package result requires exact typed TAR authority")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "package_plan_identity": self.package_plan_identity.to_dict(),
            "packager_identity": self.packager_identity.to_dict(),
            "archive": self.archive.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path="TreePackageResult",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"package_plan_identity", "packager_identity", "archive"}
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["package_plan_identity"]),
            ContentIdentity.from_dict(data["packager_identity"]),
            BlobRef.from_dict(data["archive"]),
        )

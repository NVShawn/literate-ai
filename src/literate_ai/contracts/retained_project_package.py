"""Distinct retained-origin package assertions; decoding never grants admission."""

from dataclasses import dataclass
from typing import ClassVar

from .executable_components.packages import (
    PackagedFile,
    PackageInput,
    PackageKind,
)
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths
from .retained_project_lifecycle import _fields


class _ArchiveShape:
    # Mechanical provider interface only. Runtime dependency evidence stays in
    # the resolved BOM; these records make no standalone runtime-admission claim.
    package_kind = PackageKind.ARCHIVE
    entrypoints = ()
    runtime_requirements = ()
    native_library_root = None
    native_library_layout = None


def _files(values, kind):
    if (
        type(values) is not tuple
        or not 1 <= len(values) <= 100_000
        or any(not isinstance(v, kind) for v in values)
        or tuple(v.path for v in values) != tuple(sorted(v.path for v in values))
    ):
        raise ValueError("retained.package.canonical-files-required")
    canonical_relative_posix_paths((v.path for v in values), label="retained package")


@dataclass(frozen=True, slots=True)
class RetainedProjectPackagePlan(_ArchiveShape):
    retained_plan_identity: ContentIdentity
    receipt_identity: ContentIdentity
    provider_lock_identity: ContentIdentity
    provider_revision: ContentIdentity
    target_identity: ContentIdentity
    packager_identity: ContentIdentity
    inputs: tuple[PackageInput, ...]
    provider: str = "zip"

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-package-plan@1"

    def __post_init__(self):
        if self.provider not in {"zip", "pkg"}:
            raise ValueError("retained.package.unsupported-provider")
        for name in self._identity_fields():
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError("retained.package.typed-identities-required")
        _files(self.inputs, PackageInput)
        if any(v.target_identity != self.target_identity for v in self.inputs):
            raise ValueError("retained.package.input-target-mismatch")

    @staticmethod
    def _identity_fields():
        return (
            "retained_plan_identity",
            "receipt_identity",
            "provider_lock_identity",
            "provider_revision",
            "target_identity",
            "packager_identity",
        )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "provider": self.provider,
            "release_authorized": False,
            **{name: getattr(self, name).uri for name in self._identity_fields()},
            "inputs": [v.to_dict() for v in self.inputs],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            set(cls._identity_fields())
            | {"schema", "authority_mode", "provider", "release_authorized", "inputs"},
        )
        if (
            value["schema"] != cls.SCHEMA
            or value["authority_mode"] != "retained-project"
            or value["provider"] not in {"zip", "pkg"}
            or value["release_authorized"] is not False
            or not isinstance(value["inputs"], list)
            or len(value["inputs"]) > 100_000
        ):
            raise ValueError("retained.package.original-archive-authority-required")
        return cls(
            *(ContentIdentity.parse_uri(value[n]) for n in cls._identity_fields()),
            tuple(PackageInput.from_dict(v) for v in value["inputs"]),
            value["provider"],
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectPackageResult(_ArchiveShape):
    plan: RetainedProjectPackagePlan
    files: tuple[PackagedFile, ...]
    artifacts: tuple[PackagedFile, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-package-result@1"

    def __post_init__(self):
        if not isinstance(self.plan, RetainedProjectPackagePlan):
            raise TypeError("retained.package.typed-plan-required")
        _files(self.files, PackagedFile)
        _files(self.artifacts, PackagedFile)
        if len(self.artifacts) != 1:
            raise ValueError("retained.package.one-archive-required")

    @property
    def package_plan_identity(self):
        return self.plan.identity

    @property
    def target_identity(self):
        return self.plan.target_identity

    @property
    def packager_identity(self):
        return self.plan.packager_identity

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "plan": self.plan.to_dict(),
            "files": [v.to_dict() for v in self.files],
            "artifacts": [v.to_dict() for v in self.artifacts],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "plan", "files", "artifacts"})
        if value["schema"] != cls.SCHEMA or any(
            not isinstance(value[n], list) or len(value[n]) > 100_000
            for n in ("files", "artifacts")
        ):
            raise ValueError("retained.package.invalid-result")
        return cls(
            RetainedProjectPackagePlan.from_dict(value["plan"]),
            tuple(PackagedFile.from_dict(v) for v in value["files"]),
            tuple(PackagedFile.from_dict(v) for v in value["artifacts"]),
        )

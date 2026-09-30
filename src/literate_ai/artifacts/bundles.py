"""CAS-backed package manifests with typed forward-only dependency edges."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from literate_ai.contracts import ComponentRevisionRef, MigrationError
from literate_ai.storage import BlobRef, FileSystemCAS

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPENDENCY_KINDS = {
    "direct",
    "transitive",
    "build",
    "runtime",
    "optional",
    "capability",
    "provider-generated",
}


class BundleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BundleKind(StrEnum):
    SOURCE = "source"
    BUILD = "build"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class BundleDependency:
    component_revision_digest: str
    kind: str
    required: bool
    bundle_manifest: BlobRef | None = None
    component_ref: ComponentRevisionRef | None = None

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.component_revision_digest):
            raise ValueError("dependency Component revision must be an exact digest")
        if (
            self.component_ref is not None
            and self.component_ref.revision_identity.uri
            != self.component_revision_digest
        ):
            raise ValueError("dependency Component ref and revision digest disagree")
        if self.kind not in _DEPENDENCY_KINDS:
            raise ValueError(f"unsupported dependency kind {self.kind!r}")
        if self.kind == "optional" and self.required:
            raise ValueError("optional dependency cannot be required")
        if self.required and self.bundle_manifest is None:
            raise ValueError("required dependency must identify its exact bundle")

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_revision_digest": self.component_revision_digest,
            "kind": self.kind,
            "required": self.required,
            "bundle_manifest": (
                self.bundle_manifest.to_dict() if self.bundle_manifest else None
            ),
            "component_ref": (
                self.component_ref.to_dict() if self.component_ref else None
            ),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_component_ref: ComponentRevisionRef | None = None,
    ) -> BundleDependency:
        bundle = value.get("bundle_manifest")
        raw_component_ref = value.get("component_ref")
        return cls(
            component_revision_digest=str(value["component_revision_digest"]),
            kind=str(value["kind"]),
            required=bool(value["required"]),
            bundle_manifest=BlobRef.from_dict(bundle) if bundle else None,
            component_ref=(
                ComponentRevisionRef.from_dict(raw_component_ref)
                if raw_component_ref is not None
                else legacy_component_ref
            ),
        )


@dataclass(frozen=True, slots=True)
class BundleManifest:
    kind: BundleKind
    component_revision_digest: str
    effective_revision_digest: str
    flavor_set_digest: str | None
    roots: Mapping[str, BlobRef]
    dependencies: tuple[BundleDependency, ...]
    provenance: tuple[BlobRef, ...]
    authorization_digest: str | None = None
    toolchain_digest: str | None = None
    component_ref: ComponentRevisionRef | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("component_revision_digest", self.component_revision_digest),
            ("effective_revision_digest", self.effective_revision_digest),
        ):
            if not _DIGEST.fullmatch(value):
                raise ValueError(f"{name} must be an exact digest")
        if self.schema_version != 2:
            raise ValueError("new bundle manifests must use schema version 2")
        if self.component_ref is None:
            raise ValueError("bundle requires an exact Component revision ref")
        if self.component_ref.revision_identity.uri != self.component_revision_digest:
            raise ValueError("bundle Component ref and revision digest disagree")
        for name, value in (
            ("flavor_set_digest", self.flavor_set_digest),
            ("authorization_digest", self.authorization_digest),
            ("toolchain_digest", self.toolchain_digest),
        ):
            if value is not None and not _DIGEST.fullmatch(value):
                raise ValueError(f"{name} must be an exact digest")
        if not self.roots:
            raise ValueError("bundle requires at least one root artifact")
        dependency_keys = [
            (item.component_revision_digest, item.kind) for item in self.dependencies
        ]
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("bundle contains duplicate typed dependency edges")
        if any(item.component_ref is None for item in self.dependencies):
            raise ValueError("bundle dependencies require exact Component refs")
        if self.kind is BundleKind.BUILD and not self.authorization_digest:
            raise ValueError("build bundle requires its exact authorization")
        if self.kind is BundleKind.BUILD and not self.toolchain_digest:
            raise ValueError("build bundle requires its exact toolchain")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "component_revision_digest": self.component_revision_digest,
            "component_ref": self.component_ref.to_dict(),
            "effective_revision_digest": self.effective_revision_digest,
            "flavor_set_digest": self.flavor_set_digest,
            "roots": {
                key: value.to_dict() for key, value in sorted(self.roots.items())
            },
            "dependencies": [
                item.to_dict()
                for item in sorted(
                    self.dependencies,
                    key=lambda item: (item.kind, item.component_revision_digest),
                )
            ],
            "provenance": [item.to_dict() for item in self.provenance],
            "authorization_digest": self.authorization_digest,
            "toolchain_digest": self.toolchain_digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_component_ref: ComponentRevisionRef | None = None,
        legacy_dependency_refs: Mapping[str, ComponentRevisionRef] | None = None,
    ) -> BundleManifest:
        try:
            schema_version = int(value["schema_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("bundle schema version is invalid") from exc
        if schema_version > 2:
            raise MigrationError(
                "contracts.migration_future_schema",
                f"cannot read future bundle schema {schema_version}",
            )
        if schema_version < 1:
            raise MigrationError(
                "contracts.migration_schema_unsupported",
                f"unsupported bundle schema {schema_version}",
            )
        raw_component_ref = value.get("component_ref")
        if schema_version == 1 and legacy_component_ref is None:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "bundle v1 lacks Component coordinate and semantic version",
            )
        if schema_version == 1:
            missing_dependency_refs = [
                str(item.get("component_revision_digest"))
                for item in value.get("dependencies", ())
                if str(item.get("component_revision_digest"))
                not in (legacy_dependency_refs or {})
            ]
            if missing_dependency_refs:
                raise MigrationError(
                    "contracts.migration_legacy_ambiguous",
                    "bundle v1 dependencies lack exact Component refs: "
                    + ", ".join(sorted(missing_dependency_refs)),
                )
        component_ref = (
            ComponentRevisionRef.from_dict(raw_component_ref)
            if raw_component_ref is not None
            else legacy_component_ref
            if schema_version == 1
            else None
        )
        assert component_ref is not None
        roots = value.get("roots")
        dependencies = value.get("dependencies")
        provenance = value.get("provenance")
        if not isinstance(roots, Mapping):
            raise ValueError("bundle roots must be an object")
        if not isinstance(dependencies, list) or not isinstance(provenance, list):
            raise ValueError("bundle dependencies and provenance must be arrays")
        return cls(
            schema_version=2,
            kind=BundleKind(str(value["kind"])),
            component_revision_digest=str(value["component_revision_digest"]),
            effective_revision_digest=str(value["effective_revision_digest"]),
            flavor_set_digest=(
                str(value["flavor_set_digest"])
                if value.get("flavor_set_digest") is not None
                else None
            ),
            roots={str(key): BlobRef.from_dict(item) for key, item in roots.items()},
            dependencies=tuple(
                BundleDependency.from_dict(
                    item,
                    legacy_component_ref=(legacy_dependency_refs or {}).get(
                        str(item.get("component_revision_digest"))
                    ),
                )
                for item in dependencies
            ),
            provenance=tuple(BlobRef.from_dict(item) for item in provenance),
            authorization_digest=(
                str(value["authorization_digest"])
                if value.get("authorization_digest") is not None
                else None
            ),
            toolchain_digest=(
                str(value["toolchain_digest"])
                if value.get("toolchain_digest") is not None
                else None
            ),
            component_ref=component_ref,
        )


class BundleStore:
    def __init__(self, cas: FileSystemCAS) -> None:
        self.cas = cas

    def put(self, manifest: BundleManifest) -> BlobRef:
        for reference in manifest.roots.values():
            self.cas.verify(reference)
        for reference in manifest.provenance:
            self.cas.verify(reference)
        for dependency in manifest.dependencies:
            if dependency.bundle_manifest:
                self.cas.verify(dependency.bundle_manifest)
            elif dependency.required:
                raise BundleError(
                    "bundle.required_dependency_missing",
                    "Required bundle is absent for "
                    f"{dependency.component_revision_digest}",
                )
        return self.cas.put_manifest(
            manifest.to_dict(),
            media_type="application/vnd.literate-ai.bundle+json",
        )

    def get(self, reference: BlobRef) -> BundleManifest:
        manifest = BundleManifest.from_dict(self.cas.get_manifest(reference))
        self.verify(manifest)
        return manifest

    def verify(self, manifest: BundleManifest) -> None:
        for reference in (*manifest.roots.values(), *manifest.provenance):
            self.cas.verify(reference)
        for dependency in manifest.dependencies:
            if dependency.required and dependency.bundle_manifest is None:
                raise BundleError(
                    "bundle.required_dependency_missing",
                    "Required bundle is absent for "
                    f"{dependency.component_revision_digest}",
                )
            if dependency.bundle_manifest:
                self.cas.verify(dependency.bundle_manifest)


class ArtifactResolver:
    """Resolve a complete acyclic required bundle closure."""

    def __init__(self, bundle_store: BundleStore) -> None:
        self.bundle_store = bundle_store

    def closure(
        self, root: BlobRef, *, include_optional: bool = False
    ) -> tuple[BlobRef, ...]:
        ordered: list[BlobRef] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(reference: BlobRef) -> None:
            if reference.identity in visited:
                return
            if reference.identity in visiting:
                raise BundleError("bundle.dependency_cycle", reference.identity)
            visiting.add(reference.identity)
            manifest = self.bundle_store.get(reference)
            for dependency in manifest.dependencies:
                if not dependency.required and not include_optional:
                    continue
                if dependency.bundle_manifest is None:
                    if dependency.required:
                        raise BundleError(
                            "bundle.required_dependency_missing",
                            dependency.component_revision_digest,
                        )
                    continue
                visit(dependency.bundle_manifest)
            visiting.remove(reference.identity)
            visited.add(reference.identity)
            ordered.append(reference)

        visit(root)
        return tuple(ordered)

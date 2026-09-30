"""Reviewed native workspace inputs; a plan is neither execution nor admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, list_value, string_value
from .blobs import BlobRef
from .cargo_workspace import CargoWorkspaceExpectation
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .repositories import RepositoryBuildCommand, RepositoryBuildEnvironment


@dataclass(frozen=True, slots=True)
class CargoManifestChange:
    """Workspace-relative manifest/lock bytes before and after integration.

    None denotes absence. Unchanged references are intentional read preconditions.
    Retiring source and proving its ownership belong to the boundary-transfer plan.
    """

    path: str
    before: BlobRef | None
    after: BlobRef | None

    def __post_init__(self) -> None:
        relative = canonical_relative_posix_path(self.path, label="Cargo manifest")
        if len(self.path) > 128 or relative.name not in {"Cargo.toml", "Cargo.lock"}:
            fail(
                "CargoManifestChange.path",
                "requires a short Cargo manifest or lock path",
            )
        if self.before is None and self.after is None:
            fail("CargoManifestChange", "requires an existing or prospective file")
        for value in (self.before, self.after):
            if value is not None and (
                not isinstance(value, BlobRef) or value.size <= 0
            ):
                fail("CargoManifestChange", "requires nonempty exact blob references")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Any) -> CargoManifestChange:
        data = fields(
            value,
            path="CargoManifestChange",
            required=frozenset({"path", "before", "after"}),
        )
        return cls(
            data["path"],
            *(
                BlobRef.from_dict(data[key]) if data[key] is not None else None
                for key in ("before", "after")
            ),
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoWorkspacePlan:
    """Pinned native graph, manifest delta, invocation and existing gate commands.

    Admission must compare these commands with current retained gate authority,
    reopen the referenced bytes, measure the selected tools and verify actual Cargo
    metadata. These values alone cannot prove full coverage or successful execution.
    """

    workspace_root: str
    graph: CargoWorkspaceExpectation
    manifests: tuple[CargoManifestChange, ...]
    cargo_identity: ContentIdentity
    rustc_identity: ContentIdentity
    target: str
    features: tuple[str, ...]
    all_features: bool
    no_default_features: bool
    gates: tuple[RepositoryBuildCommand, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-workspace-plan@1"
    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "workspace_root",
            "graph",
            "manifests",
            "cargo_identity",
            "rustc_identity",
            "target",
            "features",
            "all_features",
            "no_default_features",
            "gates",
        }
    )

    def __post_init__(self) -> None:
        if self.workspace_root != ".":
            canonical_relative_posix_path(
                self.workspace_root, label="Cargo workspace root"
            )
        string_value(
            self.workspace_root,
            "RetainedCargoWorkspacePlan.workspace_root",
            max_length=128,
        )
        if not isinstance(self.graph, CargoWorkspaceExpectation):
            fail("RetainedCargoWorkspacePlan.graph", "requires a typed Cargo graph")
        if (
            not isinstance(self.manifests, tuple)
            or not 1 <= len(self.manifests) <= 4096
            or any(not isinstance(item, CargoManifestChange) for item in self.manifests)
        ):
            fail(
                "RetainedCargoWorkspacePlan.manifests",
                "requires bounded typed manifest changes",
            )
        paths = tuple(item.path for item in self.manifests)
        canonical_relative_posix_paths(paths, label="Cargo manifests")
        if paths != tuple(sorted(paths)):
            fail("RetainedCargoWorkspacePlan.manifests", "requires path order")
        present = {item.path for item in self.manifests if item.after is not None}
        expected = {"Cargo.toml", "Cargo.lock"} | {
            "Cargo.toml" if package.root == "." else package.root + "/Cargo.toml"
            for package in self.graph.packages
        }
        if not expected <= present:
            fail(
                "RetainedCargoWorkspacePlan.manifests",
                "missing prospective workspace, package manifest or lock",
            )
        # Validate package directories by their manifest paths, allowing a root
        # package and nested packages while refusing cross-platform aliases.
        canonical_relative_posix_paths(
            sorted(expected), label="Cargo package manifests"
        )
        for package in self.graph.packages:
            for item in package.targets:
                canonical_relative_posix_path(item.source, label="Cargo target source")
        output = canonical_relative_posix_path(
            self.graph.output_directory, label="Cargo output"
        )
        input_paths = (
            *paths,
            *(t.source for p in self.graph.packages for t in p.targets),
        )
        for path in input_paths:
            if path.casefold() == str(output).casefold() or path.casefold().startswith(
                str(output).casefold() + "/"
            ):
                fail(
                    "RetainedCargoWorkspacePlan.graph",
                    "Cargo output overlaps workspace inputs",
                )
        for key in ("cargo_identity", "rustc_identity"):
            if not isinstance(getattr(self, key), ContentIdentity):
                fail("RetainedCargoWorkspacePlan." + key, "requires a tool identity")
        string_value(self.target, "RetainedCargoWorkspacePlan.target", max_length=128)
        # A named triple cannot smuggle another Cargo option or an ambient JSON
        # target path. Custom target files require their own future byte binding.
        if not self.target[0].isalnum() or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in self.target
        ):
            fail("RetainedCargoWorkspacePlan.target", "requires a named target triple")
        if (
            not isinstance(self.features, tuple)
            or len(self.features) > 4096
            or any(
                not isinstance(f, str)
                or not f
                or len(f) > 256
                or any(c.isspace() or c in ",\x00" for c in f)
                for f in self.features
            )
            or self.features != tuple(sorted(set(self.features)))
        ):
            fail(
                "RetainedCargoWorkspacePlan.features",
                "requires sorted unique feature names",
            )
        if (
            type(self.all_features) is not bool
            or type(self.no_default_features) is not bool
        ):
            fail(
                "RetainedCargoWorkspacePlan.features",
                "requires explicit feature switches",
            )
        if (
            not isinstance(self.gates, tuple)
            or not 1 <= len(self.gates) <= 128
            or any(not isinstance(g, RepositoryBuildCommand) for g in self.gates)
            or len({g.step_id for g in self.gates}) != len(self.gates)
        ):
            fail(
                "RetainedCargoWorkspacePlan.gates",
                "requires distinct reviewed gate commands",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def metadata_command(self, *, offline: bool) -> RepositoryBuildCommand:
        if type(offline) is not bool:
            fail("RetainedCargoWorkspacePlan.offline", "requires a boolean")
        argv = [
            "cargo",
            "metadata",
            "--locked",
            "--format-version=1",
            "--filter-platform",
            self.target,
        ]
        if offline:
            argv.append("--offline")
        if self.features:
            argv.append("--features=" + ",".join(self.features))
        if self.all_features:
            argv.append("--all-features")
        if self.no_default_features:
            argv.append("--no-default-features")
        return RepositoryBuildCommand(
            "retained-cargo-metadata",
            tuple(argv),
            self.workspace_root,
            (
                RepositoryBuildEnvironment(
                    "CARGO_TARGET_DIR", self.graph.output_directory
                ),
            ),
            network=not offline,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "workspace_root": self.workspace_root,
            "graph": self.graph.to_dict(),
            "manifests": [m.to_dict() for m in self.manifests],
            "cargo_identity": self.cargo_identity.to_dict(),
            "rustc_identity": self.rustc_identity.to_dict(),
            "target": self.target,
            "features": list(self.features),
            "all_features": self.all_features,
            "no_default_features": self.no_default_features,
            "gates": [g.to_dict() for g in self.gates],
        }

    @classmethod
    def from_dict(cls, value: Any) -> RetainedCargoWorkspacePlan:
        data = contract_fields(
            value,
            path="RetainedCargoWorkspacePlan",
            schema_uri=cls.SCHEMA,
            required=cls._FIELDS,
        )
        arrays = {}
        for key, maximum in (("manifests", 4096), ("features", 4096), ("gates", 128)):
            items = list_value(data[key], "RetainedCargoWorkspacePlan." + key)
            if len(items) > maximum:
                fail("RetainedCargoWorkspacePlan." + key, "array limit exceeded")
            arrays[key] = items
        return cls(
            data["workspace_root"],
            CargoWorkspaceExpectation.from_dict(data["graph"]),
            tuple(CargoManifestChange.from_dict(m) for m in arrays["manifests"]),
            ContentIdentity.from_dict(data["cargo_identity"]),
            ContentIdentity.from_dict(data["rustc_identity"]),
            data["target"],
            tuple(arrays["features"]),
            data["all_features"],
            data["no_default_features"],
            tuple(RepositoryBuildCommand.from_dict(g) for g in arrays["gates"]),
        )

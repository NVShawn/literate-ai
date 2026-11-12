"""Versioned Cargo workspace intent, not scratch ownership or admission proof.

The receiving v2 binding must independently prove retained wrapper ownership,
ignored/empty or exactly owned destinations, native exports, manifest/lock bytes,
complete patch resolution and current input custody before materialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, list_value, string_value
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .retained_cargo import RetainedCargoWorkspacePlan
from .retained_cargo_custody import RetainedCargoCustodyPolicy
from .retained_cargo_patches import CargoWorkspaceExpectationV2
from .retained_cargo_policy import RetainedLibraryGatePolicyV2


@dataclass(frozen=True, slots=True)
class RetainedCargoScratchRoot:
    path: str
    kind: str

    def __post_init__(self) -> None:
        string_value(self.path, self.__class__.__name__ + ".path", max_length=128)
        root = canonical_relative_posix_path(self.path, label="Cargo scratch root")
        if root.parts[0].casefold() in {".git", ".literate", ".codegraph"}:
            fail(self.__class__.__name__, "scratch cannot own repository metadata")
        if not isinstance(self.kind, str) or self.kind not in {
            "member-packages",
            "cargo-output",
            "build-output",
        }:
            fail(self.__class__.__name__ + ".kind", "requires a reviewed scratch role")

    def to_dict(self):
        return {"path": self.path, "kind": self.kind}

    @classmethod
    def from_dict(cls, value: Any):
        data = fields(value, path=cls.__name__, required=frozenset({"path", "kind"}))
        return cls(data["path"], data["kind"])


@dataclass(frozen=True, slots=True)
class RetainedCargoWorkspacePlanV2:
    common: RetainedCargoWorkspacePlan
    graph: CargoWorkspaceExpectationV2
    scratch_roots: tuple[RetainedCargoScratchRoot, ...]
    input_custody: RetainedCargoCustodyPolicy

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-workspace-plan@2"
    COMMON_FIELDS: ClassVar[frozenset[str]] = RetainedCargoWorkspacePlan._FIELDS
    MAXIMUM_SCRATCH_ROOTS: ClassVar[int] = 64

    def __post_init__(self) -> None:
        if type(self.common) is not RetainedCargoWorkspacePlan:
            fail(
                self.__class__.__name__, "requires exact common workspace declarations"
            )
        if (
            not isinstance(self.graph, CargoWorkspaceExpectationV2)
            or self.graph.common != self.common.graph
        ):
            fail(self.__class__.__name__, "workspace graph declarations differ")
        if not isinstance(self.input_custody, RetainedCargoCustodyPolicy):
            fail(self.__class__.__name__, "requires explicit v2 input custody")
        if (
            not isinstance(self.scratch_roots, tuple)
            or len(self.scratch_roots) > self.MAXIMUM_SCRATCH_ROOTS
            or any(
                not isinstance(x, RetainedCargoScratchRoot) for x in self.scratch_roots
            )
        ):
            fail(self.__class__.__name__, "requires bounded typed scratch declarations")
        paths = tuple(x.path for x in self.scratch_roots)
        if paths != tuple(sorted(paths)):
            fail(self.__class__.__name__, "requires scratch roots in path order")
        canonical_relative_posix_paths(paths, label="disjoint Cargo scratch roots")
        output = PurePosixPath(self.common.graph.output_directory)
        for root in self.scratch_roots:
            coordinate = PurePosixPath(root.path)
            if root.kind == "cargo-output":
                if coordinate != output:
                    fail(
                        self.__class__.__name__,
                        "scratch output differs from the Cargo graph",
                    )
            else:
                canonical_relative_posix_paths(
                    (root.path, self.common.graph.output_directory),
                    label="disjoint portable Cargo scratch/output roots",
                )
            if root.kind == "build-output":
                # A retained gate may create secondary artifacts beneath an
                # existing package parent. It may never hide a reviewed manifest
                # or target source. Filesystem absence, ignore status and live
                # exclusive ownership remain the scratch receiver's job.
                protected = {
                    *(item.path for item in self.common.manifests),
                    *(
                        target.source
                        for package in self.common.graph.packages
                        for target in package.targets
                    ),
                }
                for source in sorted(protected):
                    canonical_relative_posix_paths(
                        (root.path, source),
                        label="disjoint build output and reviewed Cargo inputs",
                    )
            if root.kind == "member-packages" and not any(
                package.root in self.common.graph.members
                and (
                    PurePosixPath(package.root) == coordinate
                    or coordinate in PurePosixPath(package.root).parents
                )
                for package in self.common.graph.packages
            ):
                fail(
                    self.__class__.__name__,
                    "member scratch has no reviewed workspace member",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def require_gate_policy(self, policy: RetainedLibraryGatePolicyV2) -> None:
        if not isinstance(policy, RetainedLibraryGatePolicyV2):
            fail(self.__class__.__name__, "requires a version-matched v2 gate policy")
        if (
            policy.input_custody.identity != self.input_custody.identity
            or policy.commands != self.common.gates
        ):
            fail(self.__class__.__name__, "workspace and gate declarations differ")
        if not {self.common.cargo_identity, self.common.rustc_identity} <= set(
            policy.toolchains
        ):
            fail(
                self.__class__.__name__,
                "gate omits reviewed Cargo or Rust tool identities",
            )

    def metadata_command(self, *, offline: bool):
        return self.common.metadata_command(offline=offline)

    def to_dict(self):
        result = self.common.to_dict()
        result.update(
            schema=self.SCHEMA,
            graph=self.graph.to_dict(),
            scratch_roots=[x.to_dict() for x in self.scratch_roots],
            input_custody=self.input_custody.to_dict(),
        )
        return result

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=cls.COMMON_FIELDS | {"scratch_roots", "input_custody"},
        )
        roots = list_value(data["scratch_roots"], cls.__name__ + ".scratch_roots")
        if len(roots) > cls.MAXIMUM_SCRATCH_ROOTS:
            fail(cls.__name__, "scratch root count exceeds allowance")
        graph = CargoWorkspaceExpectationV2.from_dict(data["graph"])
        common = {key: data[key] for key in cls.COMMON_FIELDS}
        common.update(
            schema=RetainedCargoWorkspacePlan.SCHEMA, graph=graph.common.to_dict()
        )
        return cls(
            RetainedCargoWorkspacePlan.from_dict(common),
            graph,
            tuple(RetainedCargoScratchRoot.from_dict(x) for x in roots),
            RetainedCargoCustodyPolicy.from_dict(data["input_custody"]),
        )

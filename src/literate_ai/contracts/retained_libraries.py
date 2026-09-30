"""Closed retained-library export sets; integrity metadata, never qualification.

Reuse the existing artifact graph and import-surface contracts. Qualification,
importer trust, delivery, Cargo integration and source retirement belong to later
admission boundaries; parsing this record performs no I/O and grants no authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, list_value, string_value
from .blobs import BlobRef
from .executable_components.artifacts import ArtifactBuildGraph, ExactLinkPlan
from .identity import ContentIdentity, canonical_identity
from .library_products import LibraryArtifactProduct
from .paths import canonical_relative_posix_paths
from .repositories import RepositoryBuildCommand


@dataclass(frozen=True, slots=True)
class RetainedLibraryGatePolicy:
    """Reviewed consumer commands and tools, independent of mutable source.

    This policy neither proves complete gate coverage nor grants execution or
    admission. Current source and external inputs need separate run custody.
    """

    importer_project_id: str
    commands: tuple[RepositoryBuildCommand, ...]
    toolchains: tuple[ContentIdentity, ...]
    external_input_variables: tuple[str, ...] = ()

    SCHEMA: ClassVar[str] = "literate-ai/retained-library-gate-policy@1"
    MAX_COMMANDS: ClassVar[int] = 128
    MAX_TOOLCHAINS: ClassVar[int] = 64

    def __post_init__(self) -> None:
        label = "RetainedLibraryGatePolicy"
        string_value(
            self.importer_project_id, f"{label}.importer_project_id", max_length=256
        )
        if any(ord(char) < 32 for char in self.importer_project_id):
            fail(
                f"{label}.importer_project_id",
                "requires a printable project identifier",
            )
        if (
            not isinstance(self.commands, tuple)
            or not 1 <= len(self.commands) <= self.MAX_COMMANDS
            or any(
                not isinstance(item, RepositoryBuildCommand) for item in self.commands
            )
        ):
            fail(f"{label}.commands", "requires 1 to 128 typed commands")
        if len({item.step_id for item in self.commands}) != len(self.commands):
            fail(f"{label}.commands", "requires distinct step identifiers")
        if (
            not isinstance(self.toolchains, tuple)
            or not 1 <= len(self.toolchains) <= self.MAX_TOOLCHAINS
            or any(not isinstance(item, ContentIdentity) for item in self.toolchains)
        ):
            fail(f"{label}.toolchains", "requires 1 to 64 typed identities")
        keys = tuple(item.uri for item in self.toolchains)
        if keys != tuple(sorted(set(keys))):
            fail(f"{label}.toolchains", "requires unique tools in identity order")
        names = self.external_input_variables
        if (
            not isinstance(names, tuple)
            or len(names) > 64
            or any(
                not isinstance(name, str)
                or re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) is None
                for name in names
            )
            or names != tuple(sorted(set(names)))
        ):
            fail(
                f"{label}.external_input_variables",
                "requires bounded sorted unique environment names",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema": self.SCHEMA,
            "importer_project_id": self.importer_project_id,
            "commands": [item.to_dict() for item in self.commands],
            "toolchains": [item.to_dict() for item in self.toolchains],
        }
        if self.external_input_variables:
            result["external_input_variables"] = list(self.external_input_variables)
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RetainedLibraryGatePolicy"
    ) -> RetainedLibraryGatePolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"importer_project_id", "commands", "toolchains"}),
            optional=frozenset({"external_input_variables"}),
        )
        commands = list_value(data["commands"], f"{path}.commands")
        toolchains = list_value(data["toolchains"], f"{path}.toolchains")
        if not 1 <= len(commands) <= cls.MAX_COMMANDS:
            fail(f"{path}.commands", "requires 1 to 128 commands")
        if not 1 <= len(toolchains) <= cls.MAX_TOOLCHAINS:
            fail(f"{path}.toolchains", "requires 1 to 64 toolchains")
        return cls(
            string_value(
                data["importer_project_id"],
                f"{path}.importer_project_id",
                max_length=256,
            ),
            tuple(
                RepositoryBuildCommand.from_dict(item, path=f"{path}.commands[{index}]")
                for index, item in enumerate(commands)
            ),
            tuple(
                ContentIdentity.from_dict(item, path=f"{path}.toolchains[{index}]")
                for index, item in enumerate(toolchains)
            ),
            tuple(
                list_value(
                    data.get("external_input_variables", []),
                    f"{path}.external_input_variables",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RetainedLibraryExportSet:
    graph: ArtifactBuildGraph
    link_plan_identity: ContentIdentity
    libraries: tuple[LibraryArtifactProduct, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-library-export-set@1"
    MAX_LIBRARIES: ClassVar[int] = 1024

    def __post_init__(self) -> None:
        if not isinstance(self.graph, ArtifactBuildGraph):
            fail("RetainedLibraryExportSet.graph", "requires an artifact build graph")
        if not isinstance(self.link_plan_identity, ContentIdentity):
            fail("RetainedLibraryExportSet.link_plan_identity", "requires an identity")
        if (
            not isinstance(self.libraries, tuple)
            or not 1 <= len(self.libraries) <= self.MAX_LIBRARIES
            or any(
                not isinstance(item, LibraryArtifactProduct) for item in self.libraries
            )
        ):
            fail(
                "RetainedLibraryExportSet.libraries",
                "requires 1 to 1024 typed library products",
            )
        keys = tuple(item.artifact_export.identity.uri for item in self.libraries)
        if keys != tuple(sorted(set(keys))):
            fail(
                "RetainedLibraryExportSet.libraries",
                "requires unique products in export identity order",
            )
        exports = {
            item.identity.uri: item
            for manifest in self.graph.manifests
            for item in manifest.exports
        }
        expected = {key for key, item in exports.items() if item.role == "library"}
        if set(keys) != expected:
            fail(
                "RetainedLibraryExportSet.libraries",
                "must describe exactly every library export in the graph",
            )
        # Match the whole export, not only the package byte identity. Component,
        # target, toolchain, authorization and dependency bindings must agree too.
        if any(
            exports[key] != product.artifact_export
            for key, product in zip(keys, self.libraries, strict=True)
        ):
            fail(
                "RetainedLibraryExportSet.libraries",
                "library export differs from the graph",
            )
        selected = self.link_plan
        if any(
            exports[root.uri].role != "library"
            for root in selected.resolved_root_artifact_identities
        ):
            fail(
                "RetainedLibraryExportSet.link_plan_identity",
                "every selected root must be a library export",
            )

    @property
    def link_plan(self) -> ExactLinkPlan:
        for item in self.graph.link_plans:
            if item.identity == self.link_plan_identity:
                return item
        fail(
            "RetainedLibraryExportSet.link_plan_identity",
            "selected link plan is absent from the graph",
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "graph": self.graph.to_dict(),
            "link_plan_identity": self.link_plan_identity.to_dict(),
            "libraries": [item.to_dict() for item in self.libraries],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RetainedLibraryExportSet"
    ) -> RetainedLibraryExportSet:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"graph", "link_plan_identity", "libraries"}),
        )
        libraries = list_value(data["libraries"], f"{path}.libraries")
        if not 1 <= len(libraries) <= cls.MAX_LIBRARIES:
            fail(f"{path}.libraries", "requires 1 to 1024 library products")
        return cls(
            ArtifactBuildGraph.from_dict(data["graph"], path=f"{path}.graph"),
            ContentIdentity.from_dict(
                data["link_plan_identity"], path=f"{path}.link_plan_identity"
            ),
            tuple(
                LibraryArtifactProduct.from_dict(
                    item, path=f"{path}.libraries[{index}]"
                )
                for index, item in enumerate(libraries)
            ),
        )


@dataclass(frozen=True, slots=True)
class RetainedLibraryBinding:
    """Reviewed importer intent; structural integrity alone never admits consumption.

    The workspace plan must separately describe the exact retained manifest/lock
    delta, target/features and full gates. Admission reopens it and current provider
    evidence; neither an opaque plan reference nor a named store proves trust.
    """

    importer_project_id: str
    source_store_id: str
    exports: RetainedLibraryExportSet
    qualification_archive: BlobRef
    qualification_identity: ContentIdentity
    run_identity: ContentIdentity
    verifier_identity: ContentIdentity
    policy_identity: ContentIdentity
    workspace_plan: BlobRef
    destinations: tuple[tuple[ContentIdentity, str], ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-library-binding@1"
    MAX_DESTINATIONS: ClassVar[int] = 4096
    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "importer_project_id",
            "source_store_id",
            "exports",
            "qualification_archive",
            "qualification_identity",
            "run_identity",
            "verifier_identity",
            "policy_identity",
            "workspace_plan",
            "destinations",
        }
    )

    def __post_init__(self) -> None:
        string_value(
            self.importer_project_id,
            "RetainedLibraryBinding.importer_project_id",
            max_length=256,
        )
        if any(ord(char) < 32 for char in self.importer_project_id):
            fail(
                "RetainedLibraryBinding.importer_project_id",
                "requires a printable project identifier",
            )
        if (
            not isinstance(self.source_store_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.source_store_id)
            is None
        ):
            fail(
                "RetainedLibraryBinding.source_store_id",
                "requires a configured store name, not a URL or path",
            )
        if not isinstance(self.exports, RetainedLibraryExportSet):
            fail("RetainedLibraryBinding.exports", "requires a typed closed export set")
        for name in (
            "qualification_identity",
            "run_identity",
            "verifier_identity",
            "policy_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"RetainedLibraryBinding.{name}", "requires a content identity")
        for name, media in (
            ("qualification_archive", "application/zip"),
            ("workspace_plan", "application/json"),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, BlobRef)
                or value.media_type != media
                or value.size <= 0
            ):
                fail(
                    f"RetainedLibraryBinding.{name}",
                    "requires a nonempty typed blob with the expected media type",
                )
        if (
            not isinstance(self.destinations, tuple)
            or not 1 <= len(self.destinations) <= self.MAX_DESTINATIONS
            or any(
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], ContentIdentity)
                or not isinstance(row[1], str)
                or len(row[1]) > 128
                for row in self.destinations
            )
        ):
            fail(
                "RetainedLibraryBinding.destinations",
                "requires bounded typed export/path pairs",
            )
        identities = tuple(identity.uri for identity, _ in self.destinations)
        expected = {
            export.identity.uri
            for manifest in self.exports.graph.manifests
            for export in manifest.exports
        }
        if identities != tuple(sorted(expected)):
            fail(
                "RetainedLibraryBinding.destinations",
                "must name every graph export exactly once in identity order",
            )
        try:
            canonical_relative_posix_paths(
                (path for _, path in self.destinations), label="destinations"
            )
        except (ValueError, TypeError):
            fail(
                "RetainedLibraryBinding.destinations",
                "requires portable nonoverlapping project-relative paths",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "importer_project_id": self.importer_project_id,
            "source_store_id": self.source_store_id,
            "exports": self.exports.to_dict(),
            "qualification_archive": self.qualification_archive.to_dict(),
            "qualification_identity": self.qualification_identity.to_dict(),
            "run_identity": self.run_identity.to_dict(),
            "verifier_identity": self.verifier_identity.to_dict(),
            "policy_identity": self.policy_identity.to_dict(),
            "workspace_plan": self.workspace_plan.to_dict(),
            "destinations": [
                {"export_identity": identity.to_dict(), "path": path}
                for identity, path in self.destinations
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RetainedLibraryBinding"
    ) -> RetainedLibraryBinding:
        data = contract_fields(
            value, path=path, schema_uri=cls.SCHEMA, required=cls._FIELDS
        )
        rows = list_value(data["destinations"], f"{path}.destinations")
        if not 1 <= len(rows) <= cls.MAX_DESTINATIONS:
            fail(f"{path}.destinations", "requires a bounded nonempty destination list")
        destinations = []
        for index, row in enumerate(rows):
            prefix = f"{path}.destinations[{index}]"
            item = fields(
                row, path=prefix, required=frozenset({"export_identity", "path"})
            )
            destinations.append(
                (
                    ContentIdentity.from_dict(
                        item["export_identity"], path=f"{prefix}.export_identity"
                    ),
                    item["path"],
                )
            )
        return cls(
            data["importer_project_id"],
            data["source_store_id"],
            RetainedLibraryExportSet.from_dict(data["exports"], path=f"{path}.exports"),
            BlobRef.from_dict(data["qualification_archive"]),
            *(
                ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in (
                    "qualification_identity",
                    "run_identity",
                    "verifier_identity",
                    "policy_identity",
                )
            ),
            BlobRef.from_dict(data["workspace_plan"]),
            tuple(destinations),
        )

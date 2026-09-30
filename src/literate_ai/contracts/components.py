"""Logical Component definitions and immutable Component revisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    fail,
    fields,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .capabilities import Capability, CapabilityRequirement
from .flavors import FlavorSlot
from .identity import (
    SCHEMA_PREFIX,
    SCHEMA_V2_PREFIX,
    ComponentCoordinate,
    ComponentRevisionRef,
    ContentIdentity,
    ContentReference,
    contract_identity,
)
from .specifications import SpecificationSet
from .versioning import semantic_version

LEGACY_COMPONENT_DEFINITION_SCHEMA = f"{SCHEMA_PREFIX}component-definition"
COMPONENT_DEFINITION_SCHEMA = f"{SCHEMA_V2_PREFIX}component-definition"
COMPONENT_REVISION_SCHEMA = f"{SCHEMA_V2_PREFIX}component-revision"
PORTABLE_APPLICATION_ENTRYPOINT_KIND = "portable-application"
NATIVE_CLI_ENTRYPOINT_KIND = "native-cli"
PERSISTENT_SERVICE_ENTRYPOINT_KIND = "persistent-service"
WEB_APPLICATION_ENTRYPOINT_KIND = "web-application"

# Framework-owned runtime dispatcher modes the generated artifact interprets. The
# runtime drivers pass exactly one of these as the trailing argv token so the
# Standard lifecycle can drive the built artifact after source custody is gone.
# TEST runs the generated native test suite; SMOKE runs one one-shot example (the
# portable-application EXECUTE mode); SERVE binds host/port and stays alive so a
# persistent-service can be polled for readiness (issue #214).
LITAI_TEST_MODE_FLAG = "--litai-test"
LITAI_SMOKE_MODE_FLAG = "--litai-smoke"
LITAI_SERVE_MODE_FLAG = "--litai-serve"
SAMPLE_HARNESS_ENTRYPOINT_KINDS = frozenset(
    {
        PORTABLE_APPLICATION_ENTRYPOINT_KIND,
        PERSISTENT_SERVICE_ENTRYPOINT_KIND,
        WEB_APPLICATION_ENTRYPOINT_KIND,
    }
)

# CLI-only generated assumptions. Other Component kinds must not inherit these.
CLI_APPLICATION_LIFECYCLE_ASSUMPTIONS = (
    "one JSON-array argv ABI for the runnable artifact",
    "framework --litai-test and --litai-smoke dispatcher modes",
    "process-invocation acceptance rather than import or render oracles",
    "at least one portable-application entrypoint",
)


class ComponentKind(StrEnum):
    """Lifecycle kind for one Component. ``cli-application`` is a subclass of
    ``component``, not the implicit base."""

    COMPONENT = "component"
    CLI_APPLICATION = "cli-application"
    NATIVE_CLI_APPLICATION = "native-cli-application"
    LIBRARY = "library"
    PERSISTENT_SERVICE = "persistent-service"
    PACKAGED_MODULE = "packaged-module"
    UI = "ui"
    SCHEMA_ONLY = "schema-only"


_REVIEWED_NODE_COMPONENT_KINDS = {
    "cli": ComponentKind.CLI_APPLICATION,
    "library": ComponentKind.LIBRARY,
    "service": ComponentKind.PERSISTENT_SERVICE,
}


def infer_component_kind(entrypoints: Sequence[Entrypoint]) -> ComponentKind:
    """Infer a lifecycle kind from declared entrypoints when ``kind`` is omitted."""

    kinds = {item.kind for item in entrypoints}
    if PORTABLE_APPLICATION_ENTRYPOINT_KIND in kinds:
        return ComponentKind.CLI_APPLICATION
    if NATIVE_CLI_ENTRYPOINT_KIND in kinds:
        return ComponentKind.NATIVE_CLI_APPLICATION
    if PERSISTENT_SERVICE_ENTRYPOINT_KIND in kinds:
        return ComponentKind.PERSISTENT_SERVICE
    return ComponentKind.COMPONENT


def resolve_component_kind(
    authored: str, entrypoints: Sequence[Entrypoint]
) -> ComponentKind:
    """Return the authored kind, or infer it when the field is omitted."""

    if authored:
        try:
            return ComponentKind(authored)
        except ValueError:
            fail("ComponentAuthoring.kind", f"unsupported Component kind {authored!r}")
    return infer_component_kind(entrypoints)


def component_kind_from_reviewed_node(node_kind: str) -> ComponentKind:
    """Map a reviewed inverse graph node kind onto a Component lifecycle kind."""

    try:
        return _REVIEWED_NODE_COMPONENT_KINDS[node_kind]
    except KeyError:
        fail("ComponentAuthoring.kind", f"unsupported reviewed node kind {node_kind!r}")


def validate_component_kind(
    kind: ComponentKind, entrypoints: Sequence[Entrypoint]
) -> None:
    """Fail closed when entrypoints contradict the Component lifecycle kind."""

    declared = {item.kind for item in entrypoints}
    cli = PORTABLE_APPLICATION_ENTRYPOINT_KIND
    native_cli = NATIVE_CLI_ENTRYPOINT_KIND
    service = PERSISTENT_SERVICE_ENTRYPOINT_KIND
    if kind is ComponentKind.CLI_APPLICATION:
        if cli not in declared:
            fail(
                "ComponentAuthoring.kind",
                "cli-application requires a portable-application entrypoint",
            )
        if native_cli in declared:
            fail(
                "ComponentAuthoring.kind",
                "cli-application cannot declare a native-cli entrypoint",
            )
        return
    if kind is ComponentKind.NATIVE_CLI_APPLICATION:
        if native_cli not in declared:
            fail(
                "ComponentAuthoring.kind",
                "native-cli-application requires a native-cli entrypoint",
            )
        if cli in declared:
            fail(
                "ComponentAuthoring.kind",
                "native-cli-application cannot declare a portable-application "
                "JSON entrypoint",
            )
        return
    if kind is ComponentKind.LIBRARY:
        if entrypoints:
            fail("ComponentAuthoring.kind", "library entrypoints must be empty")
        return
    if kind is ComponentKind.PERSISTENT_SERVICE:
        if service not in declared:
            fail(
                "ComponentAuthoring.kind",
                "persistent-service requires a persistent-service entrypoint",
            )
        if cli in declared or native_cli in declared:
            fail(
                "ComponentAuthoring.kind",
                "persistent-service cannot declare a portable-application CLI "
                "entrypoint",
            )
        return
    if kind is ComponentKind.PACKAGED_MODULE:
        if cli in declared or native_cli in declared:
            fail(
                "ComponentAuthoring.kind",
                "packaged-module cannot declare a portable-application CLI entrypoint",
            )
        return
    if kind is ComponentKind.UI:
        if cli in declared or native_cli in declared:
            fail(
                "ComponentAuthoring.kind",
                "ui cannot inherit the CLI argv contract",
            )
        return
    if kind is ComponentKind.SCHEMA_ONLY:
        if entrypoints:
            fail("ComponentAuthoring.kind", "schema-only entrypoints must be empty")
        return
    if kind is ComponentKind.COMPONENT and cli in declared:
        fail(
            "ComponentAuthoring.kind",
            "portable-application entrypoints require kind cli-application",
        )
    if kind is ComponentKind.COMPONENT and native_cli in declared:
        fail(
            "ComponentAuthoring.kind",
            "native-cli entrypoints require kind native-cli-application",
        )


def _adapt_unreleased_v1_envelope(value: Any, schema: str) -> Any:
    """Rebind only the known post-v0.1.1 Component record envelope."""

    legacy = schema.replace(SCHEMA_V2_PREFIX, SCHEMA_PREFIX, 1)
    if isinstance(value, dict) and value.get("schema") == legacy:
        return {**value, "schema": schema}
    return value


def _entrypoint_path(value: str, path: str) -> str:
    raw = string_value(value, path)
    if raw.startswith("/") or "\\" in raw:
        fail(path, "must be a relative POSIX path")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        fail(
            path, "must be normalized and must not contain empty, '.' or '..' segments"
        )
    return raw


@dataclass(frozen=True, slots=True)
class Entrypoint:
    name: str
    kind: str
    path: str
    deployment_unit: str | None = None

    def __post_init__(self) -> None:
        string_value(self.name, "Entrypoint.name")
        string_value(self.kind, "Entrypoint.kind")
        _entrypoint_path(self.path, "Entrypoint.path")
        if self.deployment_unit is not None:
            string_value(self.deployment_unit, "Entrypoint.deployment_unit")

    @property
    def resolved_deployment_unit(self) -> str:
        """The declared deployment unit, or the entrypoint's own implicit unit.

        A Component that names no deployment unit places every entrypoint in one
        shared implicit unit keyed by the entrypoint name -- the current
        single-unit behavior. Multi-entrypoint Components opt in by declaring an
        explicit ``deployment_unit`` so surfaces restart independently.
        """

        return self.deployment_unit if self.deployment_unit is not None else self.name

    def to_dict(self) -> dict[str, str]:
        value = {"name": self.name, "kind": self.kind, "path": self.path}
        # Additive extension: absent (not present-and-null) unless declared, so a
        # single-entrypoint Component serializes byte-identically to today.
        if self.deployment_unit is not None:
            value["deployment_unit"] = self.deployment_unit
        return value

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "Entrypoint") -> Entrypoint:
        data = fields(
            value,
            path=path,
            required=frozenset({"name", "kind", "path"}),
            optional=frozenset({"deployment_unit"}),
        )
        raw_unit = data.get("deployment_unit")
        return cls(
            name=string_value(data["name"], f"{path}.name"),
            kind=string_value(data["kind"], f"{path}.kind"),
            path=_entrypoint_path(data["path"], f"{path}.path"),
            deployment_unit=(
                None
                if raw_unit is None
                else string_value(raw_unit, f"{path}.deployment_unit")
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    coordinate: ComponentCoordinate
    version: str
    display_name: str
    description: str
    profiles: tuple[str, ...]
    sample: bool
    provides: tuple[Capability, ...]
    requires: tuple[CapabilityRequirement, ...]
    specification_provider: str
    specification_roots: tuple[str, ...]
    authoring_inputs: tuple[ContentReference, ...]
    workflow_definition: ContentReference
    routing_policy: ContentReference
    flavor_slots: tuple[FlavorSlot, ...]
    entrypoints: tuple[Entrypoint, ...]
    acceptance_contracts: tuple[ContentReference, ...]
    source_dependencies: tuple[ContentReference, ...] = ()

    SCHEMA: ClassVar[str] = COMPONENT_DEFINITION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, ComponentCoordinate):
            fail("ComponentDefinition.coordinate", "must be a ComponentCoordinate")
        semantic_version(self.version, "ComponentDefinition.version")
        string_value(self.display_name, "ComponentDefinition.display_name")
        string_value(
            self.description,
            "ComponentDefinition.description",
            nonempty=False,
            max_length=16384,
        )
        string_value(
            self.specification_provider, "ComponentDefinition.specification_provider"
        )
        if not isinstance(self.sample, bool):
            fail("ComponentDefinition.sample", "must be a boolean")
        for field_name in ("profiles", "specification_roots"):
            values = getattr(self, field_name)
            unique(values, f"ComponentDefinition.{field_name}")
            for index, value in enumerate(values):
                string_value(value, f"ComponentDefinition.{field_name}[{index}]")
        if not self.specification_roots:
            fail("ComponentDefinition.specification_roots", "must not be empty")
        unique(
            tuple(item.name for item in self.provides),
            "ComponentDefinition.provides",
            "capability names",
        )
        unique(
            tuple(item.requirement_id for item in self.requires),
            "ComponentDefinition.requires",
            "requirement IDs",
        )
        unique(
            tuple(item.slot_id for item in self.flavor_slots),
            "ComponentDefinition.flavor_slots",
            "slot IDs",
        )
        unique(
            tuple(item.name for item in self.entrypoints),
            "ComponentDefinition.entrypoints",
            "entrypoint names",
        )
        unique(
            tuple(item.resolved_deployment_unit for item in self.entrypoints),
            "ComponentDefinition.entrypoints",
            "resolved deployment units",
        )
        unique(
            tuple(item.uri for item in self.source_dependencies),
            "ComponentDefinition.source_dependencies",
            "repository source dependency URIs",
        )
        for index, reference in enumerate(self.source_dependencies):
            if reference.kind != "repository-source-dependency":
                fail(
                    f"ComponentDefinition.source_dependencies[{index}].kind",
                    "must be 'repository-source-dependency'",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.SCHEMA,
            "coordinate": self.coordinate.to_dict(),
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "profiles": list(self.profiles),
            "sample": self.sample,
            "provides": [item.to_dict() for item in self.provides],
            "requires": [item.to_dict() for item in self.requires],
            "specification_provider": self.specification_provider,
            "specification_roots": list(self.specification_roots),
            "authoring_inputs": [item.to_dict() for item in self.authoring_inputs],
            "workflow_definition": self.workflow_definition.to_dict(),
            "routing_policy": self.routing_policy.to_dict(),
            "flavor_slots": [item.to_dict() for item in self.flavor_slots],
            "entrypoints": [item.to_dict() for item in self.entrypoints],
            "acceptance_contracts": [
                item.to_dict() for item in self.acceptance_contracts
            ],
        }
        # Keep the optional extension absent when it carries no information.
        if self.source_dependencies:
            value["source_dependencies"] = [
                item.to_dict() for item in self.source_dependencies
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentDefinition"
    ) -> ComponentDefinition:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "coordinate",
                    "version",
                    "display_name",
                    "description",
                    "profiles",
                    "sample",
                    "provides",
                    "requires",
                    "specification_provider",
                    "specification_roots",
                    "authoring_inputs",
                    "workflow_definition",
                    "routing_policy",
                    "flavor_slots",
                    "entrypoints",
                    "acceptance_contracts",
                }
            ),
            optional=frozenset({"source_dependencies"}),
        )
        return cls(
            coordinate=ComponentCoordinate.from_dict(
                data["coordinate"], path=f"{path}.coordinate"
            ),
            version=semantic_version(data["version"], f"{path}.version"),
            display_name=string_value(data["display_name"], f"{path}.display_name"),
            description=string_value(
                data["description"],
                f"{path}.description",
                nonempty=False,
                max_length=16384,
            ),
            profiles=string_tuple(data["profiles"], f"{path}.profiles"),
            sample=bool_value(data["sample"], f"{path}.sample"),
            provides=parse_tuple(
                data["provides"], f"{path}.provides", Capability.from_dict
            ),
            requires=parse_tuple(
                data["requires"], f"{path}.requires", CapabilityRequirement.from_dict
            ),
            specification_provider=string_value(
                data["specification_provider"], f"{path}.specification_provider"
            ),
            specification_roots=string_tuple(
                data["specification_roots"], f"{path}.specification_roots"
            ),
            authoring_inputs=parse_tuple(
                data["authoring_inputs"],
                f"{path}.authoring_inputs",
                ContentReference.from_dict,
            ),
            workflow_definition=ContentReference.from_dict(
                data["workflow_definition"], path=f"{path}.workflow_definition"
            ),
            routing_policy=ContentReference.from_dict(
                data["routing_policy"], path=f"{path}.routing_policy"
            ),
            flavor_slots=parse_tuple(
                data["flavor_slots"], f"{path}.flavor_slots", FlavorSlot.from_dict
            ),
            entrypoints=parse_tuple(
                data["entrypoints"], f"{path}.entrypoints", Entrypoint.from_dict
            ),
            acceptance_contracts=parse_tuple(
                data["acceptance_contracts"],
                f"{path}.acceptance_contracts",
                ContentReference.from_dict,
            ),
            source_dependencies=parse_tuple(
                data.get("source_dependencies", ()),
                f"{path}.source_dependencies",
                ContentReference.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentRevision:
    definition: ComponentDefinition
    specifications: SpecificationSet
    source_snapshot: ContentIdentity | None
    parent_revisions: tuple[ContentIdentity, ...]
    parent_runs: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = COMPONENT_REVISION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.definition, ComponentDefinition):
            fail("ComponentRevision.definition", "must be a ComponentDefinition")
        if not isinstance(self.specifications, SpecificationSet):
            fail("ComponentRevision.specifications", "must be a SpecificationSet")
        if not isinstance(self.source_snapshot, (ContentIdentity, type(None))):
            fail(
                "ComponentRevision.source_snapshot", "must be a ContentIdentity or null"
            )
        unique(
            tuple(item.uri for item in self.parent_revisions),
            "ComponentRevision.parent_revisions",
            "parent revision identities",
        )
        unique(
            tuple(item.uri for item in self.parent_runs),
            "ComponentRevision.parent_runs",
            "parent run identities",
        )

    @property
    def coordinate(self) -> ComponentCoordinate:
        return self.definition.coordinate

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def ref(self) -> ComponentRevisionRef:
        return ComponentRevisionRef(
            self.definition.coordinate,
            self.definition.version,
            self.identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "definition": self.definition.to_dict(),
            "specifications": self.specifications.to_dict(),
            "source_snapshot": (
                None if self.source_snapshot is None else self.source_snapshot.to_dict()
            ),
            "parent_revisions": [item.to_dict() for item in self.parent_revisions],
            "parent_runs": [item.to_dict() for item in self.parent_runs],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentRevision"
    ) -> ComponentRevision:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "definition",
                    "specifications",
                    "source_snapshot",
                    "parent_revisions",
                    "parent_runs",
                }
            ),
        )
        source = data["source_snapshot"]
        return cls(
            definition=ComponentDefinition.from_dict(
                data["definition"], path=f"{path}.definition"
            ),
            specifications=SpecificationSet.from_dict(
                data["specifications"], path=f"{path}.specifications"
            ),
            source_snapshot=(
                None
                if source is None
                else ContentIdentity.from_dict(source, path=f"{path}.source_snapshot")
            ),
            parent_revisions=parse_tuple(
                data["parent_revisions"],
                f"{path}.parent_revisions",
                ContentIdentity.from_dict,
            ),
            parent_runs=parse_tuple(
                data["parent_runs"], f"{path}.parent_runs", ContentIdentity.from_dict
            ),
        )


__all__ = [
    "CLI_APPLICATION_LIFECYCLE_ASSUMPTIONS",
    "COMPONENT_DEFINITION_SCHEMA",
    "COMPONENT_REVISION_SCHEMA",
    "ComponentDefinition",
    "ComponentKind",
    "ComponentRevision",
    "Entrypoint",
    "LITAI_SERVE_MODE_FLAG",
    "LITAI_SMOKE_MODE_FLAG",
    "LITAI_TEST_MODE_FLAG",
    "NATIVE_CLI_ENTRYPOINT_KIND",
    "PERSISTENT_SERVICE_ENTRYPOINT_KIND",
    "PORTABLE_APPLICATION_ENTRYPOINT_KIND",
    "SAMPLE_HARNESS_ENTRYPOINT_KINDS",
    "WEB_APPLICATION_ENTRYPOINT_KIND",
    "component_kind_from_reviewed_node",
    "infer_component_kind",
    "resolve_component_kind",
    "validate_component_kind",
]

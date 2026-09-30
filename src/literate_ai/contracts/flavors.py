"""First-class target Flavor and effective-revision contracts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .capabilities import Capability, CapabilityRequirement
from .identity import (
    SCHEMA_PREFIX,
    SCHEMA_V2_PREFIX,
    ComponentRevisionRef,
    ContentIdentity,
    ContentReference,
    FlavorCoordinate,
    VersionedContentRef,
    contract_identity,
)
from .migrations import MigrationError
from .provider_resolution import ProviderOverrideDeclaration
from .versioning import semantic_version

LEGACY_FLAVOR_DEFINITION_SCHEMA = f"{SCHEMA_PREFIX}flavor-definition"
LEGACY_TARGET_PROFILE_SCHEMA = f"{SCHEMA_PREFIX}target-profile"
FLAVOR_DEFINITION_SCHEMA = f"{SCHEMA_V2_PREFIX}flavor-definition"
FLAVOR_REVISION_SCHEMA = f"{SCHEMA_V2_PREFIX}flavor-revision"
TARGET_PROFILE_SCHEMA = f"{SCHEMA_V2_PREFIX}target-profile"
FLAVOR_SET_LOCK_SCHEMA = f"{SCHEMA_V2_PREFIX}flavor-set-lock"
EFFECTIVE_SPECIFICATION_SET_SCHEMA = f"{SCHEMA_V2_PREFIX}effective-specification-set"
EFFECTIVE_REVISION_SCHEMA = f"{SCHEMA_V2_PREFIX}effective-revision"
TOOLCHAIN_CONSTRAINT_SCHEMA = f"{SCHEMA_V2_PREFIX}toolchain-constraint"
TOOLCHAIN_CONSTRAINT_CONTENT_KIND = "toolchain-constraint"

_TOOLCHAIN_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


def _adapt_unreleased_v1_envelope(value: Any, schema: str) -> Any:
    """Rebind only a known post-v0.1.1 Flavor-family record envelope."""

    legacy = schema.replace(SCHEMA_V2_PREFIX, SCHEMA_PREFIX, 1)
    if isinstance(value, dict) and value.get("schema") == legacy:
        return {**value, "schema": schema}
    return value


class FlavorAxis(StrEnum):
    PLATFORM_OS = "platform.os"
    PLATFORM_ARCHITECTURE = "platform.architecture"
    ACCELERATOR = "accelerator"
    IMPLEMENTATION_LANGUAGE_ECOSYSTEM = "implementation.language-ecosystem"
    IMPLEMENTATION_UI_FRAMEWORK = "implementation.ui-framework"
    BUILD_SYSTEM = "build.system"
    TOOLCHAIN = "toolchain"
    PACKAGING = "packaging"
    DEPLOYMENT = "deployment"
    DOCUMENTATION_ECOSYSTEM = "documentation.ecosystem"


class FlavorCardinality(StrEnum):
    EXACTLY_ONE = "exactly-one"
    ZERO_OR_ONE = "zero-or-one"
    ONE_OR_MORE = "one-or-more"
    BOUNDED = "bounded"


class ContributionKind(StrEnum):
    CAPABILITY = "capability"
    REQUIREMENT = "requirement"
    SPECIFICATION = "specification"
    WORKFLOW = "workflow"
    AUTHORING_SKILL = "authoring-skill"
    VALIDATOR = "validator"
    BUILDER = "builder"
    TOOLCHAIN = "toolchain"
    PACKAGING = "packaging"
    RUNTIME = "runtime"
    SOURCE_DEPENDENCY = "source-dependency"
    EXTENSION = "extension"


class MergeOperator(StrEnum):
    ADDITIVE_SET = "additive-set"
    KEYED_UNION = "keyed-union"
    EXACT_SINGLETON = "exact-singleton"
    EXPLICIT_CONFLICT = "explicit-conflict"


class CandidateStatus(StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FlavorSelectionCandidate:
    """Provider-neutral material needed to apply ordered Flavor selectors."""

    candidate_identity: str
    flavor_id: str
    axis: str
    value: str
    conflicts: tuple[str, ...] = ()
    coordinate_uri: str | None = None
    slot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        string_value(
            self.candidate_identity,
            "FlavorSelectionCandidate.candidate_identity",
        )
        string_value(self.flavor_id, "FlavorSelectionCandidate.flavor_id")
        string_value(self.axis, "FlavorSelectionCandidate.axis")
        string_value(self.value, "FlavorSelectionCandidate.value")
        for index, conflict in enumerate(self.conflicts):
            string_value(
                conflict,
                f"FlavorSelectionCandidate.conflicts[{index}]",
            )
        if self.coordinate_uri is not None:
            string_value(
                self.coordinate_uri,
                "FlavorSelectionCandidate.coordinate_uri",
            )
        if (
            any(not item for item in self.slot_ids)
            or len(set(self.slot_ids)) != len(self.slot_ids)
            or tuple(sorted(self.slot_ids)) != self.slot_ids
        ):
            fail(
                "FlavorSelectionCandidate.slot_ids",
                "must be sorted unique non-empty IDs",
            )

    @property
    def reference_ids(self) -> frozenset[str]:
        return frozenset(
            item for item in (self.flavor_id, self.coordinate_uri) if item is not None
        )


@dataclass(frozen=True, slots=True)
class FlavorSlot:
    slot_id: str
    axis: FlavorAxis
    cardinality: FlavorCardinality
    capability_contract: str
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        string_value(self.slot_id, "FlavorSlot.slot_id")
        string_value(self.capability_contract, "FlavorSlot.capability_contract")
        if not isinstance(self.axis, FlavorAxis):
            fail("FlavorSlot.axis", "must be a FlavorAxis")
        if not isinstance(self.cardinality, FlavorCardinality):
            fail("FlavorSlot.cardinality", "must be a FlavorCardinality")
        if self.cardinality is FlavorCardinality.BOUNDED:
            if self.minimum is None or self.maximum is None:
                fail("FlavorSlot", "bounded cardinality requires minimum and maximum")
            int_value(self.minimum, "FlavorSlot.minimum")
            int_value(self.maximum, "FlavorSlot.maximum", minimum=self.minimum)
        elif self.minimum is not None or self.maximum is not None:
            fail("FlavorSlot", "minimum and maximum are only valid for bounded slots")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "axis": self.axis.value,
            "cardinality": self.cardinality.value,
            "capability_contract": self.capability_contract,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "FlavorSlot") -> FlavorSlot:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "slot_id",
                    "axis",
                    "cardinality",
                    "capability_contract",
                    "minimum",
                    "maximum",
                }
            ),
        )
        minimum = data["minimum"]
        maximum = data["maximum"]
        return cls(
            slot_id=string_value(data["slot_id"], f"{path}.slot_id"),
            axis=enum_value(FlavorAxis, data["axis"], f"{path}.axis"),
            cardinality=enum_value(
                FlavorCardinality, data["cardinality"], f"{path}.cardinality"
            ),
            capability_contract=string_value(
                data["capability_contract"], f"{path}.capability_contract"
            ),
            minimum=(
                None if minimum is None else int_value(minimum, f"{path}.minimum")
            ),
            maximum=(
                None if maximum is None else int_value(maximum, f"{path}.maximum")
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetConstraint:
    axis: FlavorAxis
    value: str
    optional: bool = False
    slot_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.axis, FlavorAxis):
            fail("TargetConstraint.axis", "must be a FlavorAxis")
        string_value(self.value, "TargetConstraint.value")
        if not isinstance(self.optional, bool):
            fail("TargetConstraint.optional", "must be a boolean")
        if self.slot_id is not None:
            string_value(self.slot_id, "TargetConstraint.slot_id")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "axis": self.axis.value,
            "value": self.value,
            "optional": self.optional,
        }
        if self.slot_id is not None:
            value["slot_id"] = self.slot_id
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "TargetConstraint"
    ) -> TargetConstraint:
        data = fields(
            value,
            path=path,
            required=frozenset({"axis", "value", "optional"}),
            optional=frozenset({"slot_id"}),
        )
        return cls(
            axis=enum_value(FlavorAxis, data["axis"], f"{path}.axis"),
            value=string_value(data["value"], f"{path}.value"),
            optional=bool_value(data["optional"], f"{path}.optional"),
            slot_id=(
                string_value(data["slot_id"], f"{path}.slot_id")
                if "slot_id" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetProfile:
    profile_id: str
    version: str
    provider: str
    provider_configuration: ContentIdentity
    constraints: tuple[TargetConstraint, ...]

    SCHEMA: ClassVar[str] = TARGET_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.profile_id, "TargetProfile.profile_id")
        semantic_version(self.version, "TargetProfile.version")
        string_value(self.provider, "TargetProfile.provider")
        unique(
            tuple(
                f"slot:{constraint.slot_id}"
                if constraint.slot_id is not None
                else f"axis:{constraint.axis.value}"
                for constraint in self.constraints
            ),
            "TargetProfile.constraints",
            "constraint targets",
        )
        axis_wide = {
            constraint.axis
            for constraint in self.constraints
            if constraint.slot_id is None
        }
        slot_scoped = {
            constraint.axis
            for constraint in self.constraints
            if constraint.slot_id is not None
        }
        if axis_wide & slot_scoped:
            fail(
                "TargetProfile.constraints",
                "cannot mix axis-wide and slot-scoped constraints on one axis",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "target-profile", self.profile_id, self.version, self.identity
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "profile_id": self.profile_id,
            "version": self.version,
            "provider": self.provider,
            "provider_configuration": self.provider_configuration.to_dict(),
            "constraints": [constraint.to_dict() for constraint in self.constraints],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "TargetProfile") -> TargetProfile:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "profile_id",
                    "version",
                    "provider",
                    "provider_configuration",
                    "constraints",
                }
            ),
        )
        return cls(
            profile_id=string_value(data["profile_id"], f"{path}.profile_id"),
            version=semantic_version(data["version"], f"{path}.version"),
            provider=string_value(data["provider"], f"{path}.provider"),
            provider_configuration=ContentIdentity.from_dict(
                data["provider_configuration"], path=f"{path}.provider_configuration"
            ),
            constraints=parse_tuple(
                data["constraints"], f"{path}.constraints", TargetConstraint.from_dict
            ),
        )

    @classmethod
    def from_legacy_dict(
        cls,
        value: Any,
        *,
        version: str | None = None,
        path: str = "TargetProfile",
    ) -> TargetProfile:
        """Read the pre-version profile only with an explicit SemVer binding."""

        if version is None:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "legacy target profile has no semantic version",
            )
        if not isinstance(value, dict):
            fail(path, "must be an object")
        migrated = dict(value)
        migrated["version"] = semantic_version(version, f"{path}.version")
        return cls.from_dict(migrated, path=path)


def _toolchain_version_prefix(value: Any, path: str) -> tuple[int, ...]:
    parts = tuple(
        int_value(item, f"{path}[{index}]")
        for index, item in enumerate(list_value(value, path))
    )
    if not 1 <= len(parts) <= 3:
        fail(path, "must contain one to three non-negative integer parts")
    return parts


@dataclass(frozen=True, slots=True)
class ToolchainConstraint:
    """A strict machine-readable toolchain requirement contributed by a Flavor.

    ``command`` is an argument vector, never shell text. Version values are prefixes:
    ``[3, 12]`` accepts any Python 3.12 patch release, while ``[20]`` accepts any
    Node.js 20 release. ``maximum_exclusive_version`` is the first rejected prefix,
    so ``[9]`` plus ``[13]`` is the closed range ``>=9,<13``. Host adapters apply
    these fields only after the exact contribution artifact has been resolved and
    its content identity verified.
    """

    toolchain: str
    command: tuple[str, ...] | None = None
    minimum_version: tuple[int, ...] | None = None
    required_version: tuple[int, ...] | None = None
    remediation: str | None = None
    remediation_uri: str | None = None
    maximum_exclusive_version: tuple[int, ...] | None = None

    SCHEMA: ClassVar[str] = TOOLCHAIN_CONSTRAINT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.toolchain, "ToolchainConstraint.toolchain", max_length=64)
        if _TOOLCHAIN_NAME_RE.fullmatch(self.toolchain) is None:
            fail(
                "ToolchainConstraint.toolchain",
                "must be a lower-case portable toolchain name",
            )
        if self.command is not None:
            if not self.command:
                fail("ToolchainConstraint.command", "must not be empty")
            for index, item in enumerate(self.command):
                string_value(item, f"ToolchainConstraint.command[{index}]")
                if "\x00" in item:
                    fail(
                        f"ToolchainConstraint.command[{index}]",
                        "must not contain a NUL character",
                    )
        for field_name in (
            "minimum_version",
            "required_version",
            "maximum_exclusive_version",
        ):
            version = getattr(self, field_name)
            if version is not None:
                _toolchain_version_prefix(
                    list(version), f"ToolchainConstraint.{field_name}"
                )
        if self.minimum_version is not None and self.required_version is not None:
            minimum = (*self.minimum_version, *(0,) * (3 - len(self.minimum_version)))
            maximum_required = (
                *self.required_version,
                *((2**63 - 1,) * (3 - len(self.required_version))),
            )
            if maximum_required < minimum:
                fail(
                    "ToolchainConstraint.required_version",
                    "has no version in common with minimum_version",
                )
        if self.maximum_exclusive_version is not None:
            exclusive = (
                *self.maximum_exclusive_version,
                *(0,) * (3 - len(self.maximum_exclusive_version)),
            )
            if self.minimum_version is not None:
                minimum = (
                    *self.minimum_version,
                    *(0,) * (3 - len(self.minimum_version)),
                )
                if not minimum < exclusive:
                    fail(
                        "ToolchainConstraint.maximum_exclusive_version",
                        "has no version in common with minimum_version",
                    )
            if self.required_version is not None:
                required = (
                    *self.required_version,
                    *(0,) * (3 - len(self.required_version)),
                )
                if not required < exclusive:
                    fail(
                        "ToolchainConstraint.maximum_exclusive_version",
                        "has no version in common with required_version",
                    )
        if self.remediation is not None:
            string_value(
                self.remediation, "ToolchainConstraint.remediation", max_length=1024
            )
        if self.remediation_uri is not None:
            string_value(
                self.remediation_uri,
                "ToolchainConstraint.remediation_uri",
                max_length=2048,
            )
            if not self.remediation_uri.startswith("https://"):
                fail(
                    "ToolchainConstraint.remediation_uri",
                    "must be an HTTPS URI",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.SCHEMA,
            "toolchain": self.toolchain,
        }
        if self.command is not None:
            value["command"] = list(self.command)
        if self.minimum_version is not None:
            value["minimum_version"] = list(self.minimum_version)
        if self.required_version is not None:
            value["required_version"] = list(self.required_version)
        if self.maximum_exclusive_version is not None:
            value["maximum_exclusive_version"] = list(self.maximum_exclusive_version)
        if self.remediation is not None:
            value["remediation"] = self.remediation
        if self.remediation_uri is not None:
            value["remediation_uri"] = self.remediation_uri
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ToolchainConstraint"
    ) -> ToolchainConstraint:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"toolchain"}),
            optional=frozenset(
                {
                    "command",
                    "minimum_version",
                    "required_version",
                    "maximum_exclusive_version",
                    "remediation",
                    "remediation_uri",
                }
            ),
        )
        return cls(
            toolchain=string_value(data["toolchain"], f"{path}.toolchain"),
            command=(
                string_tuple(data["command"], f"{path}.command")
                if "command" in data
                else None
            ),
            minimum_version=(
                _toolchain_version_prefix(
                    data["minimum_version"], f"{path}.minimum_version"
                )
                if "minimum_version" in data
                else None
            ),
            required_version=(
                _toolchain_version_prefix(
                    data["required_version"], f"{path}.required_version"
                )
                if "required_version" in data
                else None
            ),
            maximum_exclusive_version=(
                _toolchain_version_prefix(
                    data["maximum_exclusive_version"],
                    f"{path}.maximum_exclusive_version",
                )
                if "maximum_exclusive_version" in data
                else None
            ),
            remediation=(
                string_value(data["remediation"], f"{path}.remediation")
                if "remediation" in data
                else None
            ),
            remediation_uri=(
                string_value(data["remediation_uri"], f"{path}.remediation_uri")
                if "remediation_uri" in data
                else None
            ),
        )

    def version_range(self) -> str:
        """Return the closed ``>=minimum,<exclusive`` range, or fail closed."""

        if self.minimum_version is None or self.maximum_exclusive_version is None:
            raise ValueError(
                "toolchain constraint does not declare a closed version range"
            )
        minimum = ".".join(str(part) for part in self.minimum_version)
        exclusive = ".".join(str(part) for part in self.maximum_exclusive_version)
        return f">={minimum},<{exclusive}"


def merge_toolchain_constraints(
    constraints: Sequence[ToolchainConstraint],
    *,
    path: str = "ToolchainConstraint",
) -> ToolchainConstraint:
    """Intersect compatible constraints for exactly one named toolchain.

    Commands use exact equality, required versions use prefix intersection,
    minimum versions retain the strongest lower bound, and exclusive upper bounds
    retain the tightest maximum. An empty or incompatible set is a contract error;
    callers must never recover by silently dropping a source.
    """

    if not constraints:
        fail(path, "requires at least one constraint")
    names = {item.toolchain for item in constraints}
    if len(names) != 1:
        fail(path, "cannot merge constraints for different toolchains")

    commands = {item.command for item in constraints if item.command is not None}
    if len(commands) > 1:
        fail(f"{path}.command", "conflicting exact command constraints")
    command = next(iter(commands), None)

    minimums = tuple(
        item.minimum_version for item in constraints if item.minimum_version is not None
    )
    minimum = (
        max(
            minimums,
            key=lambda item: ((*item, *(0,) * (3 - len(item))), len(item)),
        )
        if minimums
        else None
    )

    requireds = tuple(
        item.required_version
        for item in constraints
        if item.required_version is not None
    )
    required: tuple[int, ...] | None = None
    for candidate in sorted(requireds, key=len):
        if required is None:
            required = candidate
            continue
        common = min(len(required), len(candidate))
        if required[:common] != candidate[:common]:
            fail(
                f"{path}.required_version",
                "conflicting exact version-prefix constraints",
            )
        if len(candidate) > len(required):
            required = candidate

    exclusives = tuple(
        item.maximum_exclusive_version
        for item in constraints
        if item.maximum_exclusive_version is not None
    )
    maximum_exclusive = (
        min(
            exclusives,
            key=lambda item: ((*item, *(0,) * (3 - len(item))), -len(item)),
        )
        if exclusives
        else None
    )

    remediation_values = {
        item.remediation for item in constraints if item.remediation is not None
    }
    remediation_uris = {
        item.remediation_uri for item in constraints if item.remediation_uri is not None
    }
    if len(remediation_values) > 1 or len(remediation_uris) > 1:
        fail(path, "conflicting toolchain remediation metadata")

    return ToolchainConstraint(
        toolchain=next(iter(names)),
        command=command,
        minimum_version=minimum,
        required_version=required,
        maximum_exclusive_version=maximum_exclusive,
        remediation=next(iter(remediation_values), None),
        remediation_uri=next(iter(remediation_uris), None),
    )


@dataclass(frozen=True, slots=True)
class ContributionReference:
    contribution_id: str
    kind: ContributionKind
    merge_operator: MergeOperator
    slot: str
    content: ContentReference

    def __post_init__(self) -> None:
        string_value(self.contribution_id, "ContributionReference.contribution_id")
        string_value(self.slot, "ContributionReference.slot")
        if not isinstance(self.kind, ContributionKind):
            fail("ContributionReference.kind", "must be a ContributionKind")
        if not isinstance(self.merge_operator, MergeOperator):
            fail("ContributionReference.merge_operator", "must be a MergeOperator")
        if self.kind is ContributionKind.TOOLCHAIN:
            if self.merge_operator is not MergeOperator.EXACT_SINGLETON:
                fail(
                    "ContributionReference.merge_operator",
                    "toolchain constraints require exact-singleton merge semantics",
                )
            if self.content.kind != TOOLCHAIN_CONSTRAINT_CONTENT_KIND:
                fail(
                    "ContributionReference.content.kind",
                    "toolchain constraints require "
                    f"{TOOLCHAIN_CONSTRAINT_CONTENT_KIND!r}",
                )
        if self.kind is ContributionKind.SOURCE_DEPENDENCY:
            if self.merge_operator is not MergeOperator.KEYED_UNION:
                fail(
                    "ContributionReference.merge_operator",
                    "repository source dependencies require keyed-union merge "
                    "semantics",
                )
            if self.content.kind != "repository-source-dependency":
                fail(
                    "ContributionReference.content.kind",
                    "source dependency contributions require "
                    "'repository-source-dependency' content",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "kind": self.kind.value,
            "merge_operator": self.merge_operator.value,
            "slot": self.slot,
            "content": self.content.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ContributionReference"
    ) -> ContributionReference:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"contribution_id", "kind", "merge_operator", "slot", "content"}
            ),
        )
        return cls(
            contribution_id=string_value(
                data["contribution_id"], f"{path}.contribution_id"
            ),
            kind=enum_value(ContributionKind, data["kind"], f"{path}.kind"),
            merge_operator=enum_value(
                MergeOperator, data["merge_operator"], f"{path}.merge_operator"
            ),
            slot=string_value(data["slot"], f"{path}.slot"),
            content=ContentReference.from_dict(data["content"], path=f"{path}.content"),
        )


@dataclass(frozen=True, slots=True)
class FlavorCoRequisiteGroup:
    """Exactly-one alternative Flavor requirement.

    This expresses a portable Flavor's need for one platform realization without
    making that portable Flavor own host-specific installation policy.
    """

    group_id: str
    alternatives: tuple[str, ...]

    def __post_init__(self) -> None:
        string_value(self.group_id, "FlavorCoRequisiteGroup.group_id")
        if len(self.alternatives) < 2:
            fail(
                "FlavorCoRequisiteGroup.alternatives",
                "must contain at least two alternative Flavor coordinates",
            )
        unique(self.alternatives, "FlavorCoRequisiteGroup.alternatives")
        for index, value in enumerate(self.alternatives):
            string_value(value, f"FlavorCoRequisiteGroup.alternatives[{index}]")
            prefix = "flavor://"
            coordinate = value.removeprefix(prefix)
            namespace, separator, name = coordinate.partition("/")
            if not value.startswith(prefix) or not separator or "/" in name:
                fail(
                    f"FlavorCoRequisiteGroup.alternatives[{index}]",
                    "must be a canonical Flavor coordinate URI",
                )
            parsed = FlavorCoordinate(namespace, name)
            if parsed.uri != value:
                fail(
                    f"FlavorCoRequisiteGroup.alternatives[{index}]",
                    "must be a canonical Flavor coordinate URI",
                )

    def to_dict(self) -> dict[str, Any]:
        return {"group_id": self.group_id, "alternatives": list(self.alternatives)}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "FlavorCoRequisiteGroup"
    ) -> FlavorCoRequisiteGroup:
        data = fields(
            value,
            path=path,
            required=frozenset({"group_id", "alternatives"}),
        )
        return cls(
            string_value(data["group_id"], f"{path}.group_id"),
            string_tuple(data["alternatives"], f"{path}.alternatives"),
        )


@dataclass(frozen=True, slots=True)
class FlavorDefinition:
    coordinate: FlavorCoordinate
    version: str
    display_name: str
    primary_axis: FlavorAxis
    secondary_constraints: tuple[TargetConstraint, ...]
    applicable_capabilities: tuple[str, ...]
    provides: tuple[Capability, ...]
    requires: tuple[CapabilityRequirement, ...]
    specification_fragments: tuple[ContentReference, ...]
    authoring_inputs: tuple[ContentReference, ...]
    contributions: tuple[ContributionReference, ...]
    conflicts: tuple[str, ...]
    co_requisites: tuple[str, ...]
    order_before: tuple[str, ...]
    order_after: tuple[str, ...]
    supported_targets: tuple[str, ...]
    co_requisite_groups: tuple[FlavorCoRequisiteGroup, ...] = ()
    provider_overrides: tuple[ProviderOverrideDeclaration, ...] = ()

    SCHEMA: ClassVar[str] = FLAVOR_DEFINITION_SCHEMA

    def __post_init__(self) -> None:
        semantic_version(self.version, "FlavorDefinition.version")
        string_value(self.display_name, "FlavorDefinition.display_name")
        if not isinstance(self.primary_axis, FlavorAxis):
            fail("FlavorDefinition.primary_axis", "must be a FlavorAxis")
        if any(item.axis is self.primary_axis for item in self.secondary_constraints):
            fail(
                "FlavorDefinition.secondary_constraints",
                "must not repeat the primary axis",
            )
        for field_name in (
            "applicable_capabilities",
            "conflicts",
            "co_requisites",
            "order_before",
            "order_after",
            "supported_targets",
        ):
            values = getattr(self, field_name)
            unique(values, f"FlavorDefinition.{field_name}")
            for index, value in enumerate(values):
                string_value(value, f"FlavorDefinition.{field_name}[{index}]")
        if len(self.supported_targets) != 1:
            fail(
                "FlavorDefinition.supported_targets",
                "must contain exactly one primary-axis target",
            )
        unique(
            tuple(item.contribution_id for item in self.contributions),
            "FlavorDefinition.contributions",
            "contribution IDs",
        )
        unique(
            tuple(item.group_id for item in self.co_requisite_groups),
            "FlavorDefinition.co_requisite_groups",
            "group IDs",
        )
        if any(
            not isinstance(item, ProviderOverrideDeclaration)
            for item in self.provider_overrides
        ):
            fail(
                "FlavorDefinition.provider_overrides",
                "must contain ProviderOverrideDeclaration values",
            )
        override_ids = tuple(item.resolution_id for item in self.provider_overrides)
        if override_ids != tuple(sorted(set(override_ids))):
            fail(
                "FlavorDefinition.provider_overrides",
                "must use unique canonical provider-resolution ID order",
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
            "primary_axis": self.primary_axis.value,
            "secondary_constraints": [
                item.to_dict() for item in self.secondary_constraints
            ],
            "applicable_capabilities": list(self.applicable_capabilities),
            "provides": [item.to_dict() for item in self.provides],
            "requires": [item.to_dict() for item in self.requires],
            "specification_fragments": [
                item.to_dict() for item in self.specification_fragments
            ],
            "authoring_inputs": [item.to_dict() for item in self.authoring_inputs],
            "contributions": [item.to_dict() for item in self.contributions],
            "conflicts": list(self.conflicts),
            "co_requisites": list(self.co_requisites),
            "order_before": list(self.order_before),
            "order_after": list(self.order_after),
            "supported_targets": list(self.supported_targets),
        }
        if self.co_requisite_groups:
            value["co_requisite_groups"] = [
                item.to_dict() for item in self.co_requisite_groups
            ]
        if self.provider_overrides:
            value["provider_overrides"] = [
                item.to_dict() for item in self.provider_overrides
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "FlavorDefinition"
    ) -> FlavorDefinition:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        required = frozenset(
            {
                "coordinate",
                "version",
                "display_name",
                "primary_axis",
                "secondary_constraints",
                "applicable_capabilities",
                "provides",
                "requires",
                "specification_fragments",
                "authoring_inputs",
                "contributions",
                "conflicts",
                "co_requisites",
                "order_before",
                "order_after",
                "supported_targets",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=required,
            optional=frozenset({"co_requisite_groups", "provider_overrides"}),
        )
        return cls(
            coordinate=FlavorCoordinate.from_dict(
                data["coordinate"], path=f"{path}.coordinate"
            ),
            version=semantic_version(data["version"], f"{path}.version"),
            display_name=string_value(data["display_name"], f"{path}.display_name"),
            primary_axis=enum_value(
                FlavorAxis, data["primary_axis"], f"{path}.primary_axis"
            ),
            secondary_constraints=parse_tuple(
                data["secondary_constraints"],
                f"{path}.secondary_constraints",
                TargetConstraint.from_dict,
            ),
            applicable_capabilities=string_tuple(
                data["applicable_capabilities"], f"{path}.applicable_capabilities"
            ),
            provides=parse_tuple(
                data["provides"], f"{path}.provides", Capability.from_dict
            ),
            requires=parse_tuple(
                data["requires"], f"{path}.requires", CapabilityRequirement.from_dict
            ),
            specification_fragments=parse_tuple(
                data["specification_fragments"],
                f"{path}.specification_fragments",
                ContentReference.from_dict,
            ),
            authoring_inputs=parse_tuple(
                data["authoring_inputs"],
                f"{path}.authoring_inputs",
                ContentReference.from_dict,
            ),
            contributions=parse_tuple(
                data["contributions"],
                f"{path}.contributions",
                ContributionReference.from_dict,
            ),
            conflicts=string_tuple(data["conflicts"], f"{path}.conflicts"),
            co_requisites=string_tuple(data["co_requisites"], f"{path}.co_requisites"),
            order_before=string_tuple(data["order_before"], f"{path}.order_before"),
            order_after=string_tuple(data["order_after"], f"{path}.order_after"),
            supported_targets=string_tuple(
                data["supported_targets"], f"{path}.supported_targets"
            ),
            co_requisite_groups=(
                parse_tuple(
                    data["co_requisite_groups"],
                    f"{path}.co_requisite_groups",
                    FlavorCoRequisiteGroup.from_dict,
                )
                if "co_requisite_groups" in data
                else ()
            ),
            provider_overrides=parse_tuple(
                data.get("provider_overrides", ()),
                f"{path}.provider_overrides",
                ProviderOverrideDeclaration.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class FlavorRevision:
    definition: FlavorDefinition
    source_snapshot: ContentIdentity | None
    parent_revisions: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = FLAVOR_REVISION_SCHEMA

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "flavor",
            self.definition.coordinate.uri,
            self.definition.version,
            self.identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "definition": self.definition.to_dict(),
            "source_snapshot": None
            if self.source_snapshot is None
            else self.source_snapshot.to_dict(),
            "parent_revisions": [item.to_dict() for item in self.parent_revisions],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "FlavorRevision") -> FlavorRevision:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"definition", "source_snapshot", "parent_revisions"}),
        )
        source = data["source_snapshot"]
        return cls(
            definition=FlavorDefinition.from_dict(
                data["definition"], path=f"{path}.definition"
            ),
            source_snapshot=None
            if source is None
            else ContentIdentity.from_dict(source, path=f"{path}.source_snapshot"),
            parent_revisions=parse_tuple(
                data["parent_revisions"],
                f"{path}.parent_revisions",
                ContentIdentity.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class FlavorCandidateDecision:
    flavor_revision: ContentIdentity
    status: CandidateStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, CandidateStatus):
            fail("FlavorCandidateDecision.status", "must be a CandidateStatus")
        if self.status is not CandidateStatus.SELECTED and not self.reasons:
            fail("FlavorCandidateDecision.reasons", "must explain non-selection")
        for index, reason in enumerate(self.reasons):
            string_value(reason, f"FlavorCandidateDecision.reasons[{index}]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "flavor_revision": self.flavor_revision.to_dict(),
            "status": self.status.value,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "FlavorCandidateDecision"
    ) -> FlavorCandidateDecision:
        data = fields(
            value,
            path=path,
            required=frozenset({"flavor_revision", "status", "reasons"}),
        )
        return cls(
            flavor_revision=ContentIdentity.from_dict(
                data["flavor_revision"], path=f"{path}.flavor_revision"
            ),
            status=enum_value(CandidateStatus, data["status"], f"{path}.status"),
            reasons=string_tuple(data["reasons"], f"{path}.reasons"),
        )


@dataclass(frozen=True, slots=True)
class FlavorSetLock:
    base_revision: ContentIdentity
    target_profile: TargetProfile
    resolution_policy: ContentIdentity
    candidates: tuple[FlavorCandidateDecision, ...]
    selected_revisions: tuple[ContentIdentity, ...]
    base_component: ComponentRevisionRef
    selected_flavors: tuple[VersionedContentRef, ...]

    SCHEMA: ClassVar[str] = FLAVOR_SET_LOCK_SCHEMA

    def __post_init__(self) -> None:
        candidate_ids = tuple(item.flavor_revision.uri for item in self.candidates)
        unique(
            candidate_ids, "FlavorSetLock.candidates", "candidate revision identities"
        )
        selected_ids = tuple(item.uri for item in self.selected_revisions)
        unique(
            selected_ids,
            "FlavorSetLock.selected_revisions",
            "selected revision identities",
        )
        decision_selected = {
            item.flavor_revision.uri
            for item in self.candidates
            if item.status is CandidateStatus.SELECTED
        }
        if decision_selected != set(selected_ids):
            fail(
                "FlavorSetLock.selected_revisions",
                "must exactly match selected candidate decisions",
            )
        if self.base_component.revision_identity != self.base_revision:
            fail(
                "FlavorSetLock.base_component",
                "must bind the exact base revision",
            )
        if tuple(item.content_identity for item in self.selected_flavors) != tuple(
            self.selected_revisions
        ):
            fail(
                "FlavorSetLock.selected_flavors",
                "must bind every selected revision in resolution order",
            )
        if any(item.kind != "flavor" for item in self.selected_flavors):
            fail("FlavorSetLock.selected_flavors", "must contain only Flavor refs")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "base_revision": self.base_revision.to_dict(),
            "target_profile": self.target_profile.to_dict(),
            "resolution_policy": self.resolution_policy.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "selected_revisions": [item.to_dict() for item in self.selected_revisions],
            "base_component": self.base_component.to_dict(),
            "selected_flavors": [item.to_dict() for item in self.selected_flavors],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "FlavorSetLock") -> FlavorSetLock:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "base_revision",
                    "target_profile",
                    "resolution_policy",
                    "candidates",
                    "selected_revisions",
                    "base_component",
                    "selected_flavors",
                }
            ),
        )
        return cls(
            base_revision=ContentIdentity.from_dict(
                data["base_revision"], path=f"{path}.base_revision"
            ),
            target_profile=TargetProfile.from_dict(
                data["target_profile"], path=f"{path}.target_profile"
            ),
            resolution_policy=ContentIdentity.from_dict(
                data["resolution_policy"], path=f"{path}.resolution_policy"
            ),
            candidates=parse_tuple(
                data["candidates"],
                f"{path}.candidates",
                FlavorCandidateDecision.from_dict,
            ),
            selected_revisions=parse_tuple(
                data["selected_revisions"],
                f"{path}.selected_revisions",
                ContentIdentity.from_dict,
            ),
            base_component=ComponentRevisionRef.from_dict(
                data["base_component"], path=f"{path}.base_component"
            ),
            selected_flavors=parse_tuple(
                data["selected_flavors"],
                f"{path}.selected_flavors",
                VersionedContentRef.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class EffectiveSpecificationSet:
    base_specification_set: ContentIdentity
    flavor_specification_fragments: tuple[ContentIdentity, ...]
    flavor_set_lock: ContentIdentity
    diagnostics: tuple[str, ...] = ()

    SCHEMA: ClassVar[str] = EFFECTIVE_SPECIFICATION_SET_SCHEMA

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "base_specification_set": self.base_specification_set.to_dict(),
            "flavor_specification_fragments": [
                item.to_dict() for item in self.flavor_specification_fragments
            ],
            "flavor_set_lock": self.flavor_set_lock.to_dict(),
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "EffectiveSpecificationSet"
    ) -> EffectiveSpecificationSet:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "base_specification_set",
                    "flavor_specification_fragments",
                    "flavor_set_lock",
                    "diagnostics",
                }
            ),
        )
        return cls(
            base_specification_set=ContentIdentity.from_dict(
                data["base_specification_set"], path=f"{path}.base_specification_set"
            ),
            flavor_specification_fragments=parse_tuple(
                data["flavor_specification_fragments"],
                f"{path}.flavor_specification_fragments",
                ContentIdentity.from_dict,
            ),
            flavor_set_lock=ContentIdentity.from_dict(
                data["flavor_set_lock"], path=f"{path}.flavor_set_lock"
            ),
            diagnostics=string_tuple(data["diagnostics"], f"{path}.diagnostics"),
        )


@dataclass(frozen=True, slots=True)
class EffectiveRevision:
    """Derived immutable revision for one base, target, and exact Flavor set."""

    base_revision: ContentIdentity
    target_profile: ContentIdentity
    flavor_set_lock: ContentIdentity
    effective_specification_set: ContentIdentity
    contributions: tuple[ContributionReference, ...]
    resolver_policy: ContentIdentity

    SCHEMA: ClassVar[str] = EFFECTIVE_REVISION_SCHEMA

    def __post_init__(self) -> None:
        unique(
            tuple(item.contribution_id for item in self.contributions),
            "EffectiveRevision.contributions",
            "contribution IDs",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "base_revision": self.base_revision.to_dict(),
            "target_profile": self.target_profile.to_dict(),
            "flavor_set_lock": self.flavor_set_lock.to_dict(),
            "effective_specification_set": self.effective_specification_set.to_dict(),
            "contributions": [item.to_dict() for item in self.contributions],
            "resolver_policy": self.resolver_policy.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "EffectiveRevision"
    ) -> EffectiveRevision:
        value = _adapt_unreleased_v1_envelope(value, cls.SCHEMA)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "base_revision",
                    "target_profile",
                    "flavor_set_lock",
                    "effective_specification_set",
                    "contributions",
                    "resolver_policy",
                }
            ),
        )
        return cls(
            base_revision=ContentIdentity.from_dict(
                data["base_revision"], path=f"{path}.base_revision"
            ),
            target_profile=ContentIdentity.from_dict(
                data["target_profile"], path=f"{path}.target_profile"
            ),
            flavor_set_lock=ContentIdentity.from_dict(
                data["flavor_set_lock"], path=f"{path}.flavor_set_lock"
            ),
            effective_specification_set=ContentIdentity.from_dict(
                data["effective_specification_set"],
                path=f"{path}.effective_specification_set",
            ),
            contributions=parse_tuple(
                data["contributions"],
                f"{path}.contributions",
                ContributionReference.from_dict,
            ),
            resolver_policy=ContentIdentity.from_dict(
                data["resolver_policy"], path=f"{path}.resolver_policy"
            ),
        )


EffectiveComponentRevision = EffectiveRevision


__all__ = [
    "EFFECTIVE_REVISION_SCHEMA",
    "EFFECTIVE_SPECIFICATION_SET_SCHEMA",
    "FLAVOR_DEFINITION_SCHEMA",
    "FLAVOR_REVISION_SCHEMA",
    "FLAVOR_SET_LOCK_SCHEMA",
    "TARGET_PROFILE_SCHEMA",
    "CandidateStatus",
    "ContributionKind",
    "ContributionReference",
    "EffectiveComponentRevision",
    "EffectiveRevision",
    "EffectiveSpecificationSet",
    "FlavorAxis",
    "FlavorCandidateDecision",
    "FlavorCardinality",
    "FlavorDefinition",
    "FlavorRevision",
    "FlavorSetLock",
    "FlavorSlot",
    "MergeOperator",
    "TargetConstraint",
    "TargetProfile",
]

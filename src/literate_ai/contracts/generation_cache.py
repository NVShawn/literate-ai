"""Exact, non-authoritative cache contracts for accepted generated source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    optional_string,
    parse_tuple,
    string_value,
    unique,
)
from .blobs import BlobRef
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .sbom import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
)
from .source_index import SourceIntelligenceArtifact
from .standard_lifecycle import StandardSourceCacheMembershipDocument

LEGACY_SOURCE_DERIVATION_CACHE_KEY_SCHEMA = (
    f"{SCHEMA_PREFIX}source-derivation-cache-key"
)
SOURCE_DERIVATION_CACHE_KEY_SCHEMA = (
    "urn:literate-ai:schema:v2:source-derivation-cache-key"
)
ACCEPTED_SOURCE_LOOKUP_KEY_SCHEMA = (
    "urn:literate-ai:schema:v1:accepted-source-lookup-key"
)
LEGACY_ACCEPTED_SOURCE_DERIVATION_SCHEMA = f"{SCHEMA_PREFIX}accepted-source-derivation"
ACCEPTED_SOURCE_DERIVATION_SCHEMA = (
    "urn:literate-ai:schema:v2:accepted-source-derivation"
)
LEGACY_SOURCE_CACHE_TARGET_SCHEMA = f"{SCHEMA_PREFIX}source-cache-target"
SOURCE_CACHE_TARGET_SCHEMA = "urn:literate-ai:schema:v2:source-cache-target"
LEGACY_SOURCE_CACHE_CONFIGURATION_SCHEMA = f"{SCHEMA_PREFIX}source-cache-configuration"
SOURCE_CACHE_CONFIGURATION_SCHEMA = (
    "urn:literate-ai:schema:v2:source-cache-configuration"
)
LEGACY_SOURCE_CACHE_MODEL_BINDING_SCHEMA = f"{SCHEMA_PREFIX}source-cache-model-binding"
SOURCE_CACHE_MODEL_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:source-cache-model-binding"
)
LEGACY_CACHED_SOURCE_FILE_SCHEMA = f"{SCHEMA_PREFIX}cached-source-file"
CACHED_SOURCE_FILE_SCHEMA = "urn:literate-ai:schema:v2:cached-source-file"
LEGACY_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA = (
    f"{SCHEMA_PREFIX}accepted-source-cache-entry"
)
ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA = (
    "urn:literate-ai:schema:v2:accepted-source-cache-entry"
)
STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-accepted-source-cache-entry"
)
SOURCE_INTELLIGENCE_ATTACHMENT_SCHEMA = f"{SCHEMA_PREFIX}source-intelligence-attachment"
CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR = "cli-configured-default"

_PORTABLE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_FILESYSTEM_V2 = "filesystem-v2"


def source_cache_model_selector(
    configured_model: str | None,
    *,
    path: str = "model_selector",
) -> str:
    """Map omission to the portable cache sentinel without conflating user input."""

    if configured_model is None:
        return CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
    selector = string_value(configured_model, path)
    if selector == CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR:
        fail(
            path,
            f"{CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR!r} is reserved for an omitted "
            "coding-CLI model selection",
        )
    return selector


class SourceCacheMode(StrEnum):
    OFF = "off"
    READ_ONLY = "read-only"
    WRITE_ONLY = "write-only"
    READ_WRITE = "read-write"

    @property
    def can_read(self) -> bool:
        return self in {self.READ_ONLY, self.READ_WRITE}

    @property
    def can_write(self) -> bool:
        return self in {self.WRITE_ONLY, self.READ_WRITE}


class SourceCacheRootKind(StrEnum):
    PROJECT_RELATIVE = "project-relative"
    OPERATOR_BOUND = "operator-bound"


@dataclass(frozen=True, slots=True)
class SourceCacheModelBinding:
    """Provider plus visible selector, optionally strengthened by an exact revision."""

    provider_id: str
    model_selector: str
    exact_revision_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = SOURCE_CACHE_MODEL_BINDING_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.provider_id, "SourceCacheModelBinding.provider_id")
        string_value(self.model_selector, "SourceCacheModelBinding.model_selector")
        if self.exact_revision_identity is not None and not isinstance(
            self.exact_revision_identity, ContentIdentity
        ):
            fail(
                "SourceCacheModelBinding.exact_revision_identity",
                "must be a ContentIdentity or null",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "provider_id": self.provider_id,
            "model_selector": self.model_selector,
            "exact_revision_identity": (
                None
                if self.exact_revision_identity is None
                else self.exact_revision_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceCacheModelBinding"
    ) -> SourceCacheModelBinding:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_SOURCE_CACHE_MODEL_BINDING_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"provider_id", "model_selector", "exact_revision_identity"}
            ),
        )
        revision = data["exact_revision_identity"]
        return cls(
            provider_id=string_value(data["provider_id"], f"{path}.provider_id"),
            model_selector=string_value(
                data["model_selector"], f"{path}.model_selector"
            ),
            exact_revision_identity=(
                None
                if revision is None
                else ContentIdentity.from_dict(
                    revision, path=f"{path}.exact_revision_identity"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedSourceLookupKey:
    """Stable generation semantics used only for accepted-source reuse.

    Session, workspace, channel, nonce, and orchestration-envelope identities are
    intentionally absent.  They remain bound by the complete derivation key and the
    admitted generation custody.
    """

    recipe_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    coding_cli_tool_binding_identity: ContentIdentity
    model_binding: SourceCacheModelBinding
    source_semantics_identity: ContentIdentity

    SCHEMA: ClassVar[str] = ACCEPTED_SOURCE_LOOKUP_KEY_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "recipe_identity",
            "execution_plan_identity",
            "coding_cli_tool_binding_identity",
            "source_semantics_identity",
        ):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                fail(
                    f"AcceptedSourceLookupKey.{field_name}",
                    "must be a ContentIdentity",
                )
        if not isinstance(self.model_binding, SourceCacheModelBinding):
            fail(
                "AcceptedSourceLookupKey.model_binding",
                "must be a SourceCacheModelBinding",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "recipe_identity": self.recipe_identity.to_dict(),
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "coding_cli_tool_binding_identity": (
                self.coding_cli_tool_binding_identity.to_dict()
            ),
            "model_binding": self.model_binding.to_dict(),
            "source_semantics_identity": self.source_semantics_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "AcceptedSourceLookupKey"
    ) -> AcceptedSourceLookupKey:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "recipe_identity",
                    "execution_plan_identity",
                    "coding_cli_tool_binding_identity",
                    "model_binding",
                    "source_semantics_identity",
                }
            ),
        )
        return cls(
            recipe_identity=ContentIdentity.from_dict(
                data["recipe_identity"], path=f"{path}.recipe_identity"
            ),
            execution_plan_identity=ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            coding_cli_tool_binding_identity=ContentIdentity.from_dict(
                data["coding_cli_tool_binding_identity"],
                path=f"{path}.coding_cli_tool_binding_identity",
            ),
            model_binding=SourceCacheModelBinding.from_dict(
                data["model_binding"], path=f"{path}.model_binding"
            ),
            source_semantics_identity=ContentIdentity.from_dict(
                data["source_semantics_identity"],
                path=f"{path}.source_semantics_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceDerivationCacheKey:
    """Everything known before one coding CLI may be safely skipped.

    ``coding_cli_tool_binding_identity`` is the portable CLI name, launcher digest, and
    isolation contract. Machine-local operational selection paths are excluded. Model
    binding records the provider-visible selector honestly; an exact revision is used
    only when the provider actually supplies one.
    """

    recipe_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    coding_cli_tool_binding_identity: ContentIdentity
    model_binding: SourceCacheModelBinding
    request_identity: ContentIdentity
    source_semantics_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = SOURCE_DERIVATION_CACHE_KEY_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "recipe_identity",
            "execution_plan_identity",
            "coding_cli_tool_binding_identity",
            "request_identity",
        ):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                fail(
                    f"SourceDerivationCacheKey.{field_name}",
                    "must be a ContentIdentity",
                )
        if not isinstance(self.model_binding, SourceCacheModelBinding):
            fail(
                "SourceDerivationCacheKey.model_binding",
                "must be a SourceCacheModelBinding",
            )
        if self.source_semantics_identity is not None and not isinstance(
            self.source_semantics_identity, ContentIdentity
        ):
            fail(
                "SourceDerivationCacheKey.source_semantics_identity",
                "must be a ContentIdentity or null",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def accepted_source_lookup(self) -> AcceptedSourceLookupKey:
        """Return the reusable identity without transaction-local request custody."""

        return AcceptedSourceLookupKey(
            recipe_identity=self.recipe_identity,
            execution_plan_identity=self.execution_plan_identity,
            coding_cli_tool_binding_identity=self.coding_cli_tool_binding_identity,
            model_binding=self.model_binding,
            source_semantics_identity=(
                self.request_identity
                if self.source_semantics_identity is None
                else self.source_semantics_identity
            ),
        )

    @property
    def accepted_source_lookup_identity(self) -> ContentIdentity:
        return self.accepted_source_lookup.identity

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "recipe_identity": self.recipe_identity.to_dict(),
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "coding_cli_tool_binding_identity": (
                self.coding_cli_tool_binding_identity.to_dict()
            ),
            "model_binding": self.model_binding.to_dict(),
            "request_identity": self.request_identity.to_dict(),
            "source_semantics_identity": (
                None
                if self.source_semantics_identity is None
                else self.source_semantics_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceDerivationCacheKey"
    ) -> SourceDerivationCacheKey:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_SOURCE_DERIVATION_CACHE_KEY_SCHEMA
        ):
            value = {
                **value,
                "schema": cls.SCHEMA,
                "source_semantics_identity": None,
            }
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "recipe_identity",
                    "execution_plan_identity",
                    "coding_cli_tool_binding_identity",
                    "model_binding",
                    "request_identity",
                    "source_semantics_identity",
                }
            ),
        )
        return cls(
            recipe_identity=ContentIdentity.from_dict(
                data["recipe_identity"], path=f"{path}.recipe_identity"
            ),
            execution_plan_identity=ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            coding_cli_tool_binding_identity=ContentIdentity.from_dict(
                data["coding_cli_tool_binding_identity"],
                path=f"{path}.coding_cli_tool_binding_identity",
            ),
            model_binding=SourceCacheModelBinding.from_dict(
                data["model_binding"], path=f"{path}.model_binding"
            ),
            request_identity=ContentIdentity.from_dict(
                data["request_identity"], path=f"{path}.request_identity"
            ),
            source_semantics_identity=(
                None
                if data["source_semantics_identity"] is None
                else ContentIdentity.from_dict(
                    data["source_semantics_identity"],
                    path=f"{path}.source_semantics_identity",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedSourceDerivation:
    """One immutable generated tree admitted after the full guarded lifecycle."""

    cache_key: SourceDerivationCacheKey
    component_lock_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    managed_sbom_graph_identity: ContentIdentity
    source_sbom_identity: ContentIdentity
    resolved_sbom_identity: ContentIdentity
    source_sbom_binding: CycloneDxBomBinding
    resolved_sbom_binding: CycloneDxBomBinding
    generated_test_suite_identity: ContentIdentity
    build_evidence_identity: ContentIdentity
    test_evidence_identity: ContentIdentity
    acceptance_identity: ContentIdentity
    provenance_identity: ContentIdentity

    SCHEMA: ClassVar[str] = ACCEPTED_SOURCE_DERIVATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, SourceDerivationCacheKey):
            fail(
                "AcceptedSourceDerivation.cache_key",
                "must be a SourceDerivationCacheKey",
            )
        for field_name in (
            "component_lock_identity",
            "source_tree_identity",
            "managed_sbom_graph_identity",
            "source_sbom_identity",
            "resolved_sbom_identity",
            "generated_test_suite_identity",
            "build_evidence_identity",
            "test_evidence_identity",
            "acceptance_identity",
            "provenance_identity",
        ):
            if not isinstance(getattr(self, field_name), ContentIdentity):
                fail(
                    f"AcceptedSourceDerivation.{field_name}",
                    "must be a ContentIdentity",
                )
        if not isinstance(self.source_sbom_binding, CycloneDxBomBinding):
            fail(
                "AcceptedSourceDerivation.source_sbom_binding",
                "must be a CycloneDxBomBinding",
            )
        if not isinstance(self.resolved_sbom_binding, CycloneDxBomBinding):
            fail(
                "AcceptedSourceDerivation.resolved_sbom_binding",
                "must be a CycloneDxBomBinding",
            )
        if (
            self.source_sbom_binding.lifecycle is not CycloneDxLifecycle.SOURCE
            or self.resolved_sbom_binding.lifecycle is not CycloneDxLifecycle.RESOLVED
            or self.source_sbom_binding.bom_identity != self.source_sbom_identity
            or self.resolved_sbom_binding.bom_identity != self.resolved_sbom_identity
            or self.source_sbom_binding.managed_graph_identity
            != self.managed_sbom_graph_identity
            or self.resolved_sbom_binding.managed_graph_identity
            != self.managed_sbom_graph_identity
            or self.source_sbom_binding.resolved_graph_identity
            != self.resolved_sbom_binding.resolved_graph_identity
            or self.source_sbom_binding.resolved_graph_identity
            != self.component_lock_identity
            or self.resolved_sbom_binding.source_bom_identity
            != self.source_sbom_binding.bom_identity
        ):
            fail(
                "AcceptedSourceDerivation.source_sbom_binding",
                "source and resolved BOM bindings must form the exact accepted "
                "transition",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "cache_key": self.cache_key.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "managed_sbom_graph_identity": self.managed_sbom_graph_identity.to_dict(),
            "source_sbom_identity": self.source_sbom_identity.to_dict(),
            "resolved_sbom_identity": self.resolved_sbom_identity.to_dict(),
            "source_sbom_binding": self.source_sbom_binding.to_dict(),
            "resolved_sbom_binding": self.resolved_sbom_binding.to_dict(),
            "generated_test_suite_identity": (
                self.generated_test_suite_identity.to_dict()
            ),
            "build_evidence_identity": self.build_evidence_identity.to_dict(),
            "test_evidence_identity": self.test_evidence_identity.to_dict(),
            "acceptance_identity": self.acceptance_identity.to_dict(),
            "provenance_identity": self.provenance_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "AcceptedSourceDerivation",
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> AcceptedSourceDerivation:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_ACCEPTED_SOURCE_DERIVATION_SCHEMA
        ):
            if not isinstance(legacy_component_lock_identity, ContentIdentity):
                fail(
                    path,
                    "legacy derivation requires an exact Component lock migration "
                    "identity",
                )
            value = dict(value)
            value.pop("source_index_identity", None)
            value["component_lock_identity"] = legacy_component_lock_identity.to_dict()
            value["schema"] = cls.SCHEMA
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "cache_key",
                    "component_lock_identity",
                    "source_tree_identity",
                    "managed_sbom_graph_identity",
                    "source_sbom_identity",
                    "resolved_sbom_identity",
                    "source_sbom_binding",
                    "resolved_sbom_binding",
                    "generated_test_suite_identity",
                    "build_evidence_identity",
                    "test_evidence_identity",
                    "acceptance_identity",
                    "provenance_identity",
                }
            ),
        )
        return cls(
            cache_key=SourceDerivationCacheKey.from_dict(
                data["cache_key"],
                path=f"{path}.cache_key",
            ),
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path=f"{path}.component_lock_identity",
            ),
            source_tree_identity=ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            managed_sbom_graph_identity=ContentIdentity.from_dict(
                data["managed_sbom_graph_identity"],
                path=f"{path}.managed_sbom_graph_identity",
            ),
            source_sbom_identity=ContentIdentity.from_dict(
                data["source_sbom_identity"], path=f"{path}.source_sbom_identity"
            ),
            resolved_sbom_identity=ContentIdentity.from_dict(
                data["resolved_sbom_identity"],
                path=f"{path}.resolved_sbom_identity",
            ),
            source_sbom_binding=CycloneDxBomBinding.from_dict(
                data["source_sbom_binding"], path=f"{path}.source_sbom_binding"
            ),
            resolved_sbom_binding=CycloneDxBomBinding.from_dict(
                data["resolved_sbom_binding"], path=f"{path}.resolved_sbom_binding"
            ),
            generated_test_suite_identity=ContentIdentity.from_dict(
                data["generated_test_suite_identity"],
                path=f"{path}.generated_test_suite_identity",
            ),
            build_evidence_identity=ContentIdentity.from_dict(
                data["build_evidence_identity"],
                path=f"{path}.build_evidence_identity",
            ),
            test_evidence_identity=ContentIdentity.from_dict(
                data["test_evidence_identity"], path=f"{path}.test_evidence_identity"
            ),
            acceptance_identity=ContentIdentity.from_dict(
                data["acceptance_identity"], path=f"{path}.acceptance_identity"
            ),
            provenance_identity=ContentIdentity.from_dict(
                data["provenance_identity"], path=f"{path}.provenance_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceCacheTarget:
    """One configured cache target without embedding an operator filesystem path."""

    target_id: str
    root_kind: SourceCacheRootKind
    root_reference: str
    format: str = _FILESYSTEM_V2

    SCHEMA: ClassVar[str] = SOURCE_CACHE_TARGET_SCHEMA

    def __post_init__(self) -> None:
        target_id = string_value(self.target_id, "SourceCacheTarget.target_id")
        if _PORTABLE_ID.fullmatch(target_id) is None:
            fail("SourceCacheTarget.target_id", "must be a portable identifier")
        if not isinstance(self.root_kind, SourceCacheRootKind):
            fail("SourceCacheTarget.root_kind", "must be a SourceCacheRootKind")
        reference = string_value(
            self.root_reference, "SourceCacheTarget.root_reference"
        )
        if self.root_kind is SourceCacheRootKind.PROJECT_RELATIVE:
            try:
                canonical_relative_posix_path(
                    reference, label="SourceCacheTarget.root_reference"
                )
            except (TypeError, ValueError):
                fail(
                    "SourceCacheTarget.root_reference",
                    "must be a canonical portable relative path",
                )
        elif _PORTABLE_ID.fullmatch(reference) is None:
            fail(
                "SourceCacheTarget.root_reference",
                "operator-bound roots must use a portable binding identifier",
            )
        if self.format not in {"filesystem-v1", _FILESYSTEM_V2}:
            fail(
                "SourceCacheTarget.format",
                "must select the current layout or its read-only v1 compatibility "
                "reader",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target_id": self.target_id,
            "format": self.format,
            "root_kind": self.root_kind.value,
            "root_reference": self.root_reference,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceCacheTarget"
    ) -> SourceCacheTarget:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_SOURCE_CACHE_TARGET_SCHEMA
        ):
            if value.get("format") != "filesystem-v1":
                fail(path, "legacy target must use the frozen filesystem-v1 format")
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target_id", "format", "root_kind", "root_reference"}),
        )
        return cls(
            target_id=string_value(data["target_id"], f"{path}.target_id"),
            format=string_value(data["format"], f"{path}.format"),
            root_kind=enum_value(
                SourceCacheRootKind, data["root_kind"], f"{path}.root_kind"
            ),
            root_reference=string_value(
                data["root_reference"], f"{path}.root_reference"
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceCacheConfiguration:
    """Mode, ordered read targets, and the sole explicit write target."""

    mode: SourceCacheMode
    targets: tuple[SourceCacheTarget, ...]
    write_target_id: str | None = None
    require_unique: bool = True

    SCHEMA: ClassVar[str] = SOURCE_CACHE_CONFIGURATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SourceCacheMode):
            fail("SourceCacheConfiguration.mode", "must be a SourceCacheMode")
        for index, target in enumerate(self.targets):
            if not isinstance(target, SourceCacheTarget):
                fail(
                    f"SourceCacheConfiguration.targets[{index}]",
                    "must be a SourceCacheTarget",
                )
        target_ids = tuple(target.target_id for target in self.targets)
        unique(target_ids, "SourceCacheConfiguration.targets", "target IDs")
        bool_value(self.require_unique, "SourceCacheConfiguration.require_unique")
        if self.mode is not SourceCacheMode.OFF and not self.targets:
            fail(
                "SourceCacheConfiguration.targets",
                "enabled cache modes require at least one target",
            )
        if self.mode.can_write:
            if self.write_target_id is None:
                fail(
                    "SourceCacheConfiguration.write_target_id",
                    "write modes require exactly one target ID",
                )
            if self.write_target_id not in target_ids:
                fail(
                    "SourceCacheConfiguration.write_target_id",
                    "must name one configured target",
                )
        elif self.write_target_id is not None:
            fail(
                "SourceCacheConfiguration.write_target_id",
                "non-writing modes must not select a write target",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "mode": self.mode.value,
            "targets": [target.to_dict() for target in self.targets],
            "write_target_id": self.write_target_id,
            "require_unique": self.require_unique,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceCacheConfiguration"
    ) -> SourceCacheConfiguration:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_SOURCE_CACHE_CONFIGURATION_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"mode", "targets", "write_target_id", "require_unique"}
            ),
        )
        return cls(
            mode=enum_value(SourceCacheMode, data["mode"], f"{path}.mode"),
            targets=parse_tuple(
                data["targets"], f"{path}.targets", SourceCacheTarget.from_dict
            ),
            write_target_id=optional_string(
                data["write_target_id"], f"{path}.write_target_id"
            ),
            require_unique=bool_value(data["require_unique"], f"{path}.require_unique"),
        )


@dataclass(frozen=True, slots=True)
class CachedSourceFile:
    """One portable generated-source path and its immutable bytes."""

    path: str
    blob: BlobRef

    SCHEMA: ClassVar[str] = CACHED_SOURCE_FILE_SCHEMA

    def __post_init__(self) -> None:
        try:
            canonical_relative_posix_path(self.path, label="CachedSourceFile.path")
        except (TypeError, ValueError):
            fail(
                "CachedSourceFile.path",
                "must be one canonical portable relative path",
            )
        if not isinstance(self.blob, BlobRef):
            fail("CachedSourceFile.blob", "must be a BlobRef")

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.SCHEMA, "path": self.path, "blob": self.blob.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CachedSourceFile"
    ) -> CachedSourceFile:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_CACHED_SOURCE_FILE_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"path", "blob"}),
        )
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class SourceIntelligenceAttachment:
    """A rebuildable provider artifact attached to, but not identifying, source.

    The accepted source entry deliberately does not contain this object. Any number of
    provider/runtime versions may describe one immutable source tree without creating a
    second generation result or making an exact cache key ambiguous.
    """

    source_tree_identity: ContentIdentity
    intelligence: SourceIntelligenceArtifact
    artifact: BlobRef

    SCHEMA: ClassVar[str] = SOURCE_INTELLIGENCE_ATTACHMENT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.source_tree_identity, ContentIdentity):
            fail(
                "SourceIntelligenceAttachment.source_tree_identity",
                "must be a ContentIdentity",
            )
        if not isinstance(self.intelligence, SourceIntelligenceArtifact):
            fail(
                "SourceIntelligenceAttachment.intelligence",
                "must be a SourceIntelligenceArtifact",
            )
        if not isinstance(self.artifact, BlobRef):
            fail("SourceIntelligenceAttachment.artifact", "must be a BlobRef")
        if self.intelligence.source_tree_identity != self.source_tree_identity.uri:
            fail(
                "SourceIntelligenceAttachment.intelligence",
                "must describe the attached source tree",
            )
        if self.intelligence.artifact_identity != self.artifact.identity:
            fail(
                "SourceIntelligenceAttachment.artifact",
                "must contain the exact provider artifact named by its evidence",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "intelligence": self.intelligence.to_dict(),
            "artifact": self.artifact.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceIntelligenceAttachment"
    ) -> SourceIntelligenceAttachment:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"source_tree_identity", "intelligence", "artifact"}),
        )
        return cls(
            source_tree_identity=ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            intelligence=SourceIntelligenceArtifact.from_dict(data["intelligence"]),
            artifact=BlobRef.from_dict(data["artifact"], path=f"{path}.artifact"),
        )


@dataclass(frozen=True, slots=True)
class AcceptedSourceCacheEntry:
    """Complete immutable bytes and evidence for one accepted derivation."""

    derivation: AcceptedSourceDerivation
    source_files: tuple[CachedSourceFile, ...]
    managed_sbom_graph: CycloneDxManagedGraph
    source_sbom: BlobRef
    resolved_sbom: BlobRef
    generated_test_suite: BlobRef
    build_evidence: BlobRef
    test_evidence: BlobRef
    acceptance_evidence: BlobRef
    provenance_evidence: BlobRef
    repository_resolutions: tuple[CycloneDxRepositorySourceResolution, ...] = ()

    SCHEMA: ClassVar[str] = ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.derivation, AcceptedSourceDerivation):
            fail(
                "AcceptedSourceCacheEntry.derivation",
                "must be an AcceptedSourceDerivation",
            )
        if not self.source_files:
            fail("AcceptedSourceCacheEntry.source_files", "must not be empty")
        for index, source_file in enumerate(self.source_files):
            if not isinstance(source_file, CachedSourceFile):
                fail(
                    f"AcceptedSourceCacheEntry.source_files[{index}]",
                    "must be a CachedSourceFile",
                )
        paths = tuple(source_file.path for source_file in self.source_files)
        try:
            canonical_relative_posix_paths(
                paths,
                label="AcceptedSourceCacheEntry.source_files",
            )
        except (TypeError, ValueError):
            fail(
                "AcceptedSourceCacheEntry.source_files",
                "must form one canonical cross-platform file tree",
            )
        if paths != tuple(sorted(paths)):
            fail(
                "AcceptedSourceCacheEntry.source_files",
                "must use canonical path order",
            )
        if not isinstance(self.managed_sbom_graph, CycloneDxManagedGraph):
            fail(
                "AcceptedSourceCacheEntry.managed_sbom_graph",
                "must be a CycloneDxManagedGraph",
            )
        for field_name in (
            "source_sbom",
            "resolved_sbom",
            "generated_test_suite",
            "build_evidence",
            "test_evidence",
            "acceptance_evidence",
            "provenance_evidence",
        ):
            if not isinstance(getattr(self, field_name), BlobRef):
                fail(f"AcceptedSourceCacheEntry.{field_name}", "must be a BlobRef")
        if len(self.repository_resolutions) > 256:
            fail(
                "AcceptedSourceCacheEntry.repository_resolutions",
                "must contain at most 256 repository-source resolutions",
            )
        for index, resolution in enumerate(self.repository_resolutions):
            if not isinstance(resolution, CycloneDxRepositorySourceResolution):
                fail(
                    f"AcceptedSourceCacheEntry.repository_resolutions[{index}]",
                    "must be a CycloneDxRepositorySourceResolution",
                )
        unique(
            tuple(
                resolution.dependency_identity.uri
                for resolution in self.repository_resolutions
            ),
            "AcceptedSourceCacheEntry.repository_resolutions",
            "dependency identities",
        )
        if self.managed_sbom_graph.identity != (
            self.derivation.managed_sbom_graph_identity
        ):
            fail(
                "AcceptedSourceCacheEntry.managed_sbom_graph",
                "must bind the derivation managed dependency graph",
            )
        if (
            self.derivation.component_lock_identity
            != self.managed_sbom_graph.resolved_graph_identity
            or self.derivation.source_sbom_binding.resolved_graph_identity
            != self.managed_sbom_graph.resolved_graph_identity
            or self.derivation.resolved_sbom_binding.resolved_graph_identity
            != self.managed_sbom_graph.resolved_graph_identity
        ):
            fail(
                "AcceptedSourceCacheEntry.managed_sbom_graph",
                "must bind the exact Component lock named by the derivation and both "
                "BOMs",
            )
        source_sbom_files = tuple(
            item
            for item in self.source_files
            if item.path == CYCLONEDX_SOURCE_SBOM_PATH
        )
        if len(source_sbom_files) != 1 or source_sbom_files[0].blob != self.source_sbom:
            fail(
                "AcceptedSourceCacheEntry.source_sbom",
                "must be the exact SBOM stored in the generated source tree",
            )
        links = (
            (
                "source_sbom",
                self.source_sbom.identity,
                self.derivation.source_sbom_identity.uri,
            ),
            (
                "resolved_sbom",
                self.resolved_sbom.identity,
                self.derivation.resolved_sbom_identity.uri,
            ),
            (
                "generated_test_suite",
                self.generated_test_suite.identity,
                self.derivation.generated_test_suite_identity.uri,
            ),
            (
                "build_evidence",
                self.build_evidence.identity,
                self.derivation.build_evidence_identity.uri,
            ),
            (
                "test_evidence",
                self.test_evidence.identity,
                self.derivation.test_evidence_identity.uri,
            ),
            (
                "acceptance_evidence",
                self.acceptance_evidence.identity,
                self.derivation.acceptance_identity.uri,
            ),
            (
                "provenance_evidence",
                self.provenance_evidence.identity,
                self.derivation.provenance_identity.uri,
            ),
        )
        for field_name, actual, expected in links:
            if actual != expected:
                fail(
                    f"AcceptedSourceCacheEntry.{field_name}",
                    "does not match its bound evidence identity",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def source_tree_identity(self) -> ContentIdentity:
        """Match ``StandardSourceAdmissionCacheEntry``'s uniform accessor."""

        return self.derivation.source_tree_identity

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "derivation": self.derivation.to_dict(),
            "source_files": [
                source_file.to_dict() for source_file in self.source_files
            ],
            "managed_sbom_graph": self.managed_sbom_graph.to_dict(),
            "source_sbom": self.source_sbom.to_dict(),
            "resolved_sbom": self.resolved_sbom.to_dict(),
            "generated_test_suite": self.generated_test_suite.to_dict(),
            "build_evidence": self.build_evidence.to_dict(),
            "test_evidence": self.test_evidence.to_dict(),
            "acceptance_evidence": self.acceptance_evidence.to_dict(),
            "provenance_evidence": self.provenance_evidence.to_dict(),
            "repository_resolutions": [
                resolution.to_dict() for resolution in self.repository_resolutions
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "AcceptedSourceCacheEntry",
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> AcceptedSourceCacheEntry:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA
        ):
            legacy = dict(value)
            raw_derivation = legacy.get("derivation")
            raw_intelligence = legacy.pop("source_index", None)
            raw_artifact = legacy.pop("codegraph_database", None)
            if not isinstance(raw_derivation, dict):
                fail(f"{path}.derivation", "legacy derivation must be an object")
            if not isinstance(raw_intelligence, dict):
                fail(f"{path}.source_index", "legacy source index must be an object")
            intelligence = SourceIntelligenceArtifact.from_dict(raw_intelligence)
            artifact = BlobRef.from_dict(
                raw_artifact, path=f"{path}.codegraph_database"
            )
            legacy_index_identity = ContentIdentity.from_dict(
                raw_derivation.get("source_index_identity"),
                path=f"{path}.derivation.source_index_identity",
            )
            if (
                legacy_index_identity.uri != raw_intelligence.get("index_identity")
                or artifact.identity != intelligence.artifact_identity
                or intelligence.source_tree_identity
                != ContentIdentity.from_dict(
                    raw_derivation.get("source_tree_identity"),
                    path=f"{path}.derivation.source_tree_identity",
                ).uri
            ):
                fail(path, "legacy source-index attachment is internally inconsistent")
            legacy["schema"] = cls.SCHEMA
            value = legacy
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "derivation",
                    "source_files",
                    "managed_sbom_graph",
                    "source_sbom",
                    "resolved_sbom",
                    "generated_test_suite",
                    "build_evidence",
                    "test_evidence",
                    "acceptance_evidence",
                    "provenance_evidence",
                    "repository_resolutions",
                }
            ),
        )
        return cls(
            derivation=AcceptedSourceDerivation.from_dict(
                data["derivation"],
                path=f"{path}.derivation",
                legacy_component_lock_identity=legacy_component_lock_identity,
            ),
            source_files=parse_tuple(
                data["source_files"],
                f"{path}.source_files",
                CachedSourceFile.from_dict,
            ),
            managed_sbom_graph=CycloneDxManagedGraph.from_dict(
                data["managed_sbom_graph"], path=f"{path}.managed_sbom_graph"
            ),
            source_sbom=BlobRef.from_dict(
                data["source_sbom"], path=f"{path}.source_sbom"
            ),
            resolved_sbom=BlobRef.from_dict(
                data["resolved_sbom"], path=f"{path}.resolved_sbom"
            ),
            generated_test_suite=BlobRef.from_dict(
                data["generated_test_suite"], path=f"{path}.generated_test_suite"
            ),
            build_evidence=BlobRef.from_dict(
                data["build_evidence"], path=f"{path}.build_evidence"
            ),
            test_evidence=BlobRef.from_dict(
                data["test_evidence"], path=f"{path}.test_evidence"
            ),
            acceptance_evidence=BlobRef.from_dict(
                data["acceptance_evidence"], path=f"{path}.acceptance_evidence"
            ),
            provenance_evidence=BlobRef.from_dict(
                data["provenance_evidence"], path=f"{path}.provenance_evidence"
            ),
            repository_resolutions=parse_tuple(
                data["repository_resolutions"],
                f"{path}.repository_resolutions",
                CycloneDxRepositorySourceResolution.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StandardAcceptedSourceCacheEntry(AcceptedSourceCacheEntry):
    """Accepted source whose complete Standard resume evidence survives storage."""

    standard_source_membership: StandardSourceCacheMembershipDocument

    SCHEMA: ClassVar[str] = STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        AcceptedSourceCacheEntry.__post_init__(self)
        membership = self.standard_source_membership
        if not isinstance(membership, StandardSourceCacheMembershipDocument):
            fail(
                "StandardAcceptedSourceCacheEntry.standard_source_membership",
                "must be a StandardSourceCacheMembershipDocument",
            )
        derivation = self.derivation
        output = membership.generation.output
        candidate = output.candidate
        key = derivation.cache_key
        bindings = (
            (
                candidate.tree_identity,
                derivation.source_tree_identity,
                "source tree",
            ),
            (
                candidate.source_bom_identity,
                derivation.source_sbom_identity,
                "source SBOM",
            ),
            (
                candidate.generated_test_suite_identity,
                derivation.generated_test_suite_identity,
                "generated test suite",
            ),
            (
                candidate.recipe_identity,
                key.recipe_identity,
                "recipe",
            ),
            (
                output.provenance_identity,
                derivation.provenance_identity,
                "generation provenance",
            ),
            (
                output.provenance.component_lock_identity,
                derivation.component_lock_identity,
                "Component lock",
            ),
            (
                membership.acceptance_identity,
                derivation.acceptance_identity,
                "acceptance evidence",
            ),
        )
        for actual, expected, label in bindings:
            if actual != expected:
                fail(
                    "StandardAcceptedSourceCacheEntry.standard_source_membership",
                    f"must bind the accepted derivation's exact {label}",
                )

    def to_dict(self) -> dict[str, object]:
        document = AcceptedSourceCacheEntry.to_dict(self)
        document["standard_source_membership"] = (
            self.standard_source_membership.to_dict()
        )
        return document

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardAcceptedSourceCacheEntry",
    ) -> StandardAcceptedSourceCacheEntry:
        if not isinstance(value, dict) or value.get("schema") != cls.SCHEMA:
            fail(path, f"schema must be exactly {cls.SCHEMA!r}")
        legacy = dict(value)
        membership = legacy.pop("standard_source_membership", None)
        legacy["schema"] = ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA
        base = AcceptedSourceCacheEntry.from_dict(legacy, path=path)
        return cls(
            derivation=base.derivation,
            source_files=base.source_files,
            managed_sbom_graph=base.managed_sbom_graph,
            source_sbom=base.source_sbom,
            resolved_sbom=base.resolved_sbom,
            generated_test_suite=base.generated_test_suite,
            build_evidence=base.build_evidence,
            test_evidence=base.test_evidence,
            acceptance_evidence=base.acceptance_evidence,
            provenance_evidence=base.provenance_evidence,
            repository_resolutions=base.repository_resolutions,
            standard_source_membership=(
                StandardSourceCacheMembershipDocument.from_dict(
                    membership, path=f"{path}.standard_source_membership"
                )
            ),
        )


def reusable_source_derivations(
    key: SourceDerivationCacheKey,
    records: tuple[AcceptedSourceDerivation, ...],
    *,
    force_regeneration: bool,
) -> tuple[AcceptedSourceDerivation, ...]:
    """Return all exact accepted candidates, or none for forced regeneration."""

    if not isinstance(key, SourceDerivationCacheKey):
        fail("reusable_source_derivations.key", "must be a SourceDerivationCacheKey")
    bool_value(force_regeneration, "reusable_source_derivations.force_regeneration")
    if force_regeneration:
        return ()
    for index, record in enumerate(records):
        if not isinstance(record, AcceptedSourceDerivation):
            fail(
                f"reusable_source_derivations.records[{index}]",
                "must be an AcceptedSourceDerivation",
            )
    lookup_identity = key.accepted_source_lookup_identity
    matches = tuple(
        record
        for record in records
        if record.cache_key.accepted_source_lookup_identity == lookup_identity
    )
    return tuple(sorted(matches, key=lambda record: record.identity.uri))


__all__ = [
    "ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA",
    "ACCEPTED_SOURCE_DERIVATION_SCHEMA",
    "ACCEPTED_SOURCE_LOOKUP_KEY_SCHEMA",
    "CACHED_SOURCE_FILE_SCHEMA",
    "CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR",
    "SOURCE_CACHE_CONFIGURATION_SCHEMA",
    "SOURCE_CACHE_MODEL_BINDING_SCHEMA",
    "SOURCE_CACHE_TARGET_SCHEMA",
    "SOURCE_INTELLIGENCE_ATTACHMENT_SCHEMA",
    "STANDARD_ACCEPTED_SOURCE_CACHE_ENTRY_SCHEMA",
    "SOURCE_DERIVATION_CACHE_KEY_SCHEMA",
    "AcceptedSourceCacheEntry",
    "AcceptedSourceDerivation",
    "AcceptedSourceLookupKey",
    "CachedSourceFile",
    "SourceCacheConfiguration",
    "SourceCacheMode",
    "SourceCacheModelBinding",
    "SourceCacheRootKind",
    "SourceCacheTarget",
    "SourceDerivationCacheKey",
    "SourceIntelligenceAttachment",
    "StandardAcceptedSourceCacheEntry",
    "reusable_source_derivations",
    "source_cache_model_selector",
]

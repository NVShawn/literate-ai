"""Typed source-cache negotiation for one project rebuild.

The project lifecycle driver is content-pinned project TCB.  It may compute the
operational derivation keys and materialize an untrusted cache candidate, but it may
not publish into a configured cache.  The outer ``litai rebuild`` process owns this
authorization envelope and independently validates the returned decision and any
publication offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    parse_tuple,
    string_value,
    unique,
)
from .generation_cache import (
    AcceptedSourceCacheEntry,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceDerivationCacheKey,
    SourceIntelligenceAttachment,
)
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)
from .paths import canonical_relative_posix_path
from .testing import ProjectTestReceipt

LEGACY_REBUILD_SOURCE_CACHE_CONTROL_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-control"
)
REBUILD_SOURCE_CACHE_CONTROL_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-control"
)
REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA = "literate-ai/rebuild-derivation-plan@2"
REBUILD_PROJECT_AUTHORITY_SCHEMA = "literate-ai/rebuild-project-authority@1"
LEGACY_REBUILD_SOURCE_CACHE_DERIVATION_MANIFEST_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-derivation-manifest"
)
REBUILD_SOURCE_CACHE_DERIVATION_MANIFEST_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-derivation-manifest"
)
LEGACY_REBUILD_SOURCE_CACHE_DECISION_ITEM_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-decision-item"
)
REBUILD_SOURCE_CACHE_DECISION_ITEM_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-decision-item"
)
LEGACY_REBUILD_SOURCE_CACHE_DECISION_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-decision"
)
REBUILD_SOURCE_CACHE_DECISION_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-decision"
)
LEGACY_REBUILD_SOURCE_CACHE_OPERATOR_ROOT_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-operator-root"
)
REBUILD_SOURCE_CACHE_OPERATOR_ROOT_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-operator-root"
)
LEGACY_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBER_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-publication-member"
)
REBUILD_SOURCE_CACHE_PUBLICATION_MEMBER_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-publication-member"
)
LEGACY_REBUILD_SOURCE_CACHE_PUBLICATION_OFFER_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-publication-offer"
)
REBUILD_SOURCE_CACHE_PUBLICATION_OFFER_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-publication-offer"
)
REBUILD_SOURCE_CACHE_PUBLICATION_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-publication-result"
)
LEGACY_REBUILD_SOURCE_CACHE_LIFECYCLE_MEMBER_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-lifecycle-member"
)
REBUILD_SOURCE_CACHE_LIFECYCLE_MEMBER_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-lifecycle-member"
)
LEGACY_REBUILD_SOURCE_CACHE_LIFECYCLE_BINDING_SCHEMA = (
    f"{SCHEMA_PREFIX}rebuild-source-cache-lifecycle-binding"
)
REBUILD_SOURCE_CACHE_LIFECYCLE_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-lifecycle-binding"
)
REBUILD_SOURCE_CACHE_LIFECYCLE_EVIDENCE_SET_SCHEMA = (
    "urn:literate-ai:schema:v2:rebuild-source-cache-lifecycle-evidence-set"
)

MAX_REBUILD_SOURCE_CACHE_TARGETS = 32
MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES = 64
MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS = 64
MAX_REBUILD_SOURCE_CACHE_CANDIDATES_PER_ITEM = 64
MAX_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBERS = 64
MAX_REBUILD_COMPONENT_LOCKS = 4096


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _optional_identity(value: Any, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return ContentIdentity.from_dict(value, path=path)


def _component_lock_identities(
    values: tuple[ContentIdentity, ...], path: str
) -> tuple[ContentIdentity, ...]:
    if not values:
        fail(path, "must contain every resolved Component lock")
    if len(values) > MAX_REBUILD_COMPONENT_LOCKS:
        fail(path, f"must contain at most {MAX_REBUILD_COMPONENT_LOCKS} identities")
    for index, identity in enumerate(values):
        _identity(identity, f"{path}[{index}]")
    uris = tuple(identity.uri for identity in values)
    unique(uris, path, "Component lock identities")
    if uris != tuple(sorted(uris)):
        fail(path, "must use canonical identity order")
    return values


def rebuild_project_authority_identity(
    validated_project_authority_identity: ContentIdentity,
    component_lock_identities: tuple[ContentIdentity, ...],
) -> ContentIdentity:
    """Bind validated project authority to one exact planned Component-lock set."""

    base = _identity(
        validated_project_authority_identity,
        "rebuild_project_authority_identity.validated_project_authority_identity",
    )
    locks = _component_lock_identities(
        component_lock_identities,
        "rebuild_project_authority_identity.component_lock_identities",
    )
    return canonical_identity(
        {
            "schema": REBUILD_PROJECT_AUTHORITY_SCHEMA,
            "validated_project_authority_identity": base.uri,
            "component_lock_identities": [identity.uri for identity in locks],
        }
    )


def _configuration_identity(
    configuration: SourceCacheConfiguration | None,
) -> ContentIdentity:
    material: object = (
        {
            "schema": f"{SCHEMA_PREFIX}source-cache-unconfigured",
            "configured": False,
        }
        if configuration is None
        else configuration.to_dict()
    )
    return canonical_identity(material)


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheOperatorRoot:
    """One runtime filesystem binding for an operator-named cache root."""

    reference: str
    path: str

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_OPERATOR_ROOT_SCHEMA

    def __post_init__(self) -> None:
        reference = string_value(
            self.reference, "RebuildSourceCacheOperatorRoot.reference"
        )
        raw_path = string_value(self.path, "RebuildSourceCacheOperatorRoot.path")
        path = Path(raw_path)
        if not path.is_absolute() or str(path) != raw_path:
            fail(
                "RebuildSourceCacheOperatorRoot.path",
                "must be one normalized absolute host path",
            )
        if reference in {".", ".."}:
            fail(
                "RebuildSourceCacheOperatorRoot.reference",
                "must name an operator binding",
            )

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.SCHEMA, "reference": self.reference, "path": self.path}

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCacheOperatorRoot",
    ) -> RebuildSourceCacheOperatorRoot:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_REBUILD_SOURCE_CACHE_OPERATOR_ROOT_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"reference", "path"}),
        )
        return cls(
            string_value(data["reference"], f"{path}.reference"),
            string_value(data["path"], f"{path}.path"),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheDerivationManifest:
    """Read-only planning result frozen by the outer CLI before execution."""

    planning_request_identity: ContentIdentity
    project_revision_identity: ContentIdentity
    lifecycle_driver_identity: ContentIdentity
    lifecycle_plan_identity: ContentIdentity
    component_lock_identities: tuple[ContentIdentity, ...]
    cache_keys: tuple[SourceDerivationCacheKey, ...]

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_DERIVATION_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "planning_request_identity",
            "project_revision_identity",
            "lifecycle_driver_identity",
            "lifecycle_plan_identity",
        ):
            _identity(
                getattr(self, name),
                f"RebuildSourceCacheDerivationManifest.{name}",
            )
        _component_lock_identities(
            self.component_lock_identities,
            "RebuildSourceCacheDerivationManifest.component_lock_identities",
        )
        if not self.cache_keys:
            fail(
                "RebuildSourceCacheDerivationManifest.cache_keys",
                "must contain every planned source derivation",
            )
        if len(self.cache_keys) > MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS:
            fail(
                "RebuildSourceCacheDerivationManifest.cache_keys",
                f"must contain at most {MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS} keys",
            )
        for index, key in enumerate(self.cache_keys):
            if not isinstance(key, SourceDerivationCacheKey):
                fail(
                    f"RebuildSourceCacheDerivationManifest.cache_keys[{index}]",
                    "must be a SourceDerivationCacheKey",
                )
        key_uris = tuple(key.identity.uri for key in self.cache_keys)
        unique(
            key_uris,
            "RebuildSourceCacheDerivationManifest.cache_keys",
            "cache keys",
        )
        if key_uris != tuple(sorted(key_uris)):
            fail(
                "RebuildSourceCacheDerivationManifest.cache_keys",
                "must use canonical cache-key order",
            )
        expected_plan_identity = canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                "component_lock_identities": [
                    identity.uri for identity in self.component_lock_identities
                ],
                "cache_key_identities": list(key_uris),
            }
        )
        if self.lifecycle_plan_identity != expected_plan_identity:
            fail(
                "RebuildSourceCacheDerivationManifest.lifecycle_plan_identity",
                "must be the canonical identity of the exact planned Component-lock "
                "and cache-key sets",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def cache_key_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(key.identity for key in self.cache_keys)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "planning_request_identity": self.planning_request_identity.to_dict(),
            "project_revision_identity": self.project_revision_identity.to_dict(),
            "lifecycle_driver_identity": self.lifecycle_driver_identity.to_dict(),
            "lifecycle_plan_identity": self.lifecycle_plan_identity.to_dict(),
            "component_lock_identities": [
                identity.to_dict() for identity in self.component_lock_identities
            ],
            "cache_keys": [key.to_dict() for key in self.cache_keys],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCacheDerivationManifest",
    ) -> RebuildSourceCacheDerivationManifest:
        if (
            isinstance(value, dict)
            and value.get("schema")
            == LEGACY_REBUILD_SOURCE_CACHE_DERIVATION_MANIFEST_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "planning_request_identity",
                    "project_revision_identity",
                    "lifecycle_driver_identity",
                    "lifecycle_plan_identity",
                    "component_lock_identities",
                    "cache_keys",
                }
            ),
        )
        return cls(
            planning_request_identity=ContentIdentity.from_dict(
                data["planning_request_identity"],
                path=f"{path}.planning_request_identity",
            ),
            project_revision_identity=ContentIdentity.from_dict(
                data["project_revision_identity"],
                path=f"{path}.project_revision_identity",
            ),
            lifecycle_driver_identity=ContentIdentity.from_dict(
                data["lifecycle_driver_identity"],
                path=f"{path}.lifecycle_driver_identity",
            ),
            lifecycle_plan_identity=ContentIdentity.from_dict(
                data["lifecycle_plan_identity"],
                path=f"{path}.lifecycle_plan_identity",
            ),
            component_lock_identities=parse_tuple(
                data["component_lock_identities"],
                f"{path}.component_lock_identities",
                ContentIdentity.from_dict,
            ),
            cache_keys=parse_tuple(
                data["cache_keys"],
                f"{path}.cache_keys",
                SourceDerivationCacheKey.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheControl:
    """CLI-owned authorization for cache work during one exact lifecycle request."""

    lifecycle_request_identity: ContentIdentity
    project_revision_identity: ContentIdentity
    configuration: SourceCacheConfiguration | None
    derivation_manifest: RebuildSourceCacheDerivationManifest
    component_lock_identities: tuple[ContentIdentity, ...]
    force_regeneration: bool = False
    requested_entry_identities: tuple[ContentIdentity, ...] = ()
    operator_roots: tuple[RebuildSourceCacheOperatorRoot, ...] = ()

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_CONTROL_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.lifecycle_request_identity,
            "RebuildSourceCacheControl.lifecycle_request_identity",
        )
        _identity(
            self.project_revision_identity,
            "RebuildSourceCacheControl.project_revision_identity",
        )
        if not isinstance(
            self.derivation_manifest, RebuildSourceCacheDerivationManifest
        ):
            fail(
                "RebuildSourceCacheControl.derivation_manifest",
                "must be an outer-owned RebuildSourceCacheDerivationManifest",
            )
        if (
            self.derivation_manifest.project_revision_identity
            != self.project_revision_identity
        ):
            fail(
                "RebuildSourceCacheControl.derivation_manifest",
                "must bind the same exact project revision",
            )
        _component_lock_identities(
            self.component_lock_identities,
            "RebuildSourceCacheControl.component_lock_identities",
        )
        if (
            self.component_lock_identities
            != self.derivation_manifest.component_lock_identities
        ):
            fail(
                "RebuildSourceCacheControl.component_lock_identities",
                "must exactly match the planning manifest lock set",
            )
        if self.configuration is not None and not isinstance(
            self.configuration, SourceCacheConfiguration
        ):
            fail(
                "RebuildSourceCacheControl.configuration",
                "must be a SourceCacheConfiguration or null",
            )
        if (
            self.configuration is not None
            and len(self.configuration.targets) > MAX_REBUILD_SOURCE_CACHE_TARGETS
        ):
            fail(
                "RebuildSourceCacheControl.configuration.targets",
                f"must contain at most {MAX_REBUILD_SOURCE_CACHE_TARGETS} targets",
            )
        bool_value(
            self.force_regeneration, "RebuildSourceCacheControl.force_regeneration"
        )
        if (
            len(self.requested_entry_identities)
            > MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES
        ):
            fail(
                "RebuildSourceCacheControl.requested_entry_identities",
                "must contain at most "
                f"{MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES} identities",
            )
        for index, identity in enumerate(self.requested_entry_identities):
            _identity(
                identity,
                f"RebuildSourceCacheControl.requested_entry_identities[{index}]",
            )
        entry_uris = tuple(item.uri for item in self.requested_entry_identities)
        unique(
            entry_uris,
            "RebuildSourceCacheControl.requested_entry_identities",
            "entry identities",
        )
        if entry_uris != tuple(sorted(entry_uris)):
            fail(
                "RebuildSourceCacheControl.requested_entry_identities",
                "must use canonical identity order",
            )
        if self.force_regeneration and self.requested_entry_identities:
            fail(
                "RebuildSourceCacheControl",
                "force regeneration and explicit cache entries are mutually exclusive",
            )
        for index, root in enumerate(self.operator_roots):
            if not isinstance(root, RebuildSourceCacheOperatorRoot):
                fail(
                    f"RebuildSourceCacheControl.operator_roots[{index}]",
                    "must be a RebuildSourceCacheOperatorRoot",
                )
        if len(self.operator_roots) > MAX_REBUILD_SOURCE_CACHE_TARGETS:
            fail(
                "RebuildSourceCacheControl.operator_roots",
                f"must contain at most {MAX_REBUILD_SOURCE_CACHE_TARGETS} bindings",
            )
        references = tuple(item.reference for item in self.operator_roots)
        unique(
            references,
            "RebuildSourceCacheControl.operator_roots",
            "operator root references",
        )
        if references != tuple(sorted(references)):
            fail(
                "RebuildSourceCacheControl.operator_roots",
                "must use canonical reference order",
            )
        required_references = (
            set()
            if self.configuration is None
            or self.configuration.mode is SourceCacheMode.OFF
            else {
                target.root_reference
                for target in self.configuration.targets
                if target.root_kind is SourceCacheRootKind.OPERATOR_BOUND
            }
        )
        if set(references) != required_references:
            fail(
                "RebuildSourceCacheControl.operator_roots",
                "must bind every and only configured operator root reference",
            )
        if self.configuration is None and self.requested_entry_identities:
            fail(
                "RebuildSourceCacheControl.requested_entry_identities",
                "cannot select an entry when the source cache is unconfigured",
            )
        if (
            self.configuration is not None
            and not self.configuration.mode.can_read
            and self.requested_entry_identities
        ):
            fail(
                "RebuildSourceCacheControl.requested_entry_identities",
                "cannot select an entry when configured cache reads are disabled",
            )

    @property
    def configuration_identity(self) -> ContentIdentity:
        return _configuration_identity(self.configuration)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "lifecycle_request_identity": self.lifecycle_request_identity.to_dict(),
            "project_revision_identity": self.project_revision_identity.to_dict(),
            "configuration": (
                None if self.configuration is None else self.configuration.to_dict()
            ),
            "derivation_manifest": self.derivation_manifest.to_dict(),
            "component_lock_identities": [
                identity.to_dict() for identity in self.component_lock_identities
            ],
            "force_regeneration": self.force_regeneration,
            "requested_entry_identities": [
                item.to_dict() for item in self.requested_entry_identities
            ],
            "operator_roots": [item.to_dict() for item in self.operator_roots],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RebuildSourceCacheControl"
    ) -> RebuildSourceCacheControl:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_REBUILD_SOURCE_CACHE_CONTROL_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "lifecycle_request_identity",
                    "project_revision_identity",
                    "configuration",
                    "derivation_manifest",
                    "component_lock_identities",
                    "force_regeneration",
                    "requested_entry_identities",
                    "operator_roots",
                }
            ),
        )
        configuration = data["configuration"]
        return cls(
            lifecycle_request_identity=ContentIdentity.from_dict(
                data["lifecycle_request_identity"],
                path=f"{path}.lifecycle_request_identity",
            ),
            project_revision_identity=ContentIdentity.from_dict(
                data["project_revision_identity"],
                path=f"{path}.project_revision_identity",
            ),
            configuration=(
                None
                if configuration is None
                else SourceCacheConfiguration.from_dict(
                    configuration, path=f"{path}.configuration"
                )
            ),
            derivation_manifest=RebuildSourceCacheDerivationManifest.from_dict(
                data["derivation_manifest"],
                path=f"{path}.derivation_manifest",
            ),
            component_lock_identities=parse_tuple(
                data["component_lock_identities"],
                f"{path}.component_lock_identities",
                ContentIdentity.from_dict,
            ),
            force_regeneration=bool_value(
                data["force_regeneration"], f"{path}.force_regeneration"
            ),
            requested_entry_identities=parse_tuple(
                data["requested_entry_identities"],
                f"{path}.requested_entry_identities",
                ContentIdentity.from_dict,
            ),
            operator_roots=parse_tuple(
                data["operator_roots"],
                f"{path}.operator_roots",
                RebuildSourceCacheOperatorRoot.from_dict,
            ),
        )


class RebuildSourceCacheOutcome(StrEnum):
    UNCONFIGURED = "cache-unconfigured"
    FORCED_REGENERATION = "forced-regeneration"
    READ_DISABLED = "cache-read-disabled"
    MISS = "miss"
    HIT = "hit"


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheDecisionItem:
    """One exact derivation decision; a hit remains acceptance-untrusted."""

    cache_key: SourceDerivationCacheKey
    outcome: RebuildSourceCacheOutcome
    candidate_identities: tuple[ContentIdentity, ...] = ()
    selected_entry_identity: ContentIdentity | None = None
    source_tree_identity: ContentIdentity | None = None
    current_acceptance_trusted: bool = False
    generation_skipped: bool = False

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_DECISION_ITEM_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, SourceDerivationCacheKey):
            fail(
                "RebuildSourceCacheDecisionItem.cache_key",
                "must be a SourceDerivationCacheKey",
            )
        if not isinstance(self.outcome, RebuildSourceCacheOutcome):
            fail(
                "RebuildSourceCacheDecisionItem.outcome",
                "must be a RebuildSourceCacheOutcome",
            )
        if (
            len(self.candidate_identities)
            > MAX_REBUILD_SOURCE_CACHE_CANDIDATES_PER_ITEM
        ):
            fail(
                "RebuildSourceCacheDecisionItem.candidate_identities",
                "must contain at most "
                f"{MAX_REBUILD_SOURCE_CACHE_CANDIDATES_PER_ITEM} identities",
            )
        for index, identity in enumerate(self.candidate_identities):
            _identity(
                identity,
                f"RebuildSourceCacheDecisionItem.candidate_identities[{index}]",
            )
        candidate_uris = tuple(item.uri for item in self.candidate_identities)
        unique(
            candidate_uris,
            "RebuildSourceCacheDecisionItem.candidate_identities",
            "candidate identities",
        )
        if candidate_uris != tuple(sorted(candidate_uris)):
            fail(
                "RebuildSourceCacheDecisionItem.candidate_identities",
                "must use canonical identity order",
            )
        for name in (
            "selected_entry_identity",
            "source_tree_identity",
        ):
            value = getattr(self, name)
            if value is not None:
                _identity(value, f"RebuildSourceCacheDecisionItem.{name}")
        bool_value(
            self.current_acceptance_trusted,
            "RebuildSourceCacheDecisionItem.current_acceptance_trusted",
        )
        bool_value(
            self.generation_skipped,
            "RebuildSourceCacheDecisionItem.generation_skipped",
        )
        if self.current_acceptance_trusted:
            fail(
                "RebuildSourceCacheDecisionItem.current_acceptance_trusted",
                "cache decisions can never establish current acceptance",
            )
        hit = self.outcome is RebuildSourceCacheOutcome.HIT
        hit_fields_present = all(
            item is not None
            for item in (
                self.selected_entry_identity,
                self.source_tree_identity,
            )
        )
        if hit:
            if (
                not hit_fields_present
                or self.selected_entry_identity not in self.candidate_identities
                or not self.generation_skipped
            ):
                fail(
                    "RebuildSourceCacheDecisionItem",
                    "a hit must select one candidate, bind materialized source, "
                    "and skip only generation",
                )
        elif (
            self.candidate_identities
            or hit_fields_present
            or any(
                item is not None
                for item in (
                    self.selected_entry_identity,
                    self.source_tree_identity,
                )
            )
            or self.generation_skipped
        ):
            fail(
                "RebuildSourceCacheDecisionItem",
                "a non-hit cannot select or materialize cached source",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        def optional(value: ContentIdentity | None) -> object:
            return None if value is None else value.to_dict()

        return {
            "schema": self.SCHEMA,
            "cache_key": self.cache_key.to_dict(),
            "outcome": self.outcome.value,
            "candidate_identities": [
                item.to_dict() for item in self.candidate_identities
            ],
            "selected_entry_identity": optional(self.selected_entry_identity),
            "source_tree_identity": optional(self.source_tree_identity),
            "current_acceptance_trusted": self.current_acceptance_trusted,
            "generation_skipped": self.generation_skipped,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RebuildSourceCacheDecisionItem"
    ) -> RebuildSourceCacheDecisionItem:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_REBUILD_SOURCE_CACHE_DECISION_ITEM_SCHEMA
        ):
            value = dict(value)
            value.pop("source_index_identity", None)
            value["schema"] = cls.SCHEMA
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "cache_key",
                    "outcome",
                    "candidate_identities",
                    "selected_entry_identity",
                    "source_tree_identity",
                    "current_acceptance_trusted",
                    "generation_skipped",
                }
            ),
        )
        return cls(
            cache_key=SourceDerivationCacheKey.from_dict(
                data["cache_key"], path=f"{path}.cache_key"
            ),
            outcome=enum_value(
                RebuildSourceCacheOutcome, data["outcome"], f"{path}.outcome"
            ),
            candidate_identities=parse_tuple(
                data["candidate_identities"],
                f"{path}.candidate_identities",
                ContentIdentity.from_dict,
            ),
            selected_entry_identity=_optional_identity(
                data["selected_entry_identity"], f"{path}.selected_entry_identity"
            ),
            source_tree_identity=_optional_identity(
                data["source_tree_identity"], f"{path}.source_tree_identity"
            ),
            current_acceptance_trusted=bool_value(
                data["current_acceptance_trusted"],
                f"{path}.current_acceptance_trusted",
            ),
            generation_skipped=bool_value(
                data["generation_skipped"], f"{path}.generation_skipped"
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheDecision:
    """Canonical aggregate decision returned by the project lifecycle driver."""

    control_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    configuration_identity: ContentIdentity
    mode: str
    fresh_generation_required: bool
    items: tuple[RebuildSourceCacheDecisionItem, ...] = ()

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_DECISION_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "control_identity",
            "lifecycle_request_identity",
            "configuration_identity",
        ):
            _identity(getattr(self, name), f"RebuildSourceCacheDecision.{name}")
        mode = string_value(self.mode, "RebuildSourceCacheDecision.mode")
        allowed_modes = {"unconfigured", *(item.value for item in SourceCacheMode)}
        if mode not in allowed_modes:
            fail(
                "RebuildSourceCacheDecision.mode",
                "must be unconfigured or one source-cache mode",
            )
        bool_value(
            self.fresh_generation_required,
            "RebuildSourceCacheDecision.fresh_generation_required",
        )
        if len(self.items) > MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS:
            fail(
                "RebuildSourceCacheDecision.items",
                f"must contain at most {MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS} items",
            )
        for index, item in enumerate(self.items):
            if not isinstance(item, RebuildSourceCacheDecisionItem):
                fail(
                    f"RebuildSourceCacheDecision.items[{index}]",
                    "must be a RebuildSourceCacheDecisionItem",
                )
        keys = tuple(item.cache_key.identity.uri for item in self.items)
        unique(keys, "RebuildSourceCacheDecision.items", "cache keys")
        if keys != tuple(sorted(keys)):
            fail(
                "RebuildSourceCacheDecision.items",
                "must use canonical cache-key order",
            )
        expected_fresh = not self.items or any(
            item.outcome is not RebuildSourceCacheOutcome.HIT for item in self.items
        )
        if self.fresh_generation_required != expected_fresh:
            fail(
                "RebuildSourceCacheDecision.fresh_generation_required",
                "does not match the exact derivation decisions",
            )

    @classmethod
    def fresh(cls, control: RebuildSourceCacheControl) -> RebuildSourceCacheDecision:
        if control.configuration is not None and (
            control.configuration.mode is not SourceCacheMode.OFF
        ):
            fail(
                "RebuildSourceCacheDecision",
                "only an unconfigured or explicitly off cache has a CLI-owned fresh "
                "decision",
            )
        outcome = (
            RebuildSourceCacheOutcome.UNCONFIGURED
            if control.configuration is None
            else RebuildSourceCacheOutcome.READ_DISABLED
        )
        items = tuple(
            RebuildSourceCacheDecisionItem(key, outcome)
            for key in control.derivation_manifest.cache_keys
        )
        return cls(
            control.identity,
            control.lifecycle_request_identity,
            control.configuration_identity,
            (
                "unconfigured"
                if control.configuration is None
                else control.configuration.mode.value
            ),
            True,
            items,
        )

    def validate_against(self, control: RebuildSourceCacheControl) -> None:
        if self.control_identity != control.identity:
            fail(
                "RebuildSourceCacheDecision.control_identity",
                "does not bind the CLI-owned cache control",
            )
        if self.lifecycle_request_identity != control.lifecycle_request_identity:
            fail(
                "RebuildSourceCacheDecision.lifecycle_request_identity",
                "does not bind the exact lifecycle request",
            )
        if self.configuration_identity != control.configuration_identity:
            fail(
                "RebuildSourceCacheDecision.configuration_identity",
                "does not bind the project cache configuration",
            )
        expected_mode = (
            "unconfigured"
            if control.configuration is None
            else control.configuration.mode.value
        )
        if self.mode != expected_mode:
            fail(
                "RebuildSourceCacheDecision.mode",
                "does not match the project cache configuration",
            )
        configuration = control.configuration
        expected_keys = tuple(
            key.identity.uri for key in control.derivation_manifest.cache_keys
        )
        actual_keys = tuple(item.cache_key.identity.uri for item in self.items)
        if actual_keys != expected_keys:
            fail(
                "RebuildSourceCacheDecision.items",
                "must exactly cover the outer-owned derivation-key manifest",
            )
        expected_outcomes = (
            {RebuildSourceCacheOutcome.UNCONFIGURED}
            if configuration is None
            else (
                {RebuildSourceCacheOutcome.READ_DISABLED}
                if configuration.mode is SourceCacheMode.OFF
                else (
                    {RebuildSourceCacheOutcome.FORCED_REGENERATION}
                    if control.force_regeneration
                    else (
                        {RebuildSourceCacheOutcome.READ_DISABLED}
                        if not configuration.mode.can_read
                        else {
                            RebuildSourceCacheOutcome.MISS,
                            RebuildSourceCacheOutcome.HIT,
                        }
                    )
                )
            )
        )
        if any(item.outcome not in expected_outcomes for item in self.items):
            fail(
                "RebuildSourceCacheDecision.items",
                "contains an outcome forbidden by the exact cache control",
            )
        selected = {
            item.selected_entry_identity.uri
            for item in self.items
            if item.selected_entry_identity is not None
        }
        requested = {item.uri for item in control.requested_entry_identities}
        if not requested.issubset(selected):
            fail(
                "RebuildSourceCacheDecision.items",
                "does not consume every explicitly selected cache entry",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "control_identity": self.control_identity.to_dict(),
            "lifecycle_request_identity": self.lifecycle_request_identity.to_dict(),
            "configuration_identity": self.configuration_identity.to_dict(),
            "mode": self.mode,
            "fresh_generation_required": self.fresh_generation_required,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RebuildSourceCacheDecision"
    ) -> RebuildSourceCacheDecision:
        if (
            isinstance(value, dict)
            and value.get("schema") == LEGACY_REBUILD_SOURCE_CACHE_DECISION_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "control_identity",
                    "lifecycle_request_identity",
                    "configuration_identity",
                    "mode",
                    "fresh_generation_required",
                    "items",
                }
            ),
        )
        return cls(
            control_identity=ContentIdentity.from_dict(
                data["control_identity"], path=f"{path}.control_identity"
            ),
            lifecycle_request_identity=ContentIdentity.from_dict(
                data["lifecycle_request_identity"],
                path=f"{path}.lifecycle_request_identity",
            ),
            configuration_identity=ContentIdentity.from_dict(
                data["configuration_identity"],
                path=f"{path}.configuration_identity",
            ),
            mode=string_value(data["mode"], f"{path}.mode"),
            fresh_generation_required=bool_value(
                data["fresh_generation_required"],
                f"{path}.fresh_generation_required",
            ),
            items=parse_tuple(
                data["items"],
                f"{path}.items",
                RebuildSourceCacheDecisionItem.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheLifecycleMember:
    """Current lifecycle evidence for one exact source derivation."""

    cache_key: SourceDerivationCacheKey
    accepted_entry_identity: ContentIdentity | None
    source_tree_identity: ContentIdentity
    source_intelligence_identity: ContentIdentity
    build_evidence_identity: ContentIdentity
    test_evidence_identity: ContentIdentity
    acceptance_evidence_identity: ContentIdentity
    workspace_admission_identity: ContentIdentity
    provenance_evidence_identity: ContentIdentity
    source_sbom_identity: ContentIdentity
    resolved_sbom_identity: ContentIdentity

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_LIFECYCLE_MEMBER_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.cache_key, SourceDerivationCacheKey):
            fail(
                "RebuildSourceCacheLifecycleMember.cache_key",
                "must be a SourceDerivationCacheKey",
            )
        if self.accepted_entry_identity is not None:
            _identity(
                self.accepted_entry_identity,
                "RebuildSourceCacheLifecycleMember.accepted_entry_identity",
            )
        for name in (
            "source_tree_identity",
            "source_intelligence_identity",
            "build_evidence_identity",
            "test_evidence_identity",
            "acceptance_evidence_identity",
            "workspace_admission_identity",
            "provenance_evidence_identity",
            "source_sbom_identity",
            "resolved_sbom_identity",
        ):
            _identity(getattr(self, name), f"RebuildSourceCacheLifecycleMember.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def evidence_identity(self, kind: str) -> ContentIdentity:
        fields = {
            "acceptance-result": self.acceptance_evidence_identity,
            "build-result": self.build_evidence_identity,
            "source-intelligence": self.source_intelligence_identity,
            "generation-provenance": self.provenance_evidence_identity,
            "resolved-sbom": self.resolved_sbom_identity,
            "source-sbom": self.source_sbom_identity,
            "test-report": self.test_evidence_identity,
            "workspace-admission": self.workspace_admission_identity,
        }
        try:
            return fields[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported lifecycle evidence kind: {kind}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "cache_key": self.cache_key.to_dict(),
            "accepted_entry_identity": (
                None
                if self.accepted_entry_identity is None
                else self.accepted_entry_identity.to_dict()
            ),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "source_intelligence_identity": (
                self.source_intelligence_identity.to_dict()
            ),
            "build_evidence_identity": self.build_evidence_identity.to_dict(),
            "test_evidence_identity": self.test_evidence_identity.to_dict(),
            "acceptance_evidence_identity": (
                self.acceptance_evidence_identity.to_dict()
            ),
            "workspace_admission_identity": (
                self.workspace_admission_identity.to_dict()
            ),
            "provenance_evidence_identity": (
                self.provenance_evidence_identity.to_dict()
            ),
            "source_sbom_identity": self.source_sbom_identity.to_dict(),
            "resolved_sbom_identity": self.resolved_sbom_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCacheLifecycleMember",
    ) -> RebuildSourceCacheLifecycleMember:
        if (
            isinstance(value, dict)
            and value.get("schema")
            == LEGACY_REBUILD_SOURCE_CACHE_LIFECYCLE_MEMBER_SCHEMA
        ):
            value = dict(value)
            value["schema"] = cls.SCHEMA
            value["source_intelligence_identity"] = value.pop(
                "source_index_identity", None
            )
        required = frozenset(
            {
                "cache_key",
                "accepted_entry_identity",
                "source_tree_identity",
                "source_intelligence_identity",
                "build_evidence_identity",
                "test_evidence_identity",
                "acceptance_evidence_identity",
                "workspace_admission_identity",
                "provenance_evidence_identity",
                "source_sbom_identity",
                "resolved_sbom_identity",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=required,
        )
        return cls(
            cache_key=SourceDerivationCacheKey.from_dict(
                data["cache_key"], path=f"{path}.cache_key"
            ),
            accepted_entry_identity=_optional_identity(
                data["accepted_entry_identity"],
                f"{path}.accepted_entry_identity",
            ),
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in required
                if name not in {"cache_key", "accepted_entry_identity"}
            },
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheLifecycleBinding:
    """Complete current-run membership joining cache decisions to acceptance."""

    control_identity: ContentIdentity
    decision_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    lifecycle_plan_identity: ContentIdentity
    receipt_subject_identity: ContentIdentity
    component_lock_identities: tuple[ContentIdentity, ...]
    derivation_key_identities: tuple[ContentIdentity, ...]
    members: tuple[RebuildSourceCacheLifecycleMember, ...]

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_LIFECYCLE_BINDING_SCHEMA
    EVIDENCE_KINDS: ClassVar[tuple[str, ...]] = (
        "acceptance-result",
        "build-result",
        "source-intelligence",
        "generation-provenance",
        "resolved-sbom",
        "source-sbom",
        "test-report",
        "workspace-admission",
    )

    def __post_init__(self) -> None:
        for name in (
            "control_identity",
            "decision_identity",
            "lifecycle_request_identity",
            "lifecycle_plan_identity",
            "receipt_subject_identity",
        ):
            _identity(getattr(self, name), f"RebuildSourceCacheLifecycleBinding.{name}")
        _component_lock_identities(
            self.component_lock_identities,
            "RebuildSourceCacheLifecycleBinding.component_lock_identities",
        )
        if not self.members:
            fail(
                "RebuildSourceCacheLifecycleBinding.members",
                "must cover at least one exact derivation",
            )
        if len(self.members) > MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS:
            fail(
                "RebuildSourceCacheLifecycleBinding.members",
                f"must contain at most {MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS} items",
            )
        for index, member in enumerate(self.members):
            if not isinstance(member, RebuildSourceCacheLifecycleMember):
                fail(
                    f"RebuildSourceCacheLifecycleBinding.members[{index}]",
                    "must be a RebuildSourceCacheLifecycleMember",
                )
        for index, identity in enumerate(self.derivation_key_identities):
            _identity(
                identity,
                "RebuildSourceCacheLifecycleBinding.derivation_key_identities"
                f"[{index}]",
            )
        keys = tuple(member.cache_key.identity for member in self.members)
        key_uris = tuple(item.uri for item in keys)
        unique(key_uris, "RebuildSourceCacheLifecycleBinding.members", "cache keys")
        if key_uris != tuple(sorted(key_uris)):
            fail(
                "RebuildSourceCacheLifecycleBinding.members",
                "must use canonical cache-key order",
            )
        if self.derivation_key_identities != keys:
            fail(
                "RebuildSourceCacheLifecycleBinding.derivation_key_identities",
                "must exactly enumerate the canonical complete member key set",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def evidence_identity(self, kind: str) -> ContentIdentity:
        if kind not in self.EVIDENCE_KINDS:
            raise ValueError(f"unsupported lifecycle evidence kind: {kind}")
        return canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_LIFECYCLE_EVIDENCE_SET_SCHEMA,
                "kind": kind,
                "members": [
                    {
                        "cache_key_identity": member.cache_key.identity.uri,
                        "evidence_identity": member.evidence_identity(kind).uri,
                    }
                    for member in self.members
                ],
            }
        )

    def validate_against(
        self,
        control: RebuildSourceCacheControl,
        decision: RebuildSourceCacheDecision,
        receipt: ProjectTestReceipt,
    ) -> None:
        if self.control_identity != control.identity:
            fail(
                "RebuildSourceCacheLifecycleBinding.control_identity",
                "does not bind the CLI-owned cache control",
            )
        if self.decision_identity != decision.identity:
            fail(
                "RebuildSourceCacheLifecycleBinding.decision_identity",
                "does not bind the validated cache decision",
            )
        if self.lifecycle_request_identity != control.lifecycle_request_identity:
            fail(
                "RebuildSourceCacheLifecycleBinding.lifecycle_request_identity",
                "does not bind the exact lifecycle request",
            )
        if self.component_lock_identities != control.component_lock_identities:
            fail(
                "RebuildSourceCacheLifecycleBinding.component_lock_identities",
                "does not bind the exact planned Component lock set",
            )
        if self.receipt_subject_identity != receipt.subject_identity:
            fail(
                "RebuildSourceCacheLifecycleBinding.receipt_subject_identity",
                "does not bind the current receipt subject",
            )
        evidence = {item.kind: item.identity for item in receipt.evidence}
        if evidence.get("lifecycle-plan") != self.lifecycle_plan_identity:
            fail(
                "RebuildSourceCacheLifecycleBinding.lifecycle_plan_identity",
                "does not bind receipt lifecycle-plan evidence",
            )
        if (
            self.lifecycle_plan_identity
            != control.derivation_manifest.lifecycle_plan_identity
        ):
            fail(
                "RebuildSourceCacheLifecycleBinding.lifecycle_plan_identity",
                "does not bind the outer-owned derivation plan",
            )
        if evidence.get("source-cache-lifecycle") != self.identity:
            fail(
                "RebuildSourceCacheLifecycleBinding",
                "receipt does not bind the exact current lifecycle membership",
            )
        for kind in self.EVIDENCE_KINDS:
            if evidence.get(kind) != self.evidence_identity(kind):
                fail(
                    f"RebuildSourceCacheLifecycleBinding.{kind}",
                    "receipt evidence does not cover every exact derivation member",
                )
        decision_by_key = {item.cache_key.identity.uri: item for item in decision.items}
        member_by_key = {
            member.cache_key.identity.uri: member for member in self.members
        }
        expected_keys = {
            key.identity.uri for key in control.derivation_manifest.cache_keys
        }
        if set(decision_by_key) != expected_keys or set(member_by_key) != expected_keys:
            fail(
                "RebuildSourceCacheLifecycleBinding.members",
                "does not exactly cover the outer-owned derivation-key manifest",
            )
        for key, item in decision_by_key.items():
            member = member_by_key[key]
            if item.outcome is RebuildSourceCacheOutcome.HIT:
                if (
                    member.source_tree_identity != item.source_tree_identity
                    or member.accepted_entry_identity is not None
                ):
                    fail(
                        "RebuildSourceCacheLifecycleBinding.members",
                        "a hit must bind the materialized tree and cannot be "
                        "re-published as current acceptance",
                    )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "control_identity": self.control_identity.to_dict(),
            "decision_identity": self.decision_identity.to_dict(),
            "lifecycle_request_identity": self.lifecycle_request_identity.to_dict(),
            "lifecycle_plan_identity": self.lifecycle_plan_identity.to_dict(),
            "receipt_subject_identity": self.receipt_subject_identity.to_dict(),
            "component_lock_identities": [
                identity.to_dict() for identity in self.component_lock_identities
            ],
            "derivation_key_identities": [
                item.to_dict() for item in self.derivation_key_identities
            ],
            "members": [member.to_dict() for member in self.members],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCacheLifecycleBinding",
    ) -> RebuildSourceCacheLifecycleBinding:
        if (
            isinstance(value, dict)
            and value.get("schema")
            == LEGACY_REBUILD_SOURCE_CACHE_LIFECYCLE_BINDING_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "control_identity",
                    "decision_identity",
                    "lifecycle_request_identity",
                    "lifecycle_plan_identity",
                    "receipt_subject_identity",
                    "component_lock_identities",
                    "derivation_key_identities",
                    "members",
                }
            ),
        )
        return cls(
            control_identity=ContentIdentity.from_dict(
                data["control_identity"], path=f"{path}.control_identity"
            ),
            decision_identity=ContentIdentity.from_dict(
                data["decision_identity"], path=f"{path}.decision_identity"
            ),
            lifecycle_request_identity=ContentIdentity.from_dict(
                data["lifecycle_request_identity"],
                path=f"{path}.lifecycle_request_identity",
            ),
            lifecycle_plan_identity=ContentIdentity.from_dict(
                data["lifecycle_plan_identity"],
                path=f"{path}.lifecycle_plan_identity",
            ),
            receipt_subject_identity=ContentIdentity.from_dict(
                data["receipt_subject_identity"],
                path=f"{path}.receipt_subject_identity",
            ),
            component_lock_identities=parse_tuple(
                data["component_lock_identities"],
                f"{path}.component_lock_identities",
                ContentIdentity.from_dict,
            ),
            derivation_key_identities=parse_tuple(
                data["derivation_key_identities"],
                f"{path}.derivation_key_identities",
                ContentIdentity.from_dict,
            ),
            members=parse_tuple(
                data["members"],
                f"{path}.members",
                RebuildSourceCacheLifecycleMember.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCachePublicationMember:
    """One admitted entry plus its runtime-local, read-only caller CAS."""

    entry: AcceptedSourceCacheEntry
    caller_cas_path: str
    intelligence_attachments: tuple[SourceIntelligenceAttachment, ...] = ()

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_PUBLICATION_MEMBER_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.entry, AcceptedSourceCacheEntry):
            fail(
                "RebuildSourceCachePublicationMember.entry",
                "must be an AcceptedSourceCacheEntry",
            )
        try:
            canonical_relative_posix_path(
                self.caller_cas_path,
                label="RebuildSourceCachePublicationMember.caller_cas_path",
            )
        except (TypeError, ValueError):
            fail(
                "RebuildSourceCachePublicationMember.caller_cas_path",
                "must be a canonical runtime-relative path",
            )
        if len(self.intelligence_attachments) > 64:
            fail(
                "RebuildSourceCachePublicationMember.intelligence_attachments",
                "must contain at most 64 detached provider artifacts",
            )
        for index, attachment in enumerate(self.intelligence_attachments):
            if not isinstance(attachment, SourceIntelligenceAttachment):
                fail(
                    "RebuildSourceCachePublicationMember.intelligence_attachments"
                    f"[{index}]",
                    "must be a SourceIntelligenceAttachment",
                )
            if attachment.source_tree_identity != (
                self.entry.derivation.source_tree_identity
            ):
                fail(
                    "RebuildSourceCachePublicationMember.intelligence_attachments"
                    f"[{index}]",
                    "must describe the member's accepted source tree",
                )
        identities = tuple(item.identity.uri for item in self.intelligence_attachments)
        unique(
            identities,
            "RebuildSourceCachePublicationMember.intelligence_attachments",
            "attachment identities",
        )
        if identities != tuple(sorted(identities)):
            fail(
                "RebuildSourceCachePublicationMember.intelligence_attachments",
                "must use canonical identity order",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entry": self.entry.to_dict(),
            "caller_cas_path": self.caller_cas_path,
            "intelligence_attachments": [
                item.to_dict() for item in self.intelligence_attachments
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCachePublicationMember",
    ) -> RebuildSourceCachePublicationMember:
        if (
            isinstance(value, dict)
            and value.get("schema")
            == LEGACY_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBER_SCHEMA
        ):
            value = {
                **value,
                "schema": cls.SCHEMA,
                "intelligence_attachments": [],
            }
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"entry", "caller_cas_path", "intelligence_attachments"}
            ),
        )
        return cls(
            entry=AcceptedSourceCacheEntry.from_dict(
                data["entry"], path=f"{path}.entry"
            ),
            caller_cas_path=string_value(
                data["caller_cas_path"], f"{path}.caller_cas_path"
            ),
            intelligence_attachments=parse_tuple(
                data["intelligence_attachments"],
                f"{path}.intelligence_attachments",
                SourceIntelligenceAttachment.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildSourceCachePublicationOffer:
    """Driver-produced offer; only the outer CLI may realize it."""

    control_identity: ContentIdentity
    decision_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    component_lock_identities: tuple[ContentIdentity, ...]
    receipt_subject_identity: ContentIdentity
    acceptance_evidence_identity: ContentIdentity
    workspace_admission_identity: ContentIdentity
    target_id: str
    members: tuple[RebuildSourceCachePublicationMember, ...]

    SCHEMA: ClassVar[str] = REBUILD_SOURCE_CACHE_PUBLICATION_OFFER_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "control_identity",
            "decision_identity",
            "lifecycle_request_identity",
            "receipt_subject_identity",
            "acceptance_evidence_identity",
            "workspace_admission_identity",
        ):
            _identity(getattr(self, name), f"RebuildSourceCachePublicationOffer.{name}")
        _component_lock_identities(
            self.component_lock_identities,
            "RebuildSourceCachePublicationOffer.component_lock_identities",
        )
        string_value(self.target_id, "RebuildSourceCachePublicationOffer.target_id")
        if len(self.members) > MAX_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBERS:
            fail(
                "RebuildSourceCachePublicationOffer.members",
                "must contain at most "
                f"{MAX_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBERS} entries",
            )
        for index, member in enumerate(self.members):
            if not isinstance(member, RebuildSourceCachePublicationMember):
                fail(
                    f"RebuildSourceCachePublicationOffer.members[{index}]",
                    "must be a RebuildSourceCachePublicationMember",
                )
        keys = tuple(
            member.entry.derivation.cache_key.identity.uri for member in self.members
        )
        unique(keys, "RebuildSourceCachePublicationOffer.members", "cache keys")
        if keys != tuple(sorted(keys)):
            fail(
                "RebuildSourceCachePublicationOffer.members",
                "must use canonical cache-key order",
            )

    @property
    def publication_result(self) -> dict[str, object]:
        return {
            "schema": REBUILD_SOURCE_CACHE_PUBLICATION_RESULT_SCHEMA,
            "control_identity": self.control_identity.uri,
            "decision_identity": self.decision_identity.uri,
            "lifecycle_request_identity": self.lifecycle_request_identity.uri,
            "component_lock_identities": [
                identity.uri for identity in self.component_lock_identities
            ],
            "target_id": self.target_id,
            "entry_identities": [member.entry.identity.uri for member in self.members],
        }

    @property
    def publication_result_identity(self) -> ContentIdentity:
        return canonical_identity(self.publication_result)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def validate_against(
        self,
        control: RebuildSourceCacheControl,
        decision: RebuildSourceCacheDecision,
        lifecycle: RebuildSourceCacheLifecycleBinding,
    ) -> None:
        if self.control_identity != control.identity:
            fail(
                "RebuildSourceCachePublicationOffer.control_identity",
                "does not bind the CLI-owned cache control",
            )
        if self.decision_identity != decision.identity:
            fail(
                "RebuildSourceCachePublicationOffer.decision_identity",
                "does not bind the validated cache decision",
            )
        if self.lifecycle_request_identity != control.lifecycle_request_identity:
            fail(
                "RebuildSourceCachePublicationOffer.lifecycle_request_identity",
                "does not bind the exact lifecycle request",
            )
        if (
            self.component_lock_identities != control.component_lock_identities
            or self.component_lock_identities != lifecycle.component_lock_identities
        ):
            fail(
                "RebuildSourceCachePublicationOffer.component_lock_identities",
                "does not bind the exact planned Component lock set",
            )
        configuration = control.configuration
        if configuration is None or not configuration.mode.can_write:
            fail(
                "RebuildSourceCachePublicationOffer",
                "cache control does not authorize publication",
            )
        if self.target_id != configuration.write_target_id:
            fail(
                "RebuildSourceCachePublicationOffer.target_id",
                "does not name the sole configured write target",
            )
        if lifecycle.control_identity != control.identity or (
            lifecycle.decision_identity != decision.identity
        ):
            fail(
                "RebuildSourceCachePublicationOffer",
                "does not use the validated current lifecycle membership",
            )
        if self.receipt_subject_identity != lifecycle.receipt_subject_identity:
            fail(
                "RebuildSourceCachePublicationOffer.receipt_subject_identity",
                "does not bind the current lifecycle subject",
            )
        if self.acceptance_evidence_identity != lifecycle.evidence_identity(
            "acceptance-result"
        ):
            fail(
                "RebuildSourceCachePublicationOffer.acceptance_evidence_identity",
                "does not bind current per-key acceptance evidence",
            )
        if self.workspace_admission_identity != lifecycle.evidence_identity(
            "workspace-admission"
        ):
            fail(
                "RebuildSourceCachePublicationOffer.workspace_admission_identity",
                "does not bind current per-key workspace admission",
            )
        decision_keys = {
            item.cache_key.identity.uri
            for item in decision.items
            if item.outcome is not RebuildSourceCacheOutcome.HIT
        }
        member_keys = {
            member.entry.derivation.cache_key.identity.uri for member in self.members
        }
        if member_keys != decision_keys:
            fail(
                "RebuildSourceCachePublicationOffer.members",
                "must offer exactly one current entry per non-hit decision key and "
                "must omit cache hits",
            )
        lifecycle_by_key = {
            member.cache_key.identity.uri: member for member in lifecycle.members
        }
        planned_locks = frozenset(
            identity.uri for identity in self.component_lock_identities
        )
        for member in self.members:
            entry = member.entry
            if entry.derivation.component_lock_identity.uri not in planned_locks:
                fail(
                    "RebuildSourceCachePublicationOffer.members",
                    "entry Component lock is outside the exact planned Component "
                    "lock set",
                )
            current = lifecycle_by_key[entry.derivation.cache_key.identity.uri]
            derivation = entry.derivation
            if current.accepted_entry_identity != entry.identity:
                fail(
                    "RebuildSourceCachePublicationOffer.members",
                    "entry identity is not covered by current lifecycle membership",
                )
            expected = {
                "source_tree_identity": current.source_tree_identity,
                "build_evidence_identity": current.build_evidence_identity,
                "test_evidence_identity": current.test_evidence_identity,
                "acceptance_identity": current.acceptance_evidence_identity,
                "provenance_identity": current.provenance_evidence_identity,
                "source_sbom_identity": current.source_sbom_identity,
                "resolved_sbom_identity": current.resolved_sbom_identity,
            }
            for name, identity in expected.items():
                if getattr(derivation, name) != identity:
                    fail(
                        "RebuildSourceCachePublicationOffer.members",
                        f"entry {name} is not current lifecycle evidence",
                    )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "control_identity": self.control_identity.to_dict(),
            "decision_identity": self.decision_identity.to_dict(),
            "lifecycle_request_identity": self.lifecycle_request_identity.to_dict(),
            "component_lock_identities": [
                identity.to_dict() for identity in self.component_lock_identities
            ],
            "receipt_subject_identity": self.receipt_subject_identity.to_dict(),
            "acceptance_evidence_identity": self.acceptance_evidence_identity.to_dict(),
            "workspace_admission_identity": self.workspace_admission_identity.to_dict(),
            "target_id": self.target_id,
            "members": [member.to_dict() for member in self.members],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RebuildSourceCachePublicationOffer",
    ) -> RebuildSourceCachePublicationOffer:
        if (
            isinstance(value, dict)
            and value.get("schema")
            == LEGACY_REBUILD_SOURCE_CACHE_PUBLICATION_OFFER_SCHEMA
        ):
            value = {**value, "schema": cls.SCHEMA}
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "control_identity",
                    "decision_identity",
                    "lifecycle_request_identity",
                    "component_lock_identities",
                    "receipt_subject_identity",
                    "acceptance_evidence_identity",
                    "workspace_admission_identity",
                    "target_id",
                    "members",
                }
            ),
        )
        return cls(
            control_identity=ContentIdentity.from_dict(
                data["control_identity"], path=f"{path}.control_identity"
            ),
            decision_identity=ContentIdentity.from_dict(
                data["decision_identity"], path=f"{path}.decision_identity"
            ),
            lifecycle_request_identity=ContentIdentity.from_dict(
                data["lifecycle_request_identity"],
                path=f"{path}.lifecycle_request_identity",
            ),
            component_lock_identities=parse_tuple(
                data["component_lock_identities"],
                f"{path}.component_lock_identities",
                ContentIdentity.from_dict,
            ),
            receipt_subject_identity=ContentIdentity.from_dict(
                data["receipt_subject_identity"],
                path=f"{path}.receipt_subject_identity",
            ),
            acceptance_evidence_identity=ContentIdentity.from_dict(
                data["acceptance_evidence_identity"],
                path=f"{path}.acceptance_evidence_identity",
            ),
            workspace_admission_identity=ContentIdentity.from_dict(
                data["workspace_admission_identity"],
                path=f"{path}.workspace_admission_identity",
            ),
            target_id=string_value(data["target_id"], f"{path}.target_id"),
            members=parse_tuple(
                data["members"],
                f"{path}.members",
                RebuildSourceCachePublicationMember.from_dict,
            ),
        )


__all__ = [
    "MAX_REBUILD_COMPONENT_LOCKS",
    "MAX_REBUILD_SOURCE_CACHE_CANDIDATES_PER_ITEM",
    "MAX_REBUILD_SOURCE_CACHE_DECISION_ITEMS",
    "MAX_REBUILD_SOURCE_CACHE_PUBLICATION_MEMBERS",
    "MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES",
    "MAX_REBUILD_SOURCE_CACHE_TARGETS",
    "REBUILD_SOURCE_CACHE_CONTROL_SCHEMA",
    "REBUILD_PROJECT_AUTHORITY_SCHEMA",
    "REBUILD_SOURCE_CACHE_DERIVATION_MANIFEST_SCHEMA",
    "REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA",
    "REBUILD_SOURCE_CACHE_DECISION_ITEM_SCHEMA",
    "REBUILD_SOURCE_CACHE_DECISION_SCHEMA",
    "REBUILD_SOURCE_CACHE_OPERATOR_ROOT_SCHEMA",
    "REBUILD_SOURCE_CACHE_LIFECYCLE_BINDING_SCHEMA",
    "REBUILD_SOURCE_CACHE_LIFECYCLE_EVIDENCE_SET_SCHEMA",
    "REBUILD_SOURCE_CACHE_LIFECYCLE_MEMBER_SCHEMA",
    "REBUILD_SOURCE_CACHE_PUBLICATION_MEMBER_SCHEMA",
    "REBUILD_SOURCE_CACHE_PUBLICATION_OFFER_SCHEMA",
    "REBUILD_SOURCE_CACHE_PUBLICATION_RESULT_SCHEMA",
    "RebuildSourceCacheControl",
    "RebuildSourceCacheDecision",
    "RebuildSourceCacheDecisionItem",
    "RebuildSourceCacheLifecycleBinding",
    "RebuildSourceCacheLifecycleMember",
    "RebuildSourceCacheOperatorRoot",
    "RebuildSourceCacheOutcome",
    "RebuildSourceCachePublicationMember",
    "RebuildSourceCachePublicationOffer",
    "rebuild_project_authority_identity",
]

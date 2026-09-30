"""Provider-neutral deterministic capability-based provider resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    parse_tuple,
    string_tuple,
    string_value,
)
from .identity import ContentIdentity, canonical_identity, contract_identity

_PORTABLE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")


def portable_name(value: object, path: str) -> str:
    raw = string_value(value, path, max_length=127)
    if _PORTABLE_NAME.fullmatch(raw) is None:
        fail(path, "must be a portable lower-case identifier")
    return raw


PROVIDER_CAPABILITY_SET_SCHEMA = "urn:literate-ai:schema:v2:provider-capability-set"
PROVIDER_RESOLUTION_REQUEST_SCHEMA = (
    "urn:literate-ai:schema:v2:provider-resolution-request"
)
PROVIDER_RESOLUTION_SCHEMA = "urn:literate-ai:schema:v2:provider-resolution"
PROVIDER_RESOLUTION_DECLARATION_SCHEMA = (
    "urn:literate-ai:schema:v2:provider-resolution-declaration"
)
PROVIDER_OVERRIDE_DECLARATION_SCHEMA = (
    "urn:literate-ai:schema:v2:provider-override-declaration"
)

PROVIDER_RESOLVER_POLICY_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/provider-resolver-policy@1",
        "preferred": "always-when-sufficient",
        "fallback": "ordered-only-after-preferred-insufficient",
        "tie_break": "fallback-order-then-provider-id",
        "override": "exact-flavor-declaration-only-when-sufficient",
        "unsatisfied": "fail-closed",
    }
)


class ProviderResolutionError(ValueError):
    """A stable provider catalog or resolution failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySet:
    """One stable provider identity and its exact advertised capabilities."""

    provider_id: str
    capabilities: tuple[str, ...]

    SCHEMA: ClassVar[str] = PROVIDER_CAPABILITY_SET_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.provider_id, "ProviderCapabilitySet.provider_id")
        capabilities = string_tuple(
            self.capabilities, "ProviderCapabilitySet.capabilities"
        )
        if not capabilities or len(capabilities) > 256:
            fail(
                "ProviderCapabilitySet.capabilities",
                "must contain between 1 and 256 capabilities",
            )
        if capabilities != tuple(sorted(set(capabilities))):
            fail(
                "ProviderCapabilitySet.capabilities",
                "must be unique and use canonical lexical order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "provider_id": self.provider_id,
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProviderCapabilitySet"
    ) -> ProviderCapabilitySet:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"provider_id", "capabilities"}),
        )
        return cls(
            portable_name(data["provider_id"], f"{path}.provider_id"),
            string_tuple(data["capabilities"], f"{path}.capabilities"),
        )


@dataclass(frozen=True, slots=True)
class ProviderOverrideDeclaration:
    """One exact Flavor-owned override of a named provider resolution."""

    resolution_id: str
    provider_id: str

    SCHEMA: ClassVar[str] = PROVIDER_OVERRIDE_DECLARATION_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.resolution_id, "ProviderOverrideDeclaration.resolution_id")
        portable_name(self.provider_id, "ProviderOverrideDeclaration.provider_id")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "resolution_id": self.resolution_id,
            "provider_id": self.provider_id,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProviderOverrideDeclaration"
    ) -> ProviderOverrideDeclaration:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"resolution_id", "provider_id"}),
        )
        return cls(
            portable_name(data["resolution_id"], f"{path}.resolution_id"),
            portable_name(data["provider_id"], f"{path}.provider_id"),
        )


@dataclass(frozen=True, slots=True)
class ProviderResolutionDeclaration:
    """Human-owned provider catalog and preferred/fallback selection policy."""

    resolution_id: str
    preferred_provider: str
    required_capabilities: tuple[str, ...]
    fallback_order: tuple[str, ...]
    capability_sets: tuple[ProviderCapabilitySet, ...]

    SCHEMA: ClassVar[str] = PROVIDER_RESOLUTION_DECLARATION_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.resolution_id, "ProviderResolutionDeclaration.resolution_id")
        ProviderResolutionRequest(
            preferred_provider=self.preferred_provider,
            required_capabilities=self.required_capabilities,
            fallback_order=self.fallback_order,
            resolution_id=self.resolution_id,
        )
        if not self.capability_sets or len(self.capability_sets) > 256:
            fail(
                "ProviderResolutionDeclaration.capability_sets",
                "must contain between 1 and 256 provider capability sets",
            )
        if any(
            not isinstance(item, ProviderCapabilitySet) for item in self.capability_sets
        ):
            fail(
                "ProviderResolutionDeclaration.capability_sets",
                "must contain ProviderCapabilitySet values",
            )
        provider_ids = tuple(item.provider_id for item in self.capability_sets)
        if provider_ids != tuple(sorted(set(provider_ids))):
            fail(
                "ProviderResolutionDeclaration.capability_sets",
                "must use unique canonical provider-ID order",
            )
        referenced = {self.preferred_provider, *self.fallback_order}
        missing = sorted(referenced - set(provider_ids))
        if missing:
            fail(
                "ProviderResolutionDeclaration.capability_sets",
                "must declare every preferred and fallback provider: "
                + ", ".join(missing),
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def request(
        self,
        override: ProviderOverrideDeclaration | None = None,
        *,
        override_provenance_identity: ContentIdentity | None = None,
    ) -> ProviderResolutionRequest:
        if override is None:
            if override_provenance_identity is not None:
                fail(
                    "ProviderResolutionDeclaration.override_provenance_identity",
                    "cannot supply override provenance without a declaration",
                )
            return ProviderResolutionRequest(
                preferred_provider=self.preferred_provider,
                required_capabilities=self.required_capabilities,
                fallback_order=self.fallback_order,
                resolution_id=self.resolution_id,
            )
        if not isinstance(override, ProviderOverrideDeclaration):
            raise TypeError("provider override must be a ProviderOverrideDeclaration")
        if override.resolution_id != self.resolution_id:
            fail(
                "ProviderResolutionDeclaration.resolution_id",
                "override names a different provider resolution",
            )
        if not isinstance(override_provenance_identity, ContentIdentity):
            fail(
                "ProviderResolutionDeclaration.override_provenance_identity",
                "Flavor override requires an exact provenance identity",
            )
        return ProviderResolutionRequest(
            preferred_provider=self.preferred_provider,
            required_capabilities=self.required_capabilities,
            fallback_order=self.fallback_order,
            override_provider=override.provider_id,
            override_declaration_identity=override.identity,
            override_provenance_identity=override_provenance_identity,
            resolution_id=self.resolution_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "resolution_id": self.resolution_id,
            "preferred_provider": self.preferred_provider,
            "required_capabilities": list(self.required_capabilities),
            "fallback_order": list(self.fallback_order),
            "capability_sets": [item.to_dict() for item in self.capability_sets],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProviderResolutionDeclaration"
    ) -> ProviderResolutionDeclaration:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "resolution_id",
                    "preferred_provider",
                    "required_capabilities",
                    "fallback_order",
                    "capability_sets",
                }
            ),
        )
        return cls(
            resolution_id=portable_name(data["resolution_id"], f"{path}.resolution_id"),
            preferred_provider=portable_name(
                data["preferred_provider"], f"{path}.preferred_provider"
            ),
            required_capabilities=string_tuple(
                data["required_capabilities"], f"{path}.required_capabilities"
            ),
            fallback_order=string_tuple(
                data["fallback_order"], f"{path}.fallback_order"
            ),
            capability_sets=parse_tuple(
                data["capability_sets"],
                f"{path}.capability_sets",
                ProviderCapabilitySet.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderResolutionRequest:
    """Exact preferred/fallback policy and optional Flavor override authority."""

    preferred_provider: str
    required_capabilities: tuple[str, ...]
    fallback_order: tuple[str, ...]
    override_provider: str | None = None
    override_declaration_identity: ContentIdentity | None = None
    override_provenance_identity: ContentIdentity | None = None
    resolution_id: str = "default"

    SCHEMA: ClassVar[str] = PROVIDER_RESOLUTION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.resolution_id, "ProviderResolutionRequest.resolution_id")
        portable_name(
            self.preferred_provider,
            "ProviderResolutionRequest.preferred_provider",
        )
        required = string_tuple(
            self.required_capabilities,
            "ProviderResolutionRequest.required_capabilities",
        )
        if not required or len(required) > 256:
            fail(
                "ProviderResolutionRequest.required_capabilities",
                "must contain between 1 and 256 capabilities",
            )
        if required != tuple(sorted(set(required))):
            fail(
                "ProviderResolutionRequest.required_capabilities",
                "must be unique and use canonical lexical order",
            )
        fallback = string_tuple(
            self.fallback_order,
            "ProviderResolutionRequest.fallback_order",
        )
        if len(fallback) > 256:
            fail(
                "ProviderResolutionRequest.fallback_order",
                "must contain at most 256 providers",
            )
        if len(fallback) != len(set(fallback)):
            fail(
                "ProviderResolutionRequest.fallback_order",
                "must not repeat providers",
            )
        for index, provider_id in enumerate(fallback):
            portable_name(
                provider_id, f"ProviderResolutionRequest.fallback_order[{index}]"
            )
        if self.preferred_provider in fallback:
            fail(
                "ProviderResolutionRequest.fallback_order",
                "must not repeat the preferred provider",
            )
        override_values = (
            self.override_provider,
            self.override_declaration_identity,
            self.override_provenance_identity,
        )
        if any(item is not None for item in override_values) and not all(
            item is not None for item in override_values
        ):
            fail(
                "ProviderResolutionRequest.override_provider",
                "override provider, declaration identity, and provenance identity "
                "must be supplied together",
            )
        if self.override_provider is not None:
            portable_name(
                self.override_provider,
                "ProviderResolutionRequest.override_provider",
            )
        for name in (
            "override_declaration_identity",
            "override_provenance_identity",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ContentIdentity):
                fail(f"ProviderResolutionRequest.{name}", "must be a ContentIdentity")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "preferred_provider": self.preferred_provider,
            "required_capabilities": list(self.required_capabilities),
            "fallback_order": list(self.fallback_order),
            "override_provider": self.override_provider,
            "override_declaration_identity": (
                None
                if self.override_declaration_identity is None
                else self.override_declaration_identity.to_dict()
            ),
            "override_provenance_identity": (
                None
                if self.override_provenance_identity is None
                else self.override_provenance_identity.to_dict()
            ),
        }
        if self.resolution_id != "default":
            value["resolution_id"] = self.resolution_id
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProviderResolutionRequest"
    ) -> ProviderResolutionRequest:
        names = frozenset(
            {
                "preferred_provider",
                "required_capabilities",
                "fallback_order",
                "override_provider",
                "override_declaration_identity",
                "override_provenance_identity",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset({"resolution_id"}),
        )

        def optional_identity(name: str) -> ContentIdentity | None:
            raw = data[name]
            return (
                None
                if raw is None
                else ContentIdentity.from_dict(raw, path=f"{path}.{name}")
            )

        override = data["override_provider"]
        if override is not None:
            override = portable_name(override, f"{path}.override_provider")
        return cls(
            portable_name(data["preferred_provider"], f"{path}.preferred_provider"),
            string_tuple(
                data["required_capabilities"],
                f"{path}.required_capabilities",
            ),
            string_tuple(data["fallback_order"], f"{path}.fallback_order"),
            override,
            optional_identity("override_declaration_identity"),
            optional_identity("override_provenance_identity"),
            portable_name(
                data.get("resolution_id", "default"), f"{path}.resolution_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    """Selected provider and complete evidence bound into lock and plan identity."""

    request: ProviderResolutionRequest
    selected_provider: str
    evaluated_capability_set_identities: tuple[ContentIdentity, ...]
    catalog_identity: ContentIdentity
    resolver_policy_identity: ContentIdentity
    fallback_reason: str | None
    override_declaration_identity: ContentIdentity | None
    override_provenance_identity: ContentIdentity | None

    SCHEMA: ClassVar[str] = PROVIDER_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request, ProviderResolutionRequest):
            fail("ProviderResolution.request", "must be a ProviderResolutionRequest")
        portable_name(self.selected_provider, "ProviderResolution.selected_provider")
        identities = self.evaluated_capability_set_identities
        if not identities or any(
            not isinstance(item, ContentIdentity) for item in identities
        ):
            fail(
                "ProviderResolution.evaluated_capability_set_identities",
                "must contain ContentIdentity values",
            )
        uris = tuple(item.uri for item in identities)
        if uris != tuple(sorted(set(uris))):
            fail(
                "ProviderResolution.evaluated_capability_set_identities",
                "must be unique and use canonical identity order",
            )
        for name in ("catalog_identity", "resolver_policy_identity"):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"ProviderResolution.{name}", "must be a ContentIdentity")
        if self.resolver_policy_identity != PROVIDER_RESOLVER_POLICY_IDENTITY:
            fail(
                "ProviderResolution.resolver_policy_identity",
                "must bind the canonical provider resolver policy",
            )
        overridden = self.request.override_provider is not None
        if overridden:
            if (
                self.selected_provider != self.request.override_provider
                or self.fallback_reason is not None
                or self.override_declaration_identity
                != self.request.override_declaration_identity
                or self.override_provenance_identity
                != self.request.override_provenance_identity
            ):
                fail(
                    "ProviderResolution",
                    "override selection must preserve exact request provenance",
                )
        elif (
            self.override_declaration_identity is not None
            or self.override_provenance_identity is not None
        ):
            fail(
                "ProviderResolution",
                "non-override selection cannot invent override provenance",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request": self.request.to_dict(),
            "selected_provider": self.selected_provider,
            "evaluated_capability_set_identities": [
                item.to_dict() for item in self.evaluated_capability_set_identities
            ],
            "catalog_identity": self.catalog_identity.to_dict(),
            "resolver_policy_identity": self.resolver_policy_identity.to_dict(),
            "fallback_reason": self.fallback_reason,
            "override_declaration_identity": (
                None
                if self.override_declaration_identity is None
                else self.override_declaration_identity.to_dict()
            ),
            "override_provenance_identity": (
                None
                if self.override_provenance_identity is None
                else self.override_provenance_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProviderResolution"
    ) -> ProviderResolution:
        names = frozenset(
            {
                "request",
                "selected_provider",
                "evaluated_capability_set_identities",
                "catalog_identity",
                "resolver_policy_identity",
                "fallback_reason",
                "override_declaration_identity",
                "override_provenance_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)

        def optional_identity(name: str) -> ContentIdentity | None:
            raw = data[name]
            return (
                None
                if raw is None
                else ContentIdentity.from_dict(raw, path=f"{path}.{name}")
            )

        fallback_reason = data["fallback_reason"]
        if fallback_reason is not None and not isinstance(fallback_reason, str):
            fail(f"{path}.fallback_reason", "must be a string or null")
        return cls(
            ProviderResolutionRequest.from_dict(
                data["request"], path=f"{path}.request"
            ),
            portable_name(data["selected_provider"], f"{path}.selected_provider"),
            parse_tuple(
                data["evaluated_capability_set_identities"],
                f"{path}.evaluated_capability_set_identities",
                ContentIdentity.from_dict,
            ),
            ContentIdentity.from_dict(
                data["catalog_identity"], path=f"{path}.catalog_identity"
            ),
            ContentIdentity.from_dict(
                data["resolver_policy_identity"],
                path=f"{path}.resolver_policy_identity",
            ),
            fallback_reason,
            optional_identity("override_declaration_identity"),
            optional_identity("override_provenance_identity"),
        )


def resolve_provider(
    request: ProviderResolutionRequest,
    capability_sets: tuple[ProviderCapabilitySet, ...],
) -> ProviderResolution:
    """Select exactly one sufficient provider under the canonical policy."""

    if not isinstance(request, ProviderResolutionRequest):
        raise TypeError("provider resolution requires a ProviderResolutionRequest")
    if not capability_sets or any(
        not isinstance(item, ProviderCapabilitySet) for item in capability_sets
    ):
        raise ProviderResolutionError(
            "provider_resolution.catalog_invalid",
            "provider catalog must contain ProviderCapabilitySet values",
        )
    providers = {item.provider_id: item for item in capability_sets}
    if len(providers) != len(capability_sets):
        raise ProviderResolutionError(
            "provider_resolution.catalog_duplicate",
            "provider catalog repeats a provider identity",
        )
    catalog = tuple(sorted(capability_sets, key=lambda item: item.provider_id))
    catalog_identity = canonical_identity(
        {
            "schema": "literate-ai/provider-capability-catalog@1",
            "capability_sets": [item.identity.uri for item in catalog],
        }
    )
    required = set(request.required_capabilities)

    def candidate(provider_id: str) -> ProviderCapabilitySet:
        value = providers.get(provider_id)
        if value is None:
            raise ProviderResolutionError(
                "provider_resolution.provider_unknown",
                f"provider {provider_id!r} is absent from the exact catalog",
            )
        return value

    def sufficient(value: ProviderCapabilitySet) -> bool:
        return required <= set(value.capabilities)

    evaluated: dict[str, ProviderCapabilitySet] = {}

    def evaluate(provider_id: str) -> ProviderCapabilitySet:
        value = candidate(provider_id)
        evaluated[provider_id] = value
        return value

    override = request.override_provider
    fallback_reason: str | None = None
    if override is not None:
        selected = evaluate(override)
        if not sufficient(selected):
            missing = sorted(required - set(selected.capabilities))
            raise ProviderResolutionError(
                "provider_resolution.override_insufficient",
                f"Flavor override {override!r} lacks required capabilities: "
                + ", ".join(missing),
            )
    else:
        preferred = evaluate(request.preferred_provider)
        if sufficient(preferred):
            selected = preferred
        else:
            missing = sorted(required - set(preferred.capabilities))
            fallback_reason = (
                f"preferred provider {preferred.provider_id!r} lacks: "
                + ", ".join(missing)
            )
            selected = None
            for provider_id in request.fallback_order:
                fallback = evaluate(provider_id)
                if sufficient(fallback):
                    selected = fallback
                    break
            if selected is None:
                raise ProviderResolutionError(
                    "provider_resolution.unsatisfied",
                    "no provider satisfies every required capability",
                )

    return ProviderResolution(
        request=request,
        selected_provider=selected.provider_id,
        evaluated_capability_set_identities=tuple(
            sorted(
                (item.identity for item in evaluated.values()),
                key=lambda item: item.uri,
            )
        ),
        catalog_identity=catalog_identity,
        resolver_policy_identity=PROVIDER_RESOLVER_POLICY_IDENTITY,
        fallback_reason=fallback_reason,
        override_declaration_identity=request.override_declaration_identity,
        override_provenance_identity=request.override_provenance_identity,
    )


def resolve_provider_declaration(
    declaration: ProviderResolutionDeclaration,
    override: ProviderOverrideDeclaration | None = None,
    *,
    override_provenance_identity: ContentIdentity | None = None,
) -> ProviderResolution:
    """Resolve one authored declaration with an optional exact Flavor override."""

    if not isinstance(declaration, ProviderResolutionDeclaration):
        raise TypeError("provider resolution requires a ProviderResolutionDeclaration")
    return resolve_provider(
        declaration.request(
            override,
            override_provenance_identity=override_provenance_identity,
        ),
        declaration.capability_sets,
    )


__all__ = [
    "PROVIDER_CAPABILITY_SET_SCHEMA",
    "PROVIDER_OVERRIDE_DECLARATION_SCHEMA",
    "PROVIDER_RESOLUTION_DECLARATION_SCHEMA",
    "PROVIDER_RESOLUTION_REQUEST_SCHEMA",
    "PROVIDER_RESOLUTION_SCHEMA",
    "PROVIDER_RESOLVER_POLICY_IDENTITY",
    "ProviderCapabilitySet",
    "ProviderOverrideDeclaration",
    "ProviderResolution",
    "ProviderResolutionDeclaration",
    "ProviderResolutionError",
    "ProviderResolutionRequest",
    "resolve_provider",
    "resolve_provider_declaration",
]

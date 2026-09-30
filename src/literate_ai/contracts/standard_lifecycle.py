"""Versioned wire contracts for the Standard post-source lifecycle boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from literate_ai.security import BuildAuthorization, BuildRequest

from ._validation import contract_fields, fail, parse_tuple, unique
from .executable_components import (
    ArtifactMaterializationPlan,
    ComponentBuildManifest,
    CompositeBuildRequest,
    SourceGenerationResumeCandidate,
)
from .identity import ContentIdentity, contract_identity

STANDARD_COMPONENT_BUILD_INTENT_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-build-intent"
)
STANDARD_BUILD_AUTHORIZATION_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-build-authorization"
)
STANDARD_COMPONENT_BUILD_PLAN_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-build-plan"
)
STANDARD_SOURCE_CACHE_MEMBERSHIP_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-source-cache-membership"
)

MAX_PROVIDER_ARTIFACT_IDENTITIES = 16384


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _provider_identities(
    values: tuple[ContentIdentity, ...], path: str
) -> tuple[ContentIdentity, ...]:
    if len(values) > MAX_PROVIDER_ARTIFACT_IDENTITIES:
        fail(
            path,
            f"must contain at most {MAX_PROVIDER_ARTIFACT_IDENTITIES} identities",
        )
    for index, identity in enumerate(values):
        _identity(identity, f"{path}[{index}]")
    uris = tuple(identity.uri for identity in values)
    unique(uris, path, "provider artifact identities")
    if uris != tuple(sorted(uris)):
        fail(path, "must use canonical identity order")
    return values


@dataclass(frozen=True, slots=True)
class StandardSourceCacheMembershipDocument:
    """One previously accepted source result eligible only for current revalidation."""

    component_revision: ContentIdentity
    generation_key_identity: ContentIdentity
    generation: SourceGenerationResumeCandidate
    acceptance_identity: ContentIdentity

    SCHEMA: ClassVar[str] = STANDARD_SOURCE_CACHE_MEMBERSHIP_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.component_revision,
            "StandardSourceCacheMembershipDocument.component_revision",
        )
        _identity(
            self.generation_key_identity,
            "StandardSourceCacheMembershipDocument.generation_key_identity",
        )
        if not isinstance(self.generation, SourceGenerationResumeCandidate):
            fail(
                "StandardSourceCacheMembershipDocument.generation",
                "must be a SourceGenerationResumeCandidate",
            )
        _identity(
            self.acceptance_identity,
            "StandardSourceCacheMembershipDocument.acceptance_identity",
        )
        candidate = self.generation.output.candidate
        if (
            candidate.component_revision != self.component_revision
            or candidate.generation_key_identity != self.generation_key_identity
        ):
            fail(
                "StandardSourceCacheMembershipDocument.generation",
                "must bind the exact Component revision and generation key",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "generation": self.generation.to_dict(),
            "acceptance_identity": self.acceptance_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardSourceCacheMembershipDocument",
    ) -> StandardSourceCacheMembershipDocument:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "generation_key_identity",
                    "generation",
                    "acceptance_identity",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            SourceGenerationResumeCandidate.from_dict(
                data["generation"], path=f"{path}.generation"
            ),
            ContentIdentity.from_dict(
                data["acceptance_identity"], path=f"{path}.acceptance_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardComponentBuildIntentDocument:
    """Authorization-free intent derived from one exact accepted source tree."""

    component_revision: ContentIdentity
    source_tree_identity: ContentIdentity
    source_bundle_identity: ContentIdentity
    build_request: BuildRequest
    provider_artifact_identities: tuple[ContentIdentity, ...] = ()
    package_artifact_identities: tuple[ContentIdentity, ...] = ()
    native_sdk_input_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = STANDARD_COMPONENT_BUILD_INTENT_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.component_revision,
            "StandardComponentBuildIntentDocument.component_revision",
        )
        _identity(
            self.source_tree_identity,
            "StandardComponentBuildIntentDocument.source_tree_identity",
        )
        _identity(
            self.source_bundle_identity,
            "StandardComponentBuildIntentDocument.source_bundle_identity",
        )
        if not isinstance(self.build_request, BuildRequest):
            fail(
                "StandardComponentBuildIntentDocument.build_request",
                "must be a BuildRequest",
            )
        if (
            self.build_request.effective_revision_digest != self.component_revision.uri
            or self.build_request.source_bundle_digest
            != self.source_bundle_identity.uri
        ):
            fail(
                "StandardComponentBuildIntentDocument.build_request",
                "must bind the exact Component revision and source bundle",
            )
        _provider_identities(
            self.provider_artifact_identities,
            "StandardComponentBuildIntentDocument.provider_artifact_identities",
        )
        _provider_identities(
            self.package_artifact_identities,
            "StandardComponentBuildIntentDocument.package_artifact_identities",
        )

        if not isinstance(self.native_sdk_input_identities, tuple):
            fail(
                "StandardComponentBuildIntentDocument.native_sdk_input_identities",
                "must be a tuple",
            )
        _provider_identities(
            self.native_sdk_input_identities,
            "StandardComponentBuildIntentDocument.native_sdk_input_identities",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def build_request_identity(self) -> ContentIdentity:
        return contract_identity(self.build_request)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "source_bundle_identity": self.source_bundle_identity.to_dict(),
            "build_request": self.build_request.to_dict(),
            "provider_artifact_identities": [
                item.to_dict() for item in self.provider_artifact_identities
            ],
        }
        if self.package_artifact_identities:
            value["package_artifact_identities"] = [
                item.to_dict() for item in self.package_artifact_identities
            ]
        if self.native_sdk_input_identities:
            value["native_sdk_input_identities"] = [
                item.to_dict() for item in self.native_sdk_input_identities
            ]
        return value

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardComponentBuildIntentDocument",
    ) -> StandardComponentBuildIntentDocument:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "source_tree_identity",
                    "source_bundle_identity",
                    "build_request",
                    "provider_artifact_identities",
                }
            ),
            optional=frozenset(
                {"package_artifact_identities", "native_sdk_input_identities"}
            ),
        )
        try:
            build_request = BuildRequest.from_dict(data["build_request"])
        except (TypeError, ValueError) as exc:
            fail(f"{path}.build_request", str(exc))
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            ContentIdentity.from_dict(
                data["source_bundle_identity"], path=f"{path}.source_bundle_identity"
            ),
            build_request,
            parse_tuple(
                data["provider_artifact_identities"],
                f"{path}.provider_artifact_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data.get("package_artifact_identities", []),
                f"{path}.package_artifact_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data.get("native_sdk_input_identities", []),
                f"{path}.native_sdk_input_identities",
                ContentIdentity.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardBuildAuthorizationDocument:
    """One indexed intent and the exact typed grant issued for its request."""

    build_intent_identity: ContentIdentity
    build_request_identity: ContentIdentity
    index_identity: ContentIdentity
    grant: BuildAuthorization

    SCHEMA: ClassVar[str] = STANDARD_BUILD_AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "build_intent_identity",
            "build_request_identity",
            "index_identity",
        ):
            _identity(
                getattr(self, name),
                f"StandardBuildAuthorizationDocument.{name}",
            )
        if not isinstance(self.grant, BuildAuthorization):
            fail(
                "StandardBuildAuthorizationDocument.grant",
                "must be a BuildAuthorization",
            )
        if self.grant.request_digest != self.build_request_identity.uri:
            fail(
                "StandardBuildAuthorizationDocument.grant.request_digest",
                "must bind the exact build request identity",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def authorization_identity(self) -> ContentIdentity:
        return contract_identity(self.grant)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "build_intent_identity": self.build_intent_identity.to_dict(),
            "build_request_identity": self.build_request_identity.to_dict(),
            "index_identity": self.index_identity.to_dict(),
            "grant": self.grant.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardBuildAuthorizationDocument",
    ) -> StandardBuildAuthorizationDocument:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "build_intent_identity",
                    "build_request_identity",
                    "index_identity",
                    "grant",
                }
            ),
        )
        try:
            grant = BuildAuthorization.from_dict(data["grant"])
        except (TypeError, ValueError) as exc:
            fail(f"{path}.grant", str(exc))
        return cls(
            ContentIdentity.from_dict(
                data["build_intent_identity"],
                path=f"{path}.build_intent_identity",
            ),
            ContentIdentity.from_dict(
                data["build_request_identity"],
                path=f"{path}.build_request_identity",
            ),
            ContentIdentity.from_dict(
                data["index_identity"], path=f"{path}.index_identity"
            ),
            grant,
        )


@dataclass(frozen=True, slots=True)
class StandardComponentBuildPlanDocument:
    """Final build plan retaining the complete existing composite-build contracts."""

    component_revision: ContentIdentity
    manifest: ComponentBuildManifest
    materialization: ArtifactMaterializationPlan
    request: CompositeBuildRequest
    provider_artifact_identities: tuple[ContentIdentity, ...] = ()
    package_artifact_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = STANDARD_COMPONENT_BUILD_PLAN_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.component_revision,
            "StandardComponentBuildPlanDocument.component_revision",
        )
        for value, expected_type, name in (
            (self.manifest, ComponentBuildManifest, "manifest"),
            (self.materialization, ArtifactMaterializationPlan, "materialization"),
            (self.request, CompositeBuildRequest, "request"),
        ):
            if not isinstance(value, expected_type):
                fail(
                    f"StandardComponentBuildPlanDocument.{name}",
                    f"must be a {expected_type.__name__}",
                )
        if (
            self.manifest.component_revision != self.component_revision
            or self.request.component_revision != self.component_revision
        ):
            fail(
                "StandardComponentBuildPlanDocument.component_revision",
                "must match the manifest and composite request",
            )
        if (
            self.request.component_build_manifest_identity != self.manifest.identity
            or self.request.source_tree_identity != self.manifest.source_tree_identity
            or self.materialization.source_tree_identity
            != self.manifest.source_tree_identity
            or self.request.materialization_plan_identity
            != self.materialization.identity
            or self.request.build_system_toolchain_identity
            != self.manifest.build_system_driver_identity
        ):
            fail(
                "StandardComponentBuildPlanDocument",
                "nested build contracts do not bind the same exact authorities",
            )
        requested_actions = tuple(
            sorted(
                (item.action for item in self.request.sub_actions),
                key=lambda item: item.action_id,
            )
        )
        if requested_actions != self.manifest.actions:
            fail(
                "StandardComponentBuildPlanDocument.request.sub_actions",
                "must contain every and only manifest action",
            )
        if self.request.declared_output_ids != tuple(
            item.export_id for item in self.manifest.export_declarations
        ):
            fail(
                "StandardComponentBuildPlanDocument.request.declared_output_ids",
                "must contain every and only manifest output",
            )
        _provider_identities(
            self.provider_artifact_identities,
            "StandardComponentBuildPlanDocument.provider_artifact_identities",
        )
        _provider_identities(
            self.package_artifact_identities,
            "StandardComponentBuildPlanDocument.package_artifact_identities",
        )
        action_dependencies = tuple(
            sorted(
                {
                    item.identity.uri: item.identity
                    for action in self.manifest.actions
                    for item in action.dependency_artifacts
                }.values(),
                key=lambda item: item.uri,
            )
        )
        if action_dependencies != self.provider_artifact_identities:
            fail(
                "StandardComponentBuildPlanDocument.provider_artifact_identities",
                "must bind every and only build/runtime action dependency",
            )
        package_action_dependencies = tuple(
            sorted(
                {
                    item.identity.uri: item.identity
                    for action in self.manifest.actions
                    for item in action.package_dependency_artifacts
                }.values(),
                key=lambda item: item.uri,
            )
        )
        if package_action_dependencies != self.package_artifact_identities:
            fail(
                "StandardComponentBuildPlanDocument.package_artifact_identities",
                "must bind every and only packaging action dependency",
            )
        export_dependencies = tuple(
            sorted(
                {
                    item.uri: item
                    for declaration in self.manifest.export_declarations
                    for item in declaration.dependency_artifact_identities
                }.values(),
                key=lambda item: item.uri,
            )
        )
        expected_export_dependencies = tuple(
            sorted(
                {
                    item.uri: item
                    for item in (
                        *self.provider_artifact_identities,
                        *self.package_artifact_identities,
                    )
                }.values(),
                key=lambda item: item.uri,
            )
        )
        if export_dependencies != expected_export_dependencies:
            fail(
                "StandardComponentBuildPlanDocument.package_artifact_identities",
                "build exports must bind the exact build/runtime and package closure",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "manifest": self.manifest.to_dict(),
            "materialization": self.materialization.to_dict(),
            "request": self.request.to_dict(),
            "provider_artifact_identities": [
                item.to_dict() for item in self.provider_artifact_identities
            ],
        }
        if self.package_artifact_identities:
            value["package_artifact_identities"] = [
                item.to_dict() for item in self.package_artifact_identities
            ]
        return value

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardComponentBuildPlanDocument",
    ) -> StandardComponentBuildPlanDocument:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "manifest",
                    "materialization",
                    "request",
                    "provider_artifact_identities",
                }
            ),
            optional=frozenset({"package_artifact_identities"}),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ComponentBuildManifest.from_dict(data["manifest"], path=f"{path}.manifest"),
            ArtifactMaterializationPlan.from_dict(
                data["materialization"], path=f"{path}.materialization"
            ),
            CompositeBuildRequest.from_dict(data["request"], path=f"{path}.request"),
            parse_tuple(
                data["provider_artifact_identities"],
                f"{path}.provider_artifact_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data.get("package_artifact_identities", []),
                f"{path}.package_artifact_identities",
                ContentIdentity.from_dict,
            ),
        )


__all__ = [
    "MAX_PROVIDER_ARTIFACT_IDENTITIES",
    "STANDARD_BUILD_AUTHORIZATION_SCHEMA",
    "STANDARD_COMPONENT_BUILD_INTENT_SCHEMA",
    "STANDARD_COMPONENT_BUILD_PLAN_SCHEMA",
    "STANDARD_SOURCE_CACHE_MEMBERSHIP_SCHEMA",
    "StandardBuildAuthorizationDocument",
    "StandardComponentBuildIntentDocument",
    "StandardComponentBuildPlanDocument",
    "StandardSourceCacheMembershipDocument",
]

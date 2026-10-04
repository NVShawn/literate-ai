"""Exact phase input authority, independent of immutable compilation provenance."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile

from ._validation import contract_fields, fail, parse_tuple
from .capabilities import DependencyKind
from .executable_components import ArtifactAssemblyDependency
from .executable_components.commands import (
    ComponentCommandContract,
    ComponentCommandPhase,
)
from .identity import ContentIdentity, canonical_identity, contract_identity


@dataclass(frozen=True, slots=True)
class StandardExecutionInputScope:
    """Bound inputs for execution; this document does not grant process authority."""

    execution_plan_identity: ContentIdentity
    component_revision: ContentIdentity
    build_plan_identity: ContentIdentity
    export_identities: tuple[ContentIdentity, ...]
    build_provider_artifact_identities: tuple[ContentIdentity, ...]
    runtime_dependencies: tuple[ArtifactAssemblyDependency, ...]
    transitive_provider_artifact_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:standard-execution-input-scope"

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_identity",
            "component_revision",
            "build_plan_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"StandardExecutionInputScope.{name}", "must be a ContentIdentity")
        for name in (
            "export_identities",
            "build_provider_artifact_identities",
            "transitive_provider_artifact_identities",
        ):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or len(values) > 16384
                or any(not isinstance(item, ContentIdentity) for item in values)
                or tuple(item.uri for item in values)
                != tuple(sorted({item.uri for item in values}))
            ):
                fail(
                    f"StandardExecutionInputScope.{name}",
                    "must be bounded canonical identities",
                )
        if not self.export_identities:
            fail("StandardExecutionInputScope.export_identities", "must not be empty")
        dependencies = self.runtime_dependencies
        if (
            not isinstance(dependencies, tuple)
            or len(dependencies) > 16384
            or any(
                not isinstance(item, ArtifactAssemblyDependency)
                for item in dependencies
            )
        ):
            fail(
                "StandardExecutionInputScope.runtime_dependencies",
                "must be bounded typed dependencies",
            )
        keys = tuple(item.identity.uri for item in dependencies)
        if keys != tuple(sorted(set(keys))):
            fail(
                "StandardExecutionInputScope.runtime_dependencies",
                "must be unique and canonical",
            )
        outputs = set(self.export_identities)
        runtime_ids = {item.provider_artifact_identity for item in dependencies}
        compile_ids = set(self.build_provider_artifact_identities)
        transitive = set(self.transitive_provider_artifact_identities)
        anchors = outputs | compile_ids | transitive
        nodes = anchors | runtime_ids
        if (
            outputs.intersection(compile_ids | transitive | runtime_ids)
            or transitive.intersection(compile_ids | runtime_ids)
            or any(
                item.dependency_kind is not DependencyKind.RUNTIME
                or item.consumer_artifact_identity not in nodes
                for item in dependencies
            )
        ):
            fail(
                "StandardExecutionInputScope",
                "must bind runtime providers to exact consumer exports",
            )
        adjacency = {node: set() for node in nodes}
        indegree = {node: 0 for node in nodes}
        for item in dependencies:
            children = adjacency[item.consumer_artifact_identity]
            if item.provider_artifact_identity not in children:
                children.add(item.provider_artifact_identity)
                indegree[item.provider_artifact_identity] += 1
        ready = deque(node for node in nodes if not indegree[node])
        processed = 0
        while ready:
            node = ready.popleft()
            processed += 1
            for child in adjacency[node]:
                indegree[child] -= 1
                if not indegree[child]:
                    ready.append(child)
        if processed != len(nodes):
            fail(
                "StandardExecutionInputScope",
                "runtime dependency graph must be acyclic",
            )
        reachable = set(anchors)
        pending = list(anchors)
        while pending:
            for child in adjacency[pending.pop()]:
                if child not in reachable:
                    reachable.add(child)
                    pending.append(child)
        if reachable != nodes:
            fail(
                "StandardExecutionInputScope",
                "runtime provider is outside the execution closure",
            )
        # Each edge/provider binding must cover every consumer export exactly once,
        # and an edge cannot simultaneously claim different accepted provider states.
        by_edge: dict[
            ContentIdentity,
            tuple[ContentIdentity, dict[ContentIdentity, set[ContentIdentity]]],
        ] = {}
        provider_acceptances: dict[ContentIdentity, ContentIdentity] = {}
        for item in dependencies:
            previous = provider_acceptances.setdefault(
                item.provider_artifact_identity, item.provider_acceptance_identity
            )
            if previous != item.provider_acceptance_identity:
                fail(
                    "StandardExecutionInputScope",
                    "runtime provider has inconsistent acceptance",
                )
            acceptance, providers = by_edge.setdefault(
                item.dependency_edge_identity, (item.provider_acceptance_identity, {})
            )
            if acceptance != item.provider_acceptance_identity:
                fail(
                    "StandardExecutionInputScope",
                    "runtime edge has inconsistent acceptance",
                )
            providers.setdefault(item.provider_artifact_identity, set()).add(
                item.consumer_artifact_identity
            )
        if any(
            bool(consumers & outputs) and consumers != outputs
            for _, providers in by_edge.values()
            for consumers in providers.values()
        ):
            fail(
                "StandardExecutionInputScope",
                "runtime inputs must cover every consumer export",
            )
        if len(self.provider_artifact_identities) > 16384:
            fail("StandardExecutionInputScope", "provider closure exceeds 16384 inputs")

    @property
    def provider_artifact_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(
            sorted(
                {
                    *self.build_provider_artifact_identities,
                    *self.transitive_provider_artifact_identities,
                    *(
                        item.provider_artifact_identity
                        for item in self.runtime_dependencies
                    ),
                },
                key=lambda item: item.uri,
            )
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_revision": self.component_revision.to_dict(),
            "build_plan_identity": self.build_plan_identity.to_dict(),
            "export_identities": [item.to_dict() for item in self.export_identities],
            "build_provider_artifact_identities": [
                item.to_dict() for item in self.build_provider_artifact_identities
            ],
            "runtime_dependencies": [
                item.to_dict() for item in self.runtime_dependencies
            ],
        }
        if self.transitive_provider_artifact_identities:
            value["transitive_provider_artifact_identities"] = [
                item.to_dict() for item in self.transitive_provider_artifact_identities
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "StandardExecutionInputScope"
    ) -> StandardExecutionInputScope:
        identities = (
            "execution_plan_identity",
            "component_revision",
            "build_plan_identity",
        )
        collections = ("export_identities", "build_provider_artifact_identities")
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset((*identities, *collections, "runtime_dependencies")),
            optional=frozenset({"transitive_provider_artifact_identities"}),
        )
        return cls(
            transitive_provider_artifact_identities=parse_tuple(
                data.get("transitive_provider_artifact_identities", []),
                f"{path}.transitive_provider_artifact_identities",
                ContentIdentity.from_dict,
            ),
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identities
            },
            **{
                name: parse_tuple(
                    data[name], f"{path}.{name}", ContentIdentity.from_dict
                )
                for name in collections
            },
            runtime_dependencies=parse_tuple(
                data["runtime_dependencies"],
                f"{path}.runtime_dependencies",
                ArtifactAssemblyDependency.from_dict,
            ),
        )


def standard_execution_runtime_identity(
    contract: ComponentCommandContract,
) -> ContentIdentity:
    """Bind every execution tool, including independently selected entrypoints."""
    if not isinstance(contract, ComponentCommandContract):
        fail("standard_execution_runtime_identity", "requires a command contract")
    units = (
        contract.entrypoint_command_contracts()
        if contract.is_multi_entrypoint
        else (contract,)
    )
    return canonical_identity(
        {
            "schema": "literate-ai/standard-execution-runtime-tools@1",
            "runtimes": sorted(
                {
                    unit.tool_binding(
                        ComponentCommandPhase.EXECUTE
                    ).toolchain_identity.uri
                    for unit in units
                }
            ),
        }
    )


def standard_execution_request(
    scope: StandardExecutionInputScope,
    source_tree_identity: ContentIdentity,
    command_contract_identity: ContentIdentity,
    runtime_identity: ContentIdentity,
) -> BuildRequest:
    """Bind the existing grant protocol to an exact execution-input bundle."""
    if not isinstance(scope, StandardExecutionInputScope) or any(
        not isinstance(item, ContentIdentity)
        for item in (source_tree_identity, command_contract_identity, runtime_identity)
    ):
        fail("standard_execution_request", "requires typed execution inputs")
    bundle = canonical_identity(
        {
            "schema": "literate-ai/standard-execution-input-bundle@1",
            "input_scope_identity": scope.identity.uri,
            "source_tree_identity": source_tree_identity.uri,
        }
    )
    return BuildRequest(
        effective_revision_digest=scope.component_revision.uri,
        source_bundle_digest=bundle.uri,
        builder_id=f"standard-execute:{command_contract_identity.uri}",
        toolchain_digest=runtime_identity.uri,
        sandbox_profile="local-explicit-host-process",
        requested_privileges=("execute-component",),
        allowed_outputs=("stdout", "stderr"),
    )


@dataclass(frozen=True, slots=True)
class StandardExecutionAuthority:
    """A grant for exact runtime inputs, checked live at each launch boundary."""

    input_scope: StandardExecutionInputScope
    source_tree_identity: ContentIdentity
    command_contract_identity: ContentIdentity
    runtime_identity: ContentIdentity
    grant: BuildAuthorization

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:standard-execution-authority"

    def __post_init__(self) -> None:
        request = self.request
        if (
            not isinstance(self.grant, BuildAuthorization)
            or self.grant.request_digest != canonical_identity(request.to_dict()).uri
            or self.grant.effective_revision_digest
            != self.input_scope.component_revision.uri
            or self.grant.classification_digest != self.input_scope.identity.uri
            or self.grant.privileges != request.requested_privileges
            or self.grant.profile is SecurityProfile.BLOCKED
            or self.grant.revoked
        ):
            fail(
                "StandardExecutionAuthority",
                "grant does not authorize exact execution inputs",
            )

    @property
    def request(self) -> BuildRequest:
        return standard_execution_request(
            self.input_scope,
            self.source_tree_identity,
            self.command_contract_identity,
            self.runtime_identity,
        )

    def require_valid(self, *, now: datetime) -> None:
        self.grant.require_valid(self.request, now=now)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "input_scope": self.input_scope.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "command_contract_identity": self.command_contract_identity.to_dict(),
            "runtime_identity": self.runtime_identity.to_dict(),
            "grant": self.grant.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "StandardExecutionAuthority"
    ) -> StandardExecutionAuthority:
        identities = (
            "source_tree_identity",
            "command_contract_identity",
            "runtime_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset((*identities, "input_scope", "grant")),
        )
        return cls(
            input_scope=StandardExecutionInputScope.from_dict(
                data["input_scope"], path=f"{path}.input_scope"
            ),
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identities
            },
            grant=BuildAuthorization.from_dict(data["grant"]),
        )

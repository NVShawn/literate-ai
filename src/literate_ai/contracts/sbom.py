"""Neutral identities binding CycloneDX SBOMs to Literate AI dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    fail,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .capabilities import DependencyEdge, DependencyKind
from .component_locking import ComponentLock
from .identity import (
    SCHEMA_PREFIX,
    SCHEMA_V2_PREFIX,
    ComponentRevisionRef,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)
from .repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
)

CYCLONEDX_SPEC_VERSION = "1.7"
CYCLONEDX_SCHEMA_URI = "http://cyclonedx.org/schema/bom-1.7.schema.json"
CYCLONEDX_SOURCE_SBOM_PATH = "source/.literate/sbom.cdx.json"
CYCLONEDX_MANAGED_COMPONENT_SCHEMA = f"{SCHEMA_PREFIX}cyclonedx-managed-component"
CYCLONEDX_MANAGED_EDGE_SCHEMA = f"{SCHEMA_PREFIX}cyclonedx-managed-edge"
LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA = f"{SCHEMA_PREFIX}cyclonedx-managed-graph"
CYCLONEDX_MANAGED_GRAPH_SCHEMA = f"{SCHEMA_V2_PREFIX}cyclonedx-managed-graph"
LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA = f"{SCHEMA_PREFIX}cyclonedx-bom-binding"
CYCLONEDX_BOM_BINDING_SCHEMA = f"{SCHEMA_V2_PREFIX}cyclonedx-bom-binding"
CYCLONEDX_REPOSITORY_SOURCE_RESOLUTION_SCHEMA = (
    f"{SCHEMA_PREFIX}cyclonedx-repository-source-resolution"
)

LITERATE_COMPONENT_REVISION_PROPERTY = "literate-ai:component-revision"
LITERATE_RESOLVED_GRAPH_IDENTITY_PROPERTY = "literate-ai:resolved-graph-identity"
LEGACY_LITERATE_COMPONENT_COMPOSITION_IDENTITY_PROPERTY = (
    "literate-ai:component-composition-identity"
)
LITERATE_REPOSITORY_DEPENDENCY_PROPERTY = "literate-ai:repository-source-dependency"
LITERATE_DEPENDENCY_KIND_PROPERTY = "literate-ai:dependency-kind"
LITERATE_DEPENDENCY_SCOPE_PROPERTY = "literate-ai:dependency-scope"
LITERATE_DEPENDENCY_EDGE_PROPERTY = "literate-ai:dependency-edge"
LITERATE_SOURCE_BOM_IDENTITY_PROPERTY = "literate-ai:source-bom-identity"
LITERATE_REPOSITORY_SOURCE_LOCK_PROPERTY = "literate-ai:repository-source-lock"
LITERATE_REPOSITORY_SOURCE_SNAPSHOT_PROPERTY = "literate-ai:repository-source-snapshot"
LITERATE_REPOSITORY_SOURCE_TREE_PROPERTY = "literate-ai:repository-source-tree"
LITERATE_REPOSITORY_SOURCE_RESOLVER_PROPERTY = "literate-ai:repository-source-resolver"
LITERATE_REPOSITORY_SOURCE_INDEX_PROPERTY = "literate-ai:repository-source-index"
LITERATE_REPOSITORY_SOURCE_ADMISSION_PROPERTY = (
    "literate-ai:repository-source-admission"
)
LITERATE_REPOSITORY_SOURCE_CACHE_PROPERTY = "literate-ai:repository-source-cache"

_DEPENDENCY_SCOPES = frozenset(item.value for item in DependencyKind) | {
    "test",
    "system",
}


def _adapt_legacy_resolved_graph_identity(
    value: Any,
    *,
    legacy_schema: str,
    current_schema: str,
    path: str,
) -> Any:
    """Admit only the exact v1 field rename into the v2 envelope."""

    if not isinstance(value, Mapping) or value.get("schema") != legacy_schema:
        return value
    if "resolved_graph_identity" in value:
        fail(
            path,
            "legacy records cannot contain the v2 resolved_graph_identity field",
        )
    adapted = dict(value)
    if "composition_identity" in adapted:
        adapted["resolved_graph_identity"] = adapted.pop("composition_identity")
    adapted["schema"] = current_schema
    return adapted


class CycloneDxLifecycle(StrEnum):
    SOURCE = "pre-build"
    RESOLVED = "post-build"


class ManagedComponentKind(StrEnum):
    ROOT = "root-component"
    COMPONENT = "component"
    REPOSITORY_SOURCE = "repository-source"


def component_bom_ref(revision: ContentIdentity) -> str:
    if not isinstance(revision, ContentIdentity):
        raise TypeError("Component BOM reference requires a ContentIdentity")
    return f"urn:literate-ai:component:{revision.digest}"


def repository_dependency_bom_ref(identity: ContentIdentity) -> str:
    if not isinstance(identity, ContentIdentity):
        raise TypeError("repository BOM reference requires a ContentIdentity")
    return f"urn:literate-ai:repository-source:{identity.digest}"


def repository_dependency_identity(
    dependency: RepositorySourceDependency,
) -> ContentIdentity:
    """Identify repository inventory independently of edge kind and optionality."""

    if not isinstance(dependency, RepositorySourceDependency):
        raise TypeError("repository node identity requires a repository dependency")
    return canonical_identity(
        {
            "schema": f"{SCHEMA_PREFIX}repository-source-inventory-node",
            "dependency_id": dependency.dependency_id,
            "repository_url": dependency.repository_url,
            "revision_selector": dependency.revision_selector.to_dict(),
            "integration_contract": (
                None
                if dependency.integration_contract is None
                else dependency.integration_contract.to_dict()
            ),
        }
    )


def repository_selector_version_range(
    dependency: RepositorySourceDependency,
) -> str | None:
    """Return an honest pre-build selector without pretending it is a version."""

    if not isinstance(dependency, RepositorySourceDependency):
        raise TypeError("repository selector requires a repository dependency")
    selector = dependency.revision_selector
    if selector.kind is RepositoryRevisionKind.COMMIT:
        return None
    if selector.kind is RepositoryRevisionKind.DEFAULT:
        return "git:default"
    assert selector.value is not None
    return f"git:branch:{selector.value}"


@dataclass(frozen=True, slots=True)
class CycloneDxManagedComponent:
    """One exact framework-managed inventory node expected in a standard BOM."""

    bom_ref: str
    kind: ManagedComponentKind
    identity: ContentIdentity
    name: str
    version: str | None
    scopes: tuple[str, ...]
    version_range: str | None = None

    SCHEMA: ClassVar[str] = CYCLONEDX_MANAGED_COMPONENT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.bom_ref, "CycloneDxManagedComponent.bom_ref")
        if not isinstance(self.kind, ManagedComponentKind):
            fail("CycloneDxManagedComponent.kind", "must be a ManagedComponentKind")
        if not isinstance(self.identity, ContentIdentity):
            fail("CycloneDxManagedComponent.identity", "must be a ContentIdentity")
        string_value(self.name, "CycloneDxManagedComponent.name")
        if self.version is not None:
            string_value(self.version, "CycloneDxManagedComponent.version")
        if self.version_range is not None:
            string_value(self.version_range, "CycloneDxManagedComponent.version_range")
        if (self.version is None) == (self.version_range is None):
            fail(
                "CycloneDxManagedComponent.version",
                "must declare exactly one exact version or version range",
            )
        if (
            self.kind is not ManagedComponentKind.REPOSITORY_SOURCE
            and self.version_range is not None
        ):
            fail(
                "CycloneDxManagedComponent.version_range",
                "is only valid for a mutable repository-source selector",
            )
        if not isinstance(self.scopes, tuple):
            fail("CycloneDxManagedComponent.scopes", "must be a tuple")
        unique(self.scopes, "CycloneDxManagedComponent.scopes")
        if self.scopes != tuple(sorted(self.scopes)):
            fail("CycloneDxManagedComponent.scopes", "must be sorted")
        if set(self.scopes) - _DEPENDENCY_SCOPES:
            fail(
                "CycloneDxManagedComponent.scopes",
                "contains unsupported dependency scopes",
            )
        if self.kind is ManagedComponentKind.ROOT and self.scopes:
            fail("CycloneDxManagedComponent.scopes", "root scopes must be empty")
        if self.kind is not ManagedComponentKind.ROOT and not self.scopes:
            fail(
                "CycloneDxManagedComponent.scopes",
                "a dependency must declare at least one scope",
            )
        expected_ref = (
            repository_dependency_bom_ref(self.identity)
            if self.kind is ManagedComponentKind.REPOSITORY_SOURCE
            else component_bom_ref(self.identity)
        )
        if self.bom_ref != expected_ref:
            fail(
                "CycloneDxManagedComponent.bom_ref",
                "does not match its exact managed identity",
            )

    @property
    def binding_property(self) -> str:
        if self.kind is ManagedComponentKind.REPOSITORY_SOURCE:
            return LITERATE_REPOSITORY_DEPENDENCY_PROPERTY
        return LITERATE_COMPONENT_REVISION_PROPERTY

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "bom_ref": self.bom_ref,
            "kind": self.kind.value,
            "identity": self.identity.to_dict(),
            "name": self.name,
            "version": self.version,
            "scopes": list(self.scopes),
            "version_range": self.version_range,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CycloneDxManagedComponent"
    ) -> CycloneDxManagedComponent:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "bom_ref",
                    "kind",
                    "identity",
                    "name",
                    "version",
                    "scopes",
                    "version_range",
                }
            ),
        )
        try:
            kind = ManagedComponentKind(string_value(data["kind"], f"{path}.kind"))
        except ValueError:
            fail(f"{path}.kind", "is unsupported")
        version = data["version"]
        version_range = data["version_range"]
        return cls(
            bom_ref=string_value(data["bom_ref"], f"{path}.bom_ref"),
            kind=kind,
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
            name=string_value(data["name"], f"{path}.name"),
            version=(
                None if version is None else string_value(version, f"{path}.version")
            ),
            scopes=string_tuple(data["scopes"], f"{path}.scopes"),
            version_range=(
                None
                if version_range is None
                else string_value(version_range, f"{path}.version_range")
            ),
        )


@dataclass(frozen=True, slots=True)
class CycloneDxManagedEdge:
    source_ref: str
    target_ref: str
    kind: DependencyKind = DependencyKind.RUNTIME
    optional: bool = False
    relationship_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = CYCLONEDX_MANAGED_EDGE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.source_ref, "CycloneDxManagedEdge.source_ref")
        string_value(self.target_ref, "CycloneDxManagedEdge.target_ref")
        if self.source_ref == self.target_ref:
            fail("CycloneDxManagedEdge", "must not be a self-dependency")
        if not isinstance(self.kind, DependencyKind):
            fail("CycloneDxManagedEdge.kind", "must be a DependencyKind")
        bool_value(self.optional, "CycloneDxManagedEdge.optional")
        if self.relationship_identity is None:
            object.__setattr__(
                self,
                "relationship_identity",
                canonical_identity(
                    {
                        "schema": f"{SCHEMA_PREFIX}cyclonedx-derived-managed-edge",
                        "source_ref": self.source_ref,
                        "target_ref": self.target_ref,
                        "kind": self.kind.value,
                        "optional": self.optional,
                    }
                ),
            )
        elif not isinstance(self.relationship_identity, ContentIdentity):
            fail(
                "CycloneDxManagedEdge.relationship_identity",
                "must be a ContentIdentity",
            )

    @property
    def sort_key(self) -> tuple[str, str, str, bool, str]:
        assert self.relationship_identity is not None
        return (
            self.source_ref,
            self.target_ref,
            self.kind.value,
            self.optional,
            self.relationship_identity.uri,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CycloneDxManagedEdge):
            return NotImplemented
        return self.sort_key < other.sort_key

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_ref": self.source_ref,
            "target_ref": self.target_ref,
            "kind": self.kind.value,
            "optional": self.optional,
            "relationship_identity": self.relationship_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CycloneDxManagedEdge"
    ) -> CycloneDxManagedEdge:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_ref",
                    "target_ref",
                    "kind",
                    "optional",
                    "relationship_identity",
                }
            ),
        )
        try:
            kind = DependencyKind(string_value(data["kind"], f"{path}.kind"))
        except ValueError:
            fail(f"{path}.kind", "is unsupported")
        return cls(
            source_ref=string_value(data["source_ref"], f"{path}.source_ref"),
            target_ref=string_value(data["target_ref"], f"{path}.target_ref"),
            kind=kind,
            optional=bool_value(data["optional"], f"{path}.optional"),
            relationship_identity=ContentIdentity.from_dict(
                data["relationship_identity"],
                path=f"{path}.relationship_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class CycloneDxRepositorySourceResolution:
    """Layer-neutral exact evidence projected from repository-source resolution."""

    dependency_identity: ContentIdentity
    resolved_commit: str
    source_lock_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    resolver_identity: ContentIdentity
    index_identity: ContentIdentity
    admission_identity: ContentIdentity
    cache_record_identity: ContentIdentity

    SCHEMA: ClassVar[str] = CYCLONEDX_REPOSITORY_SOURCE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.dependency_identity, ContentIdentity):
            fail(
                "CycloneDxRepositorySourceResolution.dependency_identity",
                "must be a ContentIdentity",
            )
        RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, self.resolved_commit)
        for name in (
            "source_lock_identity",
            "source_snapshot_identity",
            "source_tree_identity",
            "resolver_identity",
            "index_identity",
            "admission_identity",
            "cache_record_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(
                    f"CycloneDxRepositorySourceResolution.{name}",
                    "must be a ContentIdentity",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "dependency_identity": self.dependency_identity.to_dict(),
            "resolved_commit": self.resolved_commit,
            "source_lock_identity": self.source_lock_identity.to_dict(),
            "source_snapshot_identity": self.source_snapshot_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "resolver_identity": self.resolver_identity.to_dict(),
            "index_identity": self.index_identity.to_dict(),
            "admission_identity": self.admission_identity.to_dict(),
            "cache_record_identity": self.cache_record_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "CycloneDxRepositorySourceResolution",
    ) -> CycloneDxRepositorySourceResolution:
        fields = (
            "dependency_identity",
            "source_lock_identity",
            "source_snapshot_identity",
            "source_tree_identity",
            "resolver_identity",
            "index_identity",
            "admission_identity",
            "cache_record_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({*fields, "resolved_commit"}),
        )
        identities = {
            name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
            for name in fields
        }
        return cls(
            resolved_commit=string_value(
                data["resolved_commit"], f"{path}.resolved_commit"
            ),
            **identities,
        )


@dataclass(frozen=True, slots=True)
class CycloneDxManagedGraph:
    """Exact resolved Component and repository-source dependency subgraph."""

    root_ref: str
    components: tuple[CycloneDxManagedComponent, ...]
    edges: tuple[CycloneDxManagedEdge, ...]
    resolved_graph_identity: ContentIdentity

    SCHEMA: ClassVar[str] = CYCLONEDX_MANAGED_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.root_ref, "CycloneDxManagedGraph.root_ref")
        if not isinstance(self.resolved_graph_identity, ContentIdentity):
            fail(
                "CycloneDxManagedGraph.resolved_graph_identity",
                "must identify the exact resolved Component graph",
            )
        if not self.components:
            fail("CycloneDxManagedGraph.components", "must not be empty")
        refs = tuple(item.bom_ref for item in self.components)
        unique(refs, "CycloneDxManagedGraph.components", "BOM references")
        if refs != tuple(sorted(refs)):
            fail("CycloneDxManagedGraph.components", "must be sorted by BOM reference")
        by_ref = {item.bom_ref: item for item in self.components}
        root = by_ref.get(self.root_ref)
        if root is None or root.kind is not ManagedComponentKind.ROOT:
            fail("CycloneDxManagedGraph.root_ref", "must identify the root Component")
        if sum(item.kind is ManagedComponentKind.ROOT for item in self.components) != 1:
            fail("CycloneDxManagedGraph.components", "must contain exactly one root")
        if self.edges != tuple(sorted(self.edges)):
            fail("CycloneDxManagedGraph.edges", "must be sorted")
        unique(self.edges, "CycloneDxManagedGraph.edges", "dependency edges")
        for edge in self.edges:
            if edge.source_ref not in by_ref or edge.target_ref not in by_ref:
                fail("CycloneDxManagedGraph.edges", "contains a dangling reference")
        _require_reachable_acyclic(self.root_ref, refs, self.edges)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "root_ref": self.root_ref,
            "components": [item.to_dict() for item in self.components],
            "edges": [item.to_dict() for item in self.edges],
            "resolved_graph_identity": self.resolved_graph_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CycloneDxManagedGraph"
    ) -> CycloneDxManagedGraph:
        value = _adapt_legacy_resolved_graph_identity(
            value,
            legacy_schema=LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA,
            current_schema=cls.SCHEMA,
            path=path,
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"root_ref", "components", "edges", "resolved_graph_identity"}
            ),
        )
        return cls(
            root_ref=string_value(data["root_ref"], f"{path}.root_ref"),
            components=parse_tuple(
                data["components"],
                f"{path}.components",
                CycloneDxManagedComponent.from_dict,
            ),
            edges=parse_tuple(
                data["edges"], f"{path}.edges", CycloneDxManagedEdge.from_dict
            ),
            resolved_graph_identity=ContentIdentity.from_dict(
                data["resolved_graph_identity"],
                path=f"{path}.resolved_graph_identity",
            ),
        )

    @classmethod
    def from_component_lock(
        cls,
        component_lock: ComponentLock,
        *,
        component_revision: ContentIdentity | None = None,
    ) -> CycloneDxManagedGraph:
        """Project one revision's reachable closure from one exact immutable lock."""

        if not isinstance(component_lock, ComponentLock):
            raise TypeError("managed SBOM graph requires an exact ComponentLock")
        known = {node.revision.identity.uri: node for node in component_lock.nodes}
        if len(known) != len(component_lock.nodes):
            raise ValueError(
                "managed SBOM graph contains duplicate Component revisions"
            )
        if component_lock.root_revision.uri not in known:
            raise ValueError("managed SBOM graph does not contain its root revision")

        selected = (
            component_lock.root_revision
            if component_revision is None
            else component_revision
        )
        if not isinstance(selected, ContentIdentity):
            raise TypeError(
                "managed SBOM projection revision must be a ContentIdentity"
            )
        if selected.uri not in known:
            raise ValueError(
                "managed SBOM projection revision is absent from the exact "
                "ComponentLock"
            )

        outgoing: dict[str, list[Any]] = {key: [] for key in known}
        for edge in component_lock.edges:
            source_uri = edge.consumer_revision.uri
            target_uri = edge.provider_revision.uri
            if source_uri not in known or target_uri not in known:
                raise ValueError("managed SBOM edge references an unknown Component")
            outgoing[source_uri].append(edge)

        reachable = {selected.uri}
        pending = [selected.uri]
        while pending:
            source_uri = pending.pop()
            for edge in outgoing[source_uri]:
                target_uri = edge.provider_revision.uri
                if target_uri not in reachable:
                    reachable.add(target_uri)
                    pending.append(target_uri)

        incoming: dict[str, set[str]] = {key: set() for key in reachable}
        managed_edges: set[CycloneDxManagedEdge] = set()
        for edge in component_lock.edges:
            source_uri = edge.consumer_revision.uri
            target_uri = edge.provider_revision.uri
            if source_uri not in reachable:
                continue
            if target_uri not in reachable:
                raise ValueError(
                    "managed SBOM reachable dependency closure is incomplete"
                )
            if edge.kind is DependencyKind.GENERATION:
                interface_matches = tuple(
                    binding
                    for binding in known[target_uri].interface_bindings
                    if binding.capability == edge.capability
                    and binding.interface_identity == edge.public_interface_identity
                )
                if len(interface_matches) != 1:
                    raise ValueError(
                        "managed SBOM generation edge lacks its exact locked "
                        "public interface"
                    )
            incoming[target_uri].add(edge.kind.value)
            managed_edges.add(
                CycloneDxManagedEdge(
                    component_bom_ref(edge.consumer_revision),
                    component_bom_ref(edge.provider_revision),
                    edge.kind,
                    edge.optional,
                    edge.identity,
                )
            )

        components: dict[str, CycloneDxManagedComponent] = {}
        repository_resolutions: dict[
            str, tuple[str, ContentIdentity, ContentIdentity, ContentIdentity]
        ] = {}
        for node in component_lock.nodes:
            revision = node.revision
            revision_identity = revision.identity
            if revision_identity.uri not in reachable:
                continue
            is_root = revision_identity == selected
            component = CycloneDxManagedComponent(
                component_bom_ref(revision_identity),
                (
                    ManagedComponentKind.ROOT
                    if is_root
                    else ManagedComponentKind.COMPONENT
                ),
                revision_identity,
                revision.coordinate.uri,
                revision.version,
                () if is_root else tuple(sorted(incoming[revision_identity.uri])),
            )
            components[component.bom_ref] = component

            for source_lock in revision.repository_sources:
                dependency = source_lock.dependency
                dependency_identity = repository_dependency_identity(dependency)
                dependency_ref = repository_dependency_bom_ref(dependency_identity)
                exact_resolution = (
                    source_lock.resolved_commit,
                    source_lock.source_snapshot,
                    source_lock.source_tree,
                    source_lock.resolver,
                )
                existing_resolution = repository_resolutions.get(dependency_ref)
                if (
                    existing_resolution is not None
                    and existing_resolution != exact_resolution
                ):
                    raise ValueError(
                        "managed SBOM repository dependency has conflicting exact "
                        "source locks"
                    )
                repository_resolutions[dependency_ref] = exact_resolution

                existing = components.get(dependency_ref)
                scopes = {dependency.dependency_kind.value}
                if existing is not None:
                    if (
                        existing.kind is not ManagedComponentKind.REPOSITORY_SOURCE
                        or existing.identity != dependency_identity
                        or existing.name != dependency.dependency_id
                        or existing.version != source_lock.resolved_commit
                        or existing.version_range is not None
                    ):
                        raise ValueError(
                            "managed SBOM repository dependency identity conflicts"
                        )
                    scopes.update(existing.scopes)
                components[dependency_ref] = CycloneDxManagedComponent(
                    dependency_ref,
                    ManagedComponentKind.REPOSITORY_SOURCE,
                    dependency_identity,
                    dependency.dependency_id,
                    source_lock.resolved_commit,
                    tuple(sorted(scopes)),
                    None,
                )
                managed_edges.add(
                    CycloneDxManagedEdge(
                        component_bom_ref(revision_identity),
                        dependency_ref,
                        dependency.dependency_kind,
                        dependency.optional,
                        dependency.identity,
                    )
                )

        return cls(
            component_bom_ref(selected),
            tuple(components[ref] for ref in sorted(components)),
            tuple(sorted(managed_edges)),
            component_lock.identity,
        )

    @classmethod
    def from_component_composition(
        cls,
        *,
        root_ref: ComponentRevisionRef,
        revision_refs: Sequence[ComponentRevisionRef],
        edges: Sequence[DependencyEdge],
        composition_identity: ContentIdentity,
        repository_dependencies: (
            Mapping[str, Sequence[RepositorySourceDependency]] | None
        ) = None,
    ) -> CycloneDxManagedGraph:
        if not isinstance(composition_identity, ContentIdentity):
            raise TypeError(
                "managed SBOM graph requires the exact ComponentComposition identity"
            )
        repositories = repository_dependencies or {}
        known = {item.revision_identity.uri: item for item in revision_refs}
        if len(known) != len(revision_refs):
            raise ValueError(
                "managed SBOM graph contains duplicate Component revisions"
            )
        if root_ref.revision_identity.uri not in known:
            raise ValueError("managed SBOM graph does not contain its root revision")
        if repository_dependencies is not None and set(repositories) != set(known):
            unknown = set(repositories) - set(known)
            if unknown:
                raise ValueError(
                    "managed SBOM repository owner mapping contains an unknown "
                    "Component"
                )
            raise ValueError(
                "managed SBOM repository owner mapping omits a transitive Component"
            )
        incoming: dict[str, set[str]] = {key: set() for key in known}
        managed_edges: set[CycloneDxManagedEdge] = set()
        for edge in edges:
            source_uri = edge.source_revision.uri
            target_uri = edge.target_revision.uri
            if source_uri not in known or target_uri not in known:
                raise ValueError("managed SBOM edge references an unknown Component")
            incoming[target_uri].add(edge.kind.value)
            managed_edges.add(
                CycloneDxManagedEdge(
                    component_bom_ref(edge.source_revision),
                    component_bom_ref(edge.target_revision),
                    edge.kind,
                    edge.requirement.optional,
                    edge.identity,
                )
            )
        components: dict[str, CycloneDxManagedComponent] = {}
        for reference in revision_refs:
            identity = reference.revision_identity
            is_root = identity == root_ref.revision_identity
            component = CycloneDxManagedComponent(
                component_bom_ref(identity),
                (
                    ManagedComponentKind.ROOT
                    if is_root
                    else ManagedComponentKind.COMPONENT
                ),
                identity,
                reference.coordinate.uri,
                reference.version,
                () if is_root else tuple(sorted(incoming[identity.uri])),
            )
            components[component.bom_ref] = component
            for dependency in repositories.get(identity.uri, ()):
                dependency_identity = repository_dependency_identity(dependency)
                dependency_ref = repository_dependency_bom_ref(dependency_identity)
                version = (
                    dependency.revision_selector.value
                    if dependency.revision_selector.kind
                    is RepositoryRevisionKind.COMMIT
                    else None
                )
                version_range = repository_selector_version_range(dependency)
                existing = components.get(dependency_ref)
                scopes = {dependency.dependency_kind.value}
                if existing is not None:
                    if (
                        existing.kind is not ManagedComponentKind.REPOSITORY_SOURCE
                        or existing.identity != dependency_identity
                        or existing.name != dependency.dependency_id
                        or existing.version != version
                        or existing.version_range != version_range
                    ):
                        raise ValueError(
                            "managed SBOM repository dependency identity conflicts"
                        )
                    scopes.update(existing.scopes)
                components[dependency_ref] = CycloneDxManagedComponent(
                    dependency_ref,
                    ManagedComponentKind.REPOSITORY_SOURCE,
                    dependency_identity,
                    dependency.dependency_id,
                    version,
                    tuple(sorted(scopes)),
                    version_range,
                )
                managed_edges.add(
                    CycloneDxManagedEdge(
                        component_bom_ref(identity),
                        dependency_ref,
                        dependency.dependency_kind,
                        dependency.optional,
                        dependency.identity,
                    )
                )
        return cls(
            component_bom_ref(root_ref.revision_identity),
            tuple(components[ref] for ref in sorted(components)),
            tuple(sorted(managed_edges)),
            composition_identity,
        )


def project_component_lock_managed_graph(
    component_lock: ComponentLock,
    component_revision: ContentIdentity,
) -> CycloneDxManagedGraph:
    """Project one node's dependency closure while retaining full-lock authority."""

    return CycloneDxManagedGraph.from_component_lock(
        component_lock,
        component_revision=component_revision,
    )


@dataclass(frozen=True, slots=True)
class CycloneDxBomBinding:
    """Compact evidence identity for one validated standard CycloneDX artifact."""

    lifecycle: CycloneDxLifecycle
    bom_identity: ContentIdentity
    graph_identity: ContentIdentity
    managed_graph_identity: ContentIdentity
    resolved_graph_identity: ContentIdentity
    source_bom_identity: ContentIdentity | None
    root_ref: str
    component_count: int
    edge_count: int

    SCHEMA: ClassVar[str] = CYCLONEDX_BOM_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, CycloneDxLifecycle):
            fail("CycloneDxBomBinding.lifecycle", "must be a CycloneDxLifecycle")
        for name in (
            "bom_identity",
            "graph_identity",
            "managed_graph_identity",
            "resolved_graph_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"CycloneDxBomBinding.{name}", "must be a ContentIdentity")
        if self.lifecycle is CycloneDxLifecycle.SOURCE:
            if self.source_bom_identity is not None:
                fail(
                    "CycloneDxBomBinding.source_bom_identity",
                    "must be null for a pre-build BOM",
                )
        elif not isinstance(self.source_bom_identity, ContentIdentity):
            fail(
                "CycloneDxBomBinding.source_bom_identity",
                "must bind the exact pre-build BOM for a post-build BOM",
            )
        string_value(self.root_ref, "CycloneDxBomBinding.root_ref")
        if type(self.component_count) is not int or self.component_count < 1:
            fail("CycloneDxBomBinding.component_count", "must be positive")
        if type(self.edge_count) is not int or self.edge_count < 0:
            fail("CycloneDxBomBinding.edge_count", "must be non-negative")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "lifecycle": self.lifecycle.value,
            "bom_identity": self.bom_identity.to_dict(),
            "graph_identity": self.graph_identity.to_dict(),
            "managed_graph_identity": self.managed_graph_identity.to_dict(),
            "resolved_graph_identity": self.resolved_graph_identity.to_dict(),
            "source_bom_identity": (
                None
                if self.source_bom_identity is None
                else self.source_bom_identity.to_dict()
            ),
            "root_ref": self.root_ref,
            "component_count": self.component_count,
            "edge_count": self.edge_count,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CycloneDxBomBinding"
    ) -> CycloneDxBomBinding:
        value = _adapt_legacy_resolved_graph_identity(
            value,
            legacy_schema=LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA,
            current_schema=cls.SCHEMA,
            path=path,
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "lifecycle",
                    "bom_identity",
                    "graph_identity",
                    "managed_graph_identity",
                    "resolved_graph_identity",
                    "source_bom_identity",
                    "root_ref",
                    "component_count",
                    "edge_count",
                }
            ),
        )
        try:
            lifecycle = CycloneDxLifecycle(
                string_value(data["lifecycle"], f"{path}.lifecycle")
            )
        except ValueError:
            fail(f"{path}.lifecycle", "is unsupported")
        counts: list[int] = []
        for name, minimum in (("component_count", 1), ("edge_count", 0)):
            count = data[name]
            if type(count) is not int or count < minimum:
                fail(f"{path}.{name}", f"must be at least {minimum}")
            counts.append(count)
        return cls(
            lifecycle,
            ContentIdentity.from_dict(
                data["bom_identity"], path=f"{path}.bom_identity"
            ),
            ContentIdentity.from_dict(
                data["graph_identity"], path=f"{path}.graph_identity"
            ),
            ContentIdentity.from_dict(
                data["managed_graph_identity"],
                path=f"{path}.managed_graph_identity",
            ),
            ContentIdentity.from_dict(
                data["resolved_graph_identity"],
                path=f"{path}.resolved_graph_identity",
            ),
            (
                None
                if data["source_bom_identity"] is None
                else ContentIdentity.from_dict(
                    data["source_bom_identity"],
                    path=f"{path}.source_bom_identity",
                )
            ),
            string_value(data["root_ref"], f"{path}.root_ref"),
            counts[0],
            counts[1],
        )


def _require_reachable_acyclic(
    root_ref: str,
    refs: Sequence[str],
    edges: Sequence[CycloneDxManagedEdge],
) -> None:
    ref_set = set(refs)
    adjacency: dict[str, list[str]] = {ref: [] for ref in ref_set}
    for edge in edges:
        adjacency[edge.source_ref].append(edge.target_ref)
    visited: set[str] = set()
    active: set[str] = set()

    def visit(ref: str) -> None:
        if ref in active:
            fail("CycloneDxManagedGraph.edges", "managed Component graph has a cycle")
        if ref in visited:
            return
        active.add(ref)
        for target in adjacency[ref]:
            visit(target)
        active.remove(ref)
        visited.add(ref)

    visit(root_ref)
    if visited != ref_set:
        fail("CycloneDxManagedGraph.components", "contains disconnected components")


__all__ = [
    "CYCLONEDX_BOM_BINDING_SCHEMA",
    "CYCLONEDX_MANAGED_COMPONENT_SCHEMA",
    "CYCLONEDX_MANAGED_EDGE_SCHEMA",
    "CYCLONEDX_MANAGED_GRAPH_SCHEMA",
    "CYCLONEDX_REPOSITORY_SOURCE_RESOLUTION_SCHEMA",
    "CYCLONEDX_SCHEMA_URI",
    "CYCLONEDX_SOURCE_SBOM_PATH",
    "CYCLONEDX_SPEC_VERSION",
    "CycloneDxBomBinding",
    "CycloneDxLifecycle",
    "CycloneDxManagedComponent",
    "CycloneDxManagedEdge",
    "CycloneDxManagedGraph",
    "CycloneDxRepositorySourceResolution",
    "project_component_lock_managed_graph",
    "LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA",
    "LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA",
    "LEGACY_LITERATE_COMPONENT_COMPOSITION_IDENTITY_PROPERTY",
    "LITERATE_COMPONENT_REVISION_PROPERTY",
    "LITERATE_DEPENDENCY_EDGE_PROPERTY",
    "LITERATE_DEPENDENCY_KIND_PROPERTY",
    "LITERATE_DEPENDENCY_SCOPE_PROPERTY",
    "LITERATE_REPOSITORY_DEPENDENCY_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_ADMISSION_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_CACHE_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_INDEX_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_LOCK_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_RESOLVER_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_SNAPSHOT_PROPERTY",
    "LITERATE_REPOSITORY_SOURCE_TREE_PROPERTY",
    "LITERATE_RESOLVED_GRAPH_IDENTITY_PROPERTY",
    "LITERATE_SOURCE_BOM_IDENTITY_PROPERTY",
    "ManagedComponentKind",
    "component_bom_ref",
    "repository_dependency_bom_ref",
    "repository_dependency_identity",
    "repository_selector_version_range",
]

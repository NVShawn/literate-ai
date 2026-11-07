"""Portable identity values for native Cargo library graphs, never admission grants."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .identity import ContentIdentity, canonical_identity
from .versioning import semantic_version

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
MAX_PROVIDER_NODES = 128
MAX_PROVIDER_EDGES = 1024


class NativeCargoProviderGraphError(ValueError):
    """A closed graph is malformed; do not expose producer-controlled details."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NativeCargoProviderGraphError("cargo.provider." + code)


def _identities(*values: object) -> None:
    _require(all(type(value) is ContentIdentity for value in values), "identity")


@dataclass(frozen=True, slots=True)
class NativeCargoProviderNode:
    """One exact qualified export and its original, unprojected Cargo manifest."""

    component_revision: ContentIdentity
    artifact_identity: ContentIdentity
    import_surface_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    original_manifest_identity: ContentIdentity
    package: str
    version: str

    def __post_init__(self) -> None:
        _identities(
            self.component_revision,
            self.artifact_identity,
            self.import_surface_identity,
            self.source_tree_identity,
            self.original_manifest_identity,
        )
        _require(
            type(self.package) is str and _NAME.fullmatch(self.package) is not None,
            "package",
        )
        try:
            semantic_version(self.version, "native Cargo provider version")
        except (TypeError, ValueError) as error:
            raise NativeCargoProviderGraphError("cargo.provider.version") from error

    def to_dict(self) -> dict[str, str]:
        return {
            "component_revision": self.component_revision.uri,
            "artifact_identity": self.artifact_identity.uri,
            "import_surface_identity": self.import_surface_identity.uri,
            "source_tree_identity": self.source_tree_identity.uri,
            "original_manifest_identity": self.original_manifest_identity.uri,
            "package": self.package,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class NativeCargoProviderEdge:
    """One native dependency alias, independently joined to its locked binding."""

    consumer_revision: ContentIdentity
    provider_revision: ContentIdentity
    alias: str

    def __post_init__(self) -> None:
        _identities(self.consumer_revision, self.provider_revision)
        _require(
            type(self.alias) is str and _NAME.fullmatch(self.alias) is not None, "alias"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "consumer_revision": self.consumer_revision.uri,
            "provider_revision": self.provider_revision.uri,
            "alias": self.alias,
        }


@dataclass(frozen=True, slots=True)
class NativeCargoProviderGraph:
    """Finite canonical DAG values; current qualified-input owners still admit it.

    One node per artifact/revision preserves a shared nominal provider in a
    diamond. This bounded profile refuses ambiguous same-named Rust packages.
    Neither construction nor decoding verifies a package or authorizes Cargo.
    """

    root_revision: ContentIdentity
    nodes: tuple[NativeCargoProviderNode, ...]
    edges: tuple[NativeCargoProviderEdge, ...]

    def __post_init__(self) -> None:
        _identities(self.root_revision)
        _require(
            type(self.nodes) is tuple
            and len(self.nodes) <= MAX_PROVIDER_NODES
            and all(type(row) is NativeCargoProviderNode for row in self.nodes),
            "nodes",
        )
        _require(
            type(self.edges) is tuple
            and len(self.edges) <= MAX_PROVIDER_EDGES
            and all(type(row) is NativeCargoProviderEdge for row in self.edges),
            "edges",
        )
        revisions = tuple(row.component_revision for row in self.nodes)
        artifacts = tuple(row.artifact_identity for row in self.nodes)
        names = tuple(row.package.replace("-", "_") for row in self.nodes)
        _require(
            len(set(revisions)) == len(revisions)
            and len(set(artifacts)) == len(artifacts)
            and len(set(names)) == len(names),
            "duplicate_provider",
        )
        _require(self.root_revision not in revisions, "root_provider")
        _require(
            tuple(sorted(self.nodes, key=lambda row: row.component_revision.uri))
            == self.nodes,
            "node_order",
        )
        keys = tuple(
            (row.consumer_revision.uri, row.alias.replace("-", "_"))
            for row in self.edges
        )
        _require(len(set(keys)) == len(keys), "duplicate_alias")
        _require(
            tuple(
                sorted(
                    self.edges,
                    key=lambda row: (
                        row.consumer_revision.uri,
                        row.alias,
                        row.provider_revision.uri,
                    ),
                )
            )
            == self.edges,
            "edge_order",
        )
        members = set(revisions) | {self.root_revision}
        adjacency = {revision: [] for revision in members}
        for row in self.edges:
            _require(
                row.consumer_revision in members and row.provider_revision in revisions,
                "missing_provider",
            )
            adjacency[row.consumer_revision].append(row.provider_revision)
        visited: set[ContentIdentity] = set()
        active: set[ContentIdentity] = set()

        def visit(revision: ContentIdentity) -> None:
            _require(revision not in active, "cycle")
            if revision in visited:
                return
            active.add(revision)
            for provider in adjacency[revision]:
                visit(provider)
            active.remove(revision)
            visited.add(revision)

        # Check all nodes, including disconnected cycles, before reachability.
        for revision in members:
            visit(revision)
        reachable: set[ContentIdentity] = set()
        pending = [self.root_revision]
        while pending:
            revision = pending.pop()
            if revision not in reachable:
                reachable.add(revision)
                pending.extend(adjacency[revision])
        _require(reachable == members, "unreachable_provider")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/native-cargo-provider-graph@1",
            "root_revision": self.root_revision.uri,
            "nodes": [row.to_dict() for row in self.nodes],
            "edges": [row.to_dict() for row in self.edges],
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeCargoProviderGraph:
        """Decode closed data values; caller still needs current artifact custody."""
        _require(
            type(value) is dict
            and set(value) == {"schema", "root_revision", "nodes", "edges"},
            "fields",
        )
        _require(
            value["schema"] == "literate-ai/native-cargo-provider-graph@1", "schema"
        )
        _require(
            type(value["nodes"]) is list and len(value["nodes"]) <= MAX_PROVIDER_NODES,
            "nodes",
        )
        _require(
            type(value["edges"]) is list and len(value["edges"]) <= MAX_PROVIDER_EDGES,
            "edges",
        )

        def identity(raw: object) -> ContentIdentity:
            _require(type(raw) is str, "identity")
            try:
                parsed = ContentIdentity.parse_uri(raw)
            except (TypeError, ValueError) as error:
                raise NativeCargoProviderGraphError(
                    "cargo.provider.identity"
                ) from error
            _require(parsed.uri == raw, "identity")
            return parsed

        nodes = []
        for row in value["nodes"]:
            _require(
                type(row) is dict
                and set(row)
                == {
                    "component_revision",
                    "artifact_identity",
                    "import_surface_identity",
                    "source_tree_identity",
                    "original_manifest_identity",
                    "package",
                    "version",
                },
                "node_fields",
            )
            nodes.append(
                NativeCargoProviderNode(
                    *(
                        identity(row[name])
                        for name in (
                            "component_revision",
                            "artifact_identity",
                            "import_surface_identity",
                            "source_tree_identity",
                            "original_manifest_identity",
                        )
                    ),
                    row["package"],
                    row["version"],
                )
            )
        edges = []
        for row in value["edges"]:
            _require(
                type(row) is dict
                and set(row) == {"consumer_revision", "provider_revision", "alias"},
                "edge_fields",
            )
            edges.append(
                NativeCargoProviderEdge(
                    identity(row["consumer_revision"]),
                    identity(row["provider_revision"]),
                    row["alias"],
                )
            )
        return cls(identity(value["root_revision"]), tuple(nodes), tuple(edges))

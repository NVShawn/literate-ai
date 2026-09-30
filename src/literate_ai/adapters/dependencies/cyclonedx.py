"""Strict CycloneDX 1.7 JSON validation and deterministic BOM construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from literate_ai.contracts.identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.sbom import (
    CYCLONEDX_SCHEMA_URI,
    CYCLONEDX_SPEC_VERSION,
    LEGACY_LITERATE_COMPONENT_COMPOSITION_IDENTITY_PROPERTY,
    LITERATE_COMPONENT_REVISION_PROPERTY,
    LITERATE_DEPENDENCY_EDGE_PROPERTY,
    LITERATE_DEPENDENCY_KIND_PROPERTY,
    LITERATE_DEPENDENCY_SCOPE_PROPERTY,
    LITERATE_REPOSITORY_DEPENDENCY_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_ADMISSION_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_CACHE_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_INDEX_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_LOCK_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_RESOLVER_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_SNAPSHOT_PROPERTY,
    LITERATE_REPOSITORY_SOURCE_TREE_PROPERTY,
    LITERATE_RESOLVED_GRAPH_IDENTITY_PROPERTY,
    LITERATE_SOURCE_BOM_IDENTITY_PROPERTY,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    ManagedComponentKind,
)

MAX_CYCLONEDX_BYTES = 16 * 1024 * 1024
MAX_CYCLONEDX_COMPONENTS = 100_000
MAX_CYCLONEDX_EDGES = 1_000_000

_DEPENDENCY_KINDS = frozenset(
    {
        "component",
        "flavor",
        "repository-source",
        "package",
        "toolchain",
        "build",
        "test",
        "runtime",
        "system",
        "root-component",
        "skill",
    }
)
_DEPENDENCY_SCOPES = frozenset(
    {
        "generation",
        "build",
        "runtime",
        "validation",
        "toolchain",
        "packaging",
        "deployment",
        "test",
        "system",
    }
)

_REPOSITORY_RESOLUTION_PROPERTIES = (
    (LITERATE_REPOSITORY_SOURCE_LOCK_PROPERTY, "source_lock_identity"),
    (LITERATE_REPOSITORY_SOURCE_SNAPSHOT_PROPERTY, "source_snapshot_identity"),
    (LITERATE_REPOSITORY_SOURCE_TREE_PROPERTY, "source_tree_identity"),
    (LITERATE_REPOSITORY_SOURCE_RESOLVER_PROPERTY, "resolver_identity"),
    (LITERATE_REPOSITORY_SOURCE_INDEX_PROPERTY, "index_identity"),
    (LITERATE_REPOSITORY_SOURCE_ADMISSION_PROPERTY, "admission_identity"),
    (LITERATE_REPOSITORY_SOURCE_CACHE_PROPERTY, "cache_record_identity"),
)

_STRICT_VALIDATOR = JsonStrictValidator(SchemaVersion.V1_7)


class CycloneDxBomError(RuntimeError):
    """A CycloneDX artifact failed a stable, fail-closed trust check."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _schema_error_summary(error: object) -> str:
    """Return one bounded, actionable validator diagnostic."""

    lines = tuple(line.strip() for line in str(error).splitlines() if line.strip())
    if not lines:
        return type(error).__name__
    summary = lines[0][:512]
    for index, line in enumerate(lines[1:], start=1):
        if line.startswith("On instance"):
            location = line[:256]
            if index + 1 < len(lines):
                location = f"{location} {lines[index + 1][:256]}"
            return f"{summary}; {location}"
    return summary


def validate_cyclonedx_bom(
    content: bytes,
    *,
    lifecycle: CycloneDxLifecycle,
    managed_graph: CycloneDxManagedGraph,
    source_content: bytes | None = None,
    source_managed_graph: CycloneDxManagedGraph | None = None,
    repository_resolutions: (
        Sequence[CycloneDxRepositorySourceResolution] | None
    ) = None,
) -> CycloneDxBomBinding:
    """Validate canonical JSON with the official library and semantic profile."""

    if lifecycle is CycloneDxLifecycle.RESOLVED:
        if source_content is None:
            raise CycloneDxBomError(
                "sbom.source-bom-required",
                "post-build CycloneDX validation requires the exact pre-build BOM",
            )
        return validate_resolved_cyclonedx_bom(
            content,
            source_content=source_content,
            source_managed_graph=source_managed_graph or managed_graph,
            resolved_managed_graph=managed_graph,
            repository_resolutions=repository_resolutions,
        )[1]
    if source_content is not None or source_managed_graph is not None:
        raise CycloneDxBomError(
            "sbom.transition-invalid",
            "pre-build CycloneDX validation cannot bind a predecessor BOM",
        )
    return _validate_cyclonedx_bom_one(
        content,
        lifecycle=lifecycle,
        managed_graph=managed_graph,
        source_bom_identity=None,
        repository_resolutions=None,
    )[0]


def _validate_cyclonedx_bom_one(
    content: bytes,
    *,
    lifecycle: CycloneDxLifecycle,
    managed_graph: CycloneDxManagedGraph,
    source_bom_identity: ContentIdentity | None,
    repository_resolutions: (Sequence[CycloneDxRepositorySourceResolution] | None),
) -> tuple[
    CycloneDxBomBinding,
    dict[str, object],
    dict[str, dict[str, object]],
    dict[str, tuple[str, ...]],
]:

    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_CYCLONEDX_BYTES
    ):
        raise CycloneDxBomError(
            "sbom.size-invalid", "CycloneDX BOM bytes are empty or exceed the limit"
        )
    if not isinstance(lifecycle, CycloneDxLifecycle):
        raise TypeError("CycloneDX lifecycle must be explicit")
    if not isinstance(managed_graph, CycloneDxManagedGraph):
        raise TypeError("CycloneDX validation requires an exact managed graph")
    document = _parse_canonical_json(content)
    if not isinstance(document, dict):
        raise CycloneDxBomError("sbom.root-invalid", "CycloneDX BOM must be an object")
    _require_standard_header(document)
    try:
        schema_errors = _STRICT_VALIDATOR.validate_str(
            content.decode("utf-8"), all_errors=True
        )
        errors = tuple(schema_errors or ())
        if errors:
            raise CycloneDxBomError(
                "sbom.schema-invalid",
                "CycloneDX BOM does not conform to the strict 1.7 JSON schema: "
                f"{_schema_error_summary(errors[0])}",
            )
    except CycloneDxBomError:
        raise
    except Exception as exc:
        raise CycloneDxBomError(
            "sbom.schema-invalid",
            "CycloneDX BOM could not be checked against the strict 1.7 JSON schema",
        ) from exc

    metadata = _object(document.get("metadata"), "sbom.lifecycle-invalid")
    if metadata.get("lifecycles") != [{"phase": lifecycle.value}]:
        raise CycloneDxBomError(
            "sbom.lifecycle-invalid",
            f"CycloneDX BOM must describe exactly the {lifecycle.value} lifecycle",
        )
    if "timestamp" in metadata or "serialNumber" in document:
        raise CycloneDxBomError(
            "sbom.nondeterministic-metadata",
            "CycloneDX BOM must omit timestamps and random serial numbers",
        )
    root = _object(metadata.get("component"), "sbom.root-missing")
    root_ref = root.get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        raise CycloneDxBomError(
            "sbom.root-missing", "CycloneDX metadata.component requires a bom-ref"
        )
    if document.get("version") != 1:
        raise CycloneDxBomError(
            "sbom.version-invalid", "CycloneDX BOM document version must be 1"
        )

    inventory = _inventory(document, root)
    if len(inventory) > MAX_CYCLONEDX_COMPONENTS:
        raise CycloneDxBomError(
            "sbom.component-limit", "CycloneDX component inventory exceeds the limit"
        )
    graph = _dependency_graph(document, inventory)
    edge_count = sum(len(targets) for targets in graph.values())
    if edge_count > MAX_CYCLONEDX_EDGES:
        raise CycloneDxBomError(
            "sbom.edge-limit", "CycloneDX dependency graph exceeds the edge limit"
        )
    _require_composition(document, root_ref, lifecycle=lifecycle)
    _require_reachable(
        root_ref,
        tuple(inventory),
        tuple(
            (source, target) for source, targets in graph.items() for target in targets
        ),
    )
    _require_versions(inventory, lifecycle)
    _require_dependency_classification(inventory, root_ref)
    _require_source_bom_binding(metadata, lifecycle, source_bom_identity)
    _require_resolved_graph_binding(metadata, managed_graph)
    _require_managed_graph(
        inventory,
        graph,
        root_ref,
        managed_graph,
        lifecycle=lifecycle,
        repository_resolutions=repository_resolutions,
    )

    graph_identity = canonical_identity(
        {
            "schema": f"{SCHEMA_PREFIX}cyclonedx-complete-dependency-graph",
            "lifecycle": lifecycle.value,
            "root_ref": root_ref,
            "components": [
                {
                    "bom_ref": ref,
                    "type": inventory[ref].get("type"),
                    "name": inventory[ref].get("name"),
                    "version": inventory[ref].get("version"),
                    "versionRange": inventory[ref].get("versionRange"),
                    "purl": inventory[ref].get("purl"),
                    "properties": inventory[ref].get("properties", []),
                }
                for ref in sorted(inventory)
            ],
            "edges": [
                [source, target] for source in sorted(graph) for target in graph[source]
            ],
        }
    )
    binding = CycloneDxBomBinding(
        lifecycle=lifecycle,
        bom_identity=ContentIdentity.parse_uri(
            f"sha256:{hashlib.sha256(content).hexdigest()}"
        ),
        graph_identity=graph_identity,
        managed_graph_identity=managed_graph.identity,
        resolved_graph_identity=managed_graph.resolved_graph_identity,
        source_bom_identity=source_bom_identity,
        root_ref=root_ref,
        component_count=len(inventory),
        edge_count=edge_count,
    )
    return binding, document, inventory, graph


def validate_resolved_cyclonedx_bom(
    resolved_content: bytes,
    *,
    source_content: bytes,
    source_managed_graph: CycloneDxManagedGraph,
    resolved_managed_graph: CycloneDxManagedGraph | None = None,
    repository_resolutions: (
        Sequence[CycloneDxRepositorySourceResolution] | None
    ) = None,
) -> tuple[CycloneDxBomBinding, CycloneDxBomBinding]:
    """Validate both lifecycle artifacts and their monotonic graph transition."""

    if not isinstance(source_managed_graph, CycloneDxManagedGraph):
        raise TypeError("resolved CycloneDX validation requires a source managed graph")
    resolved_managed = resolved_managed_graph or source_managed_graph
    if resolved_managed.identity != source_managed_graph.identity:
        raise CycloneDxBomError(
            "sbom.transition-managed-graph-changed",
            "source and resolved CycloneDX BOMs must bind the same exact "
            "ComponentComposition graph",
        )
    source_binding, _source_document, source_inventory, source_graph = (
        _validate_cyclonedx_bom_one(
            source_content,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=source_managed_graph,
            source_bom_identity=None,
            repository_resolutions=None,
        )
    )
    resolved_binding, _resolved_document, resolved_inventory, resolved_graph = (
        _validate_cyclonedx_bom_one(
            resolved_content,
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=resolved_managed,
            source_bom_identity=source_binding.bom_identity,
            repository_resolutions=repository_resolutions,
        )
    )
    _require_lifecycle_transition(
        source_binding.root_ref,
        source_inventory,
        source_graph,
        resolved_binding.root_ref,
        resolved_inventory,
        resolved_graph,
    )
    return source_binding, resolved_binding


def build_cyclonedx_bom(
    *,
    lifecycle: CycloneDxLifecycle,
    managed_graph: CycloneDxManagedGraph,
    additional_components: Sequence[Mapping[str, object]] = (),
    additional_edges: Sequence[tuple[str, str]] = (),
    source_bom: bytes | None = None,
    source_managed_graph: CycloneDxManagedGraph | None = None,
    repository_resolutions: Sequence[CycloneDxRepositorySourceResolution] = (),
    composition_aggregate: str = "complete",
) -> tuple[bytes, CycloneDxBomBinding]:
    """Build deterministic standard JSON from caller-supplied exact observations."""

    if not isinstance(lifecycle, CycloneDxLifecycle):
        raise TypeError("CycloneDX lifecycle must be explicit")
    if lifecycle is CycloneDxLifecycle.SOURCE:
        if source_bom is not None or source_managed_graph is not None:
            raise CycloneDxBomError(
                "sbom.transition-invalid",
                "pre-build CycloneDX construction cannot bind a predecessor BOM",
            )
        if repository_resolutions:
            raise CycloneDxBomError(
                "sbom.resolution-invalid",
                "pre-build CycloneDX construction cannot contain repository locks",
            )
        source_binding = None
    else:
        if composition_aggregate != "complete":
            raise CycloneDxBomError(
                "sbom.composition-incomplete",
                "post-build CycloneDX construction requires a complete composition",
            )
        if source_bom is None:
            raise CycloneDxBomError(
                "sbom.source-bom-required",
                "post-build CycloneDX construction requires the exact pre-build BOM",
            )
        source_graph = source_managed_graph or managed_graph
        source_binding = validate_cyclonedx_bom(
            source_bom,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=source_graph,
        )

    resolution_by_identity = _repository_resolution_map(
        managed_graph,
        repository_resolutions,
        required_for_mutable=lifecycle is CycloneDxLifecycle.RESOLVED,
    )
    outgoing_edges: dict[str, list[CycloneDxManagedEdge]] = {
        component.bom_ref: [] for component in managed_graph.components
    }
    for edge in managed_graph.edges:
        outgoing_edges[edge.source_ref].append(edge)
    components: dict[str, dict[str, object]] = {}
    root_document: dict[str, object] | None = None
    for item in managed_graph.components:
        properties = [
            {"name": item.binding_property, "value": item.identity.uri},
            {"name": LITERATE_DEPENDENCY_KIND_PROPERTY, "value": item.kind.value},
            *(
                {
                    "name": LITERATE_DEPENDENCY_SCOPE_PROPERTY,
                    "value": scope,
                }
                for scope in item.scopes
            ),
            *(
                {
                    "name": LITERATE_DEPENDENCY_EDGE_PROPERTY,
                    "value": value,
                }
                for value in sorted(
                    _managed_edge_value(edge) for edge in outgoing_edges[item.bom_ref]
                )
            ),
        ]
        resolution = resolution_by_identity.get(item.identity)
        if resolution is not None:
            properties.extend(_repository_resolution_properties(resolution))
        component: dict[str, object] = {
            "type": (
                "application" if item.kind is ManagedComponentKind.ROOT else "library"
            ),
            "bom-ref": item.bom_ref,
            "name": item.name,
            "properties": properties,
        }
        if lifecycle is CycloneDxLifecycle.RESOLVED and resolution is not None:
            component["version"] = resolution.resolved_commit
        elif item.version is not None:
            component["version"] = item.version
        elif lifecycle is CycloneDxLifecycle.SOURCE and item.version_range is not None:
            component["isExternal"] = True
            component["versionRange"] = item.version_range
        else:
            raise CycloneDxBomError(
                "sbom.resolution-missing",
                "post-build repository inventory requires an exact source lock",
            )
        if item.kind is ManagedComponentKind.ROOT:
            component["isExternal"] = False
            root_document = component
        else:
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE:
                component["isExternal"] = True
            components[item.bom_ref] = component
    assert root_document is not None

    for raw in additional_components:
        component = dict(raw)
        ref = component.get("bom-ref")
        if (
            not isinstance(ref, str)
            or not ref
            or ref in components
            or ref == managed_graph.root_ref
        ):
            raise CycloneDxBomError(
                "sbom.observation-invalid",
                "observed CycloneDX component has a missing or duplicate bom-ref",
            )
        components[ref] = component

    edge_set = {(item.source_ref, item.target_ref) for item in managed_graph.edges}
    edge_set.update(additional_edges)
    inventory_refs = {managed_graph.root_ref, *components}
    if any(
        not isinstance(source, str)
        or not isinstance(target, str)
        or source not in inventory_refs
        or target not in inventory_refs
        or source == target
        for source, target in edge_set
    ):
        raise CycloneDxBomError(
            "sbom.observation-invalid", "observed dependency edge is invalid"
        )
    targets: dict[str, list[str]] = {ref: [] for ref in inventory_refs}
    for source, target in sorted(edge_set):
        targets[source].append(target)
    document = {
        "$schema": CYCLONEDX_SCHEMA_URI,
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "lifecycles": [{"phase": lifecycle.value}],
            "component": root_document,
            "properties": [
                {
                    "name": LITERATE_RESOLVED_GRAPH_IDENTITY_PROPERTY,
                    "value": managed_graph.resolved_graph_identity.uri,
                },
                *(
                    ()
                    if source_binding is None
                    else (
                        {
                            "name": LITERATE_SOURCE_BOM_IDENTITY_PROPERTY,
                            "value": source_binding.bom_identity.uri,
                        },
                    )
                ),
            ],
        },
        "components": [components[ref] for ref in sorted(components)],
        "dependencies": [
            {"ref": ref, "dependsOn": sorted(targets[ref])}
            for ref in sorted(inventory_refs)
        ],
        "compositions": [
            {
                "aggregate": composition_aggregate,
                "dependencies": [managed_graph.root_ref],
            }
        ],
    }
    encoded = canonical_json_bytes(document)
    if lifecycle is CycloneDxLifecycle.SOURCE:
        binding = validate_cyclonedx_bom(
            encoded, lifecycle=lifecycle, managed_graph=managed_graph
        )
    else:
        assert source_bom is not None
        binding = validate_resolved_cyclonedx_bom(
            encoded,
            source_content=source_bom,
            source_managed_graph=source_managed_graph or managed_graph,
            resolved_managed_graph=managed_graph,
            repository_resolutions=repository_resolutions,
        )[1]
    return encoded, binding


def _managed_edge_value(edge: CycloneDxManagedEdge) -> str:
    assert edge.relationship_identity is not None
    return canonical_json_bytes(
        {
            "kind": edge.kind.value,
            "optional": edge.optional,
            "relationship": edge.relationship_identity.uri,
            "target": edge.target_ref,
        }
    ).decode("utf-8")


def _repository_resolution_properties(
    resolution: CycloneDxRepositorySourceResolution,
) -> list[dict[str, str]]:
    return [
        {"name": property_name, "value": getattr(resolution, field_name).uri}
        for property_name, field_name in _REPOSITORY_RESOLUTION_PROPERTIES
    ]


def _repository_resolution_map(
    managed_graph: CycloneDxManagedGraph,
    resolutions: Sequence[CycloneDxRepositorySourceResolution] | None,
    *,
    required_for_mutable: bool,
) -> dict[ContentIdentity, CycloneDxRepositorySourceResolution]:
    repository_components = {
        component.identity: component
        for component in managed_graph.components
        if component.kind is ManagedComponentKind.REPOSITORY_SOURCE
    }
    result: dict[ContentIdentity, CycloneDxRepositorySourceResolution] = {}
    if resolutions is not None:
        for resolution in resolutions:
            if not isinstance(resolution, CycloneDxRepositorySourceResolution):
                raise TypeError(
                    "repository resolution evidence must use the typed SBOM projection"
                )
            identity = resolution.dependency_identity
            component = repository_components.get(identity)
            if component is None:
                raise CycloneDxBomError(
                    "sbom.resolution-invalid",
                    "repository resolution names unknown managed inventory",
                )
            if identity in result:
                raise CycloneDxBomError(
                    "sbom.resolution-invalid",
                    "repository resolution evidence contains a duplicate identity",
                )
            if (
                component.version is not None
                and component.version != resolution.resolved_commit
            ):
                raise CycloneDxBomError(
                    "sbom.resolution-invalid",
                    "repository resolution changed an exact requested commit",
                )
            result[identity] = resolution
    if required_for_mutable:
        missing = {
            identity
            for identity, component in repository_components.items()
            if component.version_range is not None and identity not in result
        }
        if missing:
            raise CycloneDxBomError(
                "sbom.resolution-missing",
                "mutable repository selectors require exact source-lock evidence",
            )
    return result


def _parse_canonical_json(content: bytes) -> dict[str, object]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise CycloneDxBomError(
            "sbom.json-invalid", "CycloneDX BOM must be UTF-8 JSON"
        ) from exc

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CycloneDxBomError(
                    "sbom.json-duplicate-key",
                    "CycloneDX BOM must not contain duplicate object keys",
                )
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=object_pairs)
    except CycloneDxBomError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise CycloneDxBomError(
            "sbom.json-invalid", "CycloneDX BOM must be valid JSON"
        ) from exc
    try:
        expected = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise CycloneDxBomError(
            "sbom.json-invalid", "CycloneDX BOM contains unsupported JSON values"
        ) from exc
    if content != expected:
        raise CycloneDxBomError(
            "sbom.json-noncanonical",
            "CycloneDX BOM must use Literate AI canonical JSON encoding",
        )
    return value


def _require_standard_header(document: Mapping[str, object]) -> None:
    if (
        document.get("$schema") != CYCLONEDX_SCHEMA_URI
        or document.get("bomFormat") != "CycloneDX"
        or document.get("specVersion") != CYCLONEDX_SPEC_VERSION
    ):
        raise CycloneDxBomError(
            "sbom.standard-invalid",
            "SBOM must be CycloneDX 1.7 JSON using the official schema URI",
        )


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CycloneDxBomError(code, "CycloneDX BOM object is missing or malformed")
    return value


def _inventory(
    document: Mapping[str, object], root: dict[str, object]
) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}

    def add(item: object) -> None:
        component = _object(item, "sbom.component-invalid")
        ref = component.get("bom-ref")
        if not isinstance(ref, str) or not ref:
            raise CycloneDxBomError(
                "sbom.ref-missing", "every CycloneDX inventory object needs a bom-ref"
            )
        if ref in inventory:
            raise CycloneDxBomError(
                "sbom.ref-duplicate", "CycloneDX bom-ref values must be unique"
            )
        inventory[ref] = component
        nested = component.get("components", [])
        if not isinstance(nested, list):
            raise CycloneDxBomError(
                "sbom.component-invalid", "nested CycloneDX components are malformed"
            )
        for child in nested:
            add(child)

    add(root)
    for collection_name in ("components", "services"):
        collection = document.get(collection_name, [])
        if not isinstance(collection, list):
            raise CycloneDxBomError(
                "sbom.component-invalid", "CycloneDX inventory collection is malformed"
            )
        for item in collection:
            add(item)
    return inventory


def _dependency_graph(
    document: Mapping[str, object], inventory: Mapping[str, dict[str, object]]
) -> dict[str, tuple[str, ...]]:
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list):
        raise CycloneDxBomError(
            "sbom.dependencies-missing",
            "CycloneDX BOM must contain an explicit dependency graph",
        )
    graph: dict[str, tuple[str, ...]] = {}
    previous_ref: str | None = None
    for item in dependencies:
        dependency = _object(item, "sbom.dependency-entry-invalid")
        ref = dependency.get("ref")
        targets = dependency.get("dependsOn")
        if not isinstance(ref, str) or ref not in inventory:
            raise CycloneDxBomError(
                "sbom.dependency-ref-dangling",
                "CycloneDX dependency entry references unknown inventory",
            )
        if ref in graph:
            raise CycloneDxBomError(
                "sbom.dependency-entry-duplicate",
                "CycloneDX inventory has duplicate dependency entries",
            )
        if not isinstance(targets, list) or any(
            not isinstance(target, str) for target in targets
        ):
            raise CycloneDxBomError(
                "sbom.dependency-entry-invalid",
                "CycloneDX dependency entry requires an explicit dependsOn array",
            )
        if targets != sorted(targets) or len(set(targets)) != len(targets):
            raise CycloneDxBomError(
                "sbom.dependency-order-invalid",
                "CycloneDX dependency targets must be unique and sorted",
            )
        if any(target not in inventory for target in targets):
            raise CycloneDxBomError(
                "sbom.dependency-ref-dangling",
                "CycloneDX dependency graph contains a dangling reference",
            )
        if ref in targets:
            raise CycloneDxBomError(
                "sbom.dependency-cycle", "CycloneDX component depends on itself"
            )
        if previous_ref is not None and ref <= previous_ref:
            raise CycloneDxBomError(
                "sbom.dependency-order-invalid",
                "CycloneDX dependency entries must be sorted by ref",
            )
        previous_ref = ref
        graph[ref] = tuple(targets)
    if set(inventory) - set(graph):
        raise CycloneDxBomError(
            "sbom.dependency-entry-missing",
            "every inventory object, including leaves, needs a dependency entry",
        )
    return graph


def _require_composition(
    document: Mapping[str, object],
    root_ref: str,
    *,
    lifecycle: CycloneDxLifecycle,
) -> None:
    expected_aggregates = (
        {"complete", "incomplete_third_party_only"}
        if lifecycle is CycloneDxLifecycle.SOURCE
        else {"complete"}
    )
    compositions = document.get("compositions")
    if (
        not isinstance(compositions, list)
        or len(compositions) != 1
        or not isinstance(compositions[0], Mapping)
        or set(compositions[0]) != {"aggregate", "dependencies"}
        or compositions[0].get("aggregate") not in expected_aggregates
        or compositions[0].get("dependencies") != [root_ref]
    ):
        raise CycloneDxBomError(
            "sbom.composition-incomplete",
            "CycloneDX composition does not declare an allowed lifecycle completeness",
        )


def _require_reachable(
    root_ref: str, refs: tuple[str, ...], edges: tuple[tuple[str, str], ...]
) -> None:
    adjacency: dict[str, list[str]] = {ref: [] for ref in refs}
    for source, target in edges:
        adjacency[source].append(target)
    seen: set[str] = set()
    pending = [root_ref]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(adjacency[current])
    if seen != set(refs):
        raise CycloneDxBomError(
            "sbom.component-disconnected",
            "CycloneDX inventory contains components disconnected from the root",
        )


def _require_versions(
    inventory: Mapping[str, dict[str, object]], lifecycle: CycloneDxLifecycle
) -> None:
    for component in inventory.values():
        version = component.get("version")
        version_range = component.get("versionRange")
        if lifecycle is CycloneDxLifecycle.RESOLVED:
            if not isinstance(version, str) or not version or version_range is not None:
                raise CycloneDxBomError(
                    "sbom.resolved-version-missing",
                    "post-build inventory requires exact component versions",
                )
        elif not (
            (isinstance(version, str) and bool(version))
            or (
                isinstance(version_range, str)
                and bool(version_range)
                and component.get("isExternal") is True
            )
        ):
            raise CycloneDxBomError(
                "sbom.source-version-missing",
                "pre-build inventory requires a version or external range",
            )


def _properties(component: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    raw = component.get("properties", [])
    if not isinstance(raw, list):
        raise CycloneDxBomError(
            "sbom.properties-invalid", "CycloneDX component properties are malformed"
        )
    for item in raw:
        prop = _object(item, "sbom.properties-invalid")
        name = prop.get("name")
        value = prop.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise CycloneDxBomError(
                "sbom.properties-invalid", "CycloneDX component property is malformed"
            )
        result.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in result.items()}


def _require_source_bom_binding(
    metadata: Mapping[str, object],
    lifecycle: CycloneDxLifecycle,
    expected: ContentIdentity | None,
) -> None:
    props = _properties(metadata)
    values = props.get(LITERATE_SOURCE_BOM_IDENTITY_PROPERTY, ())
    if lifecycle is CycloneDxLifecycle.SOURCE:
        if values:
            raise CycloneDxBomError(
                "sbom.source-bom-binding-invalid",
                "pre-build CycloneDX metadata must not name a predecessor BOM",
            )
        return
    if expected is None or values != (expected.uri,):
        raise CycloneDxBomError(
            "sbom.source-bom-binding-invalid",
            "post-build CycloneDX metadata must bind the exact pre-build BOM",
        )


def _require_resolved_graph_binding(
    metadata: Mapping[str, object], managed_graph: CycloneDxManagedGraph
) -> None:
    properties = _properties(metadata)
    values = properties.get(LITERATE_RESOLVED_GRAPH_IDENTITY_PROPERTY, ())
    legacy_values = properties.get(
        LEGACY_LITERATE_COMPONENT_COMPOSITION_IDENTITY_PROPERTY, ()
    )
    if values and legacy_values:
        raise CycloneDxBomError(
            "sbom.resolved-graph-binding-invalid",
            "CycloneDX metadata cannot mix v2 and legacy graph identity bindings",
        )
    selected = values or legacy_values
    if selected != (managed_graph.resolved_graph_identity.uri,):
        raise CycloneDxBomError(
            "sbom.resolved-graph-binding-invalid",
            "CycloneDX metadata must bind the exact resolved Component graph",
        )


def _require_lifecycle_transition(
    source_root: str,
    source_inventory: Mapping[str, dict[str, object]],
    source_graph: Mapping[str, tuple[str, ...]],
    resolved_root: str,
    resolved_inventory: Mapping[str, dict[str, object]],
    resolved_graph: Mapping[str, tuple[str, ...]],
) -> None:
    if source_root != resolved_root or not set(source_inventory) <= set(
        resolved_inventory
    ):
        raise CycloneDxBomError(
            "sbom.transition-inventory-dropped",
            "post-build CycloneDX inventory must preserve every pre-build bom-ref",
        )
    source_edges = {
        (source, target)
        for source, targets in source_graph.items()
        for target in targets
    }
    resolved_edges = {
        (source, target)
        for source, targets in resolved_graph.items()
        for target in targets
    }
    if not source_edges <= resolved_edges:
        raise CycloneDxBomError(
            "sbom.transition-edge-dropped",
            "post-build CycloneDX graph must preserve every pre-build edge",
        )
    for ref, source in source_inventory.items():
        resolved = resolved_inventory[ref]
        for key, value in source.items():
            if key == "versionRange":
                version = resolved.get("version")
                if not isinstance(version, str) or not version:
                    raise CycloneDxBomError(
                        "sbom.transition-version-invalid",
                        "a source versionRange may only become an exact version",
                    )
                continue
            if key == "properties":
                source_properties = _properties(source)
                resolved_properties = _properties(resolved)
                if any(
                    resolved_properties.get(name, ())[: len(values)] != values
                    for name, values in source_properties.items()
                ):
                    raise CycloneDxBomError(
                        "sbom.transition-component-changed",
                        "post-build inventory changed pre-build component properties",
                    )
                continue
            if resolved.get(key) != value:
                raise CycloneDxBomError(
                    "sbom.transition-component-changed",
                    "post-build inventory changed a pre-build component identity field",
                )
        if "versionRange" not in source and "versionRange" in resolved:
            raise CycloneDxBomError(
                "sbom.transition-version-invalid",
                "an exact pre-build version cannot become a post-build range",
            )


def _require_dependency_classification(
    inventory: Mapping[str, dict[str, object]], root_ref: str
) -> None:
    for ref, component in inventory.items():
        props = _properties(component)
        kinds = props.get(LITERATE_DEPENDENCY_KIND_PROPERTY, ())
        scopes = props.get(LITERATE_DEPENDENCY_SCOPE_PROPERTY, ())
        if len(kinds) != 1 or kinds[0] not in _DEPENDENCY_KINDS:
            raise CycloneDxBomError(
                "sbom.dependency-kind-missing",
                "every CycloneDX inventory object needs one dependency kind",
            )
        if ref == root_ref:
            if kinds != (ManagedComponentKind.ROOT.value,) or scopes:
                raise CycloneDxBomError(
                    "sbom.dependency-scope-invalid",
                    "CycloneDX root dependency classification is invalid",
                )
            continue
        if not scopes or len(set(scopes)) != len(scopes):
            raise CycloneDxBomError(
                "sbom.dependency-scope-invalid",
                "every CycloneDX dependency needs explicit unique scopes",
            )
        if set(scopes) - _DEPENDENCY_SCOPES:
            raise CycloneDxBomError(
                "sbom.dependency-scope-invalid",
                "CycloneDX dependency contains an unsupported scope",
            )


def _require_managed_graph(
    inventory: Mapping[str, dict[str, object]],
    graph: Mapping[str, tuple[str, ...]],
    root_ref: str,
    managed: CycloneDxManagedGraph,
    *,
    lifecycle: CycloneDxLifecycle,
    repository_resolutions: (Sequence[CycloneDxRepositorySourceResolution] | None),
) -> None:
    if root_ref != managed.root_ref:
        raise CycloneDxBomError(
            "sbom.component-graph-mismatch",
            "CycloneDX root does not match the resolved Component composition",
        )
    managed_refs = {item.bom_ref for item in managed.components}
    if not managed_refs <= set(inventory):
        raise CycloneDxBomError(
            "sbom.component-graph-mismatch",
            "CycloneDX inventory omits a framework-managed dependency",
        )
    expected_claims = {
        (item.binding_property, item.identity.uri): item.bom_ref
        for item in managed.components
    }
    managed_kinds = frozenset(item.value for item in ManagedComponentKind)
    for ref, actual in inventory.items():
        props = _properties(actual)
        kinds = props.get(LITERATE_DEPENDENCY_KIND_PROPERTY, ())
        claims = tuple(
            (property_name, value)
            for property_name in (
                LITERATE_COMPONENT_REVISION_PROPERTY,
                LITERATE_REPOSITORY_DEPENDENCY_PROPERTY,
            )
            for value in props.get(property_name, ())
        )
        if ref not in managed_refs:
            if (
                any(kind in managed_kinds for kind in kinds)
                or claims
                or ref.startswith("urn:literate-ai:component:")
                or ref.startswith("urn:literate-ai:repository-source:")
            ):
                raise CycloneDxBomError(
                    "sbom.managed-identity-claimed",
                    "unmanaged CycloneDX inventory claims a managed identity or kind",
                )
            continue
        if len(claims) != 1 or expected_claims.get(claims[0]) != ref:
            raise CycloneDxBomError(
                "sbom.managed-identity-claimed",
                "CycloneDX managed identity is missing, duplicated, or claimed "
                "by another ref",
            )

    resolution_by_identity = _repository_resolution_map(
        managed,
        repository_resolutions,
        required_for_mutable=lifecycle is CycloneDxLifecycle.RESOLVED,
    )
    expected_edges_by_source: dict[str, list[str]] = {ref: [] for ref in managed_refs}
    for edge in managed.edges:
        expected_edges_by_source[edge.source_ref].append(_managed_edge_value(edge))
    for expected in managed.components:
        actual = inventory[expected.bom_ref]
        props = _properties(actual)
        if props.get(expected.binding_property) != (expected.identity.uri,):
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX component identity differs from framework resolution",
            )
        if props.get(LITERATE_DEPENDENCY_KIND_PROPERTY) != (expected.kind.value,):
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX managed component kind differs from framework resolution",
            )
        if tuple(sorted(props.get(LITERATE_DEPENDENCY_SCOPE_PROPERTY, ()))) != (
            expected.scopes
        ):
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX managed dependency scopes differ from framework resolution",
            )
        if props.get(LITERATE_DEPENDENCY_EDGE_PROPERTY, ()) != tuple(
            sorted(expected_edges_by_source[expected.bom_ref])
        ):
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX managed edge kind or optionality differs from resolution",
            )
        if actual.get("name") != expected.name:
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX managed component name differs from framework resolution",
            )
        resolution = resolution_by_identity.get(expected.identity)
        if lifecycle is CycloneDxLifecycle.SOURCE:
            if expected.version is not None:
                valid_version = (
                    actual.get("version") == expected.version
                    and "versionRange" not in actual
                )
            else:
                valid_version = (
                    actual.get("versionRange") == expected.version_range
                    and "version" not in actual
                )
        elif resolution is not None:
            valid_version = (
                actual.get("version") == resolution.resolved_commit
                and "versionRange" not in actual
            )
        else:
            valid_version = (
                expected.version is not None
                and actual.get("version") == expected.version
                and "versionRange" not in actual
            )
        if not valid_version:
            raise CycloneDxBomError(
                "sbom.component-graph-mismatch",
                "CycloneDX managed component version differs from framework resolution",
            )
        if expected.kind is ManagedComponentKind.REPOSITORY_SOURCE:
            _require_repository_resolution_properties(
                props,
                expected=resolution,
                required=(
                    lifecycle is CycloneDxLifecycle.RESOLVED
                    and expected.version_range is not None
                ),
            )
    expected_edges = {(item.source_ref, item.target_ref) for item in managed.edges}
    actual_edges = {
        (source, target)
        for source in managed_refs
        for target in graph[source]
        if target in managed_refs
    }
    if actual_edges != expected_edges:
        raise CycloneDxBomError(
            "sbom.component-graph-mismatch",
            "CycloneDX managed edges differ from Component resolution",
        )


def _require_repository_resolution_properties(
    props: Mapping[str, tuple[str, ...]],
    *,
    expected: CycloneDxRepositorySourceResolution | None,
    required: bool,
) -> None:
    observed = {
        property_name: props.get(property_name, ())
        for property_name, _field_name in _REPOSITORY_RESOLUTION_PROPERTIES
    }
    present = {name for name, values in observed.items() if values}
    if present and present != set(observed):
        raise CycloneDxBomError(
            "sbom.resolution-invalid",
            "repository resolution properties must form one complete evidence set",
        )
    if required and not present:
        raise CycloneDxBomError(
            "sbom.resolution-missing",
            "mutable repository inventory requires exact source-lock properties",
        )
    if any(len(values) != 1 for values in observed.values() if values):
        raise CycloneDxBomError(
            "sbom.resolution-invalid",
            "repository resolution properties must be unique",
        )
    if expected is not None:
        for property_name, field_name in _REPOSITORY_RESOLUTION_PROPERTIES:
            if observed[property_name] != (getattr(expected, field_name).uri,):
                raise CycloneDxBomError(
                    "sbom.resolution-mismatch",
                    "repository resolution properties differ from the exact lock",
                )


__all__ = [
    "CycloneDxBomError",
    "build_cyclonedx_bom",
    "validate_cyclonedx_bom",
    "validate_resolved_cyclonedx_bom",
]

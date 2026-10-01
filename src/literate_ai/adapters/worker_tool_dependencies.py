"""Native dependency evidence for exact privately registered worker tools."""

import json
import sys
import tempfile
from dataclasses import dataclass

from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.dependencies import (
    HostDependencyObservation,
    PortableHostDependencyObserver,
)
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

MAX_DEPENDENCY_BYTES = 4 * 1024 * 1024
_SCHEMA = "literate-ai/worker-tool-dependencies@1"
_INDEXED_SCHEMA = "literate-ai/worker-tool-dependencies@2"
MAX_EXPANDED_EDGE_BYTES = 16 * 1024 * 1024
MAX_INDEXED_EDGES = 65536


class WorkerDependencyGraphLimitError(ValueError):
    """Only bounded numeric graph measurements may cross the observation boundary."""

    def __init__(self, *, size, limit, components, edges, component_bytes, edge_bytes):
        self.metrics = dict(
            bytes=size,
            limit=limit,
            components=components,
            edges=edges,
            component_bytes=component_bytes,
            edge_bytes=edge_bytes,
        )
        super().__init__(
            "worker dependency graph exceeds its bound ("
            + ", ".join(f"{key}={value}" for key, value in self.metrics.items())
            + ")"
        )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate dependency record key")
        result[key] = value
    return result


def _decoded_edges(value):
    edges = value["edges"]
    if value["schema"] == _SCHEMA:
        return edges
    references = [value["root"], *(item["bom-ref"] for item in value["components"])]
    if not isinstance(edges, list) or len(edges) > MAX_INDEXED_EDGES:
        raise ValueError("indexed dependency edge count exceeds bound")
    if any(
        not isinstance(edge, list)
        or len(edge) != 2
        or any(
            type(index) is not int or not 0 <= index < len(references) for index in edge
        )
        for edge in edges
    ):
        raise ValueError("invalid indexed dependency edge")
    if edges != [list(edge) for edge in sorted({tuple(edge) for edge in edges})]:
        raise ValueError("indexed dependency edges must be unique and canonical")
    sizes = [len(canonical_json_bytes(ref)) for ref in references]
    if sum(sizes[a] + sizes[b] + 4 for a, b in edges) + 2 > MAX_EXPANDED_EDGE_BYTES:
        raise ValueError("expanded dependency edges exceed bound")
    return sorted([references[a], references[b]] for a, b in edges)


@dataclass(frozen=True)
class WorkerToolDependencies:
    document: bytes

    def __post_init__(self):
        if not isinstance(self.document, bytes):
            raise ValueError("worker dependency record must be bytes")
        if len(self.document) > MAX_DEPENDENCY_BYTES:
            raise ValueError(
                "worker dependency record exceeds its bound "
                f"(bytes={len(self.document)}, limit={MAX_DEPENDENCY_BYTES})"
            )
        try:
            value = json.loads(self.document, object_pairs_hook=_pairs)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema", "tools", "root", "components", "edges"}
                or value["schema"] not in {_SCHEMA, _INDEXED_SCHEMA}
                or canonical_json_bytes(value) != self.document
            ):
                raise ValueError("invalid canonical worker dependency record")
            tools, root = value["tools"], value["root"]
            if (
                not isinstance(tools, list)
                or not 1 <= len(tools) <= 256
                or tools != sorted(set(tools))
                or not isinstance(root, str)
                or not root
                or len(root) > 4096
            ):
                raise ValueError("invalid dependency selection or root")
            for tool in tools:
                ContentIdentity.parse_uri(tool)
            components, edges = value["components"], value["edges"]
            if (
                not isinstance(components, list)
                or not components
                or not isinstance(edges, list)
            ):
                raise ValueError("dependency graph is empty or invalid")
            refs = [item["bom-ref"] for item in components]
            if (
                any(not isinstance(ref, str) or not ref for ref in refs)
                or refs != sorted(set(refs))
                or root in refs
            ):
                raise ValueError("dependency references must be unique and canonical")
            edges = _decoded_edges(value)
            if any(
                not isinstance(edge, list)
                or len(edge) != 2
                or any(not isinstance(end, str) for end in edge)
                for edge in edges
            ):
                raise ValueError("invalid dependency edge")
            if edges != [
                list(edge) for edge in sorted({tuple(edge) for edge in edges})
            ]:
                raise ValueError("dependency edges must be unique and canonical")
            known = {root, *refs}
            adjacency = {}
            for source, target in edges:
                if source not in known or target not in known or source == target:
                    raise ValueError("dangling or self dependency edge")
                adjacency.setdefault(source, set()).add(target)
            seen, pending = {root}, [root]
            while pending:
                for target in adjacency.get(pending.pop(), ()):
                    if target not in seen:
                        seen.add(target)
                        pending.append(target)
            if seen != known:
                raise ValueError("unreachable worker dependency")
        except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid worker dependency record") from exc

    @property
    def identity(self):
        return canonical_identity(json.loads(self.document))

    @property
    def observation(self):
        value = json.loads(self.document)
        return HostDependencyObservation(
            tuple(value["components"]),
            tuple(tuple(edge) for edge in _decoded_edges(value)),
        )


def _graph_document(*, tools, root, components, edges):
    references = {
        root: 0,
        **{item["bom-ref"]: i + 1 for i, item in enumerate(components)},
    }
    try:
        indexed = sorted((references[a], references[b]) for a, b in edges)
    except KeyError as exc:
        raise ValueError("dangling dependency edge") from exc
    value = {
        "schema": _INDEXED_SCHEMA,
        "tools": tools,
        "root": root,
        "components": components,
        "edges": indexed,
    }
    content = canonical_json_bytes(value)
    if len(content) > MAX_DEPENDENCY_BYTES:
        # These collections come from the native observer, not oversized wire JSON.
        # Report only sizes; never include private component paths or graph contents.
        raise WorkerDependencyGraphLimitError(
            size=len(content),
            limit=MAX_DEPENDENCY_BYTES,
            components=len(components),
            edges=len(edges),
            component_bytes=len(canonical_json_bytes(components)),
            edge_bytes=len(canonical_json_bytes(edges)),
        )
    return content


def capture_worker_tool_dependencies(
    registry, identities, *, environment, root_ref, require_current
):
    if not isinstance(registry, WorkerToolchainRegistry) or not callable(
        require_current
    ):
        raise TypeError("worker dependencies require a registry and live guard")
    if not isinstance(root_ref, str) or not root_ref or len(root_ref) > 4096:
        raise ValueError("invalid dependency root")
    private_environment = dict(environment)
    if len(private_environment) > 2048 or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or "\0" in key
        or "\0" in value
        for key, value in private_environment.items()
    ):
        raise ValueError("invalid private dependency environment")
    require_current()
    bindings = registry.select(identities)
    if not bindings:
        raise ValueError("dependency selection cannot be empty")
    components, edges = [], []
    graph_bytes = 0
    for binding in bindings:
        require_current()
        binding.require_unchanged()
        overridden = {key.casefold() for key, _ in binding.environment}
        effective = {
            key: value
            for key, value in private_environment.items()
            if key.casefold() not in overridden
        }
        effective.update(dict(binding.environment))
        # Tool launchers may be symlinks or supported scripts. The native
        # observer owns their resolution; they are not produced artifacts.
        with tempfile.TemporaryDirectory(prefix="litai-tool-deps-") as empty:
            observed = PortableHostDependencyObserver(
                toolchain_commands=(binding.command,),
                windows_environment=effective if sys.platform == "win32" else None,
                macos_loader_paths=MacOsLoaderPaths.from_environment(effective)
                if sys.platform == "darwin"
                else None,
                linux_loader_paths=LinuxLoaderPaths.from_environment(effective)
                if sys.platform.startswith("linux")
                else None,
            ).observe({"artifact_path": empty}, root_ref=root_ref)
        WorkerToolDependencies(
            _graph_document(
                tools=[binding.toolchain_identity.uri],
                root=root_ref,
                components=sorted(
                    observed.components, key=lambda item: item["bom-ref"]
                ),
                edges=sorted(observed.edges),
            )
        )
        # Preserve separate loader contexts even when they contain the same image.
        remap = {
            item["bom-ref"]: "urn:literate-ai:worker-tool-dependency:"
            + canonical_identity(
                {"tool": binding.toolchain_identity.uri, "component": item}
            ).digest
            for item in observed.components
        }
        if len(remap) != len(observed.components) or root_ref in remap:
            raise ValueError("ambiguous native dependency references")
        remap[root_ref] = root_ref
        selected_components = tuple(
            item | {"bom-ref": remap[item["bom-ref"]]} for item in observed.components
        )
        selected_edges = tuple(
            (remap[source], remap[target]) for source, target in observed.edges
        )
        graph_bytes += len(canonical_json_bytes((selected_components, selected_edges)))
        if graph_bytes > MAX_DEPENDENCY_BYTES:
            # Check the complete canonical form before retaining another selection.
            _graph_document(
                tools=[item.uri for item in identities],
                root=root_ref,
                components=sorted(
                    (*components, *selected_components),
                    key=lambda item: item["bom-ref"],
                ),
                edges=sorted(set((*edges, *selected_edges))),
            )
        components.extend(selected_components)
        edges.extend(selected_edges)
        binding.require_unchanged()
        require_current()
    registry.select(identities)
    require_current()
    return WorkerToolDependencies(
        _graph_document(
            tools=[item.uri for item in identities],
            root=root_ref,
            components=sorted(components, key=lambda item: item["bom-ref"]),
            edges=sorted(set(edges)),
        )
    )

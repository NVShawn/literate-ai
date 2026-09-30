"""Build one effective authority graph from canonical project state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePath

from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.legacy_generation_catalog import (
    load_legacy_flavor_catalog_entries,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.application.repository_lineage import (
    repository_lineage_authority_graph,
)
from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphError,
    AuthorityGraphNode,
)
from literate_ai.contracts.catalog_imports import (
    CatalogImport,
    CatalogImportFile,
    CatalogImportsFile,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectError,
    discover_project,
    project_skill_catalog,
)

_BASELINE_PATH = ".literate/initialization-baseline.json"
_ENTITY_LAYOUT = (
    ("component", "component", "component.md"),
    ("flavor", "flavor", "flavor.md"),
    ("skill", "skill", "SKILL.md"),
    ("mcp", "mcp", "mcp.md"),
)


def _baseline_identities(root: Path) -> dict[str, str]:
    path = root / _BASELINE_PATH
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        files = document["files"]
        return {
            item["path"]: "sha256:" + item["identity"]["digest"]
            for item in files
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("identity"), dict)
            and isinstance(item["identity"].get("digest"), str)
        }
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise AuthorityGraphError(
            "graph.baseline_invalid", "initialization baseline is malformed"
        ) from exc


def _identity(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _import_is_unchanged(root: Path, imported: CatalogImport) -> bool:
    """Require every imported file to retain its recorded content identity."""

    return bool(imported.files) and all(
        (path := root / item.path).is_file()
        and not path.is_symlink()
        and _identity(path) == item.identity
        for item in imported.files
    )


def _candidate_identity(files: tuple[CatalogImportFile, ...]) -> str | None:
    sentinels = ("component.md", "flavor.md", "SKILL.md", "mcp.md")
    for sentinel in sentinels:
        for item in files:
            if PurePath(item.path).name == sentinel:
                return item.identity
    return files[0].identity if files else None


def _component_authoring(path: Path, *, project_root: Path):
    try:
        authoring = parse_component_markdown(
            path,
            path.read_text(encoding="utf-8"),
            project_root=project_root,
        )
    except Exception as exc:
        raise AuthorityGraphError(
            "graph.component_invalid", f"cannot read Component authority: {path}"
        ) from exc
    return authoring


def _locked_flavor_edges(
    lock: dict[str, object],
    *,
    component_by_coordinate: dict[tuple[str, str], str],
    flavor_by_revision: dict[str, str],
) -> tuple[AuthorityGraphEdge, ...]:
    """Project exact per-node Flavor selections from one validated lock shape."""

    try:
        nodes = lock["nodes"]
        assert isinstance(nodes, list)
        edges: list[AuthorityGraphEdge] = []
        for node in nodes:
            revision = node["revision"]
            selection = node["target_flavor_selection"]
            coordinate = revision["coordinate"]
            component_id = component_by_coordinate[
                (coordinate["namespace"], coordinate["name"])
            ]
            for slot in selection["slots"]:
                slot_id = slot["slot"]["slot_id"]
                for selected in slot["selected"]:
                    digest = selected["flavor_revision"]["digest"]
                    edges.append(
                        AuthorityGraphEdge(
                            flavor_by_revision[digest],
                            component_id,
                            "flavor-selection",
                            slot_id,
                        )
                    )
        return tuple(edges)
    except (AssertionError, KeyError, TypeError) as exc:
        raise AuthorityGraphError(
            "graph.flavor_selection_unresolved",
            "Component lock Flavor selection cannot resolve into the effective catalog",
        ) from exc


def _orchestration_entities(
    project_id: str, authority: RepositoryOrchestration
) -> tuple[list[AuthorityGraphNode], list[AuthorityGraphEdge]]:
    """Declare independent pins and relationship roles, never child execution order."""
    binding_id = f"orchestration:{project_id}"
    nodes = [
        AuthorityGraphNode(
            binding_id,
            "repository-orchestration",
            "Declared repository orchestration",
            project_id,
            project_id,
            inheritable=False,
            properties=tuple(
                sorted(
                    {
                        "identity": authority.identity,
                        "gitmodules_identity": authority.gitmodules_identity,
                        "verification": "not-checked",
                    }.items()
                )
            ),
        )
    ]
    edges = [
        AuthorityGraphEdge(
            f"repository:{project_id}", binding_id, "orchestration-authority"
        )
    ]
    for pin in authority.repositories:
        node_id = f"gitlink:{pin.path}"
        properties = {
            "identity": canonical_identity(pin.to_dict()).uri,
            "gitlink_path": pin.path,
            "configured_name": pin.name,
            "url": pin.url,
            "commit": pin.commit,
            "child_authority": "independent",
            "verification": "not-checked",
        }
        if pin.branch is not None:
            properties["branch"] = pin.branch
        nodes.append(
            AuthorityGraphNode(
                node_id,
                "external-repository",
                f"{pin.path} @ {pin.commit}",
                project_id,
                project_id,
                inheritable=False,
                properties=tuple(sorted(properties.items())),
            )
        )
        edges.append(AuthorityGraphEdge(binding_id, node_id, "repository-pin"))
    for relationship in authority.relationships:
        identity = canonical_identity(relationship.to_dict()).uri
        node_id = f"repository-relationship:{identity}"
        nodes.append(
            AuthorityGraphNode(
                node_id,
                "repository-relationship",
                f"{relationship.consumer} depends on {relationship.provider}",
                project_id,
                project_id,
                inheritable=False,
                properties=tuple(
                    sorted(
                        {
                            "identity": identity,
                            "consumer": relationship.consumer,
                            "provider": relationship.provider,
                            "execution_order": "not-inferred",
                        }.items()
                    )
                ),
            )
        )
        edges.extend(
            (
                AuthorityGraphEdge(binding_id, node_id, "declares-relationship"),
                AuthorityGraphEdge(
                    f"gitlink:{relationship.consumer}", node_id, "relationship-consumer"
                ),
                AuthorityGraphEdge(
                    f"gitlink:{relationship.provider}", node_id, "relationship-provider"
                ),
            )
        )
    return nodes, edges


def project_authority_graph(
    selected: Path, *, include_component_locks: bool = True
) -> AuthorityGraph:
    """Compose repository lineage and effective local/inherited catalog authority."""

    project = discover_project(selected)
    if project is None:
        raise AuthorityGraphError(
            "graph.project_not_found", f"no {PROJECT_FILENAME} found from {selected}"
        )
    _selection, lineage = FilesystemRepositoryLineageStore(project.root).load()
    project_id = project.definition.project_id
    project_node_id = f"repository:{project_id}"
    lineage_graph = repository_lineage_authority_graph(lineage)
    nodes: list[AuthorityGraphNode] = list(lineage_graph.nodes)
    edges: list[AuthorityGraphEdge] = list(lineage_graph.edges)
    identity_to_node = {
        node.identity.uri: f"repository:{node.project_id}" for node in lineage.nodes
    }
    nodes.append(
        AuthorityGraphNode(
            project_node_id,
            "repository",
            project_id,
            project_id,
            # A checkout path is deployment state, not project authority.  Keeping it
            # out of the solved graph lets an exact project move atomically from a
            # staging directory into its destination without invalidating graph-bound
            # Component locks and resolution audits.
            project_id,
            inherited=False,
            inheritable=True,
        )
    )
    for parent in lineage.selected_parents:
        edges.append(
            AuthorityGraphEdge(
                identity_to_node[parent.uri], project_node_id, "repository-parent"
            )
        )

    if project.definition.repository_orchestration is not None:
        orchestration_nodes, orchestration_edges = _orchestration_entities(
            project_id, project.definition.repository_orchestration
        )
        nodes.extend(orchestration_nodes)
        edges.extend(orchestration_edges)

    baseline = _baseline_identities(project.root)
    selected_parent = (
        identity_to_node[lineage.selected_parents[-1].uri]
        if lineage.selected_parents
        else None
    )
    imports = CatalogImportsFile.load(project.root)
    imported = {(item.kind, item.name): item for item in imports.imports}
    entity_by_path: dict[str, str] = {}
    component_by_coordinate: dict[tuple[str, str], str] = {}
    component_inputs: list[tuple[str, tuple[str, ...]]] = []
    loaded_flavors = load_legacy_flavor_catalog_entries(project.roots("flavor"))
    flavor_name_by_source_identity = {
        item.revision.source_snapshot.uri: item.recipe.flavor_id
        for item in loaded_flavors
        if item.revision.source_snapshot is not None
    }
    skill_manifests: tuple[Path, ...] = ()
    if project.roots("skill"):
        try:
            skill_manifests = tuple(
                item.manifest_path
                for item in project_skill_catalog(project).skills
                if item.manifest_path is not None
            )
        except ProjectError as exc:
            raise AuthorityGraphError(exc.code, exc.message) from exc
    for kind, root_kind, sentinel in _ENTITY_LAYOUT:
        for catalog_root in project.roots(root_kind):
            candidates = (
                tuple(
                    path
                    for path in skill_manifests
                    if path.is_relative_to(catalog_root.resolve())
                )
                if kind == "skill"
                else tuple(catalog_root.rglob(sentinel))
            )
            for path in sorted(candidates):
                if path.is_symlink() or not path.is_file():
                    continue
                item_root = path.parent
                catalog_name = item_root.relative_to(catalog_root).as_posix()
                relative = path.relative_to(project.root).as_posix()
                current_identity = _identity(path)
                name = (
                    flavor_name_by_source_identity.get(current_identity, catalog_name)
                    if kind == "flavor"
                    else catalog_name
                )
                import_record = imported.get((kind, name)) or imported.get(
                    (kind, catalog_name)
                )
                baseline_match = (
                    import_record is None and baseline.get(relative) == current_identity
                )
                import_unchanged = import_record is not None and _import_is_unchanged(
                    project.root, import_record
                )
                import_source = (
                    import_record.source.project_id if import_unchanged else None
                )
                inherited = baseline_match or import_source is not None
                provenance = import_source or (
                    identity_to_node[lineage.selected_parents[-1].uri].removeprefix(
                        "repository:"
                    )
                    if baseline_match and lineage.selected_parents
                    else project_id
                )
                node_id = f"{kind}:{name}"
                authoring = (
                    _component_authoring(path, project_root=project.root)
                    if kind == "component"
                    else None
                )
                inheritable = authoring.inheritable if authoring is not None else True
                properties = [
                    ("path", relative),
                    ("identity", current_identity),
                ]
                if authoring is not None:
                    properties.append(("coordinate", authoring.coordinate.uri))
                nodes.append(
                    AuthorityGraphNode(
                        node_id,
                        kind,
                        name,
                        project_id,
                        provenance,
                        inherited=inherited,
                        inheritable=inheritable,
                        properties=tuple(properties),
                    )
                )
                item_relative = item_root.relative_to(project.root).as_posix()
                entity_by_path[item_relative] = node_id
                entity_by_path[relative] = node_id
                if authoring is not None:
                    component_by_coordinate[
                        (authoring.coordinate.namespace, authoring.coordinate.name)
                    ] = node_id
                    component_inputs.append(
                        (
                            node_id,
                            tuple(item.uri for item in authoring.authoring_inputs),
                        )
                    )
                source_id = (
                    f"repository:{import_source}"
                    if import_source is not None
                    else selected_parent
                    if inherited
                    else project_node_id
                )
                if source_id is not None and source_id in {
                    node.node_id for node in nodes
                }:
                    edges.append(AuthorityGraphEdge(source_id, node_id, "defines"))
                if import_record is not None and not import_unchanged:
                    candidate_id = (
                        f"candidate:{kind}:{name}@{import_record.source.project_id}"
                    )
                    candidate_identity = _candidate_identity(import_record.files)
                    properties = [("disposition", "shadowed-by-local-change")]
                    if candidate_identity is not None:
                        properties.append(("identity", candidate_identity))
                    nodes.append(
                        AuthorityGraphNode(
                            candidate_id,
                            kind,
                            name,
                            import_record.source.project_id,
                            import_record.source.project_id,
                            inherited=True,
                            inheritable=True,
                            properties=tuple(properties),
                        )
                    )
                    source_repository = f"repository:{import_record.source.project_id}"
                    if source_repository in {node.node_id for node in nodes}:
                        edges.append(
                            AuthorityGraphEdge(
                                source_repository, candidate_id, "defines"
                            )
                        )
                    edges.append(
                        AuthorityGraphEdge(
                            candidate_id,
                            node_id,
                            "shadowed-by",
                            "local content differs from imported identity",
                        )
                    )

    candidate_by_origin: dict[tuple[str, str, str], str] = {}
    for decision in imports.decisions:
        if decision.disposition == "effective":
            continue
        candidate_id = (
            f"candidate:{decision.kind}:{decision.name}"
            f"@{decision.source.project_id}:{decision.disposition}"
        )
        candidate_by_origin[
            (decision.kind, decision.name, decision.source.project_id)
        ] = candidate_id
        properties = [("disposition", decision.disposition)]
        identity = _candidate_identity(decision.files)
        if identity is not None:
            properties.append(("identity", identity))
        nodes.append(
            AuthorityGraphNode(
                candidate_id,
                decision.kind,
                decision.name,
                decision.source.project_id,
                decision.source.project_id,
                inherited=True,
                inheritable=decision.disposition != "withheld",
                properties=tuple(properties),
            )
        )
        source_repository = f"repository:{decision.source.project_id}"
        if source_repository in {node.node_id for node in nodes}:
            edges.append(AuthorityGraphEdge(source_repository, candidate_id, "defines"))
    for decision in imports.decisions:
        if decision.disposition != "shadowed":
            continue
        candidate_id = candidate_by_origin[
            (decision.kind, decision.name, decision.source.project_id)
        ]
        selected = (
            candidate_by_origin.get(
                (
                    decision.kind,
                    decision.name,
                    decision.selected_source_project_id or "",
                )
            )
            or f"{decision.kind}:{decision.name}"
        )
        if selected in {node.node_id for node in nodes}:
            edges.append(AuthorityGraphEdge(candidate_id, selected, "shadowed-by"))
    flavor_by_revision = {
        item.revision.identity.digest: f"flavor:{item.recipe.flavor_id}"
        for item in loaded_flavors
    }
    locked_flavor_revision_nodes: set[str] = set()
    for component_id, references in component_inputs:
        for reference in references:
            reference_parent = str(Path(reference).parent).replace("\\", "/")
            referenced = entity_by_path.get(reference) or entity_by_path.get(
                reference_parent
            )
            if referenced is not None:
                edges.append(
                    AuthorityGraphEdge(
                        referenced, component_id, "generation-input", reference
                    )
                )
    for catalog_root in project.roots("component") if include_component_locks else ():
        for lock_path in sorted(catalog_root.rglob("component.lock.json")):
            try:
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                revisions = {
                    node["target_flavor_selection"]["component_revision"][
                        "digest"
                    ]: node["revision"]["coordinate"]
                    for node in lock["nodes"]
                }
                lock_edges = lock["edges"]
                for lock_node in lock["nodes"]:
                    for slot in lock_node["target_flavor_selection"]["slots"]:
                        for selected in slot["selected"]:
                            digest = selected["flavor_revision"]["digest"]
                            if digest in flavor_by_revision:
                                continue
                            node_id = f"flavor-revision:{digest}"
                            flavor_by_revision[digest] = node_id
                            if node_id in locked_flavor_revision_nodes:
                                continue
                            locked_flavor_revision_nodes.add(node_id)
                            nodes.append(
                                AuthorityGraphNode(
                                    node_id,
                                    "flavor-revision",
                                    digest[:12],
                                    project_id,
                                    lock_path.relative_to(project.root).as_posix(),
                                    inherited=False,
                                    inheritable=False,
                                    properties=(("identity", f"sha256:{digest}"),),
                                )
                            )
            except (
                KeyError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
            ) as exc:
                raise AuthorityGraphError(
                    "graph.component_lock_invalid",
                    f"cannot read Component dependency graph: {lock_path}",
                ) from exc
            try:
                edges.extend(
                    _locked_flavor_edges(
                        lock,
                        component_by_coordinate=component_by_coordinate,
                        flavor_by_revision=flavor_by_revision,
                    )
                )
            except AuthorityGraphError as exc:
                raise AuthorityGraphError(
                    exc.code, f"{exc.message}: {lock_path}"
                ) from exc
            for edge in lock_edges:
                try:
                    consumer_revision = edge["consumer_revision"]["digest"]
                    provider_revision = edge["provider_revision"]["digest"]
                    consumer = revisions[consumer_revision]
                    provider = revisions[provider_revision]
                    consumer_id = component_by_coordinate[
                        (consumer["namespace"], consumer["name"])
                    ]
                    provider_id = component_by_coordinate[
                        (provider["namespace"], provider["name"])
                    ]
                    requirement = edge["requirement_id"]
                except (KeyError, TypeError) as exc:
                    raise AuthorityGraphError(
                        "graph.component_lock_unresolved",
                        "Component lock edge cannot resolve into the effective "
                        "catalog: "
                        f"{lock_path}",
                    ) from exc
                edges.append(
                    AuthorityGraphEdge(
                        provider_id, consumer_id, "component-dependency", requirement
                    )
                )
    return AuthorityGraph.create(project_id, nodes, edges)


def project_lineage_authority_identity(project_root: Path) -> str:
    """Bind only repository-lineage state, not the whole project's entity catalog.

    A Component lock must fail closed when the exact repository this project was
    initialized/updated from changes -- that is genuinely project-wide state. It must
    NOT fail closed when an unrelated sibling Component, Flavor, or skill changes:
    that information is already carried precisely by the lock's own ``authorings`` and
    ``flavors`` fields. Reuse the fully validated graph (so a colliding/cyclic entity
    still fails during construction) but bind only its repository-kind nodes and the
    edges between them.
    """

    graph = project_authority_graph(project_root, include_component_locks=False)
    lineage_nodes = tuple(node for node in graph.nodes if node.kind == "repository")
    lineage_node_ids = {node.node_id for node in lineage_nodes}
    lineage_edges = tuple(
        edge
        for edge in graph.edges
        if edge.source in lineage_node_ids and edge.target in lineage_node_ids
    )
    return AuthorityGraph.create(
        graph.project_id, lineage_nodes, lineage_edges
    ).identity


__all__ = ["project_authority_graph", "project_lineage_authority_identity"]

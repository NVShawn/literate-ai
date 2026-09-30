"""litai catalog subcommands: copy, graph, and canonical Flavor migration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from literate_ai.adapters.catalog_import import (
    CatalogImportError,
    catalog_copy,
    catalog_graph,
)
from literate_ai.authority_graph import AuthorityGraphError
from literate_ai.project_authority_graph import project_authority_graph

from .errors import CliFailure

CATALOG_COPY_SCHEMA = "literate-ai/catalog-copy@1"
CATALOG_GRAPH_SCHEMA = "literate-ai/catalog-graph@1"
CATALOG_FLAVOR_NAME_MIGRATION_SCHEMA = "literate-ai/flavor-name-migration@1"

_FLAVOR_NAME_MIGRATIONS = {
    "accelerator-nvidia-cuda": "accel-nvidia-cuda",
    "google-workspace": "doc-google-workspace",
    "microsoft-365": "doc-microsoft-365",
    "package.apt": "package-apt",
    "package.brew": "package-brew",
    "package.chocolatey": "package-chocolatey",
    "package.conan": "package-conan",
    "package.pip": "package-pip",
    "package.winget": "package-winget",
}
_FLAVOR_DIRECTORY_MIGRATIONS = {
    "apt": "package-apt",
    "brew": "package-brew",
    "chocolatey": "package-chocolatey",
    "conan": "package-conan",
    "cuda": "accel-nvidia-cuda",
    "google-workspace": "doc-google-workspace",
    "microsoft-365": "doc-microsoft-365",
    "pip": "package-pip",
    "winget": "package-winget",
    "python": "lang-python",
    "cpp": "lang-cpp",
    "go": "lang-go",
    "javascript": "lang-javascript",
    "rust": "lang-rust",
    "swift": "lang-swift",
    "linux": "os-linux",
    "macos": "os-macos",
    "windows": "os-windows",
    "make": "build-make",
    "bazel": "build-bazel",
    "cargo": "build-cargo",
    "cmake": "build-cmake",
    "repo-man": "build-repo-man",
    "docker": "deploy-docker",
    "swift-apple": "toolchain-swift-apple",
    "swift-linux": "toolchain-swift-linux",
    "swift-windows": "toolchain-swift-windows",
    "cpu": "accelerator-cpu",
}
_TEXT_SUFFIXES = frozenset({".json", ".md", ".toml", ".yaml", ".yml"})


def _migration_replacements(text: str) -> str:
    for old, new in sorted(
        _FLAVOR_NAME_MIGRATIONS.items(), key=lambda item: -len(item[0])
    ):
        text = text.replace(
            f"flavor://literate-ai/{old}", f"flavor://literate-ai/{new}"
        )
        text = text.replace(f'"name": "{old}"', f'"name": "{new}"')
        text = text.replace(f'name: "{old}"', f'name: "{new}"')
    text = text.replace('target: "bazel-preferred"', 'target: "bazel"')
    text = text.replace('"target": "bazel-preferred"', '"target": "bazel"')
    return text


def migrate_flavor_names(project: Path, *, record: bool) -> dict[str, Any]:
    """Plan or atomically record the canonical built-in Flavor-name migration."""

    root = project.resolve(strict=True)
    manifest = root / "literate.project.json"
    if not manifest.is_file():
        raise CliFailure(
            "project.not_found", "flavor migration requires literate.project.json"
        )
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    flavor_roots = definition.get("flavor_roots")
    component_roots = definition.get("component_roots")
    workflow_roots = definition.get("workflow_roots", [])
    routing_roots = definition.get("routing_roots", [])
    if (
        not isinstance(flavor_roots, list)
        or not isinstance(component_roots, list)
        or not isinstance(workflow_roots, list)
        or not isinstance(routing_roots, list)
    ):
        raise CliFailure(
            "catalog.flavor_migration_project_invalid",
            "project catalog roots are invalid",
        )
    scanned_roots = [
        root / str(item)
        for item in (*flavor_roots, *component_roots, *workflow_roots, *routing_roots)
    ]
    scanned_roots.extend((root / "skills", root / "literate.project.json"))
    if (root / "src" / "literate_ai" / "project_template").is_dir():
        scanned_roots.append(root / "src" / "literate_ai" / "project_template")
    files: set[Path] = set()
    for candidate in scanned_roots:
        if candidate.is_file():
            files.add(candidate)
        elif candidate.is_dir():
            files.update(
                path
                for path in candidate.rglob("*")
                if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES
            )
    moves: list[dict[str, str]] = []
    removals: list[dict[str, str]] = []
    removed_trees: list[Path] = []
    roots_to_move = [root / str(item) for item in flavor_roots]
    template_flavors = root / "src" / "literate_ai" / "project_template" / "flavors"
    if template_flavors.is_dir():
        roots_to_move.append(template_flavors)
    for flavor_root in roots_to_move:
        for old, new in sorted(_FLAVOR_DIRECTORY_MIGRATIONS.items()):
            source = flavor_root / old
            destination = flavor_root / new
            if not source.exists():
                continue
            if destination.exists():
                removals.append(
                    {
                        "path": source.relative_to(root).as_posix(),
                        "canonical": destination.relative_to(root).as_posix(),
                    }
                )
                removed_trees.append(source)
                continue
            moves.append(
                {
                    "from": source.relative_to(root).as_posix(),
                    "to": destination.relative_to(root).as_posix(),
                }
            )
    rewrites: list[dict[str, str]] = []
    for path in sorted(files):
        if any(path == tree or tree in path.parents for tree in removed_trees):
            continue
        original = path.read_text(encoding="utf-8")
        migrated = _migration_replacements(original)
        if migrated != original:
            rewrites.append({"path": path.relative_to(root).as_posix()})
            if record:
                path.write_text(migrated, encoding="utf-8", newline="\n")
    if record:
        for item in moves:
            shutil.move(str(root / item["from"]), str(root / item["to"]))
        for tree in removed_trees:
            shutil.rmtree(tree)
    state = "recorded" if record else "planned"
    return {
        "schema": CATALOG_FLAVOR_NAME_MIGRATION_SCHEMA,
        "state": state,
        "project": str(root),
        "rewrites": rewrites,
        "moves": moves,
        "removals": removals,
        "deprecated_aliases": [
            {"alias": old, "canonical": new}
            for old, new in sorted(_FLAVOR_NAME_MIGRATIONS.items())
        ],
    }


def catalog_migrate_flavor_names_from_args(args) -> dict[str, Any]:
    project = Path(getattr(args, "project", "."))
    return migrate_flavor_names(project, record=bool(getattr(args, "record", False)))


def catalog_copy_from_args(args) -> dict[str, Any]:
    dest = Path(getattr(args, "project", ".")).resolve()
    source = args.source
    items = args.item or []
    overwrite = getattr(args, "overwrite", False)
    if not items:
        raise CliFailure(
            "cli.usage", "at least one item spec required, e.g. flavor:openscad"
        )
    try:
        result = catalog_copy(dest, source, items, overwrite=overwrite)
    except CatalogImportError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return {"schema": CATALOG_COPY_SCHEMA, **result}


def catalog_graph_from_args(args) -> dict[str, Any]:
    project = Path(getattr(args, "project", ".")).resolve()
    try:
        result = catalog_graph(project)
    except Exception as exc:
        raise CliFailure("catalog.graph_error", str(exc)) from exc
    return {"schema": CATALOG_GRAPH_SCHEMA, **result}


def authority_graph_from_args(args) -> dict[str, Any]:
    project = Path(getattr(args, "project", ".")).resolve()
    format_name = getattr(args, "format", "json")
    action = getattr(args, "graph_action", "show")
    output = getattr(args, "output", None)
    try:
        graph = project_authority_graph(project)
        view = graph.filtered(
            kinds=getattr(args, "kind", ()),
            provenance=getattr(args, "provenance", ()),
            ownership=getattr(args, "ownership", None),
            inheritance=getattr(args, "inheritance", None),
            edge_kinds=getattr(args, "edge_kind", ()),
        )
        rendered = view.render(format_name)
    except AuthorityGraphError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if output is not None:
        destination = Path(output).resolve()
        if destination.exists() or destination.is_symlink():
            raise CliFailure("graph.output_exists", "graph output already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8", newline="\n")
    return {
        "schema": view.to_dict()["schema"],
        "project": str(project),
        "format": format_name,
        "output": str(Path(output).resolve()) if output is not None else None,
        "rendered": None if output is not None else rendered,
        "graph": view.to_dict(),
        "rebalance": (
            list(graph.rebalance_recommendations()) if action == "rebalance" else None
        ),
    }


__all__ = [
    "CATALOG_COPY_SCHEMA",
    "CATALOG_GRAPH_SCHEMA",
    "CATALOG_FLAVOR_NAME_MIGRATION_SCHEMA",
    "catalog_copy_from_args",
    "catalog_graph_from_args",
    "catalog_migrate_flavor_names_from_args",
    "authority_graph_from_args",
]

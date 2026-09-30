"""DAG-safe catalog composition adapter."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.application.project_tracker import sanitize_git_url
from literate_ai.contracts.catalog_imports import (
    CatalogImport,
    CatalogImportFile,
    CatalogImportsFile,
    CatalogImportSource,
    TransitiveAncestor,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
)

_GIT_TIMEOUT_SECONDS = 300
_GIT_FILE_PROTOCOL = ("-c", "protocol.file.allow=always")
_GIT_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})


class CatalogImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Source project resolution ─────────────────────────────────────────────────


def _is_git_source_spec(source_spec: str) -> bool:
    raw = source_spec.strip()
    if raw.startswith("local:"):
        return False
    if raw.startswith("git:") or raw.startswith("git@"):
        return True
    locator = raw.split("#", 1)[0]
    return urlsplit(locator).scheme in _GIT_SCHEMES


def _split_git_locator(locator: str) -> tuple[str, str]:
    url, separator, revision = locator.rpartition("#")
    if not separator:
        return locator, "HEAD"
    if not url or not revision:
        raise CatalogImportError(
            "catalog.import_git_invalid",
            "git catalog source #revision selector must be nonempty",
        )
    if (
        revision.startswith("-")
        or revision.endswith((".", "/"))
        or ".." in revision
        or "//" in revision
    ):
        raise CatalogImportError(
            "catalog.import_git_invalid",
            "git catalog source revision is not a safe Git selector",
        )
    return url, revision


def _reject_git_credentials(url: str) -> None:
    if "://" in url:
        parsed = urlsplit(url)
        if parsed.password:
            raise CatalogImportError(
                "catalog.import_git_credentials",
                "git catalog source URLs must not contain passwords or tokens",
            )
        return
    if "@" in url and ":" in url:
        userinfo = url.split("@", 1)[0]
        if ":" in userinfo:
            raise CatalogImportError(
                "catalog.import_git_credentials",
                "git catalog source URLs must not contain passwords or tokens",
            )


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment


def _run_git(
    command: tuple[str, ...] | list[str],
    *,
    cwd: Path,
) -> str:
    argv = list(command)
    try:
        completed = run_bounded_process(
            argv,
            cwd=cwd,
            environment=_git_environment(),
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
            stdout_limit_bytes=1024 * 1024,
            stderr_limit_bytes=1024 * 1024,
            error_prefix="catalog.import_git",
        )
    except BuildError as exc:
        if exc.code.endswith("launch_failed"):
            raise CatalogImportError(
                "catalog.import_git_unavailable",
                "git is required to copy from a git catalog source",
            ) from exc
        if exc.code.endswith("timeout") or exc.code.endswith("no_progress_timeout"):
            raise CatalogImportError(
                "catalog.import_git_timeout",
                "git catalog source exceeded the Git deadline",
            ) from exc
        raise CatalogImportError(
            "catalog.import_git_failed",
            "git failed while copying a catalog source",
        ) from exc
    if completed.returncode != 0:
        raise CatalogImportError(
            "catalog.import_git_failed",
            "git failed while copying a catalog source",
        )
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CatalogImportError(
            "catalog.import_git_failed",
            "git reported non-UTF-8 output",
        ) from exc


def _clone_git_source(source_spec: str) -> tuple[Path, str]:
    raw = source_spec.strip()
    locator = raw[4:].strip() if raw.startswith("git:") else raw
    if not locator:
        raise CatalogImportError(
            "catalog.import_git_invalid",
            "git catalog source requires a repository URL and optional #revision",
        )
    url, revision = _split_git_locator(locator)
    if url.startswith("-"):
        raise CatalogImportError(
            "catalog.import_git_invalid",
            "git catalog source URL is not a safe Git locator",
        )
    _reject_git_credentials(url)
    if not urlsplit(url).scheme and not url.startswith("git@"):
        local = Path(url).expanduser()
        if local.exists():
            url = local.resolve().as_uri()
    clone_parent = Path(tempfile.mkdtemp(prefix="litai-c-"))
    clone_root = clone_parent / "src"
    try:
        _run_git(
            (
                "git",
                *_GIT_FILE_PROTOCOL,
                "clone",
                "--quiet",
                "--recurse-submodules",
                "--",
                url,
                str(clone_root),
            ),
            cwd=clone_parent,
        )
        if revision != "HEAD":
            _run_git(
                (
                    "git",
                    *_GIT_FILE_PROTOCOL,
                    "checkout",
                    "--quiet",
                    "--detach",
                    revision,
                ),
                cwd=clone_root,
            )
        commit = _run_git(
            ("git", *_GIT_FILE_PROTOCOL, "rev-parse", "HEAD"),
            cwd=clone_root,
        ).strip()
        if not (clone_root / PROJECT_FILENAME).exists():
            raise CatalogImportError(
                "catalog.import_source_invalid",
                f"git catalog source is not a literate-ai project "
                f"(no {PROJECT_FILENAME})",
            )
        return clone_root, f"git:{sanitize_git_url(url)}@{commit}"
    except Exception:
        shutil.rmtree(clone_parent, ignore_errors=True)
        raise


def _resolve_local_source(source_spec: str) -> tuple[Path, str]:
    path_str = source_spec.removeprefix("local:").strip()
    path = Path(path_str).expanduser().resolve()
    if not (path / PROJECT_FILENAME).exists():
        raise CatalogImportError(
            "catalog.import_source_invalid",
            f"source path is not a literate-ai project (no {PROJECT_FILENAME}): {path}",
        )
    return path, f"local:{path}"


@contextmanager
def _open_source(source_spec: str) -> Iterator[tuple[Path, str]]:
    """Resolve a local path or git URL into a project tree and provenance ref."""

    if _is_git_source_spec(source_spec):
        clone_root, ref = _clone_git_source(source_spec)
        clone_parent = clone_root.parent
        try:
            yield clone_root, ref
        finally:
            shutil.rmtree(clone_parent, ignore_errors=True)
        return
    yield _resolve_local_source(source_spec)


def _read_source_project(source_root: Path) -> tuple[str, str]:
    try:
        snapshot = ProjectConfigurationStore(source_root).read()
    except ProjectError as exc:
        raise CatalogImportError(
            "catalog.import_source_invalid",
            f"source project manifest is invalid: {source_root}",
        ) from exc
    return snapshot.definition.project_id, snapshot.content_identity.uri


def _read_source_ancestors(source_root: Path) -> tuple[TransitiveAncestor, ...]:
    """
    Read the source project's own initialization-origin and imports.json to
    build its ancestor chain. The result is what we embed as transitive_ancestors
    in our imports.json.
    """
    ancestors: list[TransitiveAncestor] = []

    # Root edge: initialization-origin.json → literate-ai
    origin_path = source_root / ".literate" / "initialization-origin.json"
    if origin_path.exists():
        origin = json.loads(origin_path.read_text(encoding="utf-8"))
        repo_url = origin.get("repository_url", "")
        git_rev = origin.get("git_revision", "")
        dist_name = origin.get("distribution_name", "literate-ai")
        ref = (
            f"git:{repo_url}@{git_rev}" if repo_url and git_rev else f"dist:{dist_name}"
        )
        # The project identity for literate-ai is derived from the origin record itself
        import hashlib

        origin_identity = (
            "sha256:" + hashlib.sha256(origin_path.read_bytes()).hexdigest()
        )
        ancestors.append(
            TransitiveAncestor(
                project_id=dist_name,
                project_identity=origin_identity,
                ref=ref,
            )
        )

    # Additional edges: source's own imports.json
    imports = CatalogImportsFile.load(source_root)
    for imp in imports.imports:
        # Add the direct source and all its transitive ancestors
        src = imp.source
        # Direct source of the source's imports
        already = {a.project_id for a in ancestors}
        if src.project_id not in already:
            ancestors.append(
                TransitiveAncestor(
                    project_id=src.project_id,
                    project_identity=src.project_identity,
                    ref=src.ref,
                )
            )
        for ta in src.transitive_ancestors:
            if ta.project_id not in {a.project_id for a in ancestors}:
                ancestors.append(ta)

    return tuple(ancestors)


# ── DAG cycle detection ───────────────────────────────────────────────────────


def check_dag_safe(
    dest_root: Path,
    source_project_id: str,
    source_ancestors: tuple[TransitiveAncestor, ...],
) -> None:
    """
    Raise CatalogImportError if adding this source would create a cycle.

    Cycle conditions:
    1. dest_project_id appears in source's ancestor chain
    2. source_project_id appears in dest's ancestor chain
    """
    # Read dest's own project_id
    try:
        dest_project_id = (
            ProjectConfigurationStore(dest_root).read().definition.project_id
        )
    except ProjectError as exc:
        raise CatalogImportError(
            "catalog.import_destination_invalid", "destination project is invalid"
        ) from exc

    # Compute dest's ancestor project IDs
    dest_imports = CatalogImportsFile.load(dest_root)
    dest_ancestor_ids = dest_imports.all_source_project_ids()

    # Also add the literate-ai root from dest's initialization-origin
    dest_origin = dest_root / ".literate" / "initialization-origin.json"
    if dest_origin.exists():
        origin = json.loads(dest_origin.read_text(encoding="utf-8"))
        dist_name = origin.get("distribution_name", "literate-ai")
        dest_ancestor_ids = dest_ancestor_ids | {dist_name}

    source_ancestor_ids = {a.project_id for a in source_ancestors}

    # Check: would dest appear as ancestor of source?
    # This detects cycles: if dest is already in source's ancestry, adding source → dest
    # would create a loop.  The reverse check (source already in dest's ancestry) is NOT
    # a cycle — it means this source was imported previously and is being updated.
    if dest_project_id and dest_project_id in source_ancestor_ids:
        raise CatalogImportError(
            "catalog.import_cycle",
            f"importing from '{source_project_id}' would create a cycle: "
            f"'{dest_project_id}' is already an ancestor of the source project",
        )


# ── Item selection and copy ────────────────────────────────────────────────────


def _parse_item_spec(spec: str) -> tuple[str, str]:
    """Parse a ``kind:name`` catalog selector."""
    if ":" not in spec:
        raise CatalogImportError(
            "catalog.import_item_invalid",
            f"item spec must be 'kind:name', e.g. 'flavor:openscad'; got: {spec!r}",
        )
    kind, _, name = spec.partition(":")
    kind = kind.strip().lower()
    name = name.strip()
    if kind not in ("flavor", "skill", "component", "mcp", "workflow", "routing"):
        raise CatalogImportError(
            "catalog.import_item_invalid",
            "item kind must be 'flavor', 'skill', 'component', 'mcp', "
            "'workflow', or 'routing'; "
            f"got: {kind!r}",
        )
    if not name:
        raise CatalogImportError(
            "catalog.import_item_invalid",
            f"item name must not be empty in spec: {spec!r}",
        )
    return kind, name


# Map kind → source subdirectory root
_KIND_DIR: dict[str, str] = {
    "flavor": "flavors",
    # Skill names are relative to skills/, including their catalog subdirectory.
    "skill": "skills",
    "component": "components",
    "mcp": "mcps",
    "workflow": "workflows",
    "routing": "routing",
}


def _source_item_dir(source_root: Path, kind: str, name: str) -> Path:
    base = source_root / _KIND_DIR[kind]
    # name may contain slashes (skill subdirs)
    item_dir = base
    for part in Path(name).parts:
        item_dir = item_dir / part
    if not item_dir.exists():
        raise CatalogImportError(
            "catalog.import_item_not_found",
            f"item {kind}:{name!r} not found in source project at {item_dir}",
        )
    return item_dir


def _dest_item_dir(dest_root: Path, kind: str, name: str) -> Path:
    base = dest_root / _KIND_DIR[kind]
    item_dir = base
    for part in Path(name).parts:
        item_dir = item_dir / part
    return item_dir


def _copy_item(
    source_root: Path,
    dest_root: Path,
    kind: str,
    name: str,
    overwrite: bool,
) -> list[CatalogImportFile]:
    """Copy item directory tree. Returns list of copied CatalogImportFile records."""
    import hashlib

    src_dir = _source_item_dir(source_root, kind, name)
    dst_dir = _dest_item_dir(dest_root, kind, name)

    if dst_dir.exists() and not overwrite:
        raise CatalogImportError(
            "catalog.import_conflict",
            f"destination already has {kind}:{name!r}; use --overwrite to replace it",
        )

    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)

    # Build file records with content identities
    files: list[CatalogImportFile] = []
    for file_path in sorted(dst_dir.rglob("*")):
        if file_path.is_file():
            content = file_path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            rel = str(file_path.relative_to(dest_root))
            files.append(CatalogImportFile(path=rel, identity=f"sha256:{digest}"))
    return files


# ── Public copy function ───────────────────────────────────────────────────────


def catalog_copy(
    dest_root: Path,
    source_spec: str,
    item_specs: list[str],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Copy catalog items from source_spec into dest_root, recording provenance.
    Returns a result dict suitable for CLI output.
    """
    with _open_source(source_spec) as (source_root, ref):
        source_project_id, source_project_identity = _read_source_project(source_root)
        source_ancestors = _read_source_ancestors(source_root)

        # DAG cycle check
        check_dag_safe(dest_root, source_project_id, source_ancestors)

        # Parse and validate all item specs before touching anything
        items = [_parse_item_spec(s) for s in item_specs]
        # Verify all source items exist before any writes
        for kind, name in items:
            _source_item_dir(source_root, kind, name)  # raises if missing

        # Perform copies
        now = datetime.now(tz=UTC).isoformat()
        imports_file = CatalogImportsFile.load(dest_root)
        copied: list[dict[str, Any]] = []

        for kind, name in items:
            files = _copy_item(source_root, dest_root, kind, name, overwrite)
            new_import = CatalogImport(
                kind=kind,
                name=name,
                source=CatalogImportSource(
                    project_id=source_project_id,
                    project_identity=source_project_identity,
                    ref=ref,
                    transitive_ancestors=source_ancestors,
                ),
                files=tuple(files),
                copied_at=now,
            )
            imports_file = imports_file.with_import(new_import)
            copied.append({"kind": kind, "name": name, "files": len(files)})

        imports_file.save(dest_root)
        return {
            "source": source_project_id,
            "source_ref": ref,
            "copied": copied,
            "imports_path": CatalogImportsFile.PATH,
        }


# ── DAG graph rendering ────────────────────────────────────────────────────────


def catalog_graph(project_root: Path) -> dict[str, Any]:
    """Build the provenance DAG for this project and return it as a structured dict."""
    try:
        project_id = (
            ProjectConfigurationStore(project_root).read().definition.project_id
        )
    except ProjectError as exc:
        raise CatalogImportError(
            "catalog.import_destination_invalid", "project manifest is invalid"
        ) from exc

    # Root edge
    root_node: dict[str, Any] | None = None
    origin_path = project_root / ".literate" / "initialization-origin.json"
    if origin_path.exists():
        origin = json.loads(origin_path.read_text(encoding="utf-8"))
        root_node = {
            "project_id": origin.get("distribution_name", "literate-ai"),
            "ref": "git:{}@{}".format(
                origin.get("repository_url", ""),
                origin.get("git_revision", ""),
            ),
            "role": "framework-root",
        }

    imports = CatalogImportsFile.load(project_root)
    edges: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}

    if root_node:
        nodes[root_node["project_id"]] = root_node
        # The initialization-origin IS the root edge of the DAG — always draw it.
        edges.append(
            {
                "from": root_node["project_id"],
                "to": project_id,
                "items": ["init:framework-root"],
                "copied_at": "",
            }
        )

    for imp in imports.imports:
        src = imp.source
        # Ensure source node exists
        if src.project_id not in nodes:
            nodes[src.project_id] = {"project_id": src.project_id, "ref": src.ref}
        # Ensure all transitive ancestors exist
        for ta in src.transitive_ancestors:
            if ta.project_id not in nodes:
                nodes[ta.project_id] = {"project_id": ta.project_id, "ref": ta.ref}
        # Edge: source → dest (for this item)
        edges.append(
            {
                "from": src.project_id,
                "to": project_id,
                "items": [f"{imp.kind}:{imp.name}"],
                "copied_at": imp.copied_at,
            }
        )

    # Merge edges with same from/to
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge["from"], edge["to"])
        if key not in merged:
            merged[key] = {
                "from": edge["from"],
                "to": edge["to"],
                "items": [],
                "copied_at": edge["copied_at"],
            }
        merged[key]["items"].extend(edge["items"])

    return {
        "project_id": project_id,
        "nodes": list(nodes.values()),
        "edges": list(merged.values()),
        "mermaid": _render_mermaid(
            project_id, list(nodes.values()), list(merged.values())
        ),
    }


def _render_mermaid(
    project_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> str:
    lines = ["flowchart LR"]
    seen_nodes: set[str] = set()

    def safe_id(s: str) -> str:
        return s.replace("-", "_").replace(".", "_").replace("/", "_")

    all_ids = {n["project_id"] for n in nodes} | {project_id}
    for nid in all_ids:
        if nid not in seen_nodes:
            label = nid
            if nid == project_id:
                label = f"**{nid}**"
            lines.append(f'    {safe_id(nid)}["{label}"]')
            seen_nodes.add(nid)

    for edge in edges:
        items_label = ", ".join(edge["items"][:3])
        if len(edge["items"]) > 3:
            items_label += f" +{len(edge['items']) - 3} more"
        lines.append(
            f'    {safe_id(edge["from"])} -->|"{items_label}"| {safe_id(edge["to"])}'
        )

    return "\n".join(lines)


__all__ = [
    "CatalogImportError",
    "catalog_copy",
    "catalog_graph",
    "check_dag_safe",
]

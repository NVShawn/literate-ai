"""Filesystem-backed, read-only update planning for initialized projects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.adapters.project_initialization import (
    _STARTER_DOCS,
    _STARTER_TEMPLATE_FILES,
    _TEMPLATE_FILES,
    DEFAULT_AUTHORITY_REVIEW_DOCUMENT,
    INITIALIZATION_BASELINE_FILE,
    INITIALIZATION_ORIGIN_FILE,
    LEGACY_LIFT_SHIFT_ADR,
    ProjectInitializationError,
    _template_text,
    discover_installed_initialization_origin,
    render_starter_document,
)
from literate_ai.contracts import (
    CatalogImportsFile,
    ContentIdentity,
    HashAlgorithm,
    ProjectInitializationBaseline,
    ProjectInitializationOrigin,
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ProjectUpdatePlan,
    canonical_json_bytes,
)
from literate_ai.documentation import (
    _ACTIVE_WORK_PATH,
    DocumentationError,
    _roadmap_area,
    _roadmap_header_fields,
    _roadmap_owner_fragment,
    markdown_anchors,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    ProjectError,
    discover_project,
)
from literate_ai.repository_urls import repository_urls_equivalent

_MAXIMUM_UPDATE_INPUT_BYTES = 16 * 1024 * 1024


class ProjectUpdateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


def _project_path(root: Path, relative: str) -> Path:
    path = root
    for part in Path(relative).parts:
        path /= part
        if path_is_link_or_reparse(path):
            raise ProjectUpdateError(
                "project.update_path_unsafe",
                "initialized framework path crosses a link or reparse point: "
                f"{relative}",
            )
    return path


def _read_contract(root: Path, relative: str, contract_type):
    path = _project_path(root, relative)
    if not path.is_file():
        raise ProjectUpdateError(
            "project.update_evidence_unavailable",
            f"initialized-project evidence is unavailable: {relative}",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProjectUpdateError(
            "project.update_evidence_unavailable",
            f"initialized-project evidence is unreadable: {relative}",
        ) from exc
    if len(content) > _MAXIMUM_UPDATE_INPUT_BYTES:
        raise ProjectUpdateError(
            "project.update_evidence_oversized",
            f"initialized-project evidence exceeds the size limit: {relative}",
        )
    try:
        value = json.loads(content)
        contract = contract_type.from_dict(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectUpdateError(
            "project.update_evidence_invalid",
            f"initialized-project evidence is invalid: {relative}",
        ) from exc
    if content != canonical_json_bytes(contract.to_dict()) + b"\n":
        raise ProjectUpdateError(
            "project.update_evidence_noncanonical",
            f"initialized-project evidence is not canonical: {relative}",
        )
    return contract


def _local_bytes(root: Path, relative: str) -> bytes | None:
    path = _project_path(root, relative)
    if not path.exists():
        return None
    if not path.is_file():
        raise ProjectUpdateError(
            "project.update_path_unsafe",
            f"initialized framework path is not a regular file: {relative}",
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProjectUpdateError(
            "project.update_file_unreadable",
            f"initialized framework file is unreadable: {relative}",
        ) from exc
    if len(content) > _MAXIMUM_UPDATE_INPUT_BYTES:
        raise ProjectUpdateError(
            "project.update_file_oversized",
            f"initialized framework file exceeds the size limit: {relative}",
        )
    return content


def _local_identity(root: Path, relative: str) -> ContentIdentity | None:
    content = _local_bytes(root, relative)
    return None if content is None else _identity(content)


def _catalog_import_paths(root: Path) -> frozenset[str]:
    """Return destinations recorded as catalog-inherited, not template-scaffolded."""

    imports_path = root / CatalogImportsFile.PATH
    if not imports_path.is_file():
        return frozenset()
    try:
        imports = CatalogImportsFile.load(root)
    except (OSError, TypeError, ValueError):
        return frozenset()
    return frozenset(
        file.path for imported in imports.imports for file in imported.files
    )


_PROJECT_GLOBAL_INPUT_KINDS = frozenset(
    {
        "model-selection",
        "routing-policy",
        "specification-to-source-skill",
        "toolchain-constraint",
        "workflow",
    }
)


def _project_owned_generation_input_paths(
    project: LoadedProject, imported_paths: frozenset[str]
) -> frozenset[str]:
    """Keep local Component closure out of framework-template retirement.

    Older initialization baselines cannot distinguish a copied framework catalog
    member from one retained as project authority. Current catalog-import provenance
    identifies inherited Components; every other Component is project-owned, so its
    project-global workflow, routing, and skill references must survive an update.
    """

    root = project.root
    catalog_roots = tuple(
        {
            *project.definition.skill_roots,
            *project.definition.workflow_roots,
            *project.definition.routing_roots,
        }
    )
    protected: set[str] = set()
    for component_root in project.roots("component"):
        for manifest in sorted(component_root.rglob("component.md")):
            relative = manifest.relative_to(root).as_posix()
            if relative in imported_paths:
                continue
            if manifest.is_symlink() or not manifest.is_file():
                raise ProjectUpdateError(
                    "project.update_local_component_invalid",
                    f"project-owned Component is not a regular file: {relative}",
                )
            try:
                content = _local_bytes(root, relative)
                assert content is not None
                authoring = parse_component_markdown(
                    manifest,
                    content.decode("utf-8"),
                    project_root=root,
                )
            except (ComponentMarkdownError, UnicodeError) as exc:
                raise ProjectUpdateError(
                    "project.update_local_component_invalid",
                    f"project-owned Component is invalid: {relative}",
                ) from exc
            selectors = (
                authoring.workflow_definition,
                authoring.routing_policy,
                *(
                    item
                    for item in authoring.authoring_inputs
                    if item.kind in _PROJECT_GLOBAL_INPUT_KINDS
                ),
            )
            for selector in selectors:
                candidate = Path(selector.uri).as_posix()
                if candidate in imported_paths:
                    continue
                if any(
                    candidate == catalog_root
                    or candidate.startswith(f"{catalog_root}/")
                    for catalog_root in catalog_roots
                ):
                    protected.add(candidate)
    return frozenset(protected)


_LEGACY_SHIM_AUTHORITY_PATHS = frozenset(
    {
        "components/legacy-project-wrapper/component.md",
        "flavors/legacy-project-shim/flavor.md",
        "flavors/legacy-project-shim/openspec/spec.md",
    }
)
_LEGACY_SHIM_DEPENDENT_PATHS = (
    "components/legacy-project-wrapper/implementation",
    "workflows/legacy-adoption/workflow.md",
    "routing/legacy-adoption.json",
    "skills/specification-to-source/legacy-project-shim/SKILL.md",
)
_LEGACY_HARNESS_INVENTORY_PATH = ".literate/harness-inventory.json"


def _retained_legacy_shim_authority_paths(root: Path) -> frozenset[str]:
    """Keep conversion-shim authority while retained legacy state still cites it.

    `litai init --convert` (`legacy_shim_authority()` in ``harness_inventory.py``)
    generates the wrapper Component, its Flavor, and that Flavor's specification
    from harness evidence rather than from a static packaged template, so no
    ``_TEMPLATE_FILES``/``_STARTER_TEMPLATE_FILES`` entry ever reproduces them for a
    later framework-template comparison (#340). `_project_owned_generation_input_paths`
    above already protects the workflow, routing, and skill paths a project-owned
    Component's own selectors point *at*; it cannot see the reverse relationship,
    where those retained files -- along with the retained implementation and
    harness evidence -- still cite the wrapper Component's and Flavor's own
    authority by path. Keep those three files out of mechanical template
    retirement for as long as every dependent that still needs them is present.
    A Phase 2 native rewrite that removes the dependents is the only thing that
    legitimately retires them.
    """

    inventory_path = root.joinpath(*Path(_LEGACY_HARNESS_INVENTORY_PATH).parts)
    if inventory_path.is_symlink() or not inventory_path.is_file():
        return frozenset()
    for relative in _LEGACY_SHIM_DEPENDENT_PATHS:
        candidate = root.joinpath(*Path(relative).parts)
        if candidate.is_symlink() or not candidate.exists():
            return frozenset()
    return frozenset(
        path
        for path in _LEGACY_SHIM_AUTHORITY_PATHS
        if (candidate := root.joinpath(*Path(path).parts)).is_file()
        and not candidate.is_symlink()
    )


def _upstream_template(baseline_paths: set[str]) -> dict[str, bytes]:
    converted = LEGACY_LIFT_SHIFT_ADR in baseline_paths
    content = {
        destination: _template_text(resource).encode("utf-8")
        for destination, resource in _TEMPLATE_FILES.items()
    }
    if set(_STARTER_TEMPLATE_FILES) & baseline_paths:
        content.update(
            {
                destination: _template_text(resource).encode("utf-8")
                for destination, resource in _STARTER_TEMPLATE_FILES.items()
            }
        )
    content.update(
        {
            destination: render_starter_document(
                destination, converted=converted
            ).encode("utf-8")
            for destination in _STARTER_DOCS
            if destination != DEFAULT_AUTHORITY_REVIEW_DOCUMENT
        }
    )
    return content


def _classification(
    baseline: ContentIdentity | None,
    local: ContentIdentity | None,
    upstream: ContentIdentity | None,
) -> ProjectUpdateClassification:
    if upstream is None:
        return ProjectUpdateClassification.PRESERVED_DYNAMIC
    if baseline is None:
        return (
            ProjectUpdateClassification.ALREADY_CURRENT
            if local == upstream
            else ProjectUpdateClassification.UPSTREAM_ADDED
            if local is None
            else ProjectUpdateClassification.CONFLICT
        )
    if local == baseline:
        return (
            ProjectUpdateClassification.UNCHANGED
            if upstream == baseline
            else ProjectUpdateClassification.UPSTREAM_ONLY
        )
    if local == upstream:
        return ProjectUpdateClassification.ALREADY_CURRENT
    if upstream == baseline:
        return ProjectUpdateClassification.LOCAL_ONLY
    return ProjectUpdateClassification.CONFLICT


def _framework_classification(
    path: str,
    baseline: ContentIdentity | None,
    local: ContentIdentity | None,
    upstream: ContentIdentity | None,
    *,
    catalog_roots: tuple[str, ...],
) -> ProjectUpdateClassification:
    """Retire untouched framework catalog authority without guessing dynamic files.

    The initialization baseline records identities but not whether a file was copied
    from a static template or rendered from project-specific input.  A path beneath a
    declared catalog root supplies the missing fact: it is authority that the framework
    can retire when the current template no longer contains it.  Files outside those
    taxonomic roots retain the conservative identity-only behavior.
    """

    in_catalog = any(
        path == root or path.startswith(f"{root}/") for root in catalog_roots
    )
    if baseline is not None and upstream is None and in_catalog:
        if local is None:
            return ProjectUpdateClassification.ALREADY_CURRENT
        if local == baseline:
            return ProjectUpdateClassification.UPSTREAM_ONLY
        return ProjectUpdateClassification.LOCAL_ONLY
    return _classification(baseline, local, upstream)


def _protect_dangling_roadmap_queue_owners(
    root: Path,
    files: tuple[ProjectUpdateFile, ...],
    upstream_content: dict[str, bytes],
) -> tuple[ProjectUpdateFile, ...]:
    """Refuse a blind queue replacement that would strand a preserved roadmap owner.

    ``docs/roadmap/active-work.md`` is the sole resumable queue; detailed roadmap
    documents record an ``Owning queue item`` link into one of its headings. A
    mechanical upstream-only replacement of the queue file is only safe when no
    preserved dynamic roadmap document still depends on a heading the upstream
    queue does not carry. When one does, classify the queue as a conflict instead
    of applying it, so the operator resolves the collision explicitly.
    """

    active_work_index = next(
        (index for index, item in enumerate(files) if item.path == _ACTIVE_WORK_PATH),
        None,
    )
    if active_work_index is None:
        return files
    active_work_item = files[active_work_index]
    if active_work_item.classification is not ProjectUpdateClassification.UPSTREAM_ONLY:
        return files
    upstream_active_work = upstream_content.get(_ACTIVE_WORK_PATH)
    if upstream_active_work is None:
        return files
    try:
        upstream_anchors = markdown_anchors(upstream_active_work.decode("utf-8"))
    except UnicodeError:
        return files

    for item in files:
        if item.path == _ACTIVE_WORK_PATH:
            continue
        if item.classification is not ProjectUpdateClassification.PRESERVED_DYNAMIC:
            continue
        if _roadmap_area(item.path) is None:
            continue
        content = _local_bytes(root, item.path)
        if content is None:
            continue
        try:
            text = content.decode("utf-8")
            fields = _roadmap_header_fields(item.path, text)
            _roadmap_owner_fragment(
                path=item.path,
                value=fields["Owning queue item"],
                active_work_anchors=upstream_anchors,
            )
        except (UnicodeError, DocumentationError):
            # Either the owner link is already invalid (a pre-existing defect the
            # documentation validator will surface) or it dangles specifically
            # against the upstream replacement -- both mean a mechanical
            # upstream-only apply of the queue is unsafe here.
            replaced = replace(
                active_work_item, classification=ProjectUpdateClassification.CONFLICT
            )
            return (
                files[:active_work_index] + (replaced,) + files[active_work_index + 1 :]
            )
    return files


class FilesystemProjectUpdateAdapter:
    """Classify baseline/local/current-template state without mutating the project."""

    def __init__(
        self,
        *,
        origin_provider=discover_installed_initialization_origin,
        protected_paths: frozenset[str] = frozenset(),
    ):
        self._origin_provider = origin_provider
        self._protected_paths = frozenset(protected_paths)

    def plan(self, selected: Path) -> ProjectUpdatePlan:
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise ProjectUpdateError(exc.code, exc.message) from exc
        if project is None:
            raise ProjectUpdateError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        root = project.root
        previous = _read_contract(
            root, INITIALIZATION_ORIGIN_FILE, ProjectInitializationOrigin
        )
        baseline = _read_contract(
            root, INITIALIZATION_BASELINE_FILE, ProjectInitializationBaseline
        )
        if baseline.origin_identity != previous.identity:
            raise ProjectUpdateError(
                "project.update_origin_mismatch",
                "initialization baseline does not bind the recorded origin",
            )
        try:
            upstream = self._origin_provider()
        except ProjectInitializationError as exc:
            raise ProjectUpdateError(exc.code, exc.message) from exc
        if not isinstance(upstream, ProjectInitializationOrigin):
            raise ProjectUpdateError(
                "project.update_origin_invalid",
                "installed update origin provider returned an invalid contract",
            )
        if (
            not repository_urls_equivalent(
                upstream.repository_url, previous.repository_url
            )
            or upstream.distribution_name != previous.distribution_name
        ):
            raise ProjectUpdateError(
                "project.update_origin_changed",
                "installed framework origin differs from the initializing origin",
            )
        baseline_by_path = {item.path: item.identity for item in baseline.files}
        upstream_content = _upstream_template(set(baseline_by_path))
        imported = _catalog_import_paths(root)
        inherited = (
            imported
            | _project_owned_generation_input_paths(project, imported)
            | _retained_legacy_shim_authority_paths(root)
            | self._protected_paths
        )
        # Catalog-imported paths are classified against the resolved parent catalog
        # by the lineage adapter. Comparing them here against the installed
        # init-template snapshot reports false conflicts (or would mechanically
        # overwrite current catalog bytes with a stale template).
        paths = sorted((set(baseline_by_path) | set(upstream_content)) - inherited)
        files = tuple(
            ProjectUpdateFile(
                relative,
                _framework_classification(
                    relative,
                    baseline_by_path.get(relative),
                    local := _local_identity(root, relative),
                    current := (
                        _identity(upstream_content[relative])
                        if relative in upstream_content
                        else None
                    ),
                    catalog_roots=tuple(
                        {
                            *project.definition.component_roots,
                            *project.definition.flavor_roots,
                            *project.definition.skill_roots,
                            *project.definition.workflow_roots,
                            *project.definition.routing_roots,
                            *project.definition.mcp_roots,
                        }
                    ),
                ),
                baseline_by_path.get(relative),
                local,
                current,
            )
            for relative in paths
        )
        files = _protect_dangling_roadmap_queue_owners(root, files, upstream_content)
        return ProjectUpdatePlan(
            project.definition.identity,
            baseline.identity,
            previous,
            upstream,
            files,
        )


__all__ = ["FilesystemProjectUpdateAdapter", "ProjectUpdateError"]

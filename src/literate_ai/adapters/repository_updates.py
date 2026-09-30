"""Plan repository-lineage and inherited-catalog updates without mutation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai.adapters.project_updates import _classification, _local_identity
from literate_ai.application.repository_lineage import (
    RepositoryLineageResolutionError,
    resolve_repository_lineage,
)
from literate_ai.contracts import (
    AppliedRepositoryLineageUpdate,
    CatalogImportsFile,
    CatalogImportSource,
    ContentIdentity,
    HashAlgorithm,
    ProjectUpdateClassification,
    ProjectUpdateFile,
    RepositoryLineage,
    RepositoryLineageUpdatePlan,
    RepositoryParentSelection,
    RepositoryReparentPlan,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

from .project_update_apply import _write_exact
from .project_updates import _project_path
from .project_validation import ProjectValidationError, validate_project
from .repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogPlan,
    RepositoryCatalogError,
    catalog_imports_for_plan,
    plan_inherited_catalogs,
)
from .repository_lineage import (
    FilesystemRepositoryLineageStore,
    GitRepositoryLineageError,
    GitRepositorySnapshotProvider,
    RepositoryLineageStoreError,
)

RepositoryLineageResolver = Callable[[RepositoryParentSelection], RepositoryLineage]
RepositoryCatalogPlanner = Callable[[RepositoryLineage], InheritedCatalogPlan]


class RepositoryUpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PlannedRepositoryLineageUpdate:
    contract: RepositoryLineageUpdatePlan
    catalogs: InheritedCatalogPlan


def _provider() -> GitRepositorySnapshotProvider:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    return GitRepositorySnapshotProvider(object_root / "repository-lineage")


def _default_lineage_resolver(
    selection: RepositoryParentSelection,
) -> RepositoryLineage:
    return resolve_repository_lineage(selection, _provider())


def _default_catalog_planner(lineage: RepositoryLineage) -> InheritedCatalogPlan:
    return plan_inherited_catalogs(lineage, _provider())


def _source_belongs_to_lineage(
    source: CatalogImportSource, lineage: RepositoryLineage
) -> bool:
    """Match inherited provenance across an exact same-repository reparent.

    A standalone reparent advances the locked revision before the subsequent catalog
    update.  Exact old refs therefore no longer occur in the current lineage.  The
    canonical repository URL and project ID remain stable origin authority across that
    revision change; both must match so an unrelated local catalog import stays local.
    """

    if not source.ref.startswith("git:"):
        return False
    repository_url, separator, revision = source.ref[4:].rpartition("@")
    if not separator or not repository_url or not revision:
        return False
    return any(
        node.repository_url == repository_url and node.project_id == source.project_id
        for node in lineage.nodes
    )


def _previous_inherited_files(
    root: Path,
    lineage: RepositoryLineage,
) -> dict[str, ContentIdentity]:
    imports_path = root / CatalogImportsFile.PATH
    if not imports_path.is_file():
        raise RepositoryUpdateError(
            "repository_update.provenance_missing",
            "inherited catalog provenance is missing; update cannot classify local "
            "authority safely",
        )
    try:
        imports = CatalogImportsFile.load(root)
    except (OSError, TypeError, ValueError) as exc:
        raise RepositoryUpdateError(
            "repository_update.provenance_invalid",
            "inherited catalog provenance is invalid",
        ) from exc
    result: dict[str, ContentIdentity] = {}
    for imported in imports.imports:
        if not _source_belongs_to_lineage(imported.source, lineage):
            continue
        for file in imported.files:
            try:
                identity = ContentIdentity.parse_uri(file.identity)
            except ValueError as exc:
                raise RepositoryUpdateError(
                    "repository_update.provenance_invalid",
                    f"inherited catalog identity is invalid: {file.path}",
                ) from exc
            previous = result.get(file.path)
            if previous is not None and previous != identity:
                raise RepositoryUpdateError(
                    "repository_update.provenance_ambiguous",
                    f"inherited catalog provenance repeats a path: {file.path}",
                )
            result[file.path] = identity
    return result


def _prospective_files(plan: InheritedCatalogPlan) -> dict[str, ContentIdentity]:
    result: dict[str, ContentIdentity] = {}
    for item in plan.items:
        for file in item.files:
            previous = result.get(file.destination)
            if previous is not None and previous != file.identity:
                raise RepositoryUpdateError(
                    "repository_update.catalog_ambiguous",
                    f"prospective inherited catalogs repeat a path: {file.destination}",
                )
            result[file.destination] = file.identity
    return result


def _prospective_content(
    plan: InheritedCatalogPlan,
) -> dict[str, InheritedCatalogFile]:
    result: dict[str, InheritedCatalogFile] = {}
    for item in plan.items:
        for file in item.files:
            previous = result.get(file.destination)
            if previous is not None and previous != file:
                raise RepositoryUpdateError(
                    "repository_update.catalog_ambiguous",
                    f"prospective inherited catalogs repeat a path: {file.destination}",
                )
            result[file.destination] = file
    return result


def _catalog_classification(
    baseline: ContentIdentity | None,
    local: ContentIdentity | None,
    upstream: ContentIdentity | None,
) -> ProjectUpdateClassification:
    """Classify catalog removal from exact import provenance.

    Initialized framework templates have an identity-only baseline and therefore keep
    treating an absent upstream file as dynamic. Catalog imports are stronger: each
    prior file has exact source provenance. An unchanged imported file can consequently
    be removed safely when its parent no longer exports it, while a divergent file has
    become local authority and must remain.
    """

    if baseline is not None and upstream is None:
        if local is None:
            return ProjectUpdateClassification.ALREADY_CURRENT
        if local == baseline:
            return ProjectUpdateClassification.UPSTREAM_ONLY
        return ProjectUpdateClassification.LOCAL_ONLY
    return _classification(baseline, local, upstream)


def _retired_local_authority_paths(
    root: Path,
    previous_lineage: RepositoryLineage,
    prospective: InheritedCatalogPlan,
    baseline: dict[str, ContentIdentity],
    local: dict[str, ContentIdentity | None],
) -> frozenset[str]:
    """Keep a retired imported item whole when any surviving file diverged."""

    prospective_keys = {(item.kind, item.name) for item in prospective.items}
    current = CatalogImportsFile.load(root)
    preserved: set[str] = set()
    for imported in current.imports:
        if (
            not _source_belongs_to_lineage(imported.source, previous_lineage)
            or (imported.kind, imported.name) in prospective_keys
        ):
            continue
        paths = tuple(file.path for file in imported.files)
        diverged = any(
            local.get(path) is not None and local.get(path) != baseline.get(path)
            for path in paths
        )
        if diverged:
            preserved.update(path for path in paths if local.get(path) is not None)
    return frozenset(preserved)


def _updated_imports(
    root: Path,
    planned: PlannedRepositoryLineageUpdate,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> CatalogImportsFile:
    current = CatalogImportsFile.load(root)
    planned_imports = catalog_imports_for_plan(planned.catalogs)
    kept = tuple(
        imported
        for imported in current.imports
        if not _source_belongs_to_lineage(
            imported.source, planned.contract.previous_lineage
        )
    )
    inherited = tuple(
        replace(
            imported,
            files=tuple(
                file for file in imported.files if file.path not in excluded_paths
            ),
        )
        for imported in planned_imports.imports
        if any(file.path not in excluded_paths for file in imported.files)
    )
    previous_by_key = {(item.kind, item.name): item for item in current.imports}
    refreshed = []
    for imported in inherited:
        previous = previous_by_key.get((imported.kind, imported.name))
        if (
            previous is not None
            and replace(imported, copied_at=previous.copied_at) == previous
        ):
            # The timestamp records established provenance, not another observation
            # of the same exact source and file closure during an unchanged update.
            imported = previous
        refreshed.append(imported)
    kept_keys = {(item.kind, item.name) for item in kept}
    # A locally tracked import (`kept`, not sourced from the lineage being
    # updated -- e.g. a project that deliberately re-forked an inherited
    # catalog item under its own provenance) can share a (kind, name) with
    # what the fresh inherited computation would otherwise register for the
    # same item. That single identity collision must not veto every other,
    # unrelated safe file this update would otherwise apply (#133): drop only
    # the colliding inherited entries and keep the project's existing local
    # provenance record for that key untouched. Every file belonging to a
    # dropped entry is already excluded from `apply()`'s write set by its own
    # per-file classification (a locally diverged import's files classify as
    # `conflict`/`local_only`, never `upstream_only`), so nothing here writes
    # content the plan didn't already mark safe.
    non_colliding_inherited = tuple(
        item for item in refreshed if (item.kind, item.name) not in kept_keys
    )
    return CatalogImportsFile(
        tuple(
            sorted(
                (*kept, *non_colliding_inherited),
                key=lambda item: (item.kind, item.name, item.source.ref),
            )
        ),
        planned_imports.decisions,
    )


class FilesystemRepositoryUpdateAdapter:
    """Re-resolve the recorded chain and classify every inherited catalog file."""

    def __init__(
        self,
        *,
        lineage_resolver: RepositoryLineageResolver = _default_lineage_resolver,
        catalog_planner: RepositoryCatalogPlanner = _default_catalog_planner,
    ) -> None:
        self._lineage_resolver = lineage_resolver
        self._catalog_planner = catalog_planner

    def plan(
        self, selected: Path, *, reparent_plan: RepositoryReparentPlan | None = None
    ) -> PlannedRepositoryLineageUpdate:
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise RepositoryUpdateError(exc.code, exc.message) from exc
        if project is None:
            raise RepositoryUpdateError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        store = FilesystemRepositoryLineageStore(project.root)
        try:
            selection, previous = store.load()
            if reparent_plan is not None:
                if not isinstance(reparent_plan, RepositoryReparentPlan):
                    raise TypeError("prospective update requires a typed reparent plan")
                if (
                    not reparent_plan.previous_evidence_present
                    or reparent_plan.project_identity != project.definition.identity
                    or reparent_plan.previous_selection != selection
                    or reparent_plan.previous_lineage != previous
                ):
                    raise RepositoryUpdateError(
                        "repository_update.changed_after_plan",
                        "project or parent authority changed after follow planning",
                    )
                # Model the explicit reparent stage without writing it. The enclosing
                # follow plan retains the original selection and its comparison guard.
                selection = reparent_plan.prospective_selection
                previous = reparent_plan.prospective_lineage
            prospective = self._lineage_resolver(selection)
            if reparent_plan is not None and prospective != previous:
                raise RepositoryUpdateError(
                    "repository_update.changed_after_plan",
                    "prospective parent changed after follow planning",
                )
            catalogs = self._catalog_planner(prospective)
        except (
            RepositoryCatalogError,
            RepositoryLineageResolutionError,
            RepositoryLineageStoreError,
            GitRepositoryLineageError,
        ) as exc:
            raise RepositoryUpdateError(exc.code, exc.message) from exc
        if (
            not isinstance(prospective, RepositoryLineage)
            or prospective.selection != selection
            or not isinstance(catalogs, InheritedCatalogPlan)
            or catalogs.lineage != prospective
        ):
            raise RepositoryUpdateError(
                "repository_update.resolution_invalid",
                "repository update resolution returned inconsistent evidence",
            )
        baseline = _previous_inherited_files(project.root, previous)
        upstream = _prospective_files(catalogs)
        paths = tuple(sorted(set(baseline) | set(upstream)))
        local = {path: _local_identity(project.root, path) for path in paths}
        retired_local = _retired_local_authority_paths(
            project.root, previous, catalogs, baseline, local
        )
        files = tuple(
            ProjectUpdateFile(
                path,
                (
                    ProjectUpdateClassification.LOCAL_ONLY
                    if path in retired_local
                    else _catalog_classification(
                        baseline.get(path), local[path], upstream.get(path)
                    )
                ),
                baseline.get(path),
                local[path],
                upstream.get(path),
            )
            for path in paths
        )
        return PlannedRepositoryLineageUpdate(
            RepositoryLineageUpdatePlan(
                project.definition.identity,
                previous,
                prospective,
                files,
            ),
            catalogs,
        )

    def apply(
        self,
        selected: Path,
        planned: PlannedRepositoryLineageUpdate,
        *,
        adopt_added: bool = False,
        take_upstream: frozenset[str] = frozenset(),
        keep_local: frozenset[str] = frozenset(),
        finalizer: Callable[[Path], object] | None = None,
    ) -> AppliedRepositoryLineageUpdate:
        """Apply safe inherited deltas and one optional final transaction stage."""

        if not isinstance(planned, PlannedRepositoryLineageUpdate):
            raise TypeError("repository update apply requires a typed plan")
        if not isinstance(take_upstream, frozenset) or any(
            not isinstance(path, str) or not path for path in take_upstream
        ):
            raise TypeError("take-upstream paths must be a frozenset of strings")
        if not isinstance(keep_local, frozenset) or any(
            not isinstance(path, str) or not path for path in keep_local
        ):
            raise TypeError("keep-local paths must be a frozenset of strings")
        current = self.plan(selected)
        if current != planned:
            raise RepositoryUpdateError(
                "repository_update.changed_after_plan",
                "repository lineage, catalogs, or local files changed; plan again",
            )
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise RepositoryUpdateError(exc.code, exc.message) from exc
        if (
            project is None
            or project.definition.identity != planned.contract.project_identity
        ):
            raise RepositoryUpdateError(
                "repository_update.project_changed",
                "project authority changed after repository update planning",
            )
        content = _prospective_content(planned.catalogs)
        conflict_paths = frozenset(
            item.path
            for item in planned.contract.files
            if item.classification is ProjectUpdateClassification.CONFLICT
        )
        invalid_take_upstream = tuple(sorted(take_upstream - conflict_paths))
        if invalid_take_upstream:
            raise RepositoryUpdateError(
                "repository_update.take_upstream_not_conflict",
                "take-upstream paths must be planned inherited-catalog conflicts: "
                + ", ".join(invalid_take_upstream),
            )
        removable_paths = frozenset(
            item.path
            for item in planned.contract.files
            if item.classification is ProjectUpdateClassification.UPSTREAM_ONLY
            and item.upstream_identity is None
        )
        invalid_keep_local = tuple(sorted(keep_local - removable_paths))
        if invalid_keep_local:
            raise RepositoryUpdateError(
                "repository_update.keep_local_not_removal",
                "keep-local paths must be planned inherited-catalog removals: "
                + ", ".join(invalid_keep_local),
            )
        selected_files = tuple(
            item
            for item in planned.contract.files
            if (
                item.classification is ProjectUpdateClassification.UPSTREAM_ONLY
                and item.path not in keep_local
            )
            or (
                adopt_added
                and item.classification is ProjectUpdateClassification.UPSTREAM_ADDED
            )
            or item.path in take_upstream
        )
        snapshots: dict[str, tuple[bytes | None, int | None]] = {}
        for item in selected_files:
            inherited = content.get(item.path)
            removing = item.upstream_identity is None
            if removing:
                if (
                    inherited is not None
                    or item.baseline_identity is None
                    or item.local_identity != item.baseline_identity
                ):
                    raise RepositoryUpdateError(
                        "repository_update.removal_unsafe",
                        f"prospective inherited removal is not provenance-safe for "
                        f"{item.path}",
                    )
            elif inherited is None or inherited.identity != item.upstream_identity:
                raise RepositoryUpdateError(
                    "repository_update.upstream_changed",
                    f"prospective inherited content changed for {item.path}",
                )
            target = _project_path(project.root, item.path)
            observed = target.read_bytes() if target.is_file() else None
            observed_identity = (
                None
                if observed is None
                else ContentIdentity(
                    HashAlgorithm.SHA256,
                    hashlib.sha256(observed).hexdigest(),
                )
            )
            if observed_identity != item.local_identity:
                raise RepositoryUpdateError(
                    "repository_update.local_changed",
                    f"local inherited path changed after planning: {item.path}",
                )
            snapshots[item.path] = (
                observed,
                stat.S_IMODE(target.stat().st_mode) if target.is_file() else None,
            )

        imports_path = _project_path(project.root, CatalogImportsFile.PATH)
        try:
            previous_imports = imports_path.read_bytes()
            imports = _updated_imports(
                project.root,
                planned,
                excluded_paths=frozenset(
                    item.path
                    for item in planned.contract.files
                    if item not in selected_files
                    and item.classification
                    not in (
                        ProjectUpdateClassification.UNCHANGED,
                        ProjectUpdateClassification.ALREADY_CURRENT,
                    )
                ),
            )
            imports_bytes = (
                json.dumps(imports.to_dict(), indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except (OSError, TypeError, ValueError) as exc:
            raise RepositoryUpdateError(
                "repository_update.provenance_invalid",
                "repository update provenance could not be prepared",
            ) from exc
        store = FilesystemRepositoryLineageStore(project.root)
        lineage_replaced = False
        written: list[str] = []
        created_directories: set[Path] = set()
        try:
            for item in selected_files:
                target = _project_path(project.root, item.path)
                if item.upstream_identity is None:
                    target.unlink()
                else:
                    parent = target.parent
                    while parent != project.root and not parent.exists():
                        created_directories.add(parent)
                        parent = parent.parent
                    inherited = content[item.path]
                    _write_exact(target, inherited.content)
                    if os.name != "nt":
                        os.chmod(target, 0o755 if inherited.executable else 0o644)
                written.append(item.path)
            if imports_bytes != previous_imports:
                _write_exact(imports_path, imports_bytes)
            store.replace(
                planned.contract.prospective_lineage.selection,
                planned.contract.prospective_lineage,
                expected_selection_identity=(
                    planned.contract.previous_lineage.selection.identity
                ),
                expected_lineage_identity=planned.contract.previous_lineage.identity,
            )
            lineage_replaced = True
            if finalizer is None:
                validate_project(
                    project.root,
                    require_authority_review=False,
                    synchronize_source_intelligence=True,
                )
            else:
                finalizer(project.root)
        except Exception as exc:
            if lineage_replaced:
                try:
                    store.replace(
                        planned.contract.previous_lineage.selection,
                        planned.contract.previous_lineage,
                        expected_selection_identity=(
                            planned.contract.prospective_lineage.selection.identity
                        ),
                        expected_lineage_identity=(
                            planned.contract.prospective_lineage.identity
                        ),
                    )
                except RepositoryLineageStoreError:
                    pass
            try:
                if imports_bytes != previous_imports:
                    _write_exact(imports_path, previous_imports)
                for path in reversed(written):
                    target = _project_path(project.root, path)
                    previous, mode = snapshots[path]
                    if previous is None:
                        target.unlink(missing_ok=True)
                    else:
                        _write_exact(target, previous)
                        if os.name != "nt" and mode is not None:
                            os.chmod(target, mode)
                for directory in sorted(
                    created_directories,
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            except OSError:
                pass
            if isinstance(exc, RepositoryLineageStoreError):
                raise RepositoryUpdateError(exc.code, exc.message) from exc
            if isinstance(exc, ProjectValidationError):
                raise RepositoryUpdateError(
                    "repository_update.validation_failed",
                    "updated repository catalogs failed validation and were "
                    f"rolled back: {exc.code}: {exc.message}",
                ) from exc
            raise RepositoryUpdateError(
                "repository_update.apply_failed",
                "repository update could not be applied and was rolled back",
            ) from exc

        refused: dict[ProjectUpdateClassification, list[str]] = {}
        for item in planned.contract.files:
            if item in selected_files or item.classification in (
                ProjectUpdateClassification.UNCHANGED,
                ProjectUpdateClassification.ALREADY_CURRENT,
            ):
                continue
            refused.setdefault(item.classification, []).append(item.path)
        provenance_identity = ContentIdentity(
            HashAlgorithm.SHA256,
            hashlib.sha256(imports_bytes).hexdigest(),
        )
        return AppliedRepositoryLineageUpdate(
            planned.contract.identity,
            planned.contract.previous_lineage.identity,
            planned.contract.prospective_lineage.identity,
            provenance_identity,
            tuple(
                sorted(
                    item.path
                    for item in selected_files
                    if item.classification is ProjectUpdateClassification.UPSTREAM_ONLY
                )
            ),
            tuple(
                sorted(
                    item.path
                    for item in selected_files
                    if item.classification is ProjectUpdateClassification.UPSTREAM_ADDED
                )
            ),
            tuple(sorted(take_upstream)),
            tuple(sorted(keep_local)),
            tuple(
                (classification, tuple(sorted(paths)))
                for classification, paths in sorted(
                    refused.items(), key=lambda item: item[0].value
                )
            ),
        )


__all__ = [
    "FilesystemRepositoryUpdateAdapter",
    "PlannedRepositoryLineageUpdate",
    "RepositoryUpdateError",
]

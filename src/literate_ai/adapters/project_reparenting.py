"""Plan and atomically apply repository-parent changes."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from literate_ai.application.repository_lineage import (
    RepositoryLineageResolutionError,
    resolve_repository_lineage,
)
from literate_ai.contracts import (
    RepositoryLineage,
    RepositoryParentSelection,
    RepositoryReparentChange,
    RepositoryReparentDisposition,
    RepositoryReparentPlan,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

from .project_validation import ProjectValidationError, validate_project
from .repository_lineage import (
    FilesystemRepositoryLineageStore,
    GitRepositoryLineageError,
    GitRepositorySnapshotProvider,
    RepositoryLineageStoreError,
)

RepositoryLineageResolver = Callable[[RepositoryParentSelection], RepositoryLineage]


class RepositoryReparentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _default_resolver(selection: RepositoryParentSelection) -> RepositoryLineage:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    return resolve_repository_lineage(
        selection,
        GitRepositorySnapshotProvider(object_root / "repository-lineage"),
    )


def _changes(
    previous: RepositoryLineage, prospective: RepositoryLineage
) -> tuple[RepositoryReparentChange, ...]:
    old = {item.repository_url: item for item in previous.nodes}
    new = {item.repository_url: item for item in prospective.nodes}
    result = []
    for url in sorted(set(old) | set(new)):
        before = old.get(url)
        after = new.get(url)
        disposition = (
            RepositoryReparentDisposition.ADDED
            if before is None
            else RepositoryReparentDisposition.REMOVED
            if after is None
            else RepositoryReparentDisposition.UNCHANGED
            if before == after
            else RepositoryReparentDisposition.UPDATED
        )
        result.append(RepositoryReparentChange(url, disposition, before, after))
    return tuple(result)


class FilesystemRepositoryReparentAdapter:
    """Resolve first, expose a plan, then compare-and-swap the lineage authority."""

    def __init__(self, *, resolver: RepositoryLineageResolver = _default_resolver):
        self._resolver = resolver

    def plan(
        self, selected: Path, prospective: RepositoryParentSelection
    ) -> RepositoryReparentPlan:
        if not isinstance(prospective, RepositoryParentSelection):
            raise TypeError("reparent requires typed prospective parent authority")
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        if project is None:
            raise RepositoryReparentError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        store = FilesystemRepositoryLineageStore(project.root)
        try:
            previous = store.load_optional()
            previous_evidence_present = previous is not None
            if previous is None:
                previous_selection = RepositoryParentSelection.root()
                previous_lineage = RepositoryLineage(previous_selection, (), ())
            else:
                previous_selection, previous_lineage = previous
            prospective_lineage = self._resolver(prospective)
        except (
            RepositoryLineageStoreError,
            RepositoryLineageResolutionError,
            GitRepositoryLineageError,
        ) as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        if (
            not isinstance(prospective_lineage, RepositoryLineage)
            or prospective_lineage.selection != prospective
        ):
            raise RepositoryReparentError(
                "repository_reparent.resolution_invalid",
                "prospective repository-lineage resolution returned inconsistent "
                "evidence",
            )
        return RepositoryReparentPlan(
            project.definition.identity,
            previous_selection,
            previous_lineage,
            previous_evidence_present,
            prospective,
            prospective_lineage,
            _changes(previous_lineage, prospective_lineage),
        )

    def apply(
        self,
        selected: Path,
        plan: RepositoryReparentPlan,
        *,
        validate_authority: bool = True,
    ) -> dict[str, object]:
        if not isinstance(plan, RepositoryReparentPlan):
            raise TypeError("reparent apply requires a typed plan")
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        if project is None:
            raise RepositoryReparentError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        if project.definition.identity != plan.project_identity:
            raise RepositoryReparentError(
                "repository_reparent.project_changed",
                "project authority changed after reparent planning; plan again",
            )
        try:
            refreshed = self._resolver(plan.prospective_selection)
        except (RepositoryLineageResolutionError, GitRepositoryLineageError) as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        if refreshed != plan.prospective_lineage:
            raise RepositoryReparentError(
                "repository_reparent.prospective_changed",
                "prospective parent lineage changed after planning; plan again",
            )
        store = FilesystemRepositoryLineageStore(project.root)
        if not plan.changed:
            return {
                "schema": "literate-ai/repository-reparent-apply@1",
                "state": "no-op",
                "changed": False,
                "authority_review_required": False,
            }
        try:
            if plan.previous_evidence_present:
                store.replace(
                    plan.prospective_selection,
                    plan.prospective_lineage,
                    expected_selection_identity=plan.previous_selection.identity,
                    expected_lineage_identity=plan.previous_lineage.identity,
                )
            else:
                store.replace(
                    plan.prospective_selection,
                    plan.prospective_lineage,
                    expected_absent=True,
                )
            if validate_authority:
                try:
                    validate_project(
                        project.root,
                        require_authority_review=False,
                        synchronize_source_intelligence=False,
                    )
                except ProjectValidationError:
                    self.rollback_staged_update(project.root, plan)
                    raise
        except RepositoryLineageStoreError as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        except ProjectValidationError as exc:
            raise RepositoryReparentError(
                "repository_reparent.validation_failed",
                "prospective parent authority failed project validation and was "
                "rolled back",
            ) from exc
        return {
            "schema": "literate-ai/repository-reparent-apply@1",
            "state": "applied",
            "changed": True,
            "selection_identity": plan.prospective_selection.identity.uri,
            "lineage_identity": plan.prospective_lineage.identity.uri,
            "authority_review_required": True,
        }

    @staticmethod
    def rollback_staged_update(selected: Path, plan: RepositoryReparentPlan) -> None:
        """Undo an unvalidated reparent staged by a composite project update."""

        if not isinstance(plan, RepositoryReparentPlan):
            raise TypeError("reparent rollback requires a typed plan")
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc
        if project is None:
            raise RepositoryReparentError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        store = FilesystemRepositoryLineageStore(project.root)
        try:
            if plan.previous_evidence_present:
                store.replace(
                    plan.previous_selection,
                    plan.previous_lineage,
                    expected_selection_identity=plan.prospective_selection.identity,
                    expected_lineage_identity=plan.prospective_lineage.identity,
                )
            else:
                store.clear(
                    expected_selection_identity=plan.prospective_selection.identity,
                    expected_lineage_identity=plan.prospective_lineage.identity,
                )
        except RepositoryLineageStoreError as exc:
            raise RepositoryReparentError(exc.code, exc.message) from exc


__all__ = [
    "FilesystemRepositoryReparentAdapter",
    "RepositoryReparentError",
]

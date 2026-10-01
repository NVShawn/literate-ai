"""Apply upstream-only changes, clean merges, and explicitly reviewed resolutions.

All input bytes are rechecked. File writes and the upstream baseline checkpoint
roll back together if prospective-project validation fails. Local overlays and
unselected overlapping conflicts remain untouched.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.contracts import (
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ProjectUpdatePlan,
)

from .project_updates import (
    ProjectUpdateError,
    _identity,
    _project_path,
    _upstream_template,
)

APPLY_RESULT_SCHEMA = "literate-ai/project-update-apply@1"

MECHANICAL = (
    ProjectUpdateClassification.UPSTREAM_ONLY,
    ProjectUpdateClassification.MERGEABLE,
)
OPT_IN = (ProjectUpdateClassification.UPSTREAM_ADDED,)


@dataclass(frozen=True, slots=True)
class AppliedProjectUpdate:
    """What an apply actually wrote, and what it deliberately left alone."""

    applied: tuple[str, ...]
    adopted: tuple[str, ...]
    refused: dict[str, tuple[str, ...]]
    adopted_because: str | None = None
    merged: tuple[str, ...] = ()
    resolutions: tuple[dict, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": APPLY_RESULT_SCHEMA,
            "applied": list(self.applied),
            "merged": list(self.merged),
            "resolutions": list(self.resolutions),
            "adopted": list(self.adopted),
            "refused": {
                key: list(value) for key, value in sorted(self.refused.items())
            },
            "authority_review_required": bool(self.applied or self.adopted),
        }
        if self.adopted_because:
            value["adopted_because"] = self.adopted_because
        # A refusal the operator can lift should say so; silence reads as a verdict.
        if self.refused.get(ProjectUpdateClassification.UPSTREAM_ADDED.value):
            value["hint"] = "re-run with --adopt-added to take the added files"
        return value


def _write_exact(target: Path, content: bytes) -> None:
    """Replace a file atomically; a crash must not leave a half-written file."""

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".litai-update-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply_project_update(
    plan: ProjectUpdatePlan,
    root: Path,
    *,
    adopt_added: bool = False,
    validator: Callable[[Path], object] | None = None,
    resolutions: dict | None = None,
) -> AppliedProjectUpdate:
    """Write one rollback-safe subset and optionally validate the complete result."""

    if not isinstance(plan, ProjectUpdatePlan):
        raise ProjectUpdateError(
            "project.update_plan_invalid", "apply requires a typed update plan"
        )
    root = Path(root).resolve()
    from .update_merge import UpdateBases, resolution_content

    resolutions = resolutions or {}
    bases = UpdateBases(root)
    if (
        plan.update_bases_identity is not None
        and _identity(bases.original or b"") != plan.update_bases_identity
    ):
        raise ProjectUpdateError(
            "project.update_base_changed", "update baseline changed; re-plan"
        )
    if set(resolutions) - {item.path for item in plan.files}:
        raise ProjectUpdateError(
            "project.update_resolution_invalid", "unknown resolution path"
        )
    write_content: dict[str, bytes | None] = {}
    merged: list[str] = []
    # The opt-in on upstream-added exists to guard judgment: adopting a capability a
    # project never had is a decision. A project that has not diverged from its
    # baseline has no such decision to make -- there is nothing local to protect and
    # nothing to reconcile -- so refusing there only obstructs the operator. Divergence
    # means the project changed a framework-owned file: local-only or conflict.
    diverged = any(
        item.classification
        in (
            ProjectUpdateClassification.LOCAL_ONLY,
            ProjectUpdateClassification.CONFLICT,
            ProjectUpdateClassification.MERGEABLE,
        )
        for item in plan.files
    )
    adopting = adopt_added or not diverged
    selected = set(MECHANICAL) | (set(OPT_IN) if adopting else set())

    upstream_content = _upstream_template(
        {item.path for item in plan.files if item.baseline_identity is not None}
    )
    applied: list[str] = []
    adopted: list[str] = []
    refused: dict[str, list[str]] = {}
    snapshots: dict[str, tuple[bytes | None, int | None]] = {}
    created_directories: set[Path] = set()
    selected_items: list[ProjectUpdateFile] = []

    for item in plan.files:
        if item.classification not in selected and item.path not in resolutions:
            if item.classification in (
                ProjectUpdateClassification.CONFLICT,
                ProjectUpdateClassification.LOCAL_ONLY,
                *OPT_IN,
            ):
                refused.setdefault(item.classification.value, []).append(item.path)
            continue

        content = upstream_content.get(item.path)
        removing = item.upstream_identity is None
        if removing:
            if (
                content is not None
                or item.baseline_identity is None
                or item.local_identity != item.baseline_identity
            ):
                raise ProjectUpdateError(
                    "project.update_removal_unsafe",
                    f"upstream removal for {item.path} is not baseline-identical",
                )
        elif content is None or _identity(content) != item.upstream_identity:
            raise ProjectUpdateError(
                "project.update_upstream_changed",
                f"upstream content for {item.path} changed after planning; re-plan "
                "before applying",
            )
        target = _project_path(root, item.path)
        if path_is_link_or_reparse(target):
            raise ProjectUpdateError(
                "project.update_path_unsafe",
                f"{item.path} is a link and cannot be replaced",
            )
        observed_content = target.read_bytes() if target.is_file() else None
        observed = None if observed_content is None else _identity(observed_content)
        if observed != item.local_identity:
            raise ProjectUpdateError(
                "project.update_local_changed",
                f"{item.path} changed after planning; re-plan before applying",
            )

        write_content[item.path] = resolution_content(
            item, observed_content, content, resolutions
        )
        if (
            item.classification is ProjectUpdateClassification.MERGEABLE
            or resolutions.get(item.path, {}).get("decision") == "merge"
        ):
            merged.append(item.path)
        snapshots[item.path] = (
            observed_content,
            stat.S_IMODE(target.stat().st_mode) if target.is_file() else None,
        )
        selected_items.append(item)

    written: list[str] = []
    try:
        for item in selected_items:
            target = _project_path(root, item.path)
            content = write_content[item.path]
            if content is None:
                target.unlink(missing_ok=True)
            else:
                parent = target.parent
                while parent != root and not parent.exists():
                    created_directories.add(parent)
                    parent = parent.parent
                assert content is not None
                _write_exact(target, content)
                mode = snapshots[item.path][1]
                if os.name != "nt" and mode is not None:
                    os.chmod(target, mode)
            written.append(item.path)
            if item.classification is ProjectUpdateClassification.UPSTREAM_ADDED:
                adopted.append(item.path)
            else:
                applied.append(item.path)
        for item in plan.files:
            if item in selected_items or item.classification in (
                ProjectUpdateClassification.UNCHANGED,
                ProjectUpdateClassification.ALREADY_CURRENT,
            ):
                content = upstream_content.get(item.path)
                if content is not None or item in selected_items:
                    bases.advance("framework", item.path, content)
        bases.write()
        if validator is not None:
            validator(root)
    except BaseException:
        try:
            bases.rollback()
            for path in reversed(written):
                target = _project_path(root, path)
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
        except BaseException as rollback_error:
            raise ProjectUpdateError(
                "project.update_rollback_failed",
                "project update failed and exact rollback could not be completed",
            ) from rollback_error
        raise

    return AppliedProjectUpdate(
        tuple(sorted(applied)),
        tuple(sorted(adopted)),
        {key: tuple(sorted(value)) for key, value in refused.items()},
        merged=tuple(sorted(merged)),
        resolutions=tuple(
            {**resolutions[path], "applied": True} for path in sorted(resolutions)
        ),
        adopted_because=(
            None
            if not adopted
            else "requested"
            if adopt_added
            else "the project has not diverged from its baseline"
        ),
    )


__all__ = [
    "APPLY_RESULT_SCHEMA",
    "MECHANICAL",
    "OPT_IN",
    "AppliedProjectUpdate",
    "apply_project_update",
]

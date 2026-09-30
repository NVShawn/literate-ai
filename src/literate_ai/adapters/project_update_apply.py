"""Apply the mechanically safe part of an initialized-project update.

Only ``upstream-only`` is safe without judgment: the classifier reached it by
proving the project's copy still equals the recorded baseline, so upstream is the
sole author of the change and nothing local can be lost. ``upstream-added`` is
safe only in the narrower sense that there is no local content to destroy, but
adopting a capability a project never had is a decision, and a new declared
document can leave the documentation graph unreachable, so it stays opt-in.

Everything else is deliberately refused. ``conflict`` needs semantic migration,
``local-only`` is project authority, and ``preserved-dynamic`` has no upstream
author. Those reach the operator as work items, not as writes.

The plan is a snapshot. Between planning and writing the tree can move, so every
write re-reads both sides and fails closed unless the recorded identities still
hold. That turns a lost update into a refusal.
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

MECHANICAL = (ProjectUpdateClassification.UPSTREAM_ONLY,)
OPT_IN = (ProjectUpdateClassification.UPSTREAM_ADDED,)


@dataclass(frozen=True, slots=True)
class AppliedProjectUpdate:
    """What an apply actually wrote, and what it deliberately left alone."""

    applied: tuple[str, ...]
    adopted: tuple[str, ...]
    refused: dict[str, tuple[str, ...]]
    adopted_because: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": APPLY_RESULT_SCHEMA,
            "applied": list(self.applied),
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
) -> AppliedProjectUpdate:
    """Write one rollback-safe subset and optionally validate the complete result."""

    if not isinstance(plan, ProjectUpdatePlan):
        raise ProjectUpdateError(
            "project.update_plan_invalid", "apply requires a typed update plan"
        )
    root = Path(root).resolve()
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
        if item.classification not in selected:
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

        snapshots[item.path] = (
            observed_content,
            stat.S_IMODE(target.stat().st_mode) if target.is_file() else None,
        )
        selected_items.append(item)

    written: list[str] = []
    try:
        for item in selected_items:
            target = _project_path(root, item.path)
            content = upstream_content.get(item.path)
            if item.upstream_identity is None:
                target.unlink()
            else:
                parent = target.parent
                while parent != root and not parent.exists():
                    created_directories.add(parent)
                    parent = parent.parent
                assert content is not None
                _write_exact(target, content)
            written.append(item.path)
            if item.classification is ProjectUpdateClassification.UPSTREAM_ADDED:
                adopted.append(item.path)
            else:
                applied.append(item.path)
        if validator is not None:
            validator(root)
    except BaseException:
        try:
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

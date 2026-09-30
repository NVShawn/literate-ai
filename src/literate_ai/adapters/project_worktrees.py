"""Canonical project-scoped Git worktree placement and atomic reservation."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters.repository_orchestration import (
    OrchestrationInventoryError,
    _git,
)
from literate_ai.adapters.repository_refresh_worktrees import parse_refresh_worktrees
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_PURPOSE_BYTES = 1024


class ProjectWorktreeError(ValueError):
    """Git custody cannot establish a safe project-scoped worktree location."""


def _git_text(root: Path, *arguments: str) -> str:
    try:
        return _git(root, *arguments).decode("utf-8").strip()
    except (OrchestrationInventoryError, UnicodeDecodeError) as exc:
        raise ProjectWorktreeError("Git worktree custody observation failed") from exc


@dataclass(frozen=True, slots=True)
class ProjectWorktreeLocation:
    requested_checkout: Path
    canonical_checkout: Path
    common_directory: Path
    worktree_root: Path
    registration_identity: ContentIdentity

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/project-worktree-location@1",
            "requested_checkout": str(self.requested_checkout),
            "canonical_checkout": str(self.canonical_checkout),
            "common_directory": str(self.common_directory),
            "worktree_root": str(self.worktree_root),
            "registration_identity": self.registration_identity.to_dict(),
        }


@dataclass(slots=True)
class ProjectWorktreeReservation:
    location: ProjectWorktreeLocation
    name: str
    path: Path
    reservation: Path
    _descriptor: int | None

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        os.close(descriptor)
        try:
            self.reservation.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> ProjectWorktreeReservation:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def resolve_project_worktree_location(repository: Path) -> ProjectWorktreeLocation:
    """Resolve the one in-repository placement root from exact Git custody."""

    supplied = Path(repository)
    try:
        requested = Path(_git_text(supplied, "rev-parse", "--show-toplevel")).resolve(
            strict=True
        )
        if _git_text(requested, "rev-parse", "--is-bare-repository") != "false":
            raise ProjectWorktreeError("bare repositories have no worktree placement")
        common = Path(
            _git_text(
                requested,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        ).resolve(strict=True)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise ProjectWorktreeError("repository custody path is unavailable") from exc
    if common.name != ".git":
        raise ProjectWorktreeError(
            "Git common directory does not identify a canonical non-bare checkout"
        )
    canonical = common.parent.resolve(strict=True)
    if path_is_link_or_reparse(canonical) or path_is_link_or_reparse(common):
        raise ProjectWorktreeError("canonical Git custody cannot traverse a link")
    try:
        raw = _git(requested, "worktree", "list", "--porcelain", "-z")
        registrations = parse_refresh_worktrees(raw)
    except OrchestrationInventoryError as exc:
        raise ProjectWorktreeError("registered worktree listing is invalid") from exc
    canonical_records = [
        item for item in registrations if not item.bare and item.path == canonical
    ]
    if len(canonical_records) != 1 or not any(
        not item.bare and item.path == requested for item in registrations
    ):
        raise ProjectWorktreeError(
            "canonical or requested checkout is absent from Git registration"
        )
    registration_identity = canonical_identity(
        {
            "schema": "literate-ai/project-worktree-registration@1",
            "common_directory": str(common),
            "worktrees": [
                {
                    "path": str(item.path),
                    "commit": item.commit,
                    "reference": item.reference,
                    "bare": item.bare,
                    "locked": item.locked,
                    "prunable": item.prunable,
                }
                for item in registrations
            ],
        }
    )
    worktree_root = canonical / ".worktrees"
    if os.path.lexists(worktree_root):
        try:
            require_safe_directory(worktree_root)
        except UnsafeFilesystemPathError as exc:
            raise ProjectWorktreeError(
                "project worktree root is link-like or unsafe"
            ) from exc
    return ProjectWorktreeLocation(
        requested,
        canonical,
        common,
        worktree_root,
        registration_identity,
    )


def _reservation_name(purpose: str, branch_or_revision: str) -> str:
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or len(purpose.encode("utf-8")) > _MAX_PURPOSE_BYTES
        or not isinstance(branch_or_revision, str)
        or not branch_or_revision
        or len(branch_or_revision.encode("utf-8")) > _MAX_PURPOSE_BYTES
    ):
        raise ProjectWorktreeError("worktree purpose and branch must be bounded")
    slug = _SLUG.sub("-", purpose.casefold()).strip("-")[:40].rstrip("-")
    if not slug:
        slug = "work"
    digest = hashlib.sha256(
        (purpose + "\0" + branch_or_revision).encode("utf-8")
    ).hexdigest()[:12]
    return f"{slug}-{digest}"


def reserve_project_worktree(
    repository: Path, *, purpose: str, branch_or_revision: str
) -> ProjectWorktreeReservation:
    """Atomically reserve one canonical path without creating the Git worktree."""

    before = resolve_project_worktree_location(repository)
    try:
        ensure_safe_directory(before.worktree_root)
        reservations = ensure_safe_directory(before.worktree_root / ".reservations")
    except UnsafeFilesystemPathError as exc:
        raise ProjectWorktreeError("project worktree root is unsafe") from exc
    name = _reservation_name(purpose, branch_or_revision)
    path = before.worktree_root / name
    reservation = reservations / name
    aliases = {
        child.name.casefold()
        for child in before.worktree_root.iterdir()
        if child.name != ".reservations"
    }
    if name.casefold() in aliases or os.path.lexists(path):
        raise ProjectWorktreeError("project worktree path collides")
    try:
        descriptor = os.open(
            reservation,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ProjectWorktreeError("project worktree path is already reserved") from exc
    try:
        after = resolve_project_worktree_location(repository)
        if (
            after.common_directory != before.common_directory
            or after.canonical_checkout != before.canonical_checkout
            or after.registration_identity != before.registration_identity
            or os.path.lexists(path)
        ):
            raise ProjectWorktreeError(
                "Git custody or target path changed during worktree allocation"
            )
        return ProjectWorktreeReservation(before, name, path, reservation, descriptor)
    except Exception:
        os.close(descriptor)
        reservation.unlink(missing_ok=True)
        raise


__all__ = [
    "ProjectWorktreeError",
    "ProjectWorktreeLocation",
    "ProjectWorktreeReservation",
    "reserve_project_worktree",
    "resolve_project_worktree_location",
]

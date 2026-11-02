"""Bounded read-only worktree discovery; this is never retirement authority."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .project_worktrees import (
    ProjectWorktreeError,
    ProjectWorktreeLocation,
    resolve_project_worktree_location,
)
from .repository_orchestration import (
    OrchestrationInventoryError,
    _git,
    _read_document,
)
from .repository_refresh import _local_identity_wire
from .repository_refresh_worktrees import (
    RegisteredRefreshWorktree,
    parse_refresh_worktrees,
)

_MAX_PEER_ENTRIES = 4096
_DEFAULT_PEER_ENTRIES = 256
_MAX_PATH_BYTES = 512 * 1024
_MAX_MARKER_BYTES = 4096
_DISCOVERY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class WorktreeDiscoveryEntry:
    path: Path
    classification: str
    registered: bool
    registered_commit: str | None = None
    registered_reference: str | None = None
    locked: bool = False
    prunable: bool = False
    common_directory: Path | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "classification": self.classification,
            "registered": self.registered,
            "registered_commit": self.registered_commit,
            "registered_reference": self.registered_reference,
            "locked": self.locked,
            "prunable": self.prunable,
            "common_directory": str(self.common_directory)
            if self.common_directory
            else None,
        }


@dataclass(frozen=True, slots=True)
class ProjectWorktreeDiscovery:
    location: ProjectWorktreeLocation
    entries: tuple[WorktreeDiscoveryEntry, ...]
    maximum_peer_entries: int
    filesystem_observation_identity: ContentIdentity

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/project-worktree-discovery@1",
            "location": self.location.to_dict(),
            "maximum_peer_entries": self.maximum_peer_entries,
            "filesystem_observation_identity": (
                self.filesystem_observation_identity.to_dict()
            ),
            "entries": [entry.to_dict() for entry in self.entries],
            "audit_complete": False,
            "retirement_authorized": False,
        }


def _node(path: Path) -> tuple[int, ...]:
    observed = path.lstat()
    node = observed.st_dev, observed.st_ino, observed.st_mode
    return (
        node
        if stat.S_ISDIR(observed.st_mode)
        else (*node, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)
    )


@dataclass(frozen=True, slots=True)
class _GitCustody:
    common_directory: Path
    administration_directory: Path
    checkout_pointer: Path | None
    administration_backlink: Path | None

    def owns_registration(self, path: Path, canonical: Path) -> bool:
        if path == canonical:
            return (
                self.checkout_pointer == path / ".git"
                and self.administration_directory
                == self.common_directory
                == path / ".git"
            )
        return (
            self.checkout_pointer == self.administration_directory
            and self.administration_directory.parent
            == self.common_directory / "worktrees"
            and self.administration_backlink == path / ".git"
        )


class _Observation:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + _DISCOVERY_SECONDS
        self.nodes: dict[Path, tuple[int, ...] | None] = {}
        self.path_bytes = 0

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/project-worktree-filesystem-observation@1",
                "nodes": [
                    {"path": str(path), "signature": _local_identity_wire(node)}
                    for path, node in sorted(self.nodes.items())
                ],
            }
        )

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProjectWorktreeError("worktree discovery exceeded its time bound")
        return remaining

    def remember(self, path: Path) -> bool:
        if path not in self.nodes:
            self.path_bytes += len(os.fsencode(path))
            if self.path_bytes > _MAX_PATH_BYTES:
                raise ProjectWorktreeError(
                    "worktree custody paths exceed their byte bound"
                )
        try:
            node = _node(path)
        except FileNotFoundError:
            node = None
        if path in self.nodes and self.nodes[path] != node:
            raise ProjectWorktreeError("worktree discovery filesystem custody changed")
        self.nodes[path] = node
        return node is not None

    def git(self, path: Path, *arguments: str) -> bytes:
        self.remaining()
        result = _git(path, *arguments, timeout_seconds=min(60, self.remaining()))
        self.remaining()
        return result

    def pointer(self, path: Path, *, prefix: bytes = b"") -> Path | None:
        if not self.remember(path):
            return None
        try:
            # The shared reader rejects FIFO replacement without blocking and
            # handles Windows pathname/descriptor ctime differences correctly.
            content = _read_document(
                path,
                label="Git custody pointer",
                error_suffix="worktree_pointer_invalid",
                maximum_bytes=_MAX_MARKER_BYTES,
            )
        except OrchestrationInventoryError as exc:
            raise ProjectWorktreeError(
                "Git custody pointer could not be captured within its bounds"
            ) from exc
        self.remember(path)
        payload = content.rstrip(b"\r\n")
        if (
            not payload.startswith(prefix)
            or not payload[len(prefix) :]
            or b"\0" in payload
        ):
            raise ProjectWorktreeError("Git custody pointer is invalid")
        target = Path(os.fsdecode(payload[len(prefix) :]))
        return Path(
            os.path.abspath(target if target.is_absolute() else path.parent / target)
        )

    def common(self, path: Path, *, bare: bool = False) -> _GitCustody:
        require_safe_directory(path)
        marker = path / ".git"
        self.remember(marker)
        if path_is_link_or_reparse(marker):
            raise ProjectWorktreeError("Git custody marker is link-like")
        administration = Path(
            self.git(path, "rev-parse", "--absolute-git-dir").decode("utf-8").strip()
        )
        require_safe_directory(administration)
        administration = administration.resolve(strict=True)
        self.remember(administration)
        for name in ("commondir", "gitdir", "config", "config.worktree"):
            self.remember(administration / name)
        if bare:
            if self.git(path, "rev-parse", "--is-bare-repository").strip() != b"true":
                raise ProjectWorktreeError("bare Git custody contradicts its directory")
        else:
            root = (
                self.git(path, "rev-parse", "--show-toplevel").decode("utf-8").strip()
            )
            if Path(root).resolve(strict=True) != path:
                raise ProjectWorktreeError(
                    "Git checkout root contradicts its directory"
                )
        common = Path(
            self.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode("utf-8")
            .strip()
        )
        require_safe_directory(common)
        common = common.resolve(strict=True)
        self.remember(common)
        self.remember(common / "config")
        pointer = (
            self.pointer(marker, prefix=b"gitdir: ")
            if marker.is_file()
            else marker
            if marker.is_dir()
            else None
        )
        return _GitCustody(
            common, administration, pointer, self.pointer(administration / "gitdir")
        )

    def revalidate(self) -> None:
        self.remaining()
        for path, node in self.nodes.items():
            try:
                current = _node(path)
            except FileNotFoundError:
                current = None
            if current != node:
                raise ProjectWorktreeError(
                    "worktree discovery filesystem custody changed"
                )


def _peer_paths(parent: Path, maximum: int) -> tuple[Path, ...]:
    require_safe_directory(parent)
    paths = []
    with os.scandir(parent) as children:
        for child in children:
            if len(paths) >= maximum:
                raise ProjectWorktreeError(
                    "worktree peer discovery exceeds its entry bound"
                )
            paths.append(parent / child.name)
    return tuple(sorted(paths))


def _registered_entry(
    record: RegisteredRefreshWorktree,
    location: ProjectWorktreeLocation,
    observation: _Observation,
) -> WorktreeDiscoveryEntry:
    path = record.path
    common = None
    if record.bare:
        classification = "contradictory-registration"
    elif not observation.remember(path):
        classification = "missing-registered-checkout"
    elif path_is_link_or_reparse(path):
        classification = "unsafe-registered-checkout"
    else:
        try:
            custody = observation.common(path)
            common = custody.common_directory
        except ProjectWorktreeError:
            raise
        except (
            OSError,
            UnsafeFilesystemPathError,
            OrchestrationInventoryError,
            UnicodeError,
        ):
            classification = "unverifiable-registration"
        else:
            if common != location.common_directory or not custody.owns_registration(
                path, location.canonical_checkout
            ):
                classification = "contradictory-registration"
            elif path == location.canonical_checkout:
                classification = "canonical-checkout"
            elif path.parent == location.canonical_checkout.parent:
                classification = "legacy-peer-worktree"
            elif path.is_relative_to(location.worktree_root):
                classification = "project-worktree"
            else:
                classification = "registered-external-worktree"
    return WorktreeDiscoveryEntry(
        path,
        classification,
        True,
        record.commit,
        record.reference,
        record.locked,
        record.prunable,
        common,
    )


def _peer_entry(
    path: Path, location: ProjectWorktreeLocation, observation: _Observation
) -> WorktreeDiscoveryEntry:
    observation.remember(path)
    if path_is_link_or_reparse(path):
        return WorktreeDiscoveryEntry(path, "link-like-peer", False)
    if not path.is_dir():
        return WorktreeDiscoveryEntry(path, "ordinary-peer-file", False)
    bare = False
    if not observation.remember(path / ".git"):
        bare = (
            (path / "HEAD").is_file()
            and (path / "objects").is_dir()
            and (path / "refs").is_dir()
        )
        if not bare:
            return WorktreeDiscoveryEntry(path, "ordinary-peer-directory", False)
        for name in ("HEAD", "objects", "refs"):
            observation.remember(path / name)
    common = None
    try:
        common = observation.common(path, bare=bare).common_directory
    except ProjectWorktreeError:
        raise
    except (
        OSError,
        UnsafeFilesystemPathError,
        OrchestrationInventoryError,
        UnicodeError,
    ):
        classification = "unverifiable-peer-repository"
    else:
        classification = (
            "unregistered-shared-repository"
            if common == location.common_directory
            else "independent-bare-repository"
            if bare
            else "independent-repository"
        )
    return WorktreeDiscoveryEntry(path, classification, False, common_directory=common)


def discover_project_worktrees(
    repository: Path, *, maximum_peer_entries: int = _DEFAULT_PEER_ENTRIES
) -> ProjectWorktreeDiscovery:
    """Classify registered checkouts and bounded immediate peers without mutation.

    An entry is only an ownership observation. Source, history, ignored content,
    processes and retained evidence must still be audited before any disposition.
    """
    if (
        type(maximum_peer_entries) is not int
        or not 1 <= maximum_peer_entries <= _MAX_PEER_ENTRIES
    ):
        raise ProjectWorktreeError("peer entry bound must be an integer from 1 to 4096")
    observation = _Observation()
    try:
        before = resolve_project_worktree_location(
            repository, _git_command=observation.git
        )
        parent = before.canonical_checkout.parent
        observation.remember(before.common_directory)
        peers = _peer_paths(parent, maximum_peer_entries)
        records = parse_refresh_worktrees(
            observation.git(
                before.canonical_checkout, "worktree", "list", "--porcelain", "-z"
            )
        )
        registered = {record.path for record in records}
        all_paths = registered | set(peers)
        if sum(len(os.fsencode(path)) for path in all_paths) > _MAX_PATH_BYTES:
            raise ProjectWorktreeError("worktree discovery exceeds its path byte bound")
        entries = tuple(
            sorted(
                [
                    *(
                        _registered_entry(record, before, observation)
                        for record in records
                    ),
                    *(
                        _peer_entry(path, before, observation)
                        for path in peers
                        if path not in registered
                    ),
                ],
                key=lambda entry: str(entry.path),
            )
        )
        observation.revalidate()
        if _peer_paths(parent, maximum_peer_entries) != peers:
            raise ProjectWorktreeError(
                "peer directory membership changed during discovery"
            )
        after = resolve_project_worktree_location(
            repository, _git_command=observation.git
        )
        observation.remaining()
        if before != after:
            raise ProjectWorktreeError(
                "Git worktree registration changed during discovery"
            )
        observation.revalidate()
        return ProjectWorktreeDiscovery(
            before, entries, maximum_peer_entries, observation.identity
        )
    except (
        OSError,
        UnicodeError,
        UnsafeFilesystemPathError,
        OrchestrationInventoryError,
    ) as exc:
        raise ProjectWorktreeError(
            "worktree discovery could not establish stable custody"
        ) from exc


__all__ = [
    "ProjectWorktreeDiscovery",
    "WorktreeDiscoveryEntry",
    "discover_project_worktrees",
]

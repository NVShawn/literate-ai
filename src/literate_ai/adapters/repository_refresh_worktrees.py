"""Bounded registered-worktree custody without inspecting foreign source trees.

Uses Git's stable NUL-delimited protocol and registration metadata:
https://git-scm.com/docs/git-worktree
Reobservation detects drift; it does not freeze forced same-user Git operations.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts.paths import canonical_relative_posix_path

from .repository_orchestration import OrchestrationInventoryError, _git, _oid
from .repository_refresh import RefreshFileObservation, _file, _node

_MAX_WORKTREES = 128
_MAX_LIST_BYTES = 64 * 1024
_MAX_FILE_BYTES = 4096
_MAX_TOTAL_WORKTREES = 512
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_OPERATIONS = (
    "HEAD.lock",
    "index.lock",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_LOG",
    "BISECT_START",
)


def _fail(message):
    raise OrchestrationInventoryError("refresh_worktrees_invalid", message) from None


@dataclass(frozen=True, slots=True)
class RegisteredRefreshWorktree:
    path: Path
    commit: str | None
    reference: str | None
    bare: bool
    locked: bool
    prunable: bool


@dataclass(frozen=True, slots=True)
class RefreshWorktreeRegistry:
    common_directory: Path
    directory_node: tuple[int, int, int] | None
    listing_identity: str
    worktrees: tuple[RegisteredRefreshWorktree, ...]
    worktree_nodes: tuple[tuple[Path, tuple[int, int, int] | None], ...]
    metadata_nodes: tuple[tuple[Path, tuple[int, int, int]], ...]
    files: tuple[RefreshFileObservation, ...]
    busy: tuple[tuple[Path, str], ...]


def parse_refresh_worktrees(raw: bytes) -> tuple[RegisteredRefreshWorktree, ...]:
    if not raw or len(raw) > _MAX_LIST_BYTES or not raw.endswith(b"\0\0"):
        _fail("registered worktree listing is incomplete or exceeds its byte bound")
    blocks = raw[:-2].split(b"\0\0")
    if len(blocks) > _MAX_WORKTREES:
        _fail("registered worktree count exceeds its bound")
    result = []
    paths = set()
    for block in blocks:
        fields = {}
        for index, line in enumerate(block.split(b"\0")):
            key, separator, value = line.partition(b" ")
            if (
                key
                not in {
                    b"worktree",
                    b"HEAD",
                    b"branch",
                    b"bare",
                    b"detached",
                    b"locked",
                    b"prunable",
                }
                or key in fields
                or (index == 0 and key != b"worktree")
                or (
                    key in {b"worktree", b"HEAD", b"branch"}
                    and not (separator and value)
                )
                or (key in {b"bare", b"detached"} and separator)
            ):
                _fail("registered worktree listing has ambiguous fields")
            fields[key] = value
        path = Path(os.fsdecode(fields[b"worktree"]))
        if not path.is_absolute() or path in paths:
            _fail("registered worktree paths must be unique and absolute")
        paths.add(path)
        bare = b"bare" in fields
        reference = None
        commit = None
        if bare:
            if fields.keys() - {b"worktree", b"bare", b"locked", b"prunable"}:
                _fail("bare registration unexpectedly declares a checkout")
        else:
            if b"HEAD" not in fields or (b"branch" in fields) == (
                b"detached" in fields
            ):
                _fail("registered checkout has no unambiguous HEAD state")
            raw_commit = fields[b"HEAD"]
            if raw_commit not in (b"0" * 40, b"0" * 64):
                commit = _oid(raw_commit)
            if b"branch" in fields:
                reference = fields[b"branch"].decode("utf-8")
                canonical_relative_posix_path(reference, label="registered branch")
                if not reference.startswith("refs/"):
                    _fail("registered branch is outside the reference namespace")
            elif commit is None:
                _fail("detached registration has no commit")
        result.append(
            RegisteredRefreshWorktree(
                path,
                commit,
                reference,
                bare,
                b"locked" in fields,
                b"prunable" in fields,
            )
        )
    return tuple(result)


def observe_refresh_worktrees(observations, reservations=None):
    """Capture each common store once; do not enter or repair foreign worktrees."""
    registries = []
    seen = set()
    total_count = total_bytes = 0
    for observed in observations:
        common = observed.common_directory
        if common in seen:
            continue
        seen.add(common)
        if _node(common) != observed.common_node:
            _fail("registered worktree anchor changed")
        raw = _git(observed.root, "worktree", "list", "--porcelain", "-z")
        worktrees = parse_refresh_worktrees(raw)
        worktree_nodes = []
        for item in worktrees:
            if item.bare:
                continue
            try:
                root_node = _node(item.path)
            except (OSError, UnsafeFilesystemPathError):
                root_node = None
            worktree_nodes.append((item.path, root_node))
        directory = common / "worktrees"
        node = None
        metadata = [common]
        if os.path.lexists(directory):
            node = _node(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(metadata) >= _MAX_WORKTREES:
                        _fail("worktree registration directory exceeds its bound")
                    canonical_relative_posix_path(
                        entry.name, label="worktree registration"
                    )
                    path = directory / entry.name
                    require_safe_directory(path)
                    metadata.append(path)
        if len(metadata) != len(worktrees):
            _fail("worktree listing and registration directory disagree")
        nodes, files, busy = [], [], []
        for path in sorted(metadata):
            nodes.append((path, _node(path)))
            for name in (
                ("HEAD",) if path == common else ("HEAD", "gitdir", "commondir")
            ):
                files.append(_file(path / name, maximum_bytes=_MAX_FILE_BYTES))
            for marker in _OPERATIONS:
                candidate = path / marker
                if reservations is not None and reservations.owns(candidate):
                    continue
                if os.path.lexists(candidate):
                    busy.append((path, marker))
        total_count += len(worktrees)
        total_bytes += len(raw) + sum(len(item.content) for item in files)
        if total_count > _MAX_TOTAL_WORKTREES or total_bytes > _MAX_TOTAL_BYTES:
            _fail("registered worktree custody exceeds aggregate bounds")
        registries.append(
            RefreshWorktreeRegistry(
                common,
                node,
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                worktrees,
                tuple(worktree_nodes),
                tuple(nodes),
                tuple(files),
                tuple(busy),
            )
        )
    return tuple(registries)


def _checkout_registrations(registry, observations):
    """Resolve Git's absorbed-primary spelling using captured checkout custody.

    Git can list an absorbed primary at its common administrative directory.
    Only an observed primary (git-dir == common-dir), with a captured .git
    file and verified worktree root, can identify that row. Keep the original
    registry untouched so listing bytes and metadata nodes remain revalidated.
    """
    common = registry.common_directory
    primary = [
        item
        for item in observations
        if item.common_directory == common
        and item.git_directory == common
        and isinstance(item.boundary, RefreshFileObservation)
    ]
    if len(primary) > 1:
        _fail("absorbed primary registration has ambiguous checkout ownership")
    rows = tuple(
        replace(item, path=primary[0].root)
        if primary and not item.bare and item.path == common
        else item
        for item in registry.worktrees
    )
    if len({item.path for item in rows}) != len(rows):
        _fail("resolved worktree registrations have duplicate checkout paths")
    return rows


def require_refresh_worktree_ownership(refresh):
    """Refuse shared-ref effects outside the captured independent checkout set."""
    requested = {
        refresh.repository.root / item.path: item.commit
        for item in refresh.authority.request.targets
    }
    observations = (refresh.root_git, *refresh.children)
    known = {(item.common_directory, item.root): item for item in observations}
    affected = {}
    for observed in observations:
        if requested.get(observed.root, observed.commit) == observed.commit:
            continue
        reference = observed.symbolic_reference
        if reference is None or reference.startswith(
            ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
        ):
            continue
        affected.setdefault(observed.common_directory, set()).add(reference)
    registries = {item.common_directory: item for item in refresh.worktrees}
    if affected.keys() - registries.keys():
        _fail("shared reference has no registered-worktree custody")
    for common, references in affected.items():
        registry = registries[common]
        registrations = _checkout_registrations(registry, observations)
        if (
            registry.busy
            or any(item.prunable for item in registry.worktrees)
            or any(node is None for _, node in registry.worktree_nodes)
        ):
            raise OrchestrationInventoryError(
                "refresh_worktree_busy",
                "shared-ref updates refuse active or unavailable registered worktrees",
            )
        for observed in observations:
            if (
                observed.common_directory != common
                or observed.symbolic_reference not in references
            ):
                continue
            if not any(
                not item.bare
                and (item.path, item.commit, item.reference)
                == (observed.root, observed.commit, observed.symbolic_reference)
                for item in registrations
            ):
                _fail("observed shared-ref checkout has no matching registration")
        for item in registrations:
            if item.bare or item.reference not in references:
                continue
            observed = known.get((common, item.path))
            if observed is None or (observed.commit, observed.symbolic_reference) != (
                item.commit,
                item.reference,
            ):
                raise OrchestrationInventoryError(
                    "refresh_external_worktree",
                    "shared-ref update would affect an unobserved or changed checkout",
                )

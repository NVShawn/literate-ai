"""Nonexecuting refresh input custody, separate from publication and apply.

Repeated observations detect drift; they do not acquire a transaction lock or
provide an atomic snapshot against a hostile process running as the same user.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshAuthority,
    RepositoryRefreshRequest,
)

from ._write_reservations import WriteReservationSet
from .repository_lfs import (
    HydratedLfsObservation,
    observe_hydrated_lfs_entry,
)
from .repository_lock_planning import PreparedRepositoryLock, prepare_repository_lock
from .repository_locks import MAXIMUM_LOCK_BYTES, RepositoryLockStore
from .repository_orchestration import (
    OrchestrationInventoryError,
    _git,
    _oid,
    _read_document,
    _require_nonwriting_index,
    _worktree,
    inspect_gitlink_inventory,
)

if TYPE_CHECKING:
    from .repository_refresh_directories import RefreshDirectoryObservation
    from .repository_refresh_reflogs import RefreshReflogObservation
    from .repository_refresh_worktrees import RefreshWorktreeRegistry

_CANONICAL_LFS_CLEAN_FILTERS = {
    b"clean": b"git-lfs clean -- %f",
    b"process": b"git-lfs filter-process",
}


def _local_integer_wire(value: int):
    if not -(2**63) <= value <= 2**63 - 1:
        return {"integer_decimal": str(value)}
    return value


def _local_identity_wire(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return _local_integer_wire(value)
    if isinstance(value, dict):
        return {key: _local_identity_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_local_identity_wire(item) for item in value]
    return value


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError(suffix, message)


def _node(path: Path) -> tuple[int, int, int]:
    require_safe_directory(path)
    value = path.lstat()
    return value.st_dev, value.st_ino, value.st_mode


def _signature(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class RefreshFileObservation:
    path: Path
    signature: tuple[int, ...] | None
    content: bytes | None


def _file(
    path: Path, *, optional: bool = False, maximum_bytes: int = MAXIMUM_LOCK_BYTES
) -> RefreshFileObservation:
    require_safe_directory(path.parent)
    try:
        before = _signature(path)
    except FileNotFoundError:
        if optional:
            return RefreshFileObservation(path, None, None)
        raise
    content = _read_document(
        path,
        label="refresh input",
        error_suffix="refresh_input_invalid",
        maximum_bytes=maximum_bytes,
    )
    if _signature(path) != before:
        _fail("inputs_changed", "refresh input changed during observation")
    return RefreshFileObservation(path, before, content)


def _git_path(root: Path, *arguments: str) -> Path:
    raw = _git(root, "rev-parse", "--path-format=absolute", *arguments)
    value = raw.decode("utf-8").removesuffix("\n")
    if not value or "\n" in value or "\r" in value:
        _fail("refresh_input_invalid", "Git metadata path is absent or ambiguous")
    return Path(os.path.abspath(root / value))


def _absent(path: Path, reservations: WriteReservationSet | None = None) -> None:
    require_safe_directory(path.parent)
    if reservations is not None and reservations.owns(path):
        return
    if os.path.lexists(path):
        _fail("refresh_busy", "refresh input has an active or retained writer marker")


@dataclass(frozen=True, slots=True)
class RefreshReferenceObservation:
    name: str
    file: RefreshFileObservation


@dataclass(frozen=True, slots=True)
class RefreshGitObservation:
    root: Path
    root_node: tuple[int, int, int]
    git_directory: Path
    git_node: tuple[int, int, int]
    common_directory: Path
    common_node: tuple[int, int, int]
    boundary: RefreshFileObservation | tuple[int, int, int]
    head: RefreshFileObservation
    commit: str
    symbolic_reference: str | None
    index: RefreshFileObservation
    configuration_identity: str
    references: tuple[RefreshReferenceObservation, ...]
    packed_references: RefreshFileObservation
    hydrated_lfs: tuple[HydratedLfsObservation, ...]


def _configuration(root: Path, *, clean: bool) -> str:
    raw = _git(root, "config", "--null", "--list", "--includes")
    if raw and not raw.endswith(b"\0"):
        _fail("refresh_input_invalid", "Git configuration observation is incomplete")
    for entry in raw.split(b"\0")[:-1]:
        key, _, value = entry.partition(b"\n")
        normalized = key.lower()
        if normalized == b"extensions.refstorage" and value != b"files":
            _fail(
                "refresh_refs_unsupported",
                "refresh reference custody requires the files backend",
            )
        section, section_dot, remainder = key.partition(b".")
        subsection, subsection_dot, variable = remainder.rpartition(b".")
        filter_variable = variable if subsection_dot else remainder
        clean_filter = (
            clean
            and section_dot
            and section.lower() == b"filter"
            and filter_variable.lower() in _CANONICAL_LFS_CLEAN_FILTERS
        )
        if clean_filter:
            if (
                not subsection_dot
                or subsection != b"lfs"
                or _CANONICAL_LFS_CLEAN_FILTERS[filter_variable.lower()] != value
            ):
                _fail(
                    "refresh_filter_unsupported",
                    "nonexecuting cleanliness inspection refuses external filters",
                )
    # Configuration can contain credentials and host paths; retain only a digest.
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _clean(
    root: Path, reservations: WriteReservationSet | None = None
) -> tuple[HydratedLfsObservation, ...]:
    flags = _git(root, "ls-files", "-v", "-z")
    if flags and not flags.endswith(b"\0"):
        _fail("refresh_input_invalid", "Git index flags observation is incomplete")
    for entry in flags.split(b"\0")[:-1]:
        if len(entry) < 3 or entry[1:2] != b" ":
            _fail("refresh_input_invalid", "Git index flag is malformed")
        if entry[:1].islower() or entry[:1] == b"S":
            _fail(
                "refresh_hidden_changes",
                "refresh refuses assume-unchanged and skip-worktree index entries",
            )
    # Inspect nested children ourselves before invoking any status there. Git's
    # recursive status could otherwise execute a nested child's content filter.
    status = _git(
        root,
        "-c",
        "core.untrackedCache=false",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.required=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
    )
    hydrated = []
    for entry in status.split(b"\0"):
        if not entry:
            continue
        if (
            reservations is not None
            and entry.startswith(b"?? ")
            and reservations.owns(root / os.fsdecode(entry[3:]))
        ):
            continue
        observation = observe_hydrated_lfs_entry(root, entry)
        if isinstance(observation, HydratedLfsObservation):
            hydrated.append(observation)
            continue
        _fail("refresh_child_dirty", "refresh requires clean independent children")
    return tuple(hydrated)


def _observe_git(
    root: Path, *, clean: bool, reservations: WriteReservationSet | None = None
) -> RefreshGitObservation:
    _worktree(root)
    _require_nonwriting_index(root)
    node = _node(root)
    git_directory = _git_path(root, "--git-dir")
    common_directory = _git_path(root, "--git-common-dir")
    git_node, common_node = _node(git_directory), _node(common_directory)
    for marker in (
        "HEAD.lock",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "BISECT_LOG",
    ):
        _absent(git_directory / marker, reservations)
    index_path = _git_path(root, "--git-path", "index")
    _absent(index_path.with_name(index_path.name + ".lock"), reservations)
    head = _file(git_directory / "HEAD")
    index = _file(index_path)
    boundary_path = root / ".git"
    boundary = _node(boundary_path) if boundary_path.is_dir() else _file(boundary_path)
    configuration = _configuration(root, clean=clean)
    commit = _oid(_git(root, "rev-parse", "--verify", "HEAD^{commit}").strip())
    reference = (
        _git(root, "rev-parse", "--symbolic-full-name", "HEAD")
        .decode("utf-8")
        .removesuffix("\n")
    )
    if reference != "HEAD" and not reference.startswith("refs/"):
        _fail("refresh_input_invalid", "Git HEAD has an ambiguous symbolic reference")
    from .repository_refresh_refs import observe_refresh_references

    references, packed = observe_refresh_references(
        root,
        git_directory,
        common_directory,
        head,
        commit,
        None if reference == "HEAD" else reference,
        reservations,
    )
    hydrated_lfs = _clean(root, reservations) if clean else ()
    return RefreshGitObservation(
        root,
        node,
        git_directory,
        git_node,
        common_directory,
        common_node,
        boundary,
        head,
        commit,
        None if reference == "HEAD" else reference,
        index,
        configuration,
        references,
        packed,
        hydrated_lfs,
    )


@dataclass(frozen=True, slots=True)
class PreparedRepositoryRefresh:
    """Local-only evidence; neither serializable write authority nor a public plan."""

    repository: PreparedRepositoryLock
    authority: RepositoryRefreshAuthority
    metadata_node: tuple[int, int, int]
    manifest: RefreshFileObservation
    repository_lock: RefreshFileObservation
    root_git: RefreshGitObservation
    children: tuple[RefreshGitObservation, ...]
    worktrees: tuple[RefreshWorktreeRegistry, ...]
    reflogs: tuple[RefreshReflogObservation, ...]
    metadata_directories: tuple[RefreshDirectoryObservation, ...]


def _observe(
    root: Path,
    request: RepositoryRefreshRequest,
    reservations: WriteReservationSet | None = None,
) -> PreparedRepositoryRefresh:
    _require_nonwriting_index(root)
    repository = prepare_repository_lock(root)
    authority = RepositoryRefreshAuthority(
        repository.lock.repository_orchestration, request
    )
    manifest = _file(root / "literate.project.json")
    store = RepositoryLockStore(root)
    metadata_node = _node(store.directory)
    _absent(store.writer_path, reservations)
    # Validate any existing lock, including canonical bytes. Missing/stale locks
    # remain captured inputs; refresh must invalidate or replace them explicitly.
    store.read()
    repository_lock = _file(store.path, optional=True)
    root_git = _observe_git(root, clean=False, reservations=reservations)
    children: list[RefreshGitObservation] = []

    def visit(parent: Path, *, depth: int) -> None:
        if depth > 16:
            _fail("refresh_limit_exceeded", "refresh nesting exceeds its depth bound")
        inventory = inspect_gitlink_inventory(parent)
        for child in inventory.children:
            if child.checked_out_commit is None:
                _fail("refresh_child_missing", "refresh requires initialized children")
            if depth and child.checked_out_commit != child.commit:
                _fail(
                    "refresh_child_dirty", "nested child HEAD differs from its Gitlink"
                )
            if len(children) >= 128:
                _fail("refresh_limit_exceeded", "refresh exceeds 128 observed children")
            child_root = parent / child.path
            children.append(
                _observe_git(child_root, clean=True, reservations=reservations)
            )
            visit(child_root, depth=depth + 1)

    visit(root, depth=0)
    from .repository_refresh_worktrees import observe_refresh_worktrees

    worktrees = observe_refresh_worktrees((root_git, *children), reservations)
    from .repository_refresh_directories import (
        observe_metadata_parents,
        refresh_metadata_directory_targets,
    )
    from .repository_refresh_reflogs import observe_refresh_reflogs

    reflogs = observe_refresh_reflogs(root, children, request, reservations)
    directories = observe_metadata_parents(
        refresh_metadata_directory_targets(
            root_git, children, manifest, repository_lock, reflogs
        ),
        reservations,
    )
    return PreparedRepositoryRefresh(
        repository,
        authority,
        metadata_node,
        manifest,
        repository_lock,
        root_git,
        tuple(children),
        worktrees,
        reflogs,
        directories,
    )


def _observe_checked(
    root: Path,
    request: RepositoryRefreshRequest,
    reservations: WriteReservationSet | None = None,
) -> PreparedRepositoryRefresh:
    try:
        return _observe(root, request, reservations)
    except OrchestrationInventoryError:
        raise
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        UnsafeFilesystemPathError,
    ) as exc:
        raise OrchestrationInventoryError(
            "refresh_inputs_invalid",
            "refresh inputs are unavailable, unsafe or invalid",
        ) from exc


def prepare_repository_refresh(
    root: Path, request: RepositoryRefreshRequest
) -> PreparedRepositoryRefresh:
    if not isinstance(request, RepositoryRefreshRequest):
        raise TypeError("refresh preparation requires typed explicit intent")
    prepared = _observe_checked(Path(root).absolute(), request)
    require_repository_refresh_inputs_unchanged(prepared)
    return prepared


def require_repository_refresh_inputs_unchanged(
    prepared: PreparedRepositoryRefresh,
    *,
    reservations: WriteReservationSet | None = None,
) -> None:
    if not isinstance(prepared, PreparedRepositoryRefresh):
        raise TypeError("refresh revalidation requires prepared local observations")
    if reservations is not None:
        if not isinstance(reservations, WriteReservationSet):
            raise TypeError("refresh revalidation requires live write reservations")
        reservations.verify_all()
    if (
        _observe_checked(
            prepared.repository.root, prepared.authority.request, reservations
        )
        != prepared
    ):
        _fail("inputs_changed", "refresh inputs changed after preparation")
    if reservations is not None:
        reservations.verify_all()


def require_nested_refresh_child_unchanged(
    observed: RefreshGitObservation,
    *,
    reservations: WriteReservationSet,
) -> None:
    """Revalidate one opaque initialized nested child under live reservations."""
    if not isinstance(observed, RefreshGitObservation) or not isinstance(
        reservations, WriteReservationSet
    ):
        raise TypeError("nested refresh custody requires typed live observations")
    reservations.verify_all()
    if _observe_git(observed.root, clean=True, reservations=reservations) != observed:
        _fail("inputs_changed", "nested child custody changed during refresh")
    reservations.verify_all()


def require_refresh_child_unchanged(
    observed: RefreshGitObservation,
    *,
    reservations: WriteReservationSet,
) -> None:
    """Revalidate one unchanged independent child under live reservations."""
    require_nested_refresh_child_unchanged(observed, reservations=reservations)


def require_refresh_root_git_unchanged(
    observed: RefreshGitObservation,
    *,
    reservations: WriteReservationSet,
) -> None:
    """Revalidate root Git custody while its index is an expected live target."""
    if not isinstance(observed, RefreshGitObservation) or not isinstance(
        reservations, WriteReservationSet
    ):
        raise TypeError("root refresh custody requires typed live observations")
    reservations.verify_all()
    fresh = _observe_git(observed.root, clean=False, reservations=reservations)
    if replace(fresh, index=observed.index) != observed:
        _fail("inputs_changed", "root Git custody changed during refresh")
    reservations.verify_all()


def require_refresh_prospective_inventory(
    prepared: PreparedRepositoryRefresh,
    *,
    reservations: WriteReservationSet,
) -> None:
    """Revalidate root URL/branch/Gitlink binding after prospective local changes."""
    if not isinstance(prepared, PreparedRepositoryRefresh) or not isinstance(
        reservations, WriteReservationSet
    ):
        raise TypeError("prospective inventory requires typed live custody")
    reservations.verify_all()
    targets = {
        target.path: target.commit for target in prepared.authority.request.targets
    }
    previous = prepared.repository.inventory
    expected = replace(
        previous,
        children=tuple(
            replace(
                child,
                commit=targets.get(child.path, child.commit),
                checked_out_commit=targets.get(child.path, child.checked_out_commit),
            )
            for child in previous.children
        ),
    )
    if inspect_gitlink_inventory(prepared.repository.root) != expected:
        _fail(
            "inputs_changed",
            "prospective root Gitlink and URL binding changed during refresh",
        )
    reservations.verify_all()

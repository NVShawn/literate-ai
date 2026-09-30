"""Bounded reflog custody and inert append records for a root-owned refresh.

Logging policy and line format follow https://git-scm.com/docs/git-update-ref
and https://git-scm.com/docs/git-config#Documentation/git-config.txt-corelogAllRefUpdates.
No live reflog or reference is changed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .repository_orchestration import OrchestrationInventoryError, _git
from .repository_refresh import RefreshFileObservation, _absent, _git_path
from .repository_refresh_directories import optional_metadata_file

_MAX_LOG_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_LOGS = 4096
_IDENT = re.compile(
    rb"[^<>\x00-\x1f\x7f]+ <[^<>\x00-\x1f\x7f]+> [0-9]{1,20} "
    rb"[+-](?:[01][0-9]|2[0-3])[0-5][0-9]\Z"
)


@dataclass(frozen=True, slots=True)
class RefreshReflogObservation:
    repository: str
    name: str
    before: RefreshFileObservation
    anchor: Path
    anchor_node: tuple[int, int, int]
    previous_commit: str
    prospective_commit: str
    append: bool


def _policy(root):
    raw = _git(root, "config", "--null", "--list", "--includes")
    values = [
        entry.partition(b"\n")[2]
        for entry in raw.split(b"\0")
        if entry.partition(b"\n")[0].lower() == b"core.logallrefupdates"
    ]
    if values and values[-1] == b"always":
        return "always"
    if values:
        value = _git(
            root, "config", "--type=bool", "--get", "core.logAllRefUpdates"
        ).strip()
    else:
        value = (
            b"false"
            if _git(root, "rev-parse", "--is-bare-repository").strip() == b"true"
            else b"true"
        )
    if value not in {b"true", b"false"}:
        raise OrchestrationInventoryError(
            "refresh_reflog_policy", "reflog policy is unsupported"
        )
    return value.decode("ascii")


def observe_refresh_reflogs(root, children, request, reservations=None):
    requested = {root / target.path: target.commit for target in request.targets}
    records = []
    total = 0
    for observed in children:
        target = requested.get(observed.root, observed.commit)
        if target == observed.commit:
            continue
        policy = _policy(observed.root)
        for name in ("HEAD", *(item.name for item in observed.references)):
            relative = "logs/" + name
            path = _git_path(observed.root, "--git-path", relative)
            if path == observed.git_directory / relative:
                anchor, node = observed.git_directory, observed.git_node
            elif path == observed.common_directory / relative:
                anchor, node = observed.common_directory, observed.common_node
            else:
                raise OrchestrationInventoryError(
                    "refresh_reflog_path", "reflog storage is redirected"
                )
            before = optional_metadata_file(
                path,
                anchor,
                node,
                maximum_bytes=_MAX_LOG_BYTES,
                reservations=reservations,
            )
            if before.content is not None:
                if (
                    path.lstat().st_nlink != 1
                    or (before.content and not before.content.endswith(b"\n"))
                    or b"\0" in before.content
                ):
                    raise OrchestrationInventoryError(
                        "refresh_reflog_invalid",
                        "reflog is hardlinked or cannot be appended safely",
                    )
            if path.parent.is_dir():
                _absent(path.with_name(path.name + ".lock"), reservations)
            append = (
                before.content is not None
                or policy == "always"
                or (
                    policy == "true"
                    and (
                        name == "HEAD"
                        or name.startswith(
                            ("refs/heads/", "refs/remotes/", "refs/notes/")
                        )
                    )
                )
            )
            records.append(
                RefreshReflogObservation(
                    observed.root.relative_to(root).as_posix(),
                    name,
                    before,
                    anchor,
                    node,
                    observed.commit,
                    target,
                    append,
                )
            )
            total += len(before.content or b"")
            if len(records) > _MAX_LOGS or total > _MAX_TOTAL_BYTES:
                raise OrchestrationInventoryError(
                    "refresh_reflog_limit", "reflog custody exceeds aggregate bounds"
                )
    return tuple(records)


def refresh_reflog_transitions(refresh, committer=None):
    from .repository_refresh_metadata import RefreshMetadataTransition

    if any(item.append for item in refresh.reflogs) and (
        type(committer) is not bytes
        or len(committer) > 4096
        or _IDENT.fullmatch(committer) is None
    ):
        raise OrchestrationInventoryError(
            "refresh_reflog_identity",
            "root committer identity is unavailable or malformed",
        )
    transitions = {}
    total = 0
    for item in refresh.reflogs:
        content = item.before.content
        if item.append:
            content = (content or b"") + (
                f"{item.previous_commit} {item.prospective_commit} ".encode("ascii")
                + committer
                + b"\tliterate-ai refresh\n"
            )
        if len(content or b"") > _MAX_LOG_BYTES:
            raise OrchestrationInventoryError(
                "refresh_reflog_limit", "prospective reflog exceeds its byte bound"
            )
        previous = transitions.get(item.before.path)
        if previous is not None:
            if previous.before != item.before or previous.prospective != content:
                raise OrchestrationInventoryError(
                    "refresh_reflog_conflict", "shared reflog transitions conflict"
                )
            continue
        total += len(content or b"")
        if total > _MAX_TOTAL_BYTES:
            raise OrchestrationInventoryError(
                "refresh_reflog_limit", "prospective reflogs exceed aggregate bounds"
            )
        transitions[item.before.path] = RefreshMetadataTransition(
            "reflog", item.repository, "logs/" + item.name, item.before, content
        )
    return tuple(transitions.values())


def prepare_staged_metadata(files):
    from .repository_refresh_metadata import refresh_metadata_transitions
    from .repository_refresh_ownership import _within_root_pin_only_target

    files.require_current()
    refresh = files._owner._prepared.refresh
    transitions = tuple(
        transition
        for transition in refresh_metadata_transitions(refresh)
        if not _within_root_pin_only_target(
            PurePosixPath(getattr(transition, "repository", ".")),
            files.target_modes,
        )
    )
    try:
        committer = None
        if any(item.append for item in refresh.reflogs):
            committer = _git(
                refresh.repository.root, "var", "GIT_COMMITTER_IDENT"
            ).removesuffix(b"\n")
        logs = refresh_reflog_transitions(refresh, committer)
    finally:
        files.require_current()
    return (*transitions[:-2], *logs, *transitions[-2:])

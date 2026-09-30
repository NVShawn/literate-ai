"""Exact files-backend HEAD chains, including loose absence over packed refs.

Git's layout and symbolic-ref contracts are authoritative:
https://git-scm.com/docs/gitrepository-layout
https://git-scm.com/docs/git-symbolic-ref
These observations neither advance references nor grant transaction replay.
"""

from __future__ import annotations

import os

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts.paths import canonical_relative_posix_path

from ._write_reservations import WriteReservationSet
from .repository_orchestration import _git, _oid
from .repository_refresh import (
    RefreshFileObservation,
    RefreshReferenceObservation,
    _absent,
    _fail,
    _file,
    _git_path,
)

_MAX_CHAIN = 16
_MAX_REF_BYTES = 4096
_MAX_PACKED_BYTES = 1024 * 1024


def _reference_file(anchor, relative, reservations):
    path = anchor / relative
    parent = anchor
    for part in relative.parts[:-1]:
        require_safe_directory(parent)
        candidate = parent / part
        WriteReservationSet._no_alias(candidate)
        if not os.path.lexists(candidate):
            # A packed ref need not have a loose parent directory. A later live
            # reservation may create it; that does not change the absent ref.
            return RefreshFileObservation(path, None, None)
        require_safe_directory(candidate)
        parent = candidate
    WriteReservationSet._no_alias(path)
    _absent(path.with_name(path.name + ".lock"), reservations)
    value = _file(path, optional=True, maximum_bytes=_MAX_REF_BYTES)
    if value.content is not None and path.lstat().st_nlink != 1:
        _fail("refresh_refs_unsupported", "refresh refuses hardlinked reference files")
    return value


def observe_refresh_references(
    root, git_directory, common_directory, head, commit, resolved, reservations=None
):
    """Bind every hop, not merely the final name and peeled commit Git returns."""
    if len(head.content) > _MAX_REF_BYTES or head.path.lstat().st_nlink != 1:
        _fail("refresh_refs_unsupported", "HEAD custody is oversized or hardlinked")
    _absent(common_directory / "packed-refs.lock", reservations)
    packed = _file(
        common_directory / "packed-refs",
        optional=True,
        maximum_bytes=_MAX_PACKED_BYTES,
    )
    chain = []
    content = head.content
    while content is not None and content.startswith(b"ref: "):
        name = content[5:].removesuffix(b"\n").decode("utf-8")
        relative = canonical_relative_posix_path(name, label="refresh reference")
        if (
            not name.startswith("refs/")
            or len(name.encode("utf-8")) > _MAX_REF_BYTES - 6
            or len(chain) >= _MAX_CHAIN
            or name in {item.name for item in chain}
        ):
            _fail("refresh_refs_unsupported", "reference chain is unsafe or excessive")
        _git(root, "check-ref-format", name)
        path = _git_path(root, "--git-path", name)
        # Per-worktree refs live in git_directory, ordinary refs in common_directory.
        anchors = [
            anchor
            for anchor in (git_directory, common_directory)
            if path == anchor / relative
        ]
        if not anchors:
            _fail("refresh_refs_unsupported", "reference storage is redirected")
        observed = _reference_file(anchors[0], relative, reservations)
        chain.append(RefreshReferenceObservation(name, observed))
        content = observed.content
    if (chain[-1].name if chain else None) != resolved:
        _fail("inputs_changed", "symbolic reference chain changed during observation")
    if content is None:
        # Loose absence must be backed by exactly one packed record. Git already
        # resolved HEAD; independently bind its raw (not peeled-tag) object ID.
        matches = []
        for line in (packed.content or b"").splitlines():
            oid, separator, name = line.partition(b" ")
            if separator and name == resolved.encode("utf-8"):
                matches.append(oid)
        if matches != [commit.encode("ascii")]:
            _fail("refresh_refs_unsupported", "packed reference custody is ambiguous")
    elif _oid(content.removesuffix(b"\n")) != commit:
        _fail("inputs_changed", "reference bytes differ from the observed commit")
    return tuple(chain), packed

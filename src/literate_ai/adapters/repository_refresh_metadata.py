"""Inert exact ref/manifest/lock transitions; no application or replay authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from literate_ai.projects import (
    DEFAULT_MAXIMUM_PROJECT_CONFIGURATION_BYTES,
    parse_project_configuration,
    serialize_project_configuration,
)

from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import (
    PreparedRepositoryRefresh,
    RefreshFileObservation,
    _local_integer_wire,
)
from .repository_refresh_worktrees import require_refresh_worktree_ownership


@dataclass(frozen=True, slots=True)
class RefreshMetadataTransition:
    kind: str
    repository: str
    name: str
    before: RefreshFileObservation
    prospective: bytes | None

    def to_dict(self):
        def identity(content):
            return None if content is None else hashlib.sha256(content).hexdigest()

        return {
            "kind": self.kind,
            "repository": self.repository,
            "name": self.name,
            "path": str(self.before.path),
            "before_signature": (
                None
                if self.before.signature is None
                else [_local_integer_wire(item) for item in self.before.signature]
            ),
            "before_sha256": identity(self.before.content),
            "prospective_sha256": identity(self.prospective),
            "operation": (
                "retain"
                if self.before.content == self.prospective
                else "remove"
                if self.prospective is None
                else "create"
                if self.before.content is None
                else "replace"
            ),
        }


def refresh_metadata_transitions(
    refresh: PreparedRepositoryRefresh,
) -> tuple[RefreshMetadataTransition, ...]:
    """Derive only data from custody; staging must separately guard its live owner.

    Preserve symbolic hops and packed bytes, proposing a loose terminal override
    only for changed attached HEADs. No-op targets preserve original spelling and
    absence. A changed root binding explicitly invalidates the old repository lock;
    it does not fabricate a prospective lock, receipt or child acceptance.
    """
    if not isinstance(refresh, PreparedRepositoryRefresh):
        raise TypeError("metadata transitions require typed refresh custody")
    require_refresh_worktree_ownership(refresh)
    definition = parse_project_configuration(refresh.manifest.content)
    if definition.repository_orchestration != refresh.authority.previous:
        raise OrchestrationInventoryError(
            "refresh_metadata_changed", "manifest differs from reviewed authority"
        )
    changed = refresh.authority.previous != refresh.authority.prospective
    manifest = refresh.manifest.content
    if changed:
        manifest = serialize_project_configuration(
            replace(definition, repository_orchestration=refresh.authority.prospective)
        )
    if len(manifest) > DEFAULT_MAXIMUM_PROJECT_CONFIGURATION_BYTES:
        raise OrchestrationInventoryError(
            "refresh_metadata_limit", "prospective manifest exceeds its byte bound"
        )
    transitions = {}

    def append(kind, repository, name, before, prospective):
        previous = transitions.get(before.path)
        if previous is not None:
            if previous.before != before or previous.prospective != prospective:
                raise OrchestrationInventoryError(
                    "refresh_shared_ref_conflict",
                    "observed worktrees require conflicting shared metadata states",
                )
            return
        transitions[before.path] = RefreshMetadataTransition(
            kind, repository, name, before, prospective
        )

    requested = {
        refresh.repository.root / target.path: target.commit
        for target in refresh.authority.request.targets
    }
    # Unselected observed worktrees and the root must retain their exact state.
    # Including them prevents an attached branch update from silently changing
    # another observed worktree that shares the same terminal reference.
    for observed in (refresh.root_git, *refresh.children):
        repository = observed.root.relative_to(refresh.repository.root).as_posix()
        target = requested.get(observed.root, observed.commit)
        content = (target + "\n").encode("ascii")
        if len(target) != len(observed.commit):
            raise OrchestrationInventoryError(
                "refresh_metadata_format", "target and checkout object formats differ"
            )
        attached = bool(observed.references)
        append(
            "head",
            repository,
            "HEAD",
            observed.head,
            content
            if not attached and target != observed.commit
            else observed.head.content,
        )
        for index, reference in enumerate(observed.references):
            append(
                "ref",
                repository,
                reference.name,
                reference.file,
                content
                if index == len(observed.references) - 1 and target != observed.commit
                else reference.file.content,
            )
        append(
            "packed-refs",
            repository,
            "packed-refs",
            observed.packed_references,
            observed.packed_references.content,
        )
    # Manifest is the final authority transition in the eventual transaction.
    append(
        "lock",
        ".",
        ".literate/repository.lock.json",
        refresh.repository_lock,
        None if changed else refresh.repository_lock.content,
    )
    append("manifest", ".", "literate.project.json", refresh.manifest, manifest)
    return tuple(transitions.values())

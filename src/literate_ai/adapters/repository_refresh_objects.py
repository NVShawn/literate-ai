"""Bind complete published object packs to live physical refresh custody."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy

from ._repository_pack_capture import RepositoryPackPolicy
from .repository_orchestration import OrchestrationInventoryError
from .repository_publication import (
    PublishedRepositoryPack,
    capture_published_repository_pack,
)
from .repository_refresh_files import PreparedRefreshFiles
from .repository_refresh_publication import _protected_roots

_CREATION_KEY = object()


class PreparedRefreshObjects:
    def __init__(self, key, files, packs):
        if key is not _CREATION_KEY:
            raise TypeError("refresh objects must be captured, not reconstructed")
        self.files = files
        self.packs = packs

    @property
    def identity(self):
        return canonical_identity(
            {
                "physical_custody": self.files.identity,
                "target_modes": [
                    {"path": path, "mode": mode}
                    for path, mode in self.files.target_modes
                ],
                "packs": [
                    {
                        "path": path,
                        "publication": captured.publication.identity,
                        "pack": hashlib.sha256(captured.objects.pack).hexdigest(),
                        "index": hashlib.sha256(captured.objects.index).hexdigest(),
                    }
                    for path, captured in self.packs
                ],
            }
        ).uri

    def require_current(self):
        self.files.require_current()

    def stage(self, *, metadata=False):
        from .repository_refresh_staging import stage_refresh_files

        return stage_refresh_files(self.files, objects=self, metadata=metadata)


def prepare_refresh_objects(files, *, pack_policy=None):
    if not isinstance(files, PreparedRefreshFiles):
        raise TypeError("object capture requires prepared physical custody")
    policy = RepositoryPackPolicy() if pack_policy is None else pack_policy
    if not isinstance(policy, RepositoryPackPolicy):
        raise TypeError("object capture requires a typed pack policy")
    files.require_current()
    reviewed = files._owner._prepared
    plans = dict(files.plans)
    modes = dict(files.target_modes)
    packs = []
    pack_bytes = index_bytes = count = 0
    for (path, endpoint), proof in zip(
        reviewed.endpoints, reviewed.observations, strict=True
    ):
        if modes[path] == "root-pin-only":
            continue
        files.require_current()
        try:
            captured = capture_published_repository_pack(
                endpoint,
                proof.commit,
                deadline_policy=reviewed.deadline_policy,
                protected_roots=_protected_roots(reviewed.refresh),
                tree_policy=RepositoryTreeCapturePolicy(),
                pack_policy=replace(
                    policy,
                    maximum_pack_bytes=max(1, policy.maximum_pack_bytes - pack_bytes),
                    maximum_index_bytes=max(
                        1, policy.maximum_index_bytes - index_bytes
                    ),
                    maximum_objects=max(1, policy.maximum_objects - count),
                ),
            )
        finally:
            files.require_current()
        if (
            not isinstance(captured, PublishedRepositoryPack)
            or captured.publication != proof
            or captured.tree != plans[path].prospective
        ):
            raise OrchestrationInventoryError(
                "refresh_objects_changed",
                "object export differs from reviewed publication or tree",
            )
        pack_bytes += len(captured.objects.pack)
        index_bytes += len(captured.objects.index)
        count += captured.objects.object_count
        if (
            pack_bytes > policy.maximum_pack_bytes
            or index_bytes > policy.maximum_index_bytes
            or count > policy.maximum_objects
        ):
            raise OrchestrationInventoryError(
                "refresh_objects_limit",
                "object exports exceed aggregate selection bounds",
            )
        packs.append((path, captured))
    files.require_current()
    return PreparedRefreshObjects(_CREATION_KEY, files, tuple(packs))

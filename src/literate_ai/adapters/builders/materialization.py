"""Filesystem adapter for an exact isolated artifact-materialization plan."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.contracts import ArtifactMaterializationPlan, BlobRef, ContentIdentity


class ArtifactMaterializationError(RuntimeError):
    """Exact source blobs could not be projected into a fresh execution root."""


@dataclass(frozen=True, slots=True)
class MaterializedArtifactRoot:
    plan_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    root: Path
    paths: tuple[str, ...]


BlobReader = Callable[[BlobRef], bytes]


def materialize_artifact_plan(
    plan: ArtifactMaterializationPlan,
    destination: Path,
    *,
    read_blob: BlobReader,
) -> MaterializedArtifactRoot:
    """Create one new root containing every and only exact blob named by ``plan``."""

    if not isinstance(plan, ArtifactMaterializationPlan):
        raise TypeError("materialization requires an ArtifactMaterializationPlan")
    if plan.native_sdk_input_identities:
        raise ArtifactMaterializationError(
            "native SDK inputs require producer-backed consumer materialization"
        )
    target = Path(destination)
    if target.exists() or target.is_symlink() or path_is_link_or_reparse(target):
        raise ArtifactMaterializationError(
            "artifact execution root must be absent before materialization"
        )
    parent = target.parent.resolve(strict=True)
    if path_is_link_or_reparse(parent) or not parent.is_dir():
        raise ArtifactMaterializationError(
            "artifact execution-root parent must be a regular directory"
        )
    target.mkdir(mode=0o700)
    try:
        for entry in plan.entries:
            content = read_blob(entry.blob)
            if not isinstance(content, bytes):
                raise ArtifactMaterializationError("blob reader must return bytes")
            if len(content) != entry.blob.size or (
                hashlib.sha256(content).hexdigest() != entry.blob.digest
            ):
                raise ArtifactMaterializationError(
                    f"artifact blob does not match {entry.blob.identity}"
                )
            relative = Path(*entry.path.split("/"))
            output = target / relative
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(
                path_is_link_or_reparse(parent_path)
                for parent_path in (target, *output.parents)
                if parent_path == target or parent_path.is_relative_to(target)
            ):
                raise ArtifactMaterializationError(
                    "artifact materialization encountered a link or reparse point"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(output, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        observed = tuple(
            sorted(
                path.relative_to(target).as_posix()
                for path in target.rglob("*")
                if path.is_file()
            )
        )
        expected = tuple(item.path for item in plan.entries)
        if observed != expected:
            raise ArtifactMaterializationError(
                "materialized root does not contain the exact planned path set"
            )
    except BaseException:
        shutil.rmtree(target)
        raise
    return MaterializedArtifactRoot(
        plan.identity, plan.source_tree_identity, target.resolve(strict=True), observed
    )


__all__ = [
    "ArtifactMaterializationError",
    "MaterializedArtifactRoot",
    "materialize_artifact_plan",
]

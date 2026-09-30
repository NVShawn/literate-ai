"""Provider-neutral linking, packaging, and package-result verification."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.artifacts import (
    ArtifactBuildGraph,
    ArtifactExport,
    ExactLinkPlan,
)
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.identity import ContentIdentity


class PackagingError(ValueError):
    """A link or package adapter contradicted exact package authority."""


PackageBlobReader = Callable[[BlobRef], bytes]


@dataclass(frozen=True, slots=True)
class LinkedArtifactClosure:
    graph_identity: ContentIdentity
    link_plan: ExactLinkPlan
    root: ArtifactExport
    artifacts: tuple[ArtifactExport, ...]


class PackageAdapter(Protocol):
    def package(
        self, plan: PackagePlan, *, read_blob: PackageBlobReader
    ) -> PackageResult: ...


def resolve_linked_artifact_closure(
    graph: ArtifactBuildGraph, root_artifact_identity: ContentIdentity
) -> LinkedArtifactClosure:
    """Resolve one graph-validated link plan without inventing linker semantics."""

    exports = {
        export.identity.uri: export
        for manifest in graph.manifests
        for export in manifest.exports
    }
    link = next(
        (
            item
            for item in graph.link_plans
            if item.root_artifact_identity == root_artifact_identity
        ),
        None,
    )
    if link is None:
        raise PackagingError("linked root is absent from the artifact graph")
    try:
        artifacts = tuple(
            exports[item.uri] for item in link.ordered_artifact_identities
        )
        root = exports[root_artifact_identity.uri]
    except KeyError as exc:
        raise PackagingError("link plan references an absent artifact") from exc
    return LinkedArtifactClosure(graph.identity, link, root, artifacts)


def verify_package_result(
    plan: PackagePlan,
    result: PackageResult,
    *,
    read_blob: PackageBlobReader,
) -> PackageResult:
    """Recheck a package result against its exact plan and immutable bytes."""

    exact = {
        "package plan": (result.package_plan_identity, plan.identity),
        "root Component": (
            result.root_component_revision,
            plan.root_component_revision,
        ),
        "Component lock": (
            result.component_lock_identity,
            plan.component_lock_identity,
        ),
        "target": (result.target_identity, plan.target_identity),
        "artifact graph": (
            result.artifact_graph_identity,
            plan.artifact_graph_identity,
        ),
        "packager": (result.packager_identity, plan.packager_identity),
    }
    for label, (actual, expected) in exact.items():
        if actual != expected:
            raise PackagingError(f"package result {label} identity mismatch")
    if result.package_kind is not plan.package_kind:
        raise PackagingError("package result kind differs from its exact plan")
    if result.entrypoints != plan.entrypoints:
        raise PackagingError("package result entrypoints differ from its exact plan")
    if result.runtime_requirements != plan.runtime_requirements:
        raise PackagingError(
            "package result runtime closure differs from its exact plan"
        )
    if (
        result.native_library_root != plan.native_library_root
        or result.native_library_layout != plan.native_library_layout
    ):
        raise PackagingError(
            "package result native library closure differs from its exact plan"
        )
    executable_paths = {item.path for item in plan.entrypoints}
    expected_files = {
        item.path: (
            item.role,
            item.kind,
            item.source_identity,
            item.target_identity,
            item.blob,
            item.executable or item.path in executable_paths,
        )
        for item in plan.inputs
    }
    actual_files = {
        item.path: (
            item.role,
            item.kind,
            item.source_identity,
            item.target_identity,
            item.blob,
            item.executable,
        )
        for item in result.files
    }
    if actual_files != expected_files:
        raise PackagingError(
            "package result logical files differ from the exact linked inputs"
        )
    input_files = {
        (
            item.path,
            item.role,
            item.kind,
            item.source_identity,
            item.target_identity,
            item.blob,
            item.executable,
        )
        for item in result.files
    }
    for item in result.artifacts:
        if item.kind is PackageFileKind.PACKAGE_OUTPUT:
            if (
                item.source_identity != plan.identity
                or item.target_identity != plan.target_identity
            ):
                raise PackagingError(
                    "outer package artifact does not identify its exact plan and target"
                )
        elif (
            item.path,
            item.role,
            item.kind,
            item.source_identity,
            item.target_identity,
            item.blob,
            item.executable,
        ) not in input_files:
            raise PackagingError(
                "package artifact is neither an exact input nor a plan-derived output"
            )
    for item in (*result.files, *result.artifacts):
        content = read_blob(item.blob)
        if not isinstance(content, bytes):
            raise PackagingError("package blob reader must return bytes")
        if len(content) != item.blob.size:
            raise PackagingError(f"package blob size mismatch: {item.blob.identity}")
        if hashlib.sha256(content).hexdigest() != item.blob.digest:
            raise PackagingError(f"package blob digest mismatch: {item.blob.identity}")
    return result


__all__ = [
    "LinkedArtifactClosure",
    "PackageAdapter",
    "PackageBlobReader",
    "PackagingError",
    "resolve_linked_artifact_closure",
    "verify_package_result",
]

from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_repository_lineage``."""


from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentReference,
    RepositoryParentSelection,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)

def reference(name: str, revision: str = "main") -> RepositoryParentReference:
    return RepositoryParentReference(f"https://example.test/{name}.git", revision)

def fixture() -> tuple[
    RepositoryParentSelection,
    RepositoryLineageNode,
    RepositoryLineageNode,
    RepositoryLineage,
]:
    root_ref = reference("root")
    child_ref = reference("child", "release")
    root = RepositoryLineageNode(
        "root",
        identity("1"),
        root_ref.repository_url,
        root_ref.requested_revision,
        "a" * 40,
        RepositoryParentSelection.root(),
        (),
    )
    child = RepositoryLineageNode(
        "child",
        identity("2"),
        child_ref.repository_url,
        child_ref.requested_revision,
        "b" * 40,
        RepositoryParentSelection.inherit((root_ref,)),
        (root.identity,),
    )
    selection = RepositoryParentSelection.inherit((child_ref,))
    return (
        selection,
        root,
        child,
        RepositoryLineage(selection, (root, child), (child.identity,)),
    )


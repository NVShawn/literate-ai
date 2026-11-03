"""Compose per-owner and aggregate Cargo input custody without execution rights.

The receiving transaction must independently select complete workspace/CARGO_HOME
and external-input scopes and retain live authority for every excluded output.
This module captures bytes; a root declaration never authorizes an exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.retained_cargo_tree_v2 import (
    RetainedCargoInputTreeV2,
    capture_retained_cargo_tree_v2,
)
from literate_ai.adapters.retained_project_inputs import _root
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_cargo_custody import RetainedCargoCustodyPolicy


@dataclass(frozen=True, slots=True)
class RetainedCargoInputRootV2:
    scope: str
    root: Path
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetainedCargoInputOwnersV2:
    policy: RetainedCargoCustodyPolicy
    trees: tuple[tuple[str, RetainedCargoInputTreeV2], ...]
    maximum_metadata_bytes: int

    def require_unchanged(self):
        for _, tree in self.trees:
            tree.require_unchanged()
        _require_aggregate(self.trees, self.policy)

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-input-owners@2",
                "policy": self.policy.identity.uri,
                "maximum_metadata_bytes": self.maximum_metadata_bytes,
                "trees": [
                    {"scope": scope, "tree": tree.identity.uri}
                    for scope, tree in self.trees
                ],
            }
        )

    @property
    def counts(self):
        return {
            "files": sum(tree.file_count for _, tree in self.trees),
            "entries": sum(tree.entry_count for _, tree in self.trees),
            "bytes": sum(tree.payload_bytes for _, tree in self.trees),
        }


def _require_aggregate(trees, policy):
    if (
        sum(tree.file_count for _, tree in trees) > policy.aggregate.maximum_files
        or sum(tree.entry_count for _, tree in trees) > policy.aggregate.maximum_entries
        or sum(tree.payload_bytes for _, tree in trees)
        > policy.aggregate.maximum_total_bytes
    ):
        raise ValueError("retained.cargo.v2-input-aggregate-limit")


def capture_retained_cargo_input_owners_v2(
    roots: tuple[RetainedCargoInputRootV2, ...],
    *,
    policy: RetainedCargoCustodyPolicy,
    maximum_metadata_bytes: int = 1024 * 1024 * 1024,
) -> RetainedCargoInputOwnersV2:
    """Require the exact reviewed owner set, complete trees and finite totals.

    Roots may nest only where the containing tree excludes that exact independently
    captured root. Bind-mounted aliases with the same physical root are refused.
    Every tree requires closed physical hard-link groups; bytes cannot be charged
    again through an undeclared cross-owner hard link. Regular payloads are hashed
    incrementally and discarded. No network, copying or cache filling occurs.
    """
    if type(policy) is not RetainedCargoCustodyPolicy:
        raise ValueError("retained.cargo.v2-input-policy-required")
    if (
        not isinstance(roots, tuple)
        or len(roots) != len(policy.owners)
        or any(type(item) is not RetainedCargoInputRootV2 for item in roots)
    ):
        raise ValueError("retained.cargo.v2-input-owners-invalid")
    scopes = tuple(item.scope for item in roots)
    if scopes != tuple(item.scope for item in policy.owners):
        raise ValueError("retained.cargo.v2-input-owner-set-mismatch")
    for item in roots:
        if (
            not isinstance(item.root, Path)
            or not isinstance(item.exclusions, tuple)
            or len(item.exclusions) > 128
            or any(not isinstance(path, str) for path in item.exclusions)
            or item.exclusions != tuple(sorted(set(item.exclusions)))
        ):
            raise ValueError("retained.cargo.v2-input-root-invalid")
        canonical_relative_posix_paths(item.exclusions, label="owned Cargo exclusions")
    baseline = 16 * 1024 * 1024
    if (
        type(maximum_metadata_bytes) is not int
        or not baseline * len(roots) <= maximum_metadata_bytes <= 16 * 1024**3
    ):
        raise ValueError("retained.cargo.v2-input-metadata-limit-invalid")
    # Split the finite aggregate allowance before traversing any source. Each
    # subtree budget includes the duplicate graph used by its revalidation.
    per_owner_metadata = maximum_metadata_bytes // len(roots)
    selected = tuple((item, _root(Path(item.root))) for item in roots)
    nodes = set()
    root_nodes = {}
    for item, root in selected:
        metadata = root.lstat()
        node = metadata.st_dev, metadata.st_ino
        if node in nodes:
            raise ValueError("retained.cargo.v2-input-ambiguous-root")
        nodes.add(node)
        root_nodes[item.scope] = node
        for other, containing in selected:
            if other is item:
                continue
            if root == containing:
                raise ValueError("retained.cargo.v2-input-ambiguous-root")
            if root.is_relative_to(containing):
                relative = root.relative_to(containing).as_posix()
                if relative not in other.exclusions:
                    raise ValueError("retained.cargo.v2-input-overlapping-owners")
    trees = []
    captured_nodes = set()
    for (item, root), owner in zip(selected, policy.owners, strict=True):
        tree = capture_retained_cargo_tree_v2(
            root,
            budget=owner.budget,
            exclusions=item.exclusions,
            maximum_metadata_bytes=per_owner_metadata,
        )
        if tree._physical[0][:2] != root_nodes[item.scope]:
            raise ValueError("retained.cargo.v2-input-root-changed")
        # Lexically unrelated roots can still alias through bind/nullfs mounts.
        # Closed hard-link groups alone cannot detect those duplicate payloads.
        physical = {tree._physical[0][:2]}
        physical.update(node[:2] for _, node in tree._physical[1])
        if not physical.isdisjoint(captured_nodes):
            raise ValueError("retained.cargo.v2-input-physical-owner-overlap")
        captured_nodes.update(physical)
        trees.append((item.scope, tree))
        _require_aggregate(trees, policy)
    result = RetainedCargoInputOwnersV2(policy, tuple(trees), maximum_metadata_bytes)
    result.require_unchanged()
    return result

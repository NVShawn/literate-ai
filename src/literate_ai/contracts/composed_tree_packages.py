"""Canonical selected-set geometry, never standalone or live PACKAGE authority."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from types import MappingProxyType

from .identity import ContentIdentity
from .native_sdk_trees import NativeSdkTree, _ResolutionBudget
from .retained_unit_partition import RetainedUnitPartition
from .tree_packages import _project_tree_members


@dataclass(frozen=True, slots=True)
class ComposedTreePackageProjection:
    """Verify complete closure before projecting an intentionally partial unit.

    A unit's result can contain an external hardlink or symlink; only this exact
    selected set supplies its closure. It must never pass as a standalone tree.
    Serialization/provider selection belongs to the versioned package-set plan.
    """

    partition: RetainedUnitPartition
    max_closure_steps: int
    max_closure_memory_bytes: int
    _entries: object = field(init=False, repr=False, compare=False)
    _payload_by_unit: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.partition, RetainedUnitPartition):
            raise TypeError("retained.unit.typed-partition-required")
        for value, maximum in (
            (self.max_closure_steps, 16_000_000),
            (self.max_closure_memory_bytes, 16 * 1024**3),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("retained.unit.closure-policy-out-of-bounds")
        tree = self.partition.tree
        # Charge before allocating the closure indexes, including path storage.
        footprint = 768 * tree.index.member_count + sum(
            4 * len(row.path.encode("utf-8")) for row in self.partition.ownership
        )
        footprint += sum(4 * len(path.encode("utf-8")) for path in tree.directories)
        footprint += 512 * len(self.partition.dependencies)
        if footprint > self.max_closure_memory_bytes:
            raise ValueError("retained.unit.closure-memory-policy-exceeded")
        owners = {row.path: row.unit_identity for row in self.partition.ownership}
        paths = tuple(owners)
        directories = set(tree.directories)
        files = {value.path for value in tree.files}
        aliases = {value.path: value for value in tree.aliases}
        links = {
            (edge.source_unit_identity, edge.target_unit_identity)
            for edge in self.partition.dependencies
            if edge.kind == "tree-link"
        }
        budget = _ResolutionBudget()
        steps = 0
        directory_owners = {}

        def charge():
            nonlocal steps
            steps += 1
            if steps > self.max_closure_steps:
                raise ValueError("retained.unit.closure-work-policy-exceeded")

        for alias in tree.aliases:
            source = owners[alias.path]

            def require_owner(path, source=source):
                charge()
                target = owners.get(path)
                if (
                    target is not None
                    and target != source
                    and (source, target) not in links
                ):
                    raise ValueError(
                        "retained.unit.explicit-tree-link-dependency-required"
                    )

            resolved = NativeSdkTree._resolve(
                alias.path, directories, files, aliases, budget, observe=require_owner
            )
            if resolved not in directories and resolved != ".":
                continue
            if resolved not in directory_owners:
                selected = set()
                prefix = "" if resolved == "." else resolved + "/"
                footprint += 128 + len(prefix.encode("utf-8"))
                index = bisect_left(paths, prefix)
                while index < len(paths) and paths[index].startswith(prefix):
                    charge()
                    owner = owners[paths[index]]
                    if owner not in selected:
                        footprint += 128
                        if footprint > self.max_closure_memory_bytes:
                            raise ValueError(
                                "retained.unit.closure-memory-policy-exceeded"
                            )
                        selected.add(owner)
                    index += 1
                if footprint > self.max_closure_memory_bytes:
                    raise ValueError("retained.unit.closure-memory-policy-exceeded")
                directory_owners[resolved] = selected
            for target in directory_owners[resolved]:
                charge()
                if target != source and (source, target) not in links:
                    raise ValueError(
                        "retained.unit.explicit-directory-closure-required"
                    )
        payload_by_unit = {value.identity: [] for value in self.partition.definitions}
        for row in self.partition.ownership:
            payload_by_unit[row.unit_identity].append(row.path)
        object.__setattr__(
            self, "_entries", MappingProxyType(_project_tree_members(tree, ""))
        )
        object.__setattr__(
            self,
            "_payload_by_unit",
            MappingProxyType(
                {key: tuple(value) for key, value in payload_by_unit.items()}
            ),
        )

    def members_for(self, unit_identity: ContentIdentity):
        """Project one unit plus shared ancestors, with no copied dependency bytes."""
        if (
            not isinstance(unit_identity, ContentIdentity)
            or unit_identity not in self._payload_by_unit
        ):
            raise ValueError("retained.unit.selected-unit-required")
        selected = set()
        steps = 0
        limits = self.partition.per_unit_limits
        for path in self._payload_by_unit[unit_identity]:
            selected.add(path)
            parent = path.rpartition("/")[0]
            while parent:
                steps += 1
                if steps > self.max_closure_steps:
                    raise ValueError("retained.unit.closure-work-policy-exceeded")
                selected.add(parent)
                parent = parent.rpartition("/")[0]
            if len(selected) > limits.max_entries:
                raise ValueError("retained.unit.structural-entry-bound-exceeded")
        return tuple(self._entries[path] for path in sorted(selected))

    @property
    def members(self):
        """The entire selected namespace, for independent physical verification."""
        return tuple(self._entries[path] for path in sorted(self._entries))

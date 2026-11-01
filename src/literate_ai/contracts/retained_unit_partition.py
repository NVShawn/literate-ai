"""Mechanical output partition and typed DAG checks; no admission or link proof.

This intermediate value is not a package-set plan. The composed projection must
add complete link/ancestor closure and live provenance verification before native
production. There is deliberately no monolithic graph JSON encoder here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .identity import canonical_json_bytes
from .native_tree_shards import ComposedNativeTree
from .retained_project import RetainedProjectLimits
from .retained_unit_packages import (
    RetainedUnitDependency,
    RetainedUnitOwnership,
    RetainedUnitPackageDefinition,
)


@dataclass(frozen=True, slots=True)
class RetainedUnitPartition:
    tree: ComposedNativeTree
    definitions: tuple[RetainedUnitPackageDefinition, ...]
    ownership: tuple[RetainedUnitOwnership, ...]
    dependencies: tuple[RetainedUnitDependency, ...]
    per_unit_limits: RetainedProjectLimits
    max_units: int
    max_dependencies: int
    max_metadata_bytes: int

    def __post_init__(self):
        if not isinstance(self.tree, ComposedNativeTree) or not isinstance(
            self.per_unit_limits, RetainedProjectLimits
        ):
            raise TypeError("retained.unit.typed-partition-context-required")
        for value, maximum in (
            (self.max_units, 1_000_000),
            (self.max_dependencies, 4_000_000),
            (self.max_metadata_bytes, 512 * 1024 * 1024),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("retained.unit.partition-policy-out-of-bounds")
        for values, kind, limit in (
            (self.definitions, RetainedUnitPackageDefinition, self.max_units),
            (self.ownership, RetainedUnitOwnership, self.tree.index.member_count),
            (self.dependencies, RetainedUnitDependency, self.max_dependencies),
        ):
            if type(values) is not tuple or len(values) > limit:
                raise ValueError("retained.unit.partition-row-bound-exceeded")
            if any(not isinstance(value, kind) for value in values):
                raise TypeError("retained.unit.typed-partition-rows-required")
        # Account serialized row space before constructing graph/namespace maps.
        # Decoders separately bound raw shards and process-memory admission.
        size = 0
        for group in (self.definitions, self.ownership, self.dependencies):
            for value in group:
                size += len(canonical_json_bytes(value.to_dict())) + 1
                if size > self.max_metadata_bytes:
                    raise ValueError("retained.unit.partition-metadata-bound-exceeded")
        identities = tuple(value.identity for value in self.definitions)
        if not identities or tuple(value.uri for value in identities) != tuple(
            sorted({value.uri for value in identities})
        ):
            raise ValueError("retained.unit.unique-canonical-definitions-required")
        if len({value.unit_name for value in self.definitions}) != len(identities):
            raise ValueError("retained.unit.duplicate-name")
        definitions = dict(zip(identities, self.definitions, strict=True))
        common = (
            "retained_plan_identity",
            "receipt_identity",
            "retained_source_identity",
            "payload_identity",
            "target_identity",
            "source_bom_identity",
            "resolved_bom_identity",
            "recipe_identity",
        )
        first = self.definitions[0]
        if any(
            any(getattr(value, name) != getattr(first, name) for name in common)
            for value in self.definitions
        ):
            raise ValueError("retained.unit.inconsistent-retained-context")
        if first.payload_identity != self.tree.identity:
            raise ValueError("retained.unit.payload-tree-binding-mismatch")

        # A directory is payload only when physically empty. Nonempty ancestors
        # are shared canonical structure, never duplicate payload ownership.
        nonempty = set()
        for path in (
            *self.tree.directories,
            *(v.path for v in self.tree.files),
            *(v.path for v in self.tree.aliases),
        ):
            parent = path.rpartition("/")[0]
            if parent:
                nonempty.add(parent)
        payload = {value.path: ("file", value.blob.size) for value in self.tree.files}
        payload.update(
            (
                value.path,
                (
                    value.kind,
                    len(value.target.encode("utf-8")) if value.kind == "symlink" else 0,
                ),
            )
            for value in self.tree.aliases
        )
        payload.update(
            (path, ("empty-directory", 0))
            for path in self.tree.directories
            if path not in nonempty
        )
        paths = tuple(value.path for value in self.ownership)
        if paths != tuple(sorted(set(paths))) or set(paths) != set(payload):
            raise ValueError("retained.unit.exact-output-partition-required")
        totals = {identity: [0, 0] for identity in identities}
        for row in self.ownership:
            if row.unit_identity not in definitions:
                raise ValueError("retained.unit.unregistered-owner")
            kind, size = payload[row.path]
            if row.kind != kind:
                raise ValueError("retained.unit.output-kind-mismatch")
            count_total = totals[row.unit_identity]
            count_total[0] += 1
            count_total[1] += size
            if (
                count_total[0] > self.per_unit_limits.max_entries
                or count_total[1] > self.per_unit_limits.max_total_bytes
                or size > self.per_unit_limits.max_file_bytes
            ):
                raise ValueError("retained.unit.per-unit-payload-bound-exceeded")
        if any(count == 0 for count, _ in totals.values()):
            raise ValueError("retained.unit.unused-definition")

        edge_keys = tuple(
            (value.source_unit_identity.uri, value.target_unit_identity.uri, value.kind)
            for value in self.dependencies
        )
        if edge_keys != tuple(sorted(set(edge_keys))):
            raise ValueError("retained.unit.unique-canonical-dependencies-required")
        for edge in self.dependencies:
            if (
                edge.source_unit_identity not in definitions
                or edge.target_unit_identity not in definitions
            ):
                raise ValueError("retained.unit.unselected-dependency")
            target = definitions[edge.target_unit_identity]
            if edge.target_identity != target.target_identity:
                raise ValueError("retained.unit.dependency-target-mismatch")
            if edge.kind != "tree-link" and (
                edge.required_abi != target.abi
                or edge.required_version != target.version
            ):
                raise ValueError("retained.unit.dependency-abi-version-mismatch")
            if edge.kind == "tree-link" and (
                edge.required_abi not in (None, target.abi)
                or edge.required_version not in (None, target.version)
            ):
                raise ValueError("retained.unit.dependency-abi-version-mismatch")
        self._order(definitions, self.dependencies)

    @staticmethod
    def _order(definitions, dependencies):
        # One ordering edge for each unit pair even if it has multiple types.
        definitions = tuple(definitions)
        outgoing = {identity: set() for identity in definitions}
        pending = dict.fromkeys(definitions, 0)
        for edge in dependencies:
            source, target = edge.source_unit_identity, edge.target_unit_identity
            if source not in outgoing[target]:
                outgoing[target].add(source)
                pending[source] += 1
        ready = deque(identity for identity, count in pending.items() if count == 0)
        result = []
        while ready:
            identity = ready.popleft()
            result.append(identity)
            for dependent in sorted(outgoing[identity], key=lambda item: item.uri):
                pending[dependent] -= 1
                if not pending[dependent]:
                    ready.append(dependent)
        if len(result) != len(definitions):
            raise ValueError("retained.unit.dependency-cycle")
        return tuple(result)

    def build_order(self):
        """Only build-input edges impose compilation barriers."""
        return self._order(
            (value.identity for value in self.definitions),
            (edge for edge in self.dependencies if edge.kind == "build-input"),
        )

    def verification_order(self):
        """Verify selected dependency artifacts before their dependent archives."""
        return self._order(
            (value.identity for value in self.definitions), self.dependencies
        )

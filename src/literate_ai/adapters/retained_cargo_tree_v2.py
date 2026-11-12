"""Streaming, closed-topology Cargo input custody without payload copies.

Exclusions require separate live scratch/cache ownership. This reader grants no
right to exclude a path, execute inputs, materialize packages or admit a receipt.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai.adapters.retained_project_inputs import (
    _directory_access,
    _inventory,
    _link_bytes,
    _root,
    _stamp,
    _stream,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.native_sdk_trees import (
    NativeSdkAlias,
    NativeSdkTree,
    _ResolutionBudget,
)
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_cargo_custody import RetainedCargoInputBudget


def _node(value):
    if stat.S_ISDIR(value.st_mode):
        # Writes below separately owned excluded outputs may change timestamps,
        # but may not replace a captured physical directory or its permissions.
        return value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid
    return (*_stamp(value), value.st_nlink, value.st_uid, value.st_gid)


@dataclass(frozen=True, slots=True)
class RetainedCargoInputTreeV2:
    root: Path
    budget: RetainedCargoInputBudget
    exclusions: tuple[str, ...]
    directories: tuple[str, ...]
    files: tuple[tuple[str, BlobRef, bool], ...]
    aliases: tuple[NativeSdkAlias, ...]
    maximum_metadata_bytes: int
    _physical: tuple = field(repr=False, compare=False)

    @property
    def file_count(self):
        return len(self.files)

    @property
    def entry_count(self):
        return len(self.directories) + len(self.files) + len(self.aliases)

    @property
    def payload_bytes(self):
        return sum(blob.size for _, blob, _ in self.files) + sum(
            len(alias.target.encode("utf-8"))
            for alias in self.aliases
            if alias.kind == "symlink"
        )

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-input-tree@2",
                "budget": self.budget.to_dict(),
                "maximum_metadata_bytes": self.maximum_metadata_bytes,
                "exclusions": list(self.exclusions),
                "directories": list(self.directories),
                "files": [
                    {"path": name, "blob": blob.to_dict(), "executable": executable}
                    for name, blob, executable in self.files
                ],
                "aliases": [x.to_dict() for x in self.aliases],
            }
        )

    def require_unchanged(self):
        current = capture_retained_cargo_tree_v2(
            self.root,
            budget=self.budget,
            exclusions=self.exclusions,
            maximum_metadata_bytes=self.maximum_metadata_bytes,
        )
        # Compare every freshly observed field directly. Re-encoding both
        # complete immutable records adds no custody check; current capture has
        # still streamed/hash-checked every payload and closed its namespace.
        if self._physical != current._physical or self != current:
            raise ValueError("retained.cargo.v2-input-custody-changed")


def capture_retained_cargo_tree_v2(
    root: Path,
    *,
    budget: RetainedCargoInputBudget,
    exclusions: tuple[str, ...] = (),
    maximum_metadata_bytes: int = 1024 * 1024 * 1024,
) -> RetainedCargoInputTreeV2:
    """Hash physical regular payloads once, with bounded no-follow buffers.

    Internal links reuse canonical SDK topology rules. Complete hard-link groups
    are required; their payload is charged once and other names remain aliases.
    Only metadata, digests and link text are retained. No cache is copied or filled.
    The surrounding owner must bind this tree to its exact reviewed input scope
    and charge per-owner plus aggregate policy limits before executing anything.
    """
    if type(budget) is not RetainedCargoInputBudget:
        raise ValueError("retained.cargo.v2-input-budget-required")
    if (
        type(maximum_metadata_bytes) is not int
        or not 16 * 1024 * 1024 <= maximum_metadata_bytes <= 16 * 1024**3
    ):
        raise ValueError("retained.cargo.v2-input-metadata-limit-invalid")
    if (
        not isinstance(exclusions, tuple)
        or len(exclusions) > 128
        or exclusions != tuple(sorted(set(exclusions)))
    ):
        raise ValueError("retained.cargo.v2-input-exclusions-invalid")
    canonical_relative_posix_paths(exclusions, label="owned Cargo exclusions")
    root = _root(Path(root))
    excluded = frozenset(exclusions)
    with _directory_access(root) as access:

        def metadata(name):
            return (
                access.metadata(root / name)
                if access is not None
                else (root / name).lstat()
            )

        def inventory(*, initial_nodes=None):
            descriptor = None
            if access is not None:
                descriptor = os.open(
                    ".",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=access.directory(())[0],
                )
            roots = []
            root_bytes = 0
            try:
                with os.scandir(
                    descriptor if descriptor is not None else root
                ) as entries:
                    for entry in entries:
                        if len(roots) >= budget.maximum_entries + len(exclusions):
                            raise ValueError("retained.cargo.v2-input-entry-limit")
                        root_bytes += 128 + 32 * len(entry.name.encode("utf-8"))
                        if root_bytes + 16 * 1024 * 1024 > maximum_metadata_bytes:
                            raise ValueError("retained.cargo.v2-input-metadata-limit")
                        roots.append(entry.name)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            included = 0
            metadata_bytes = 16 * 1024 * 1024 + root_bytes
            charged_links = {}

            def observe(name, node):
                nonlocal included, metadata_bytes
                if name not in excluded:
                    included += 1
                    if included > budget.maximum_entries:
                        raise ValueError("retained.cargo.v2-input-entry-limit")
                    # Charge before retention. Covers duplicate capture graphs
                    # during revalidation plus dict/list/JSON identity temporaries;
                    # symbolic text is additionally charged before it is read.
                    metadata_bytes += 2048 + 32 * len(canonical_json_bytes(name))
                    if stat.S_ISLNK(node.st_mode):
                        if not 0 <= node.st_size <= 4096:
                            raise ValueError("retained.cargo.v2-input-link-limit")
                        metadata_bytes += 64 * node.st_size
                    if metadata_bytes > maximum_metadata_bytes:
                        raise ValueError("retained.cargo.v2-input-metadata-limit")
                    if stat.S_ISLNK(node.st_mode):
                        charged_links[name] = _node(node)
                    if initial_nodes is not None:
                        initial_nodes[name] = node

            observed = _inventory(
                root,
                tuple(sorted(roots)),
                budget.maximum_entries + len(exclusions),
                allow_internal_symlinks=True,
                access=access,
                observe=observe,
                excluded_roots=excluded,
            )
            return (*observed, charged_links)

        root_node = _node(root.lstat())
        before = {}
        names, directories, charged_links = inventory(initial_nodes=before)
        # These are the same no-follow observations that admitted each initial
        # census entry. Keep them across streaming rather than replacing them
        # with a redundant later pass that could hide an intervening write.
        if set(before) != set((*directories, *names)):
            raise ValueError("retained.cargo.v2-input-membership-changed")
        # Reuse portable case/normalization policy without imposing the standalone
        # SDK tree's smaller member ceiling on a reviewed Cargo custody budget.
        keys = set()
        for name in before:
            key = unicodedata.normalize("NFC", name.casefold())
            if key in keys:
                raise ValueError("retained.cargo.v2-input-portable-alias")
            keys.add(key)
        groups = defaultdict(list)
        aliases = []
        alias_bytes = 0
        for name in names:
            node = before[name]
            if stat.S_ISLNK(node.st_mode):
                if charged_links.get(name) != _node(node):
                    raise ValueError("retained.cargo.v2-input-link-changed")
                target = _link_bytes(root, name, 4096, access)
                if len(target) != node.st_size or _node(metadata(name)) != _node(node):
                    raise ValueError("retained.cargo.v2-input-link-changed")
                alias_bytes += len(target)
                if alias_bytes > budget.maximum_total_bytes:
                    raise ValueError("retained.cargo.v2-input-total-limit")
                aliases.append(NativeSdkAlias(name, target.decode("utf-8")))
            elif stat.S_ISREG(node.st_mode):
                if node.st_size > budget.maximum_file_bytes:
                    raise ValueError("retained.cargo.v2-input-file-limit")
                key = node.st_dev, node.st_ino
                if key not in groups and len(groups) >= budget.maximum_files:
                    raise ValueError("retained.cargo.v2-input-file-count-limit")
                groups[key].append(name)
            else:
                raise ValueError("retained.cargo.v2-input-type-unsafe")
        total = alias_bytes
        owners = []
        for group in groups.values():
            owner = min(group)
            node = before[owner]
            if any(
                before[name].st_nlink != len(group)
                or _stamp(before[name]) != _stamp(node)
                for name in group
            ):
                raise ValueError("retained.cargo.v2-input-hardlink-unowned")
            total += node.st_size
            if total > budget.maximum_total_bytes:
                raise ValueError("retained.cargo.v2-input-total-limit")
            owners.append(owner)
            aliases.extend(
                NativeSdkAlias(name, owner, "hardlink")
                for name in group
                if name != owner
            )
        directory_set, file_set = set(directories), set(owners)
        alias_map = {x.path: x for x in aliases}
        resolution = _ResolutionBudget(
            remaining=max(1048576, budget.maximum_entries * 128)
        )
        for alias in aliases:
            NativeSdkTree._resolve(
                alias.path, directory_set, file_set, alias_map, resolution
            )
        files = []
        for name in sorted(owners):
            size, mode, digest = _stream(
                root, name, budget.maximum_file_bytes, access=access
            )
            if size != before[name].st_size:
                raise ValueError("retained.cargo.v2-input-size-changed")
            files.append((name, BlobRef(digest, size), bool(mode & 0o111)))
        if (
            inventory() != (names, directories, charged_links)
            or _node(root.lstat()) != root_node
        ):
            raise ValueError("retained.cargo.v2-input-membership-changed")
        physical = tuple((name, _node(before[name])) for name in sorted(before))
        if any(_node(metadata(name)) != node for name, node in physical):
            raise ValueError("retained.cargo.v2-input-physical-custody-changed")
        return RetainedCargoInputTreeV2(
            root,
            budget,
            exclusions,
            directories,
            tuple(files),
            tuple(sorted(aliases, key=lambda x: x.path)),
            maximum_metadata_bytes,
            (root_node, physical),
        )

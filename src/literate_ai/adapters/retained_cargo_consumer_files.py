"""Bounded local consumer custody; external Cargo inputs need separate authority."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_cargo_materialization import (
    RetainedCargoMaterialization,
)
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.cache_directories import resolve_cache_directories
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.projects import PROJECT_FILENAME, PinnedInputClosure


def _observe(root, exclusions, maximum_entries, *, closed_hardlinks=False):
    require_safe_directory(root)
    directories, files = [], []
    pending = [root]
    count = 0
    links = {}
    while pending:
        current = pending.pop()
        relative = current.relative_to(root).as_posix()
        directories.append((relative, directory_node(current)))
        names = []
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > maximum_entries:
                    raise ValueError("retained.consumer.entry-limit")
                names.append(entry.name)
                path = current / entry.name
                relative = path.relative_to(root).as_posix()
                # Do not use Windows' incomplete cached DirEntry stat record.
                node = path.lstat()
                if stat_is_link_or_reparse(node):
                    raise ValueError("retained.consumer.link-unsafe")
                if relative in exclusions:
                    if not stat.S_ISDIR(node.st_mode) and not (
                        relative == ".git"
                        and stat.S_ISREG(node.st_mode)
                        and node.st_nlink == 1
                    ):
                        raise ValueError("retained.consumer.excluded-root-unsafe")
                    continue
                if stat.S_ISDIR(node.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(node.st_mode) and (
                    node.st_nlink == 1 or closed_hardlinks
                ):
                    files.append((relative, _signature(node)))
                    if closed_hardlinks:
                        links.setdefault((node.st_dev, node.st_ino), []).append(
                            node.st_nlink
                        )
                else:
                    raise ValueError("retained.consumer.file-unsafe")
        canonical_relative_posix_paths(
            names, label="retained consumer directory entries"
        )
    canonical_relative_posix_paths(
        [name for name, _ in files],
        label="retained consumer paths",
    )
    if any(any(n != len(group) for n in group) for group in links.values()):
        raise ValueError("retained.consumer.external-hardlink")
    return tuple(sorted(directories)), tuple(sorted(files))


@dataclass(frozen=True, slots=True)
class RetainedCargoConsumerFiles:
    root: Path
    exclusions: tuple[str, ...]
    directories: tuple[tuple[str, tuple[int, int, int]], ...]
    nodes: tuple[tuple[str, tuple[int, ...]], ...]
    maximum_entries: int
    _closure: PinnedInputClosure = field(repr=False, compare=False)

    def current_identity(self) -> ContentIdentity:
        self._closure.require_unchanged()
        if _observe(self.root, self.exclusions, self.maximum_entries) != (
            self.directories,
            self.nodes,
        ):
            raise ValueError("retained.consumer.custody-changed")
        self._closure.require_unchanged()
        return canonical_identity(
            {
                "files": self._closure.identity,
                "directories": [name for name, _ in self.directories],
                "exclusions": list(self.exclusions),
            }
        )


def read_retained_cargo_consumer_files(
    materialized: RetainedCargoMaterialization,
    *,
    environment=None,
    maximum_entries: int = 10000,
    maximum_file_bytes: int = 16 * 1024 * 1024,
    maximum_total_bytes: int = 64 * 1024 * 1024,
) -> RetainedCargoConsumerFiles:
    """Capture local inputs without pruning arbitrary build-like directory names.

    The explicit environment must match the execution's cache configuration.
    Generated packages remain under materializer custody. Git metadata, declared
    disposable caches, reviewed Cargo output and reservation metadata are excluded.
    This is no claim about external Cargo homes, tool inputs, or positive receipts.
    """
    if not isinstance(materialized, RetainedCargoMaterialization):
        raise ValueError("retained.consumer.materialization-required")
    if (
        any(
            type(n) is not int or n < 1
            for n in (maximum_entries, maximum_file_bytes, maximum_total_bytes)
        )
        or maximum_file_bytes > maximum_total_bytes
    ):
        raise ValueError("retained.consumer.limits-invalid")
    materialized.require_unchanged()
    root = materialized._files.project.root
    plan = materialized.plan
    caches = resolve_cache_directories(root, environment=environment)
    exclusions = {".git", ".litai-locks"}
    for path in (
        caches.build_dir,
        caches.obj_dir,
        root / plan.workspace_root / plan.graph.output_directory,
    ):
        if path.is_relative_to(root):
            exclusions.add(path.relative_to(root).as_posix())
    destinations = tuple(
        PurePosixPath(p) for _, p in materialized._files.binding.destinations
    )
    required = {PROJECT_FILENAME, ".cargo/config", ".cargo/config.toml"}
    workspace = PurePosixPath(plan.workspace_root)
    for directory in (workspace, *workspace.parents):
        required.update(
            str(directory / ".cargo" / name) for name in ("config", "config.toml")
        )
    for path in (
        *(item.path for item in plan.manifests),
        *(t.source for p in plan.graph.packages for t in p.targets),
    ):
        relative = PurePosixPath(plan.workspace_root) / path
        if not any(relative.is_relative_to(p) for p in destinations):
            required.add(str(relative))
    for excluded in exclusions:
        if any(PurePosixPath(p).is_relative_to(excluded) for p in required):
            raise ValueError("retained.consumer.exclusion-hides-input")
    exclusions = tuple(sorted(exclusions))
    before = _observe(root, exclusions, maximum_entries)
    closure = PinnedInputClosure(
        maximum_files=maximum_entries,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    for relative, _ in before[1]:
        closure.pin(root / relative, boundary=root, label=relative)
    result = RetainedCargoConsumerFiles(
        root, exclusions, *before, maximum_entries, closure
    )
    result.current_identity()
    materialized.require_unchanged()
    return result

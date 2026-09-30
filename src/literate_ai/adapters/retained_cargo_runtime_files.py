"""Custody for generated and compiler runtime search directories."""

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PinnedInputClosure


def _directory_state(root, allow_file_aliases=False):
    require_safe_directory(root)
    entries = []
    with os.scandir(root) as stream:
        for entry in stream:
            if len(entries) >= 10000:
                raise ValueError("retained.runtime.entry-limit")
            node = (root / entry.name).lstat()
            if stat.S_ISLNK(node.st_mode) and allow_file_aliases:
                pass
            elif stat_is_link_or_reparse(node) or not (
                stat.S_ISREG(node.st_mode) or stat.S_ISDIR(node.st_mode)
            ):
                raise ValueError("retained.runtime.entry-unsafe")
            entries.append((entry.name, _signature(node)))
    return directory_node(root), tuple(sorted(entries))


def _file_alias(path):
    """Resolve at most 64 file links through ordinary, guarded directories."""
    links = []
    seen = set()
    while True:
        require_safe_directory(path.parent)
        node = path.lstat()
        if stat.S_ISREG(node.st_mode) and not stat_is_link_or_reparse(node):
            return (
                path,
                tuple(links),
                (
                    _signature(node),
                    tuple((p, directory_node(p)) for p in path.parents),
                ),
            )
        if not stat.S_ISLNK(node.st_mode) or path in seen or len(links) >= 64:
            raise ValueError("retained.runtime.alias-unsafe")
        seen.add(path)
        target = os.readlink(path)
        if not target or len(target) > 4096 or "\x00" in target:
            raise ValueError("retained.runtime.alias-unsafe")
        selected = Path(target)
        if selected.drive and not selected.is_absolute():
            raise ValueError("retained.runtime.alias-unsafe")
        # Permit distro links such as ../../../lib/libstd.so, without collapsing
        # an interior traversal that could cross an unobserved directory alias.
        parts = selected.parts[1:] if selected.is_absolute() else selected.parts
        ordinary = False
        for part in parts:
            if part == ".." and (ordinary or selected.is_absolute()):
                raise ValueError("retained.runtime.alias-unsafe")
            ordinary = ordinary or part != ".."
        parents = tuple((p, directory_node(p)) for p in path.parents)
        links.append((path, target, _signature(node), parents))
        path = Path(os.path.abspath(path.parent / selected))


@dataclass(frozen=True)
class RetainedCargoRuntimeFiles:
    roots: tuple[Path, ...]
    states: tuple
    closure: PinnedInputClosure
    aliases: tuple = ()
    file_alias_roots: tuple[Path, ...] = ()

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-runtime-files@1",
                "roots": [str(p) for p in self.roots],
                "files": self.closure.identity,
                **(
                    {
                        "aliases": [
                            {
                                "path": str(path),
                                "resolved": str(resolved),
                                "links": [
                                    [str(p), target] for p, target, _, _ in links
                                ],
                            }
                            for path, resolved, links, _ in self.aliases
                        ]
                    }
                    if self.aliases
                    else {}
                ),
            }
        )

    def require_unchanged(self):
        self.closure.require_unchanged()
        if tuple(
            _directory_state(root, root in self.file_alias_roots) for root in self.roots
        ) != self.states or any(
            _file_alias(path) != (resolved, links, target_state)
            for path, resolved, links, target_state in self.aliases
        ):
            raise ValueError("retained.runtime.files-changed")
        self.closure.require_unchanged()

    def environment(self, inherited):
        key = (
            "PATH"
            if os.name == "nt"
            else "DYLD_FALLBACK_LIBRARY_PATH"
            if sys.platform == "darwin"
            else "LIBPATH"
            if sys.platform.startswith("aix")
            else "LD_LIBRARY_PATH"
        )
        value = os.pathsep.join(map(str, self.roots))
        if inherited.get(key):
            value += os.pathsep + inherited[key]
        return ((key, value),)


def capture_retained_cargo_runtime_files(roots, *, file_alias_roots=()):
    """Capture every immediate file in each selected loader directory.

    Subdirectories are observed as directory entries; separately selected search
    directories capture their own files. Existing caller search paths and system
    loader libraries still require the host/external-input qualification boundary.
    Compiler callers may explicitly capture bounded file aliases and their external
    target bytes. Directory aliases and non-regular targets remain forbidden.
    """
    roots = tuple(dict.fromkeys(roots))
    if (
        type(file_alias_roots) is not tuple
        or any(
            not isinstance(root, Path) or root not in roots for root in file_alias_roots
        )
        or not roots
        or len(roots) > 128
        or any(
            not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts
            for root in roots
        )
    ):
        raise ValueError("retained.runtime.roots-invalid")
    states = []
    entries = 0
    for root in roots:
        state = _directory_state(root, root in file_alias_roots)
        entries += len(state[1])
        if entries > 10000:
            raise ValueError("retained.runtime.entry-limit")
        states.append(state)
    states = tuple(states)
    closure = PinnedInputClosure(
        maximum_files=10000,
        maximum_file_bytes=128 * 1024 * 1024,
        maximum_total_bytes=512 * 1024 * 1024,
    )
    aliases = []
    for index, (root, state) in enumerate(zip(roots, states, strict=True)):
        for name, _node in state[1]:
            path = root / name
            if stat.S_ISREG(path.lstat().st_mode):
                closure.pin(path, boundary=root, label=f"runtime-{index}/{name}")
            elif path.is_symlink():
                resolved, links, target_state = _file_alias(path)
                entries += (
                    1
                    + len(target_state[1])
                    + sum(1 + len(parents) for _, _, _, parents in links)
                )
                if entries > 10000:
                    raise ValueError("retained.runtime.entry-limit")
                closure.pin(
                    resolved,
                    boundary=Path(resolved.anchor),
                    label=f"runtime-{index}/{name}",
                )
                aliases.append((path, resolved, links, target_state))
    result = RetainedCargoRuntimeFiles(
        roots, states, closure, tuple(aliases), file_alias_roots
    )
    result.require_unchanged()
    return result

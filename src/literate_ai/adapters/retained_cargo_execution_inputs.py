"""Current consumer files, provisioned Cargo sources and merged configuration.

This captures concrete inputs, not complete arbitrary build-script dependencies
or positive test acceptance. No tools run and no file is written by this reader.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_cargo_consumer_files import (
    RetainedCargoConsumerFiles,
    _observe,
    read_retained_cargo_consumer_files,
)
from literate_ai.adapters.retained_cargo_materialization import (
    RetainedCargoMaterialization,
)
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from literate_ai.projects import PinnedInputClosure


def _state(path: Path):
    parent = path.parent
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError("retained.inputs.path-unsafe")
        parent = parent.parent
    owner = directory_node(parent)
    try:
        node = path.lstat()
    except FileNotFoundError:
        return (parent, owner, None)
    if stat_is_link_or_reparse(node) or not (
        stat.S_ISDIR(node.st_mode)
        or (stat.S_ISREG(node.st_mode) and node.st_nlink == 1)
    ):
        raise ValueError("retained.inputs.path-unsafe")
    require_safe_directory(path.parent)
    return (
        parent,
        owner,
        directory_node(path) if stat.S_ISDIR(node.st_mode) else _signature(node),
    )


def _source_tree(label: str, root: Path, maximum_entries: int):
    directories, files = _observe(
        root, (), maximum_entries, closed_hardlinks=label == "git"
    )
    if label in {"git", "registry"}:
        # Older Cargo rewrites this top-level backup marker on every access.
        # Its bytes remain pinned in _closure; keep device, inode, mode, size
        # and link count custody, but do not treat its timestamps as source data.
        files = tuple(
            (name, signature[:4] + signature[6:])
            if name == "CACHEDIR.TAG"
            else (name, signature)
            for name, signature in files
        )
    return directories, files


@dataclass(frozen=True, slots=True)
class RetainedCargoExecutionInputs:
    materialized: RetainedCargoMaterialization
    local: RetainedCargoConsumerFiles
    cargo_home: Path
    environment_identity: ContentIdentity
    maximum_entries: int
    _states: tuple = field(repr=False)
    _trees: tuple = field(repr=False)
    _closure: PinnedInputClosure = field(repr=False)
    external_roots: tuple[tuple[str, Path], ...] = ()
    gate_policy_identity: ContentIdentity | None = None

    def current_identity(self) -> ContentIdentity:
        self.materialized.require_unchanged()
        local_identity = self.local.current_identity()
        for _, path, before in self._states:
            if _state(path) != before:
                raise ValueError("retained.inputs.custody-changed")
        for label, root, before in self._trees:
            if _source_tree(label, root, self.maximum_entries) != before:
                raise ValueError("retained.inputs.custody-changed")
        self._closure.require_unchanged()
        self.materialized.require_unchanged()
        identity = {
            "schema": "literate-ai/retained-cargo-execution-inputs@1",
            "binding": self.materialized._files.binding.identity.uri,
            "consumer": local_identity.uri,
            "environment": self.environment_identity.uri,
            "external_files": self._closure.identity,
            "present": [
                label for label, _, state in self._states if state[2] is not None
            ],
            "absent": [label for label, _, state in self._states if state[2] is None],
            "directories": {
                label: [name for name, _ in before[0]]
                for label, _, before in self._trees
            },
        }
        if self.gate_policy_identity is not None:
            identity["gate_policy"] = self.gate_policy_identity.uri
            identity["external_roots"] = {
                name: str(root) for name, root in self.external_roots
            }
        return canonical_identity(identity)

    def require_metadata_paths(self, metadata: dict) -> None:
        """Require every package manifest and target to be in captured custody."""
        if not isinstance(metadata, dict):
            raise ValueError("retained.inputs.metadata-invalid")
        self.current_identity()
        files = {self.local.root / name for name, _ in self.local.nodes}
        for package in self.materialized.packages:
            files.update(package.root / item.path for item in package.files)
        for _, root, before in self._trees:
            files.update(root / name for name, _ in before[1])
        packages = metadata.get("packages")
        if not isinstance(packages, list):
            raise ValueError("retained.inputs.metadata-invalid")
        for package in packages:
            if not isinstance(package, dict) or not isinstance(
                package.get("targets"), list
            ):
                raise ValueError("retained.inputs.metadata-invalid")
            paths = [package.get("manifest_path")]
            for target in package["targets"]:
                if not isinstance(target, dict):
                    raise ValueError("retained.inputs.metadata-invalid")
                paths.append(target.get("src_path"))
            for value in paths:
                if not isinstance(value, str):
                    raise ValueError("retained.inputs.metadata-invalid")
                path = Path(value)
                if not path.is_absolute() or ".." in path.parts or path not in files:
                    raise ValueError("retained.inputs.uncaptured-package-path")
        self.current_identity()


def read_retained_cargo_execution_inputs(
    materialized: RetainedCargoMaterialization,
    *,
    environment: Mapping[str, str],
    gate_policy: RetainedLibraryGatePolicy | None = None,
    maximum_entries: int = 10000,
    maximum_file_bytes: int = 16 * 1024 * 1024,
    maximum_total_bytes: int = 256 * 1024 * 1024,
) -> RetainedCargoExecutionInputs:
    """Use explicit CARGO_HOME; no ambient home, cache filling or Git mutation.

    Cargo's mutable lock/usage metadata is outside registry/git source trees.
    Root cache-tag bytes and file ownership stay pinned across timestamp rewrites.
    Compiler/tool inputs retain their separate measured-tool custody. Config files
    are pinned, but arbitrary external paths selected by them need further review.
    """
    if not isinstance(environment, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v
        for k, v in environment.items()
    ):
        raise ValueError("retained.inputs.environment-invalid")
    selected = environment.get("CARGO_HOME")
    if not selected or not Path(selected).is_absolute() or ".." in Path(selected).parts:
        raise ValueError("retained.inputs.explicit-cargo-home-required")
    home = Path(selected)
    require_safe_directory(home)
    local = read_retained_cargo_consumer_files(
        materialized,
        environment=environment,
        maximum_entries=maximum_entries,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    external = []
    if gate_policy is not None:
        if (
            not isinstance(gate_policy, RetainedLibraryGatePolicy)
            or gate_policy.importer_project_id
            != materialized._files.project.definition.project_id
            or gate_policy.commands != materialized.plan.gates
        ):
            raise ValueError("retained.inputs.gate-policy-mismatch")
        for name in gate_policy.external_input_variables:
            value = environment.get(name)
            if not value or not Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("retained.inputs.external-directory-required")
            root = Path(value)
            try:
                require_safe_directory(root)
            except UnsafeFilesystemPathError:
                raise ValueError(
                    "retained.inputs.external-directory-unavailable"
                ) from None
            if any(
                root.is_relative_to(other) or other.is_relative_to(root)
                for other in (local.root, home, *(p for _, p in external))
            ):
                raise ValueError("retained.inputs.external-directory-overlap")
            external.append((name, root))
    remaining_bytes = maximum_total_bytes - local._closure.total_bytes
    if remaining_bytes < 1:
        raise ValueError("retained.inputs.byte-limit")
    closure = PinnedInputClosure(
        maximum_files=maximum_entries,
        maximum_file_bytes=min(maximum_file_bytes, remaining_bytes),
        maximum_total_bytes=remaining_bytes,
    )
    states, trees = [], []
    candidates = [("cargo-home", home)]
    candidates.extend(
        (f"ancestor-{index}", parent / ".cargo")
        for index, parent in enumerate(local.root.parents)
    )
    for label, root in candidates:
        states.append((label, root, _state(root)))
        for name in ("config", "config.toml"):
            path = root / name
            state = _state(path)
            states.append((f"{label}/{name}", path, state))
            if state[2] is not None:
                closure.pin(path, boundary=root, label=f"{label}/{name}")
    remaining = (
        maximum_entries - len(local.nodes) - len(local.directories) - len(states)
    )
    if remaining < 0:
        raise ValueError("retained.inputs.entry-limit")
    sources = [(name, home / name) for name in ("registry", "git")]
    sources.extend((f"external-{name}", root) for name, root in external)
    for name, root in sources:
        remaining -= 1
        if remaining < 0:
            raise ValueError("retained.inputs.entry-limit")
        state = _state(root)
        states.append((name, root, state))
        if state[2] is None:
            if name.startswith("external-"):
                raise ValueError("retained.inputs.external-directory-required")
            continue
        before = _source_tree(name, root, remaining)
        remaining -= len(before[0]) + len(before[1])
        if remaining < 0:
            raise ValueError("retained.inputs.entry-limit")
        for relative, _ in before[1]:
            closure.pin(root / relative, boundary=root, label=f"{name}/{relative}")
        trees.append((name, root, before))
    result = RetainedCargoExecutionInputs(
        materialized,
        local,
        home,
        canonical_identity(dict(environment)),
        maximum_entries,
        tuple(states),
        tuple(trees),
        closure,
        tuple(external),
        gate_policy.identity if gate_policy is not None else None,
    )
    result.current_identity()
    return result

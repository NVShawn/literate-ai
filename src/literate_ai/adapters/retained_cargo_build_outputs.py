"""Recursive custody for Cargo build-script OUT_DIR trees used by native tests."""

from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_cargo_consumer_files import _observe
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PinnedInputClosure


def _directory(path):
    try:
        return directory_node(path)
    except (OSError, UnsafeFilesystemPathError):
        raise ValueError("retained.build-outputs.directory-unavailable") from None


def _tree(root, maximum_entries):
    try:
        return _observe(root, (), maximum_entries)
    except (OSError, UnsafeFilesystemPathError):
        raise ValueError("retained.build-outputs.tree-unavailable") from None


@dataclass(frozen=True)
class RetainedCargoBuildOutputs:
    output_root: Path
    trees: tuple
    parents: tuple
    closure: PinnedInputClosure
    maximum_entries: int

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-build-outputs@1",
                "output_root": str(self.output_root),
                "files": self.closure.identity,
                "directories": {
                    root.relative_to(self.output_root).as_posix(): [
                        name for name, _ in state[0]
                    ]
                    for root, state in self.trees
                },
            }
        )

    def require_unchanged(self):
        self.closure.require_unchanged()
        if any(_directory(path) != node for path, node in self.parents) or any(
            _tree(root, self.maximum_entries) != before for root, before in self.trees
        ):
            raise ValueError("retained.build-outputs.changed")
        self.closure.require_unchanged()


def capture_retained_cargo_build_outputs(
    records,
    metadata,
    *,
    output_root,
    maximum_entries=10000,
    maximum_file_bytes=16 * 1024 * 1024,
    maximum_total_bytes=256 * 1024 * 1024,
):
    """Capture all checked build-script outputs beneath the fresh compilation root.

    No output is excluded by filename. This does not discover files that scripts
    read or write outside OUT_DIR, or establish native loader authority.
    """
    if (
        not isinstance(output_root, Path)
        or not output_root.is_absolute()
        or ".." in output_root.parts
        or any(
            type(n) is not int or n < 1
            for n in (
                maximum_entries,
                maximum_file_bytes,
                maximum_total_bytes,
            )
        )
        or maximum_file_bytes > maximum_total_bytes
    ):
        raise ValueError("retained.build-outputs.configuration-invalid")
    parents = {output_root: _directory(output_root)}
    packages = {package["id"] for package in metadata["packages"]}
    roots = set()
    for record in records:
        if record.get("reason") != "build-script-executed":
            continue
        value = record.get("out_dir")
        package_id = record.get("package_id")
        if (
            not isinstance(package_id, str)
            or package_id not in packages
            or not isinstance(value, str)
        ):
            raise ValueError("retained.build-outputs.record-invalid")
        root = Path(value)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or root == output_root
            or not root.is_relative_to(output_root)
        ):
            raise ValueError("retained.build-outputs.path-invalid")
        roots.add(root)
        if len(roots) > 128:
            raise ValueError("retained.build-outputs.root-limit")
    roots = tuple(sorted(roots))
    if any(a != b and a.is_relative_to(b) for a in roots for b in roots):
        raise ValueError("retained.build-outputs.roots-overlap")
    closure = PinnedInputClosure(
        maximum_files=maximum_entries,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    trees = []
    remaining = maximum_entries - 1
    for root in roots:
        if remaining < 1:
            raise ValueError("retained.build-outputs.entry-limit")
        before = _tree(root, remaining - 1)
        remaining -= len(before[0]) + len(before[1])
        parent = root.parent
        while parent != output_root:
            if parent not in parents:
                remaining -= 1
                if remaining < 0:
                    raise ValueError("retained.build-outputs.entry-limit")
                parents[parent] = _directory(parent)
            parent = parent.parent
        for name, _ in before[1]:
            path = root / name
            closure.pin(
                path, boundary=root, label=path.relative_to(output_root).as_posix()
            )
        trees.append((root, before))
    result = RetainedCargoBuildOutputs(
        output_root, tuple(trees), tuple(parents.items()), closure, maximum_entries
    )
    result.require_unchanged()
    return result

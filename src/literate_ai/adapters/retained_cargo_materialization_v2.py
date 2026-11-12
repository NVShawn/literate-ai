"""Publish qualified packages only inside a live retained-wrapper scratch owner.

This is the opt-in v2 receiving transaction. It neither edits authored manifests
nor retires source, executes a consumer or issues an admission receipt. Saved
plans, ignored directories and previous materializations cannot recreate custody.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.exclusive_directory import (
    directory_node,
    publish_directory_exclusive,
)
from literate_ai.adapters.qualification_capture import QualificationRunCapture
from literate_ai.adapters.retained_cargo_files_v2 import RetainedCargoFileSnapshotV2
from literate_ai.adapters.retained_cargo_import_v2 import (
    RetainedCargoCurrentInputsV2,
    verify_retained_cargo_archive_v2,
)
from literate_ai.adapters.retained_cargo_scratch_v2 import RetainedCargoScratchScopeV2
from literate_ai.adapters.retained_package_tree import (
    RetainedPackageTree,
    write_staged_package,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_cargo_workspace_v2 import (
    RetainedCargoWorkspacePlanV2,
)

_CREATION_KEY = object()


@dataclass(init=False, frozen=True, eq=False)
class RetainedCargoMaterializationV2:
    """Live original-source, reviewed-input and exact package custody.

    The caller must keep its wrapper and scratch context managers open. This
    operation-local object cannot be serialized or reopened as a saved grant.
    """

    plan: RetainedCargoWorkspacePlanV2
    capture: QualificationRunCapture
    packages: tuple[RetainedPackageTree, ...]
    files: RetainedCargoFileSnapshotV2 = field(repr=False)
    scratch: RetainedCargoScratchScopeV2 = field(repr=False)
    current: RetainedCargoCurrentInputsV2 = field(repr=False)
    _read_current: Callable = field(repr=False)

    def __init__(
        self, *, key, files, scratch, current, read_current, capture, packages
    ):
        if key is not _CREATION_KEY:
            raise TypeError("retained.materialize.v2-live-transaction-required")
        for name, value in (
            ("plan", files.reviewed.plan),
            ("capture", capture),
            ("packages", packages),
            ("files", files),
            ("scratch", scratch),
            ("current", current),
            ("_read_current", read_current),
        ):
            object.__setattr__(self, name, value)

    def __copy__(self):
        raise TypeError("retained.materialize.v2-not-copyable")

    def __deepcopy__(self, memo):
        raise TypeError("retained.materialize.v2-not-copyable")

    def __reduce_ex__(self, protocol):
        raise TypeError("retained.materialize.v2-not-serializable")

    def require_unchanged(self):
        self.scratch.require_unchanged()
        self.files.require_authored_unchanged()
        self.current.require_unchanged()
        for package in self.packages:
            package.require_unchanged()
        latest = self._read_current()
        if type(latest) is not RetainedCargoCurrentInputsV2 or latest != self.current:
            raise ValueError("retained.materialize.v2-current-inputs-changed")
        latest.require_unchanged()
        self.files.require_authored_unchanged()
        self.scratch.require_unchanged()


def materialize_retained_cargo_archive_v2(
    files: RetainedCargoFileSnapshotV2,
    *,
    scratch: RetainedCargoScratchScopeV2,
    read_current: Callable[[], RetainedCargoCurrentInputsV2],
    read_archive: Callable[[str, BlobRef, int], bytes],
    max_plan_bytes: int,
    max_archive_bytes: int,
    max_records: int,
    max_package_bytes: int,
) -> RetainedCargoMaterializationV2:
    """Verify the real v2 archive, then exclusively publish exact native exports.

    Only absent direct children of the live member-packages roots are admitted.
    Their parents are already owned/pinned by the scratch transaction; no path
    through authored source is created. Existing packages need a separate reopen
    transaction and are never adopted by this initial publication operation.
    """
    if (
        type(files) is not RetainedCargoFileSnapshotV2
        or files.manifest_state != "after"
        or files.package_state not in {"absent", "provision"}
        or type(scratch) is not RetainedCargoScratchScopeV2
        or scratch.context.reviewed is not files.reviewed
    ):
        raise ValueError("retained.materialize.v2-current-scratch-files-required")
    if not callable(read_current) or not callable(read_archive):
        raise TypeError("retained.materialize.v2-readers-required")
    files.require_unchanged()
    scratch.require_unchanged()
    observed = []

    def current():
        value = read_current()
        if type(value) is not RetainedCargoCurrentInputsV2:
            raise TypeError("retained.materialize.v2-current-inputs-required")
        if value.reviewed.identity != files.reviewed.identity:
            raise ValueError("retained.materialize.v2-review-mismatch")
        observed[:] = [value]
        return value

    plan, capture = verify_retained_cargo_archive_v2(
        read_current=current,
        read_archive=read_archive,
        max_plan_bytes=max_plan_bytes,
        max_archive_bytes=max_archive_bytes,
        max_records=max_records,
        max_package_bytes=max_package_bytes,
    )
    if plan != files.reviewed.plan:
        raise ValueError("retained.materialize.v2-plan-changed")
    baseline = observed[-1]
    root = files.reviewed.project.root
    parents = {
        scratch.context.workspace / declaration.path
        for declaration in plan.scratch_roots
        if declaration.kind == "member-packages"
    }
    destinations = dict(files.reviewed.binding.common.destinations)
    blobs = dict(capture.blobs)
    packages = []
    remaining_bytes, remaining_entries = max_package_bytes, max_records
    for manifest in files.reviewed.binding.common.exports.graph.manifests:
        for export in manifest.exports:
            destination = root / destinations[export.identity]
            if destination.parent not in parents:
                raise ValueError("retained.materialize.v2-owned-parent-required")
            content = read_directory_export(
                blobs[export.blob],
                export.blob,
                max_bytes=remaining_bytes,
                max_entries=remaining_entries,
            )
            remaining_bytes -= sum(len(item.content) for item in content)
            remaining_entries -= len(content)
            packages.append((destination, content))
    packages.sort(key=lambda item: str(item[0]))
    staged, published = [], []

    def guard():
        scratch.require_unchanged()
        files.require_authored_unchanged()
        baseline.require_unchanged()
        latest = current()
        if latest != baseline:
            raise ValueError("retained.materialize.v2-current-inputs-changed")
        latest.require_unchanged()

    try:
        for destination, content in packages:
            guard()
            # Exact case/Unicode collisions are also checked by the exclusive
            # publication primitive; never reopen a foreign existing package.
            if destination.exists() or destination.is_symlink():
                raise ValueError("retained.materialize.v2-destination-present")
            require_safe_directory(destination.parent)
            stage = Path(tempfile.mkdtemp(prefix=".rp-", dir=destination.parent))
            staged.append((destination, write_staged_package(stage, content)))
        files.require_unchanged()
        guard()
        for destination, tree in staged:
            guard()
            tree.require_unchanged()
            publish_directory_exclusive(
                tree.root,
                destination,
                expected_source=directory_node(tree.root),
                expected_parent=directory_node(destination.parent),
            )
            # Remember the move before checking; rollback may move only this
            # exact still-owned tree back, never delete a changed public name.
            published.append((destination, tree))
            tree.relocated(destination).require_unchanged()
            guard()
        result = RetainedCargoMaterializationV2(
            key=_CREATION_KEY,
            files=files,
            scratch=scratch,
            current=baseline,
            read_current=read_current,
            capture=capture,
            packages=tuple(tree.relocated(path) for path, tree in published),
        )
        result.require_unchanged()
        return result
    except BaseException:
        for destination, tree in reversed(published):
            try:
                scratch.require_unchanged()
                tree.relocated(destination).require_unchanged()
                publish_directory_exclusive(
                    destination,
                    tree.root,
                    expected_source=directory_node(destination),
                    expected_parent=directory_node(destination.parent),
                )
            except (OSError, ValueError, RuntimeError):
                # Foreign replacement and uncertain custody remain preserved.
                pass
        raise

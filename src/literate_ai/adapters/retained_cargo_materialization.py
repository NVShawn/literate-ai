"""Provision exact retained packages under current admission and owned custody.

No manifest/source transfer or consumer command runs here. A returned live guard
is not a durable admission receipt. Failed stages are retained for diagnosis;
rollback never deletes or replaces a concurrently changed destination.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import ensure_safe_directory, require_safe_directory
from literate_ai.adapters._write_reservations import (
    WriteReservationTarget,
    acquire_write_reservations,
)
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.exclusive_directory import (
    directory_node,
    publish_directory_exclusive,
)
from literate_ai.adapters.qualification_capture import QualificationRunCapture
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.retained_cargo_files import RetainedCargoFileSnapshot
from literate_ai.adapters.retained_cargo_import import (
    RetainedCargoImportInputs,
    verify_retained_cargo_archive,
)
from literate_ai.adapters.retained_package_tree import (
    RetainedPackageTree,
    write_staged_package,
)
from literate_ai.cache_directories import resolve_cache_directories
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_cargo import RetainedCargoWorkspacePlan


@dataclass(frozen=True, slots=True)
class RetainedCargoMaterialization:
    plan: RetainedCargoWorkspacePlan
    capture: QualificationRunCapture
    packages: tuple[RetainedPackageTree, ...]
    _files: RetainedCargoFileSnapshot = field(repr=False)
    _current: RetainedCargoImportInputs = field(repr=False)
    _reservations: tuple[WriteReservationTarget, ...] = field(repr=False)

    def require_unchanged(self) -> None:
        self._current.require_unchanged()
        self._files.require_authored_unchanged()
        for package in self.packages:
            package.require_unchanged()
        self._files.require_authored_unchanged()
        self._current.require_unchanged()

    @contextmanager
    def custody(self):
        """Hold package reservations around separately authorized native gates."""
        with acquire_write_reservations(self._reservations):
            self.require_unchanged()
            yield self
            self.require_unchanged()


def materialize_retained_cargo_archive(
    files: RetainedCargoFileSnapshot,
    *,
    read_current: Callable[[], RetainedCargoImportInputs],
    read_archive: Callable[[str, BlobRef, int], bytes],
    max_plan_bytes: int,
    max_archive_bytes: int,
    max_records: int,
    max_package_bytes: int,
    environment: Mapping[str, str] | None = None,
) -> RetainedCargoMaterialization:
    """Verify, stage and publish without replacing authored or foreign files.

    The current-input resolver owns provider/tool/gate/configuration custody;
    this function separately owns the supplied file snapshot and package-tree
    custody. That resolver must not pin expected package-destination absences,
    since those are the only transitions this operation is allowed to perform.
    """
    if (
        not isinstance(files, RetainedCargoFileSnapshot)
        or files.manifest_state != "after"
        or files.package_state not in {"absent", "provision", "present"}
    ):
        raise ValueError("retained.materialize.after-state-required")
    files.require_unchanged()
    observed = []

    def current():
        value = read_current()
        observed.append(value)
        return value

    plan, capture = verify_retained_cargo_archive(
        files.binding,
        files.plan_content,
        read_current=current,
        read_archive=read_archive,
        max_plan_bytes=max_plan_bytes,
        max_archive_bytes=max_archive_bytes,
        max_records=max_records,
        max_package_bytes=max_package_bytes,
    )
    if plan != files.plan:
        raise ValueError("retained.materialize.plan-changed")
    baseline = observed[-1]
    baseline.require_unchanged()
    files.require_unchanged()
    root = files.project.root
    environment = dict(os.environ if environment is None else environment)
    cache = resolve_cache_directories(root, environment=environment)
    destinations = dict(files.binding.destinations)
    blobs = dict(capture.blobs)
    packages = []
    remaining_bytes, remaining_entries = max_package_bytes, max_records
    for manifest in files.binding.exports.graph.manifests:
        for export in manifest.exports:
            destination = root / destinations[export.identity]
            if not any(
                destination != base and destination.is_relative_to(base)
                for base in (cache.build_dir, cache.obj_dir)
            ):
                raise ValueError("retained.materialize.disposable-root-required")
            content = read_directory_export(
                blobs[export.blob],
                export.blob,
                max_bytes=remaining_bytes,
                max_entries=remaining_entries,
            )
            remaining_bytes -= export.blob.size
            remaining_entries -= len(content)
            packages.append((destination, content))
    packages.sort(key=lambda row: str(row[0]))
    anchor = directory_node(root)
    reservations = tuple(
        WriteReservationTarget(
            root
            / ".litai-locks"
            / (
                "cargo-"
                + hashlib.sha256(
                    destination.relative_to(root).as_posix().casefold().encode()
                ).hexdigest()[:32]
                + ".lock"
            ),
            root,
            anchor,
        )
        for destination, _ in packages
    )
    trees, staged, published = [], [], []
    with acquire_write_reservations(reservations) as owned:
        files.require_unchanged()
        try:
            for destination, content in packages:
                try:
                    destination.lstat()
                except FileNotFoundError:
                    ensure_safe_directory(destination.parent)
                    stage = Path(
                        tempfile.mkdtemp(prefix=".rp-", dir=destination.parent)
                    )
                    tree = write_staged_package(stage, content)
                    staged.append((destination, tree))
                else:
                    trees.append(RetainedPackageTree.capture(destination, content))
            owned.verify_all()
            files.require_unchanged()
            latest = read_current()
            latest.require_unchanged()
            if latest != baseline:
                raise ValueError("retained.materialize.current-inputs-changed")
            baseline.require_unchanged()
            for tree in trees:
                tree.require_unchanged()
            for destination, tree in staged:
                tree.require_unchanged()
                owned.verify_all()
                try:
                    publish_directory_exclusive(
                        tree.root,
                        destination,
                        expected_source=directory_node(tree.root),
                        expected_parent=directory_node(destination.parent),
                    )
                except OSError as error:
                    raise OrchestrationInventoryError(
                        "retained_materialization_publication_failed",
                        "retained Cargo package publication was refused",
                    ) from error
                # Record the move before the post-publication check so a later
                # refusal can roll back an unchanged owned tree.
                published.append((destination, tree))
                trees.append(tree.relocated(destination))
            result = RetainedCargoMaterialization(
                plan,
                capture,
                tuple(sorted(trees, key=lambda item: str(item.root))),
                files,
                baseline,
                reservations,
            )
            result.require_unchanged()
            latest = read_current()
            latest.require_unchanged()
            if latest != baseline:
                raise ValueError("retained.materialize.current-inputs-changed")
            result.require_unchanged()
            owned.verify_all()
        except BaseException:
            # Keep failed private stages. Move back only trees whose exact bytes
            # and physical custody still match; preserve all foreign mutations.
            for destination, original in reversed(published):
                try:
                    original.relocated(destination).require_unchanged()
                    require_safe_directory(original.root.parent)
                    publish_directory_exclusive(
                        destination,
                        original.root,
                        expected_source=directory_node(destination),
                        expected_parent=directory_node(destination.parent),
                    )
                except (OSError, ValueError, RuntimeError):
                    pass
            raise
    return result

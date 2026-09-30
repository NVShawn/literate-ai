"""Read and guard the exact importer files named by a reviewed Cargo plan."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.retained_cargo_plan import reopen_retained_cargo_plan
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import (
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
)
from literate_ai.contracts.retained_cargo import RetainedCargoWorkspacePlan
from literate_ai.contracts.retained_libraries import RetainedLibraryBinding
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    PinnedInputClosure,
    ProjectConfigurationStore,
)


def _require_absent(root: Path, relative: str) -> None:
    """Refuse any present final entry, including dangling links and junctions."""
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            observed = current.lstat()
        except FileNotFoundError:
            return
        if index == len(parts) - 1:
            raise ValueError("retained.cargo.expected-absence-changed")
        if stat_is_link_or_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
            raise ValueError("retained.cargo.absence-parent-unsafe")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("retained.cargo.binding-duplicate-field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RetainedCargoFileSnapshot:
    project: LoadedProject
    binding: RetainedLibraryBinding
    plan: RetainedCargoWorkspacePlan
    plan_content: bytes
    manifest_state: str
    package_state: str
    absent_paths: tuple[str, ...]
    _closure: PinnedInputClosure = field(repr=False, compare=False)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "project_id": self.project.definition.project_id,
                "binding": self.binding.identity.uri,
                "plan": self.plan.identity.uri,
                "manifest_state": self.manifest_state,
                "package_state": self.package_state,
                "present_files": self._closure.identity,
                "absent_paths": list(self.absent_paths),
            }
        )

    def require_unchanged(self) -> None:
        self.require_authored_unchanged()
        for _, destination in self.binding.destinations:
            if destination in self.absent_paths:
                _require_absent(self.project.root, destination)
        self._closure.require_unchanged()

    def require_authored_unchanged(self) -> None:
        """Guard inputs during owned provisioning; caller must guard package trees.

        Only whole artifact-destination absences may transition. Other absences
        and every originally pinned file remain mandatory preconditions.
        """
        require_safe_directory(self.project.root)
        self._closure.require_unchanged()
        destinations = {path for _, path in self.binding.destinations}
        for relative in self.absent_paths:
            if relative not in destinations:
                _require_absent(self.project.root, relative)
        self._closure.require_unchanged()


def read_retained_cargo_files(
    project_root: Path,
    *,
    binding_path: str,
    reviewed_binding: BlobRef,
    plan_path: str,
    manifest_state: str,
    package_state: str = "present",
    maximum_file_bytes: int = 16 * 1024 * 1024,
    maximum_total_bytes: int = 64 * 1024 * 1024,
) -> RetainedCargoFileSnapshot:
    """Capture exact before/after manifest bytes and expected absences, read-only.

    The reviewed binding reference is an explicit importing-maintainer input; this
    reader does not turn a candidate file's self-reported identity into approval.
    An explicit absent-package phase reads the authored after-state before fresh
    provisioning, while requiring every artifact destination to be absent. Package
    manifest bytes must still pass qualified archive verification before writes.
    Provider authority, tool measurement, gate authority and archive admission are
    separate inputs to the composed importer. This snapshot authorizes no edits.
    """
    if manifest_state not in {"before", "after"}:
        raise ValueError("retained.cargo.manifest-state-invalid")
    if package_state not in {"present", "absent", "provision"} or (
        package_state != "present" and manifest_state != "after"
    ):
        raise ValueError("retained.cargo.package-state-invalid")
    if (
        type(maximum_file_bytes) is not int
        or type(maximum_total_bytes) is not int
        or not 0 < maximum_file_bytes <= maximum_total_bytes
    ):
        raise ValueError("retained.cargo.file-limits-invalid")
    if (
        not isinstance(reviewed_binding, BlobRef)
        or reviewed_binding.media_type != "application/json"
        or not 0 < reviewed_binding.size <= maximum_file_bytes
    ):
        raise ValueError("retained.cargo.binding-reference-invalid")
    for relative in (binding_path, plan_path):
        canonical_relative_posix_path(relative, label="retained Cargo authority file")
        if len(relative) > 128:
            raise ValueError("retained.cargo.authority-path-too-long")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("retained.cargo.project-path-unsafe")
    require_safe_directory(root)
    closure = PinnedInputClosure(
        maximum_files=4099,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    # The configuration store supplies the existing strict project parser. Pin the
    # exact bytes it consumed so a concurrent config replacement cannot be mixed in.
    configuration = ProjectConfigurationStore(
        root, maximum_bytes=maximum_file_bytes
    ).read()
    closure.pin(
        root / PROJECT_FILENAME,
        boundary=root,
        label=PROJECT_FILENAME,
        expected_content=configuration.content,
    )

    def read(relative: str, reference: BlobRef) -> bytes:
        if reference.size > maximum_file_bytes:
            raise ValueError("retained.cargo.file-limit-exceeded")
        content = closure.pin(
            root.joinpath(*PurePosixPath(relative).parts),
            boundary=root,
            label=relative,
            expected_identity=reference.identity,
        )
        if len(content) != reference.size:
            raise ValueError("retained.cargo.file-size-mismatch")
        return content

    try:
        binding = RetainedLibraryBinding.from_dict(
            json.loads(
                read(binding_path, reviewed_binding), object_pairs_hook=_unique_object
            )
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("retained.cargo.binding-file-invalid") from None
    if binding.importer_project_id != configuration.definition.project_id:
        raise ValueError("retained.cargo.importer-project-mismatch")
    plan_content = read(plan_path, binding.workspace_plan)
    plan = reopen_retained_cargo_plan(
        plan_content, binding, max_bytes=maximum_file_bytes
    )
    manifest_paths = tuple(
        str(PurePosixPath(plan.workspace_root) / item.path) for item in plan.manifests
    )
    canonical_relative_posix_paths(
        (PROJECT_FILENAME, binding_path, plan_path, *manifest_paths),
        label="retained Cargo input paths",
    )
    destinations = tuple(PurePosixPath(path) for _, path in binding.destinations)
    absent = []
    absent_destinations = set()
    if package_state in {"absent", "provision"}:
        for destination in destinations:
            if package_state == "provision":
                try:
                    (root / destination).lstat()
                except FileNotFoundError:
                    pass
                else:
                    require_safe_directory(root / destination)
                    continue
            _require_absent(root, str(destination))
            absent.append(str(destination))
            absent_destinations.add(destination)
    for relative, manifest in zip(manifest_paths, plan.manifests, strict=True):
        if any(
            PurePosixPath(relative).is_relative_to(destination)
            for destination in absent_destinations
        ):
            continue
        reference = getattr(manifest, manifest_state)
        if reference is None:
            _require_absent(root, relative)
            absent.append(relative)
        else:
            read(relative, reference)
    snapshot = RetainedCargoFileSnapshot(
        LoadedProject(configuration.root, configuration.definition),
        binding,
        plan,
        plan_content,
        manifest_state,
        package_state,
        tuple(sorted(absent)),
        closure,
    )
    snapshot.require_unchanged()
    return snapshot

"""Materialize FINALIZE package bytes from verified BUILD shape, never ZIP guessing."""

import json
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_ARCHIVE_BYTES,
    MAX_BUILD_ARCHIVE_FILES,
)
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_proof import reopen_finalize_input
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_package_tree import write_staged_package
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.executable_components import PackageFileKind


@contextmanager
def materialize_finalize_package(
    *,
    input_record,
    input_identity,
    records,
    cas,
    workspace_root,
    deadline,
    admission_guard,
    verify_package,
    blob_source=None,
):
    """Yield verified intent/result and temporary package custody; execute nothing."""
    records = dict(records)
    value, package = reopen_finalize_input(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=admission_guard,
        verify_package=verify_package,
        blob_source=blob_source,
    )

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    def read(reference):
        current()
        cas.verify(reference)
        content = cas.get_bytes(reference)
        current()
        return content

    # Every archive was independently reopened by the LINK/PACKAGE proof above.
    exports = {}
    selected = {
        item.source_identity
        for item in value.package_input.plan.inputs
        if item.kind is PackageFileKind.ARTIFACT
    }
    retained_bytes = retained_files = 0
    for _, result_id in value.package_input.link_results:
        current()
        linked_id = ContentIdentity.parse_uri(
            json.loads(records[result_id])["input_identity"]
        )
        linked = LinkWorkerInput.admit(records[linked_id], linked_id, deadline)
        archive = linked.acceptance_input.execution_input.build_result.artifact_archive
        members = read_directory_export(
            read(archive),
            archive,
            max_bytes=MAX_BUILD_ARCHIVE_BYTES,
            max_entries=MAX_BUILD_ARCHIVE_FILES,
        )
        by_path = {item.path: item for item in members}
        for export in linked.manifest.exports:
            if export.identity not in selected:
                continue
            if export.export_id in by_path:
                item = by_path[export.export_id]
                content, files = item.content, (replace(item, path=""),)
            else:
                prefix = export.export_id + "/"
                files = tuple(
                    replace(item, path=item.path[len(prefix) :])
                    for item in members
                    if item.path.startswith(prefix)
                )
                content = encode_directory_export(
                    files,
                    max_bytes=MAX_BUILD_ARCHIVE_BYTES,
                    max_entries=MAX_BUILD_ARCHIVE_FILES,
                )
            if (
                len(content) != export.blob.size
                or record_identity(content).uri != export.blob.identity
            ):
                raise ActionWireError(
                    "action_finalize.package_changed", "export bytes differ"
                )
            retained_bytes += sum(len(item.content) for item in files)
            retained_files += len(files)
            if (
                retained_bytes > MAX_BUILD_ARCHIVE_BYTES
                or retained_files > MAX_BUILD_ARCHIVE_FILES
            ):
                raise ActionWireError(
                    "action_finalize.package_excessive", "package exports exceed bounds"
                )
            exports[export.identity] = files
    flattened = []
    total = 0
    for item in value.package_input.plan.inputs:
        current()
        if item.kind is PackageFileKind.ARTIFACT:
            members = tuple(
                replace(
                    member,
                    path=(item.path + "/" + member.path if member.path else item.path),
                )
                for member in exports[item.source_identity]
            )
        else:
            members = (
                DirectoryExportFile(
                    item.path, read(item.blob), 0o755 if item.executable else 0o644
                ),
            )
        total += sum(len(member.content) for member in members)
        if (
            total > MAX_BUILD_ARCHIVE_BYTES
            or len(flattened) + len(members) > MAX_BUILD_ARCHIVE_FILES
        ):
            raise ActionWireError(
                "action_finalize.package_excessive", "package tree exceeds bounds"
            )
        flattened.extend(members)
    current()
    workspace_root = Path(workspace_root)
    require_safe_directory(workspace_root)
    stage = Path(tempfile.mkdtemp(prefix="finalize-", dir=workspace_root))
    owned = directory_node(stage)
    try:
        custody = write_staged_package(
            stage, tuple(sorted(flattened, key=lambda item: item.path))
        )
        current()
        custody.require_unchanged()
        yield value, package, custody
        current()
        custody.require_unchanged()
    finally:
        _remove_owned_stage(stage, owned)

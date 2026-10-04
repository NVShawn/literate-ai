"""Bounded BUILD result transfer with exact artifact and supporting-record custody."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_ARCHIVE_BYTES,
    MAX_BUILD_ARCHIVE_FILES,
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    verify_qualification_build,
)
from literate_ai.adapters.retained_package_tree import (
    RetainedPackageTree,
    write_staged_package,
)
from literate_ai.application.standard_project_lifecycle import StandardBuildOutput
from literate_ai.contracts import (
    ContentIdentity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_post_source_evidence import StandardBuildEvidence
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobNotFoundError

_SCHEMA = "literate-ai/build-worker-result@1"
_FIELDS = frozenset(
    {"schema", "input_identity", "evidence", "artifact_archive", "evidence_records"}
)


def _invalid():
    raise ActionWireError(
        "action_build.result_invalid", "BUILD result custody is invalid"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _guard(admitted: BuildWorkerInput, deadline: ActionDispatchDeadline):
    deadline.remaining()
    admitted.inputs.authorization.grant.require_valid(
        admitted.inputs.intent.build_request, now=datetime.now(UTC)
    )


@dataclass(frozen=True, slots=True)
class BuildWorkerResult:
    input_identity: ContentIdentity
    evidence: StandardBuildEvidence
    artifact_archive: BlobRef
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "input_identity": self.input_identity.uri,
                "evidence": self.evidence.to_dict(),
                "artifact_archive": self.artifact_archive.to_dict(),
                "evidence_records": [item.to_dict() for item in self.evidence_records],
            }
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        admitted = BuildWorkerInput.admit(
            input_record, input_identity, deadline, now=datetime.now(UTC)
        )
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or not isinstance(identity, ContentIdentity)
            or record_identity(content) != identity
        ):
            _invalid()
        try:
            value = json.loads(content, object_pairs_hook=_pairs)
            if (
                not isinstance(value, dict)
                or set(value) != _FIELDS
                or value["schema"] != _SCHEMA
            ):
                _invalid()
            records = value["evidence_records"]
            if (
                not isinstance(records, list)
                or not 1 <= len(records) <= MAX_BUILD_EVIDENCE_RECORDS
            ):
                _invalid()
            result = cls(
                ContentIdentity.parse_uri(value["input_identity"]),
                StandardBuildEvidence.from_dict(value["evidence"]),
                BlobRef.from_dict(value["artifact_archive"]),
                tuple(BlobRef.from_dict(item) for item in records),
            )
            identities = tuple(item.identity for item in result.evidence_records)
            evidence = result.evidence
            if (
                result.input_identity != input_identity
                or result.artifact_archive.size > MAX_BUILD_ARCHIVE_BYTES
                or any(
                    item.size > MAX_ACTION_RECORD_BYTES
                    for item in result.evidence_records
                )
                or sum(item.size for item in result.evidence_records)
                > MAX_BUILD_EVIDENCE_BYTES
                or identities != tuple(sorted(set(identities)))
                or evidence.identity.uri not in identities
                or evidence.component_revision != admitted.plan.component_revision
                or evidence.build_plan_identity != admitted.plan.identity
                or evidence.source_tree_identity != admitted.candidate.tree_identity
                or evidence.source_custody_identity != admitted.source_custody_identity
                or tuple(item.declaration for item in evidence.exports)
                != admitted.plan.manifest.export_declarations
            ):
                _invalid()
            _guard(admitted, deadline)
            return result
        except ActionWireError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
            RecursionError,
        ) as exc:
            raise ActionWireError(
                "action_build.result_invalid", "BUILD result record was refused"
            ) from exc


def _verify_files(files, evidence, reader):
    custody = reader.read_json(evidence.artifact_custody_identity)
    tree = reader.read_json(
        ContentIdentity.parse_uri(custody["artifact_tree_identity"])
    )
    expected = {item["path"]: item["sha256"] for item in tree["files"]}
    actual = {item.path: hashlib.sha256(item.content).hexdigest() for item in files}
    if actual != expected:
        _invalid()
    by_path = {item.path: item for item in files}
    for export in evidence.exports:
        if export.export_id in by_path:
            content = by_path[export.export_id].content
        else:
            prefix = export.export_id + "/"
            members = tuple(
                DirectoryExportFile(item.path[len(prefix) :], item.content, item.mode)
                for item in files
                if item.path.startswith(prefix)
            )
            content = encode_directory_export(
                members,
                max_bytes=MAX_BUILD_ARCHIVE_BYTES,
                max_entries=MAX_BUILD_ARCHIVE_FILES,
            )
        if (
            len(content) != export.blob.size
            or record_identity(content).uri != export.blob.identity
        ):
            _invalid()


def _capture_files(root, guard):
    require_safe_directory(root)
    files = []
    pending = [root]
    total = 0
    nodes = 0
    while pending:
        current = pending.pop()
        require_safe_directory(current)
        with os.scandir(current) as entries:
            for entry in entries:
                guard()
                nodes += 1
                if nodes > 100_000:
                    _invalid()
                path = current / entry.name
                node = path.lstat()
                if stat_is_link_or_reparse(node):
                    _invalid()
                if stat.S_ISDIR(node.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1:
                    _invalid()
                if (
                    len(files) >= MAX_BUILD_ARCHIVE_FILES
                    or node.st_size > MAX_BUILD_ARCHIVE_BYTES - total
                ):
                    _invalid()
                content = _read_regular_file(
                    path,
                    maximum_bytes=MAX_BUILD_ARCHIVE_BYTES - total,
                    code="action_build.result_invalid",
                )
                total += len(content)
                files.append(
                    DirectoryExportFile(
                        path.relative_to(root).as_posix(),
                        content,
                        stat.S_IMODE(node.st_mode),
                    )
                )
    files = tuple(sorted(files, key=lambda item: item.path))
    custody = RetainedPackageTree.capture(root, files)
    guard()
    return files, custody


def capture_build_result(
    *,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    ports: LocalStandardLifecyclePorts,
    output: StandardBuildOutput,
    records: tuple[tuple[ContentIdentity, bytes], ...],
    cas: FileSystemCAS,
) -> bytes:
    """Retain actual worker artifact/evidence bytes without serializing host paths."""
    admitted = BuildWorkerInput.admit(
        input_record, input_identity, deadline, now=datetime.now(UTC)
    )
    if not isinstance(output, StandardBuildOutput) or output.evidence is None:
        _invalid()
    reader = QualificationEvidenceReader(
        records,
        max_bytes=MAX_BUILD_EVIDENCE_BYTES,
        max_records=MAX_BUILD_EVIDENCE_RECORDS,
    )
    if reader.read_bytes(output.evidence.identity) != canonical_json_bytes(
        output.evidence.to_dict()
    ):
        _invalid()
    verify_qualification_build(reader, plan=admitted.plan, build=output.evidence)
    roots = {ports.artifact_path(item).parent for item in output.exports}
    if len(roots) != 1:
        _invalid()

    def guard():
        _guard(admitted, deadline)

    files, custody = _capture_files(roots.pop(), guard)
    _verify_files(files, output.evidence, reader)
    archive = encode_directory_export(
        files, max_bytes=MAX_BUILD_ARCHIVE_BYTES, max_entries=MAX_BUILD_ARCHIVE_FILES
    )
    guard()
    archive_ref = cas.put_bytes(archive)
    references = []
    for identity, content in records:
        guard()
        if len(content) > MAX_ACTION_RECORD_BYTES:
            _invalid()
        reference = cas.put_bytes(content)
        if reference.identity != identity.uri:
            _invalid()
        references.append(reference)
    custody.require_unchanged()
    guard()
    result = BuildWorkerResult(
        input_identity, output.evidence, archive_ref, tuple(references)
    )
    content = result.to_bytes()
    BuildWorkerResult.admit(
        content,
        record_identity(content),
        input_identity=input_identity,
        input_record=input_record,
        deadline=deadline,
    )
    return content


def _remove_owned_stage(stage, owned):
    try:
        if directory_node(stage) != owned:
            return
    except (OSError, ValueError):
        return

    def writable_cleanup(function, raw_path, error):
        path = Path(raw_path)
        if (
            not isinstance(error[1], PermissionError)
            or function not in {os.unlink, os.remove, os.rmdir}
            or directory_node(stage) != owned
            or not path.is_relative_to(stage)
        ):
            raise error[1]
        require_safe_directory(path.parent)
        node = path.lstat()
        if (
            stat_is_link_or_reparse(node)
            or not (stat.S_ISREG(node.st_mode) or stat.S_ISDIR(node.st_mode))
            or (stat.S_ISREG(node.st_mode) and node.st_nlink != 1)
        ):
            raise error[1]
        path.chmod(node.st_mode | stat.S_IWUSR)
        function(path)

    shutil.rmtree(stage, onerror=writable_cleanup)


def read_build_result(
    *,
    content: bytes,
    result_identity: ContentIdentity,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    cas: FileSystemCAS,
    blob_source: Callable[[BlobRef], bytes] | None = None,
    require_current: Callable[[], None] = lambda: None,
):
    """Verify BUILD custody without allocating or registering an artifact."""
    admitted = BuildWorkerInput.admit(
        input_record, input_identity, deadline, now=datetime.now(UTC)
    )
    result = BuildWorkerResult.admit(
        content,
        result_identity,
        input_identity=input_identity,
        input_record=input_record,
        deadline=deadline,
    )

    def guard():
        _guard(admitted, deadline)
        require_current()

    def fetch(reference):
        guard()
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            payload = blob_source(reference)
            guard()
            if (
                not isinstance(payload, bytes)
                or len(payload) != reference.size
                or record_identity(payload).uri != reference.identity
            ):
                _invalid()
            if cas.put_bytes(payload, media_type=reference.media_type) != reference:
                _invalid()
        payload = cas.get_bytes(reference)
        guard()
        return payload

    records = tuple(
        (ContentIdentity.parse_uri(item.identity), fetch(item))
        for item in result.evidence_records
    )
    reader = QualificationEvidenceReader(
        records,
        max_bytes=MAX_BUILD_EVIDENCE_BYTES,
        max_records=MAX_BUILD_EVIDENCE_RECORDS,
    )
    if reader.read_bytes(result.evidence.identity) != canonical_json_bytes(
        result.evidence.to_dict()
    ):
        _invalid()
    verify_qualification_build(reader, plan=admitted.plan, build=result.evidence)
    files = read_directory_export(
        fetch(result.artifact_archive),
        result.artifact_archive,
        max_bytes=MAX_BUILD_ARCHIVE_BYTES,
        max_entries=MAX_BUILD_ARCHIVE_FILES,
    )
    _verify_files(files, result.evidence, reader)
    guard()
    return admitted, result, records, reader, files


def import_build_result(
    *,
    content: bytes,
    result_identity: ContentIdentity,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    ports: LocalStandardLifecyclePorts,
    cas: FileSystemCAS,
    retain_record: Callable[[ContentIdentity, bytes], None],
    blob_source: Callable[[BlobRef], bytes] | None = None,
    admission_guard: Callable[[], None] | None = None,
) -> StandardBuildOutput:
    """Fetch, retain and admit a BUILD result; failed stages never become artifacts."""
    admitted, result, records, reader, files = read_build_result(
        content=content,
        result_identity=result_identity,
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        cas=cas,
        blob_source=blob_source,
    )

    def guard():
        _guard(admitted, deadline)

    require_safe_directory(ports.object_root)
    stage = Path(tempfile.mkdtemp(prefix="build-", dir=ports.object_root))
    owned = directory_node(stage)
    try:
        custody = write_staged_package(stage, files)
        for identity, payload in (*records, (result_identity, content)):
            guard()
            retain_record(identity, payload)

        def require_admission():
            guard()
            if admission_guard is not None:
                admission_guard()
            custody.require_unchanged()
            guard()

        require_admission()
        output = ports.admit_transferred_build(
            plan=admitted.plan,
            inputs=admitted.inputs,
            evidence=result.evidence,
            evidence_reader=reader,
            artifact=stage,
            admission_guard=require_admission,
        )
    except BaseException:
        _remove_owned_stage(stage, owned)
        raise
    return output

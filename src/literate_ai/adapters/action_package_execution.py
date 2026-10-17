"""Execute an admitted package with private adapters and bounded verified CAS bytes."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_build_limits import MAX_BUILD_ARCHIVE_BYTES
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_package_proof import reopen_package_input
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.packaging import verify_package_result
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.executable_components import PackageResult
from literate_ai.storage.cas import BlobNotFoundError


@dataclass(frozen=True)
class PackageWorkerResult:
    input_identity: ContentIdentity
    package: PackageResult

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/package-worker-result@1",
                "input_identity": self.input_identity.uri,
                "package": self.package.to_dict(),
            }
        )

    @classmethod
    def admit(
        cls, content, identity, *, input_record, input_identity, deadline, read_blob
    ):
        value = PackageWorkerInput.admit(input_record, input_identity, deadline)
        try:
            if (
                not isinstance(content, bytes)
                or len(content) > MAX_ACTION_RECORD_BYTES
                or record_identity(content) != identity
            ):
                raise ValueError("invalid result bytes")
            doc = json.loads(content)
            if (
                not isinstance(doc, dict)
                or set(doc) != {"schema", "input_identity", "package"}
                or doc["schema"] != "literate-ai/package-worker-result@1"
            ):
                raise ValueError("invalid result envelope")
            result = cls(
                ContentIdentity.parse_uri(doc["input_identity"]),
                PackageResult.from_dict(doc["package"]),
            )
            if result.input_identity != input_identity or result.to_bytes() != content:
                raise ValueError("result input differs")
            _bounded_refs(
                tuple(
                    item.blob
                    for item in (*result.package.files, *result.package.artifacts)
                )
            )

            def read(reference):
                deadline.remaining()
                content = read_blob(reference)
                deadline.remaining()
                return content

            verify_package_result(value.plan, result.package, read_blob=read)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ActionWireError(
                "action_package.result_invalid", "PACKAGE result refused"
            ) from exc
        deadline.remaining()
        return result


def _bounded_refs(refs):
    unique = set(refs)
    if len(unique) > 16384 or sum(ref.size for ref in unique) > MAX_BUILD_ARCHIVE_BYTES:
        raise ActionWireError(
            "action_package.bytes_exceeded", "PACKAGE bytes exceed transfer bound"
        )
    return unique


def execute_package_input(
    *,
    input_record,
    input_identity,
    records,
    cas,
    deadline,
    admission_guard,
    package_adapter,
    packager_identity,
    blob_source=None,
    read_created_blob=None,
):
    """Private composition supplies adapter authority; input records select no code."""
    if (
        not callable(admission_guard)
        or not callable(getattr(package_adapter, "package", None))
        or (blob_source is not None and not callable(blob_source))
    ):
        raise TypeError("PACKAGE requires private adapter and live admission")
    value = PackageWorkerInput.admit(input_record, input_identity, deadline)
    if value.plan.packager_identity != packager_identity:
        raise ActionWireError(
            "action_package.packager_mismatch", "private packager differs from plan"
        )

    def current():
        deadline.remaining()
        admission_guard()
        deadline.remaining()

    current()
    value = reopen_package_input(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        blob_source=blob_source,
    )
    inputs = _bounded_refs(tuple(item.blob for item in value.plan.inputs))

    def checked_bytes(reference, content):
        current()
        if (
            not isinstance(content, bytes)
            or len(content) != reference.size
            or record_identity(content).uri != reference.identity
        ):
            raise ActionWireError("action_package.blob_invalid", "PACKAGE blob differs")
        if cas.put_bytes(content, media_type=reference.media_type) != reference:
            raise ActionWireError(
                "action_package.blob_invalid", "PACKAGE CAS custody differs"
            )
        current()
        return content

    def read_input(reference):
        current()
        if reference not in inputs:
            raise ActionWireError(
                "action_package.input_undeclared", "undeclared package blob read"
            )
        try:
            cas.verify(reference)
        except BlobNotFoundError:
            if blob_source is None:
                raise
            checked_bytes(reference, blob_source(reference))
        content = cas.get_bytes(reference)
        current()
        return content

    for reference in sorted(inputs, key=lambda ref: ref.identity):
        read_input(reference)
    current()
    package = package_adapter.package(value.plan, read_blob=read_input)
    if not isinstance(package, PackageResult):
        raise TypeError("private packager must return PackageResult")
    refs = _bounded_refs(
        tuple(item.blob for item in (*package.files, *package.artifacts))
    )
    for reference in sorted(refs - inputs, key=lambda ref: ref.identity):
        current()
        if not callable(read_created_blob):
            raise ActionWireError(
                "action_package.output_missing",
                "private package output reader required",
            )
        checked_bytes(reference, read_created_blob(reference))

    def read_result(reference):
        current()
        cas.verify(reference)
        content = cas.get_bytes(reference)
        current()
        return content

    result = PackageWorkerResult(input_identity, package)
    content = result.to_bytes()
    PackageWorkerResult.admit(
        content,
        record_identity(content),
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        read_blob=read_result,
    )
    current()
    return content

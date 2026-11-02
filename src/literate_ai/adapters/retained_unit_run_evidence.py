"""Return the missing facts needed to reopen accepted-run assertions.

This opt-in record supplements, and never changes, the frozen retained receipt.
Decoding produces pure observations only. No SDK or retained admission owner is
created from transported data.
"""

import hashlib
import json

from literate_ai.adapters.retained_project_receipt import AdmittedRetainedProject
from literate_ai.adapters.retained_sdk_metadata import (
    SDK_METADATA_MEDIA_TYPES,
    retained_sdk_metadata_closure,
)
from literate_ai.adapters.retained_unit_inspection import (
    retained_unit_metadata_reference,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.blobs import BlobRef

RUN_EVIDENCE_SCHEMA = "literate-ai/retained-unit-accepted-run-evidence@1"


def retain_retained_unit_run_evidence(
    admission, *, maximum_bytes, maximum_memory_bytes, admission_guard=lambda: None
):
    if not isinstance(admission, AdmittedRetainedProject):
        raise TypeError("retained.unit.live-admission-required")
    if (
        type(maximum_bytes) is not int
        or not 1 <= maximum_bytes <= 512 * 1024**2
        or type(maximum_memory_bytes) is not int
        or not 1 <= maximum_memory_bytes <= 16 * 1024**3
    ):
        raise ValueError("retained.unit.bounded-run-evidence-required")
    if not callable(admission_guard):
        raise TypeError("retained.unit.callable-operation-guard-required")
    admission_guard()
    admission.require_current()
    run, store = admission.run, admission.store
    # Reference lists have their own finite cost before serializing documents.
    rows = len(run.observations) + len(run.resolved_bom.sdk_dependencies)
    if rows * 512 + 4096 > min(maximum_bytes, maximum_memory_bytes // 16):
        raise ValueError("retained.unit.run-evidence-metadata-policy-exceeded")
    total, references = 0, {}

    def check_budget(size, digest):
        if not 0 <= size <= 64 * 1024**2:
            raise ValueError("retained.unit.run-evidence-document-policy-exceeded")
        if digest not in references and (
            total + size > maximum_bytes
            or 16 * (total + size) + 512 * rows > maximum_memory_bytes
        ):
            raise ValueError("retained.unit.run-evidence-metadata-policy-exceeded")

    def retain(content, media_type="application/json"):
        nonlocal total
        admission_guard()
        digest = hashlib.sha256(content).hexdigest()
        check_budget(len(content), digest)
        if digest not in references:
            total += len(content)
            references[digest] = store.put_bytes(content, media_type=media_type)
        admission_guard()
        return references[digest].to_dict()

    sdk_references = {}

    def read_sdk_document(identity, *, reference=None):
        admission_guard()
        if reference is None:
            reference = retained_unit_metadata_reference(
                store, identity, maximum_bytes=min(maximum_bytes, 64 * 1024**2)
            )
        if (
            reference.identity != identity.uri
            or reference.media_type not in SDK_METADATA_MEDIA_TYPES
        ):
            raise ValueError("retained.unit.sdk-metadata-reference-differs")
        # Reserve headroom before loading either raw or decoded metadata.
        check_budget(reference.size, reference.digest)
        content = store.get_bytes(reference)
        value = json.loads(content)
        if canonical_json_bytes(value) != content:
            raise ValueError("retained.unit.canonical-sdk-metadata-required")
        retained = BlobRef.from_dict(retain(content, media_type=reference.media_type))
        if retained != reference:
            raise ValueError("retained.unit.sdk-metadata-reference-differs")
        sdk_references[reference.identity] = reference
        return value

    execution_identities = tuple(
        sorted(
            {
                identity
                for observation in (*run.observations, *run.acceptance_observations)
                for identity in observation.native_sdk_executions
            },
            key=lambda value: value.uri,
        )
    )
    retained_sdk_metadata_closure(
        execution_identities,
        read_document=read_sdk_document,
        guard=admission_guard,
        maximum_records=min(
            1_000_000, maximum_bytes // 512, maximum_memory_bytes // 512
        ),
    )
    document = {
        "schema": RUN_EVIDENCE_SCHEMA,
        "retained_receipt_identity": admission.receipt.identity.uri,
        "tool_observation": retain(run.resolved_bom.tool_observation),
        "export_observation": retain(run.resolved_bom.export_observation),
        "sdk_dependencies": [
            retain(canonical_json_bytes(value.to_dict()))
            for value in run.resolved_bom.sdk_dependencies
        ],
        "sdk_metadata_records": [
            sdk_references[key].to_dict() for key in sorted(sdk_references)
        ],
        "command_output_manifests": [
            retain(canonical_json_bytes(value.output_manifest.to_dict()))
            if value.output_manifest is not None
            else None
            for value in run.observations
        ],
        "admitted": False,
        "release_authorized": False,
    }
    reference = BlobRef.from_dict(retain(canonical_json_bytes(document)))
    # Copying assertions performs no execution or admission. Recheck all source
    # and receipt custody once more before any successful root can be returned.
    admission.require_current()
    admission_guard()
    return reference

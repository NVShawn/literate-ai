"""Transfer exact predecessor bytes between worker-local content-addressed stores."""

from __future__ import annotations

from collections.abc import Mapping

from literate_ai.application.component_workers import ComponentWorkerError
from literate_ai.contracts.component_workers import (
    ComponentArtifactHandoff,
    ComponentArtifactImportReceipt,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.storage.cas import BlobIntegrityError, FileSystemCAS


def import_component_artifacts(
    handoff: ComponentArtifactHandoff,
    *,
    worker_identity: ContentIdentity,
    sources: Mapping[str, FileSystemCAS],
    destination: FileSystemCAS,
) -> ComponentArtifactImportReceipt:
    """Verify source and destination bytes before issuing a complete import receipt.

    Stores are supplied by trusted transport setup, keyed by producer worker URI.
    The existing CAS streams regular files, rejects symlinks, and verifies content
    digests. A failed transfer can leave verified blobs, but never a success receipt.
    This receipt establishes byte custody, not ABI compatibility or native acceptance.
    """
    if handoff.consumer.worker_identity != worker_identity:
        raise ComponentWorkerError("artifact handoff is addressed to another worker")
    for predecessor in handoff.predecessors:
        source = sources.get(predecessor.worker_identity.uri)
        if source is None:
            raise ComponentWorkerError(
                "required predecessor worker store is unavailable"
            )
        for export in predecessor.exports:
            source.verify(export.blob)
            copied = destination.put_file(
                source.path_for(export.blob),
                media_type=export.blob.media_type,
            )
            if copied != export.blob:
                raise BlobIntegrityError("predecessor artifact changed during transfer")
            destination.verify(export.blob)
    return ComponentArtifactImportReceipt(
        handoff.identity,
        worker_identity,
        handoff.export_identities,
    )

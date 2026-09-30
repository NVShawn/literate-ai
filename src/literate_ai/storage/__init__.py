"""Durable local storage primitives for Literate AI."""

from .cas import (
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRef,
    FileSystemCAS,
    StorageError,
    StorageSafetyError,
    canonical_json_bytes,
)
from .events import AppendOnlyEventStore, EventRecord, EventStoreError
from .indexes import (
    DependencyEdge,
    DependencyIndex,
    IndexError,
    ReferenceConflictError,
    ReferenceIndex,
    ReferenceRecord,
)

__all__ = [
    "AppendOnlyEventStore",
    "BlobIntegrityError",
    "BlobNotFoundError",
    "BlobRef",
    "DependencyEdge",
    "DependencyIndex",
    "EventRecord",
    "EventStoreError",
    "FileSystemCAS",
    "IndexError",
    "ReferenceConflictError",
    "ReferenceIndex",
    "ReferenceRecord",
    "StorageError",
    "StorageSafetyError",
    "canonical_json_bytes",
]

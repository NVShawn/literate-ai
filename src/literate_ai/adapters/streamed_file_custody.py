"""Bounded fresh-byte custody without retaining generated file payloads.

This is a digest representation of file observations, never an execution grant.
Every require_unchanged reopens and hashes every admitted file. No successful
guard result or filesystem observation is shared across actions.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_native_file_custody import native_file_digest
from literate_ai.adapters.retained_project_inputs import _root
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_cargo_native_custody import (
    RetainedCargoOutputBudget,
)


def _physical(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True)
class _File:
    path: Path
    boundary: Path
    blob: BlobRef
    physical: tuple
    lineage: tuple
    labels: tuple[str, ...]


class StreamedFileCustody:
    """Operation-local expected digests with original file and ancestor custody."""

    def __init__(self, *, maximum_files, budget: RetainedCargoOutputBudget):
        if type(maximum_files) is not int or not 1 <= maximum_files <= 100000:
            raise ValueError("retained.native-custody.file-limit-invalid")
        if type(budget) is not RetainedCargoOutputBudget:
            raise TypeError("retained.native-custody.explicit-budget-required")
        self.maximum_files = maximum_files
        self.maximum_file_bytes = budget.maximum_file_bytes
        self.maximum_total_bytes = budget.maximum_total_bytes
        self._entries = {}
        self._labels = {}
        self._total_bytes = 0

    def _observe(self, path, boundary, *, maximum_bytes):
        path = Path(os.path.abspath(path))
        boundary = _root(Path(boundary))
        if not path.is_relative_to(boundary) or path == boundary:
            raise ValueError("retained.native-custody.path-escape")
        try:
            lineage = tuple((p, directory_node(p)) for p in path.parents)
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("retained.native-custody.file-unsafe")
            if before.st_size > maximum_bytes:
                raise ValueError("retained.native-custody.byte-limit")
            # Reuse the measured native descriptor/hash boundary, including its
            # Linux/Windows second content read for coarse/deferred timestamps.
            digest, count = native_file_digest(path, maximum_bytes)
            after = path.lstat()
            if _physical(before) != _physical(after):
                raise ValueError("retained.native-custody.file-changed")
            if tuple((p, directory_node(p)) for p in path.parents) != lineage:
                raise ValueError("retained.native-custody.ancestor-changed")
        except (OSError, ValueError) as error:
            if str(error).startswith("retained.native-custody."):
                raise
            raise ValueError("retained.native-custody.file-unavailable") from error
        blob = BlobRef(digest, count)
        return _File(path, boundary, blob, _physical(after), lineage, ())

    def pin(self, path, *, boundary, label):
        if not isinstance(label, str) or not label or len(label.encode()) > 1024:
            raise ValueError("retained.native-custody.label-invalid")
        path, boundary = Path(os.path.abspath(path)), _root(Path(boundary))
        key = path, boundary
        previous = self._entries.get(key)
        if previous is None and len(self._entries) >= self.maximum_files:
            raise ValueError("retained.native-custody.file-limit")
        remaining = (
            self.maximum_total_bytes
            - self._total_bytes
            + (previous.blob.size if previous is not None else 0)
        )
        observed = self._observe(
            path, boundary, maximum_bytes=min(self.maximum_file_bytes, remaining)
        )
        if previous is not None and (
            observed.blob != previous.blob
            or observed.physical != previous.physical
            or observed.lineage != previous.lineage
        ):
            raise ValueError("retained.native-custody.file-changed")
        if label in self._labels and self._labels[label] != key:
            raise ValueError("retained.native-custody.label-conflict")
        if label not in self._labels and len(self._labels) >= self.maximum_files:
            raise ValueError("retained.native-custody.label-limit")
        labels = tuple(
            sorted({label, *(previous.labels if previous is not None else ())})
        )
        self._entries[key] = _File(
            path, boundary, observed.blob, observed.physical, observed.lineage, labels
        )
        self._labels[label] = key
        self._total_bytes += observed.blob.size - (
            previous.blob.size if previous is not None else 0
        )
        return observed.blob

    @property
    def identity(self):
        # Preserve the existing byte/label identity for the same bounded files;
        # representation and physical witnesses do not rewrite content identity.
        return canonical_identity(
            {
                "schema": "literate-ai/pinned-input-closure@1",
                "inputs": [
                    {
                        "labels": list(item.labels),
                        "content_identity": item.blob.identity,
                        "bytes": item.blob.size,
                    }
                    for item in sorted(
                        self._entries.values(), key=lambda item: item.labels
                    )
                ],
            }
        ).uri

    @property
    def file_count(self):
        return len(self._entries)

    @property
    def total_bytes(self):
        return self._total_bytes

    def require_unchanged(self):
        for item in self._entries.values():
            current = self._observe(
                item.path, item.boundary, maximum_bytes=self.maximum_file_bytes
            )
            if (
                current.blob != item.blob
                or current.physical != item.physical
                or current.lineage != item.lineage
            ):
                raise ValueError("retained.native-custody.file-changed")

"""Shared test fixtures extracted from test_retained_cargo_files."""

import hashlib

from literate_ai.contracts.blobs import BlobRef


def blob(content):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/json"
    )

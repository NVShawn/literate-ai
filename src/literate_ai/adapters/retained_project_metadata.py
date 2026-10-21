"""Read original or sealed retained metadata through exact bounded byte custody."""

from __future__ import annotations

import io
import json
from pathlib import Path

from literate_ai.adapters.retained_project_inputs import _stream
from literate_ai.contracts.retained_project import RetainedProjectMember


def read_retained_project_document(
    root: Path,
    member: RetainedProjectMember,
    *,
    maximum_bytes: int = 16 * 1024 * 1024,
    error_prefix: str = "retained.metadata",
):
    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    buffer = io.BytesIO()
    size, mode, digest = _stream(root, member.path, maximum_bytes, output=buffer)
    if (size, mode, digest) != (member.size, member.mode, member.sha256):
        raise ValueError(error_prefix + ".document-changed")
    try:
        return json.loads(buffer.getvalue(), object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(error_prefix + ".document-invalid") from None

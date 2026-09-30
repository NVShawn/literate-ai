"""Promote runtime cache entries into a project's committable source cache.

A project may declare a project-relative source-cache target and track it in Git, so a
clone can reuse generated source instead of regenerating it. Nothing published into that
target: generation writes to the operator-bound runtime target, and moving an entry
across was a manual copy nobody could perform correctly, because an entry is a manifest
plus content-addressed objects plus a membership record written last.

`litai cache publish` performs that promotion through the same publication path the
lifecycle uses, so a committed entry is verified exactly as a runtime one is.

Promotion is deliberate and never automatic. A committed entry is still not authority:
every hit remains acceptance-untrusted until the current lifecycle rebuilds and verifies
it, and a hit requires an exact generation-key match, so a changed specification simply
misses rather than resolving to an older tree.
"""

from __future__ import annotations

from typing import Any

from literate_ai.adapters.cache import FilesystemProjectSourceCachePublicationAdapter
from literate_ai.application import (
    SOURCE_CACHE_PUBLICATION_NOTE,
    SourceCachePublicationError,
)

from .errors import CliFailure

CACHE_PUBLISH_SCHEMA = "literate-ai/source-cache-publish@1"


def cache_publish_from_args(args) -> dict[str, Any]:
    """Copy every runtime entry into the declared committable target."""

    try:
        result = FilesystemProjectSourceCachePublicationAdapter().publish(
            getattr(args, "project", "."),
            target_id=getattr(args, "target", None),
        )
    except SourceCachePublicationError as exc:
        raise CliFailure(exc.code, exc.message) from exc

    return {
        "schema": CACHE_PUBLISH_SCHEMA,
        "target": result.target_id,
        "destination": result.destination,
        "published": [item.digest[:16] for item in result.published_entry_identities],
        "already_present": [
            item.digest[:16] for item in result.already_present_entry_identities
        ],
        "note": SOURCE_CACHE_PUBLICATION_NOTE,
    }


__all__ = ["CACHE_PUBLISH_SCHEMA", "cache_publish_from_args"]

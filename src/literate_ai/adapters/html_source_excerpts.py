"""Read bounded source previews using hashes already bound by the authority graph.

These immutable UI observations are not a new authority graph or public wire
contract. Missing path/hash pairs are explicitly unavailable. Declared local
sources must match exactly; a stale or unsafe file refuses the whole observation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.html_surfaces import HtmlSurfaceSource
from literate_ai.contracts.html_observability import HtmlRenderRefusal
from literate_ai.contracts.identity import ContentIdentity, HashAlgorithm
from literate_ai.contracts.paths import canonical_relative_posix_path

MAXIMUM_SOURCE_BYTES = 2 * 1024 * 1024
MAXIMUM_TOTAL_BYTES = 8 * 1024 * 1024
MAXIMUM_SOURCE_FILES = 2048
EXCERPT_LINES = 80
EXCERPT_CHARACTERS = 8000


@dataclass(frozen=True, slots=True)
class HtmlSourceExcerpt:
    node_id: str
    path: str | None
    source_identity: ContentIdentity | None
    text: str | None
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "source_identity": (
                self.source_identity.to_dict() if self.source_identity else None
            ),
            "text": self.text,
            "truncated": self.truncated,
            "unavailable_reason": (
                "No identity-bound local source is declared."
                if self.text is None
                else None
            ),
        }


def _read_bound_source(
    root: Path, relative: str, expected: ContentIdentity, maximum_bytes: int
) -> bytes:
    path = canonical_relative_posix_path(relative, label="HTML source excerpt")
    if path.suffix.lower() not in (".md", ".json"):
        raise ValueError("HTML excerpts require Markdown or JSON source")
    target = root.joinpath(*path.parts)
    require_safe_directory(target.parent)
    before = target.lstat()
    if (
        stat_is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum_bytes
        or not target.resolve(strict=True).is_relative_to(root)
    ):
        raise ValueError("HTML source excerpt path is unsafe or oversized")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum_bytes:
            raise ValueError("HTML source excerpt is not a bounded regular file")
        chunks = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(65536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    require_safe_directory(target.parent)
    named = target.lstat()
    if (
        len(content) > maximum_bytes
        or stat_is_link_or_reparse(named)
        or not stat.S_ISREG(named.st_mode)
        or any(
            (metadata.st_size, metadata.st_mtime_ns)
            != (before.st_size, before.st_mtime_ns)
            for metadata in (opened, after, named)
        )
        or len(content) != before.st_size
        or ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())
        != expected
    ):
        raise ValueError("HTML source excerpt no longer matches its graph binding")
    return content


def load_source_excerpts(
    project_root: Path, source: HtmlSurfaceSource
) -> tuple[HtmlSourceExcerpt, ...] | HtmlRenderRefusal:
    """Observe only exact local source bindings; never guess or fetch other paths."""
    try:
        root = Path(project_root).resolve(strict=True)
        require_safe_directory(root)
        excerpts = []
        total_bytes = 0
        source_files = 0
        for node in source.document()["nodes"]:
            properties = node["properties"]
            relative, identity = properties.get("path"), properties.get("identity")
            if relative is None or identity is None:
                excerpts.append(HtmlSourceExcerpt(node["id"], None, None, None, False))
                continue
            source_files += 1
            if source_files > MAXIMUM_SOURCE_FILES:
                raise ValueError("HTML excerpt source file limit exceeded")
            expected = ContentIdentity.parse_uri(identity)
            content = _read_bound_source(
                root,
                relative,
                expected,
                min(MAXIMUM_SOURCE_BYTES, MAXIMUM_TOTAL_BYTES - total_bytes),
            )
            total_bytes += len(content)
            text = content.decode("utf-8")
            excerpt = "".join(text.splitlines(keepends=True)[:EXCERPT_LINES])
            excerpt = excerpt[:EXCERPT_CHARACTERS]
            excerpts.append(
                HtmlSourceExcerpt(
                    node["id"], relative, expected, excerpt, excerpt != text
                )
            )
        return tuple(excerpts)
    except (OSError, UnicodeError, ValueError, TypeError, UnsafeFilesystemPathError):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "An identity-bound source excerpt is unavailable, unsafe, or changed.",
        )

"""Atomic publication of recognized derived HTML, without following output links.

The caller holds the existing project lifecycle lock. Snapshot checks detect
non-cooperative changes but do not isolate this process from a malicious user
with the same filesystem privileges. Foreign files are never adopted as output.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    ensure_safe_directory,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters import html_emitter
from literate_ai.adapters.html_source_excerpts import HtmlSourceExcerpt
from literate_ai.adapters.html_surfaces import HtmlSurfaceSource, html_surfaces
from literate_ai.contracts.html_observability import HtmlProvenance, HtmlView
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.paths import canonical_relative_posix_path

MAXIMUM_HTML_BYTES = 64 * 1024 * 1024
_RESERVED_DIRECTORIES = frozenset({".git", ".hg", ".svn", ".codegraph", ".litai-locks"})


@dataclass(frozen=True, slots=True)
class OutputSnapshot:
    signature: tuple[int, ...]
    content: bytes


def _signature(node: os.stat_result) -> tuple[int, ...]:
    return (
        node.st_dev,
        node.st_ino,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
        node.st_mode,
    )


def html_output_path(root: Path, relative: str) -> Path:
    path = canonical_relative_posix_path(relative, label="HTML output")
    if path.suffix != ".html" or _RESERVED_DIRECTORIES.intersection(path.parts):
        raise ValueError("HTML output must be outside repository metadata")
    target = root.joinpath(*path.parts)
    # Missing parents are allowed, but every existing ancestor must be direct.
    ancestor = target.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    require_safe_directory(ancestor)
    if not target.resolve().is_relative_to(root):
        raise ValueError("HTML output is outside the selected project")
    if target.is_symlink():
        raise ValueError("HTML output is indirect")
    return target


def read_output(target: Path) -> OutputSnapshot | None:
    """Read a bounded direct file through the descriptor that was inspected."""
    try:
        before = target.lstat()
    except FileNotFoundError:
        return None
    require_safe_directory(target.parent)
    if (
        stat_is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > MAXIMUM_HTML_BYTES
    ):
        raise ValueError("existing HTML output is not a bounded direct file")
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(target, flags)
    try:
        observed = _signature(before)
        opened = _signature(os.fstat(descriptor))
        # CPython Windows pathname stat may expose birth time as ctime while
        # descriptor stat exposes metadata-change time. Bind shared fields across
        # APIs, but retain each complete clock snapshot for its own drift checks.
        if (
            opened[:4] != observed[:4]
            or opened[-1] != observed[-1]
            or _signature(target.lstat()) != observed
        ):
            raise ValueError("HTML output changed before reading")
        chunks = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        require_safe_directory(target.parent)
        if (
            len(content) != before.st_size
            or _signature(os.fstat(descriptor)) != opened
            or _signature(target.lstat()) != observed
        ):
            raise ValueError("HTML output changed while reading")
        return OutputSnapshot(observed, content)
    finally:
        os.close(descriptor)


def derived_output_provenance(content: bytes, view: HtmlView) -> HtmlProvenance:
    """Recognize exact supported-template output, not merely a borrowed marker.

    Embedded observations may be stale. This recognition permits replacement;
    it is NOT current-source verification or evidence for the staleness gate.
    """
    parser = html_emitter._MarkupInspection()
    parser.feed(content.decode("utf-8"))
    parser.close()
    provenance = HtmlProvenance.from_dict(json.loads(parser.inline["litai-provenance"]))
    if provenance.view != view or len(provenance.source_bindings) != 1:
        raise ValueError("existing output belongs to another view")
    surfaces = {surface.surface_id: surface for surface in html_surfaces()}
    source_binding = provenance.source_bindings[0]
    surface = surfaces.get(source_binding.source_label)
    if surface is None or surface.source_schema != source_binding.source_schema:
        raise ValueError("existing output uses an unregistered source binding")
    include_dag = surface.surface_id == "authority-graph"
    expected_renderer = html_emitter.html_renderer_binding(
        provenance.renderer.framework_distribution_identity,
        provenance.renderer.schema_catalog_release,
        provenance.external_assets,
        include_dag=include_dag,
        surface_id=surface.surface_id,
    )
    if provenance.renderer != expected_renderer:
        raise ValueError("existing output uses an unrecognized renderer")
    source = HtmlSurfaceSource(
        surface,
        view,
        source_binding,
        parser.inline["litai-source"].encode("utf-8"),
    )
    excerpts = None
    if include_dag:
        raw_excerpts = json.loads(parser.inline["litai-excerpts"])
        if not isinstance(raw_excerpts, list) or len(raw_excerpts) > 2048:
            raise ValueError("existing output excerpts are invalid")
        excerpts = tuple(
            HtmlSourceExcerpt(
                item["node_id"],
                item["path"],
                ContentIdentity.from_dict(item["source_identity"])
                if item["source_identity"] is not None
                else None,
                item["text"],
                item["truncated"],
            )
            for item in raw_excerpts
        )
    html_emitter._inspect_markup(content, provenance, source, excerpts)
    if html_emitter._markup(source, provenance, excerpts) != content:
        raise ValueError("existing HTML contains foreign or edited content")
    return provenance


def require_derived_output(content: bytes, view: HtmlView) -> None:
    """Reject output that is not an exact supported derived artifact."""
    derived_output_provenance(content, view)


def publish_html(
    target: Path,
    content: bytes,
    previous: OutputSnapshot | None,
    *,
    revalidate: Callable[[], None],
) -> None:
    """Publish complete bytes; missing destinations use atomic no-clobber linking."""
    if not content or len(content) > MAXIMUM_HTML_BYTES:
        raise ValueError("HTML output exceeds the publication byte limit")
    ensure_safe_directory(target.parent)
    if read_output(target) != previous:
        raise ValueError("HTML output changed before publication")
    if previous is not None and previous.content == content:
        revalidate()
        return
    descriptor, name = tempfile.mkstemp(prefix=".litai-html-", dir=target.parent)
    temporary = Path(name)
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows may finalize write timestamps only when the writer closes.
        # Reopen through the bounded no-follow reader, retain the created file's
        # identity/mode, and compare actual bytes before trusting this snapshot.
        written = read_output(temporary)
        if (
            written is None
            or written.signature[:2] != (owned.st_dev, owned.st_ino)
            or written.signature[-1] != owned.st_mode
            or written.content != content
        ):
            raise ValueError("HTML temporary output changed after writing")
        revalidate()
        require_safe_directory(target.parent)
        if read_output(target) != previous or read_output(temporary) != written:
            raise ValueError("HTML publication paths changed")
        if previous is None:
            os.link(temporary, target, follow_symlinks=False)
        else:
            os.replace(temporary, target)
    finally:
        # Do not delete a replacement installed at our temporary pathname.
        try:
            require_safe_directory(temporary.parent)
            named = temporary.lstat()
            if (named.st_dev, named.st_ino) == (owned.st_dev, owned.st_ino):
                temporary.unlink()
        except FileNotFoundError:
            pass

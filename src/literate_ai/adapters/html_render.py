"""Rendering service: existing emitter, cache primitives and atomic publication."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
)
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import observe_html_framework
from literate_ai.adapters.html_publication import (
    MAXIMUM_HTML_BYTES,
    html_output_path,
    publish_html,
    read_output,
    require_derived_output,
)
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.cache_directories import (
    CacheDirectoryError,
    ensure_cache_directory,
    require_cache_directory_current,
    resolve_cache_directories,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_observability import (
    HtmlArtifact,
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlRenderResult,
)
from literate_ai.diagnostics import debug_stage
from literate_ai.projects import ProjectError, discover_project
from literate_ai.storage import FileSystemCAS, ReferenceIndex, StorageError

_CACHE_NAMESPACE = "html"


class _SourceChanged(ValueError):
    """The last publication-bound source or installation observation drifted."""


def _utc_observation() -> str:
    # The accepted provenance contract has second precision. Independent fresh
    # observations in the same second may therefore legitimately share bytes.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refused(code: str, message: str) -> HtmlRenderResult:
    return HtmlRenderResult("refused", None, HtmlRenderRefusal(code, message))


def _cache(
    project: Path, *, writable: bool, output: Path | None = None
) -> tuple[FileSystemCAS, ReferenceIndex] | None:
    obj = resolve_cache_directories(project).obj_dir
    root = obj / "html-observability"
    if output is not None and output.is_relative_to(root):
        raise CacheDirectoryError("HTML output cannot be inside its managed cache")
    if writable:
        ensure_cache_directory(obj, kind="object", project_root=project)
        ensure_safe_directory(root / "cas")
        ensure_safe_directory(root / "index")
    elif not root.exists() and not root.is_symlink():
        return None
    require_cache_directory_current(obj, kind="object", project_root=project)
    require_safe_directory(root)
    cas = FileSystemCAS(root / "cas", create=writable)
    return cas, ReferenceIndex(root / "index", cas, read_only=not writable)


def _cached_artifact(
    cache: tuple[FileSystemCAS, ReferenceIndex],
    key: str,
) -> tuple[HtmlArtifact, bytes] | None:
    cas, index = cache
    record = index.resolve(_CACHE_NAMESPACE, key)
    if record is None:
        return None
    if (
        record.namespace != _CACHE_NAMESPACE
        or record.name != key
        or record.target.size > 1024 * 1024
    ):
        raise ValueError("HTML cache alias is invalid or oversized")
    artifact = HtmlArtifact.from_dict(cas.get_manifest(record.target))
    if artifact.byte_size > MAXIMUM_HTML_BYTES:
        raise ValueError("cached HTML is oversized")
    content = cas.get_bytes(
        BlobRef(
            artifact.artifact_identity.digest,
            artifact.byte_size,
            media_type="text/html",
        )
    )
    return artifact, content


def render_html(project: Path, request: HtmlRenderRequest) -> HtmlRenderResult:
    """Render one project-relative file. Cache policy never grants overwrite rights."""
    try:
        loaded = discover_project(project)
        if loaded is None:
            raise ValueError("missing project")
        root = loaded.root.resolve(strict=True)
        target = html_output_path(root, request.output_path)
    except (OSError, ValueError, RuntimeError, ProjectError):
        return _refused(
            "render.output_outside_project",
            "The HTML destination is unavailable, indirect or outside the project.",
        )
    with debug_stage("html.framework", phase="initial"):
        framework = observe_html_framework()
    if isinstance(framework, HtmlRenderRefusal):
        return HtmlRenderResult("refused", None, framework)

    def emit(stamp: str) -> HtmlEmission | HtmlRenderRefusal:
        with debug_stage("html.emit", surface=request.surface_id):
            return emit_html(
                root,
                request,
                framework_distribution_identity=framework.framework_distribution_identity,
                schema_catalog_release=framework.schema_catalog_release,
                generated_at=stamp,
            )

    fresh = emit(_utc_observation())
    if isinstance(fresh, HtmlRenderRefusal):
        return HtmlRenderResult("refused", None, fresh)
    try:
        if len(fresh.content) > MAXIMUM_HTML_BYTES:
            raise ValueError("HTML output exceeds the publication byte limit")
        previous = read_output(target)
        if previous is not None:
            require_derived_output(previous.content, request.view)
        with project_lifecycle_lock(root, operation="render.html"):
            status = "rendered"
            emission = fresh
            cache = None
            key = fresh.artifact.provenance.render_inputs_identity.digest
            # Even read-write lookup is genuinely read-only; only a miss publishes.
            if request.cache_mode in {"read-only", "read-write"}:
                cache = _cache(root, writable=False, output=target)
                cached = _cached_artifact(cache, key) if cache is not None else None
                if cached is not None:
                    artifact, content = cached
                    candidate = emit(artifact.provenance.generated_at)
                    rebound = replace(artifact, artifact_path=request.output_path)
                    if (
                        not isinstance(candidate, HtmlEmission)
                        or candidate.artifact != rebound
                        or candidate.content != content
                        or artifact.provenance.render_inputs_identity.digest != key
                    ):
                        raise ValueError(
                            "cached HTML does not reproduce current source"
                        )
                    emission = candidate
                    status = "cached"
            current = emit(emission.artifact.provenance.generated_at)
            if current != emission or observe_html_framework() != framework:
                return _refused(
                    "render.surface_unavailable",
                    "HTML source or renderer installation changed before publication.",
                )
            if request.cache_mode == "write-only" or (
                request.cache_mode == "read-write" and status != "cached"
            ):
                cache = _cache(root, writable=True, output=target)
                assert cache is not None
                cas, index = cache
                cas.put_bytes(emission.content, media_type="text/html")
                manifest = cas.put_manifest(emission.artifact.to_dict())
                index.set(_CACHE_NAMESPACE, key, manifest)

            def revalidate() -> None:
                if cache is not None:
                    require_cache_directory_current(
                        cache[0].root.parent.parent, kind="object", project_root=root
                    )
                if (
                    emit(emission.artifact.provenance.generated_at) != emission
                    or observe_html_framework() != framework
                ):
                    raise _SourceChanged("HTML inputs changed before publication")

            with debug_stage("html.publish", surface=request.surface_id):
                publish_html(target, emission.content, previous, revalidate=revalidate)
            return HtmlRenderResult(status, emission.artifact, None)
    except _SourceChanged:
        return _refused(
            "render.surface_unavailable",
            "HTML source or renderer installation changed before publication.",
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RecursionError,
        StorageError,
        CacheDirectoryError,
        UnsafeFilesystemPathError,
        ProjectLifecycleLockError,
    ):
        return _refused(
            "render.cache_write_forbidden",
            "HTML cache or output publication was refused: existing content must be "
            "recognized derived HTML, paths must be safe and unchanged, and cache "
            "records must reproduce the current artifact.",
        )

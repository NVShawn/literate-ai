"""Filesystem composition for deliberate project source-cache publication."""

from __future__ import annotations

from pathlib import Path

from literate_ai.application.source_cache_publication import (
    SourceCachePublicationError,
    SourceCachePublicationResult,
    SourceCachePublicationService,
)
from literate_ai.cache_directories import resolve_cache_directories
from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    ContentIdentity,
    SourceCacheRootKind,
    SourceIntelligenceAttachment,
    StandardSourceAdmissionCacheEntry,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

from .filesystem import FileSystemSourceCache, SourceCacheError

RUNTIME_SOURCE_CACHE_TARGET_ID = "standard-local"


class _FilesystemPublicationSource:
    def __init__(self, store: FileSystemSourceCache) -> None:
        self.store = store

    def published_entries(
        self,
    ) -> tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...]:
        try:
            return self.store.published_entries()
        except (OSError, SourceCacheError, TypeError, ValueError) as exc:
            raise SourceCachePublicationError(
                "source_cache.runtime_unreadable",
                "runtime source cache could not be read",
            ) from exc

    def intelligence_attachments(
        self, source_tree_identity: ContentIdentity
    ) -> tuple[SourceIntelligenceAttachment, ...]:
        try:
            return self.store.intelligence_attachments(source_tree_identity)
        except (OSError, SourceCacheError, TypeError, ValueError):
            return ()


class _FilesystemPublicationDestination:
    def __init__(
        self,
        store: FileSystemSourceCache,
        source: FileSystemSourceCache,
    ) -> None:
        if source.cas is None:
            raise SourceCachePublicationError(
                "source_cache.runtime_absent",
                "runtime source cache has no content-addressed storage",
            )
        self.store = store
        self.source_cas = source.cas

    def published_entry_identities(self) -> tuple[ContentIdentity, ...]:
        try:
            return tuple(entry.identity for entry in self.store.published_entries())
        except (OSError, SourceCacheError, TypeError, ValueError) as exc:
            raise SourceCachePublicationError(
                "source_cache.destination_unreadable",
                "destination source cache could not be read",
            ) from exc

    def publish(
        self,
        entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
        *,
        intelligence_attachments: tuple[SourceIntelligenceAttachment, ...] = (),
    ) -> ContentIdentity:
        try:
            return self.store.publish(
                entry,
                caller_cas=self.source_cas,
                intelligence_attachments=intelligence_attachments,
            )
        except (OSError, SourceCacheError, TypeError, ValueError) as exc:
            raise SourceCachePublicationError(
                "source_cache.publish_failed",
                "accepted source-cache entry could not be published",
            ) from exc


class FilesystemProjectSourceCachePublicationAdapter:
    """Resolve project policy and invoke the public publication service."""

    def __init__(self, service: SourceCachePublicationService | None = None) -> None:
        self.service = service or SourceCachePublicationService()

    def publish(
        self,
        selected: str | Path,
        *,
        target_id: str | None = None,
    ) -> SourceCachePublicationResult:
        try:
            project = discover_project(Path(selected))
        except ProjectError as exc:
            raise SourceCachePublicationError(exc.code, exc.message) from exc
        if project is None:
            raise SourceCachePublicationError(
                "project.not_found",
                f"no {PROJECT_FILENAME} found from {selected}",
            )
        configuration = project.definition.source_cache
        if configuration is None:
            raise SourceCachePublicationError(
                "source_cache.undeclared",
                "project declares no source_cache policy; nothing to publish into",
            )
        committable = [
            target
            for target in configuration.targets
            if target.root_kind is SourceCacheRootKind.PROJECT_RELATIVE
            and (target_id is None or target.target_id == target_id)
        ]
        if not committable:
            raise SourceCachePublicationError(
                "source_cache.no_committable_target",
                "project declares no project-relative source-cache target"
                + (f" named {target_id!r}" if target_id is not None else ""),
            )
        if len(committable) > 1:
            raise SourceCachePublicationError(
                "source_cache.ambiguous_target",
                "project declares several committable targets; select one: "
                + ", ".join(item.target_id for item in committable),
            )
        target = committable[0]
        directories = resolve_cache_directories(project.root)
        runtime_root = directories.build_dir / "accepted-source-cache"
        try:
            runtime = FileSystemSourceCache(
                RUNTIME_SOURCE_CACHE_TARGET_ID,
                runtime_root,
                writable=False,
            )
        except (OSError, SourceCacheError, TypeError, ValueError) as exc:
            raise SourceCachePublicationError(
                "source_cache.runtime_unreadable",
                "runtime source cache could not be opened",
            ) from exc
        if not runtime.available or runtime.cas is None:
            raise SourceCachePublicationError(
                "source_cache.runtime_absent",
                f"no runtime source cache at {runtime_root}; build before publishing",
            )
        destination_root = project.root.joinpath(*Path(target.root_reference).parts)
        try:
            destination = FileSystemSourceCache(target.target_id, destination_root)
        except (OSError, SourceCacheError, TypeError, ValueError) as exc:
            raise SourceCachePublicationError(
                "source_cache.destination_unreadable",
                "destination source cache could not be opened",
            ) from exc
        return self.service.publish(
            _FilesystemPublicationSource(runtime),
            _FilesystemPublicationDestination(destination, runtime),
            target_id=target.target_id,
            destination_reference=str(destination_root),
        )


__all__ = [
    "FilesystemProjectSourceCachePublicationAdapter",
    "RUNTIME_SOURCE_CACHE_TARGET_ID",
]

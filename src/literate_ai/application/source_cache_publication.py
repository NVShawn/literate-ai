"""Application service for deliberate accepted-source cache publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    ContentIdentity,
    SourceIntelligenceAttachment,
    StandardSourceAdmissionCacheEntry,
)

SOURCE_CACHE_PUBLICATION_NOTE = (
    "committed entries are not authority; every hit stays acceptance-untrusted"
)


class SourceCachePublicationError(RuntimeError):
    """Stable failure at the deliberate source-cache publication boundary."""

    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("source-cache publication error code cannot be empty")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("source-cache publication error message cannot be empty")
        self.code = code
        self.message = message
        super().__init__(message)


class AcceptedSourceCachePublicationSource(Protocol):
    """Read verified accepted entries and their optional detached intelligence."""

    def published_entries(
        self,
    ) -> tuple[AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry, ...]: ...

    def intelligence_attachments(
        self, source_tree_identity: ContentIdentity
    ) -> tuple[SourceIntelligenceAttachment, ...]: ...


class AcceptedSourceCachePublicationDestination(Protocol):
    """Observe and publish exact accepted entries without owning source authority."""

    def published_entry_identities(self) -> tuple[ContentIdentity, ...]: ...

    def publish(
        self,
        entry: AcceptedSourceCacheEntry | StandardSourceAdmissionCacheEntry,
        *,
        intelligence_attachments: tuple[SourceIntelligenceAttachment, ...] = (),
    ) -> ContentIdentity: ...


@dataclass(frozen=True, slots=True)
class SourceCachePublicationResult:
    """Exact result of one explicit, restartable cache-publication pass."""

    target_id: str
    destination: str
    published_entry_identities: tuple[ContentIdentity, ...]
    already_present_entry_identities: tuple[ContentIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise ValueError("source-cache publication target ID cannot be empty")
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise ValueError("source-cache publication destination cannot be empty")
        identities = (
            *self.published_entry_identities,
            *self.already_present_entry_identities,
        )
        if any(not isinstance(item, ContentIdentity) for item in identities):
            raise TypeError(
                "source-cache publication results require content identities"
            )
        uris = tuple(item.uri for item in identities)
        if len(uris) != len(set(uris)):
            raise ValueError(
                "source-cache publication results repeat an entry identity"
            )
        if tuple(item.uri for item in self.published_entry_identities) != tuple(
            sorted(item.uri for item in self.published_entry_identities)
        ):
            raise ValueError("published source-cache identities must be canonical")
        if tuple(item.uri for item in self.already_present_entry_identities) != tuple(
            sorted(item.uri for item in self.already_present_entry_identities)
        ):
            raise ValueError("existing source-cache identities must be canonical")


class SourceCachePublicationService:
    """Copy accepted entries through injected, independently verifiable cache ports."""

    def publish(
        self,
        source: AcceptedSourceCachePublicationSource,
        destination: AcceptedSourceCachePublicationDestination,
        *,
        target_id: str,
        destination_reference: str,
    ) -> SourceCachePublicationResult:
        entries = tuple(source.published_entries())
        if any(
            not isinstance(
                entry, (AcceptedSourceCacheEntry, StandardSourceAdmissionCacheEntry)
            )
            for entry in entries
        ):
            raise SourceCachePublicationError(
                "source_cache.runtime_unreadable",
                "source cache returned an invalid accepted entry",
            )
        existing = tuple(destination.published_entry_identities())
        if any(not isinstance(identity, ContentIdentity) for identity in existing):
            raise SourceCachePublicationError(
                "source_cache.destination_unreadable",
                "destination cache returned an invalid entry identity",
            )
        existing_by_uri = {identity.uri for identity in existing}
        published: list[ContentIdentity] = []
        already_present: list[ContentIdentity] = []
        for entry in sorted(entries, key=lambda item: item.identity.uri):
            if entry.identity.uri in existing_by_uri:
                already_present.append(entry.identity)
                continue
            attachments = tuple(
                source.intelligence_attachments(entry.source_tree_identity)
            )
            if any(
                not isinstance(item, SourceIntelligenceAttachment)
                for item in attachments
            ):
                raise SourceCachePublicationError(
                    "source_cache.runtime_unreadable",
                    "source cache returned an invalid intelligence attachment",
                )
            identity = destination.publish(
                entry,
                intelligence_attachments=attachments,
            )
            if identity != entry.identity:
                raise SourceCachePublicationError(
                    "source_cache.publish_mismatch",
                    "destination cache returned another entry identity",
                )
            existing_by_uri.add(identity.uri)
            published.append(identity)
        return SourceCachePublicationResult(
            target_id=target_id,
            destination=destination_reference,
            published_entry_identities=tuple(
                sorted(published, key=lambda item: item.uri)
            ),
            already_present_entry_identities=tuple(
                sorted(already_present, key=lambda item: item.uri)
            ),
        )


__all__ = [
    "AcceptedSourceCachePublicationDestination",
    "AcceptedSourceCachePublicationSource",
    "SOURCE_CACHE_PUBLICATION_NOTE",
    "SourceCachePublicationError",
    "SourceCachePublicationResult",
    "SourceCachePublicationService",
]

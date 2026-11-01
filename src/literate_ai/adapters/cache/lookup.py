"""Read-only discovery under existing Standard source-cache target authority."""

from __future__ import annotations

from pathlib import Path

from literate_ai.contracts import SourceCacheMode, SourceCacheRootKind
from literate_ai.projects import LoadedProject

from .filesystem import FileSystemSourceCache, SourceCacheError, SourceCacheResolver


def readonly_standard_source_caches(
    project: LoadedProject | None, runtime_root: Path
) -> tuple[FileSystemSourceCache, ...]:
    """Open declared readable targets; never create roots or invent host bindings.

    Match Standard's existing None/OFF legacy runtime fallback. A configured
    write-only policy remains unreadable. The runtime binding is the existing
    standard-local target, not permission to bind arbitrary operator roots.
    """
    configuration = None if project is None else project.definition.source_cache
    if configuration is None or configuration.mode is SourceCacheMode.OFF:
        return (FileSystemSourceCache("standard-local", runtime_root, writable=False),)
    if not configuration.mode.can_read:
        return ()
    operator_roots: dict[str, Path] = {}
    for target in configuration.targets:
        if target.root_kind is SourceCacheRootKind.OPERATOR_BOUND:
            if target.target_id != "standard-local":
                raise SourceCacheError(
                    "source-cache.operator-binding-missing",
                    "accepted lookup requires an explicitly available runtime binding",
                )
            operator_roots[target.root_reference] = runtime_root
    resolver = SourceCacheResolver.from_configuration(
        configuration,
        project=project,
        operator_roots=operator_roots,
        read_only=True,
    )
    return tuple(resolver.stores[target.target_id] for target in configuration.targets)


def verified_source_cache_entries(
    caches, *, entry_identities=(), component_lock_identity=None
):
    """Select verified immutable membership under explicit IDs and current lock.

    Selection narrows applicability; it never authenticates prior acceptance or
    changes cache history. Unknown, foreign and conflicting selections refuse.
    """
    from literate_ai.contracts import AcceptedSourceCacheEntry, ContentIdentity
    from literate_ai.contracts.rebuild_cache import (
        MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES,
    )

    if (
        not isinstance(entry_identities, tuple)
        or len(entry_identities) > MAX_REBUILD_SOURCE_CACHE_REQUESTED_ENTRIES
        or any(not isinstance(item, ContentIdentity) for item in entry_identities)
        or tuple(sorted(set(entry_identities), key=lambda item: item.uri))
        != entry_identities
    ):
        raise SourceCacheError(
            "source-cache.selection-invalid",
            "cache entry selection must be bounded, sorted and unique",
        )
    if component_lock_identity is not None and not isinstance(
        component_lock_identity, ContentIdentity
    ):
        raise SourceCacheError(
            "source-cache.selection-invalid",
            "cache selection requires typed Component authority",
        )
    entries = {}
    for cache in caches:
        for entry in cache.verified_published_entries():
            prior = entries.setdefault(entry.identity, entry)
            if prior != entry:
                raise SourceCacheError(
                    "source-cache.identity-collision",
                    "cache targets disagree on one entry",
                )
    requested = set(entry_identities)
    if requested - entries.keys():
        raise SourceCacheError(
            "source-cache.entry-not-found",
            "explicit cache entry is not verified membership in a readable target",
        )
    selected = tuple(
        entries[key]
        for key in sorted(requested or entries.keys(), key=lambda item: item.uri)
    )

    def lock(entry):
        return (
            entry.derivation.component_lock_identity
            if isinstance(entry, AcceptedSourceCacheEntry)
            else entry.component_lock_identity
        )

    if component_lock_identity is not None:
        if requested and any(
            lock(entry) != component_lock_identity for entry in selected
        ):
            raise SourceCacheError(
                "source-cache.selection-not-applicable",
                "explicit cache entry does not bind the requested Component lock",
            )
        selected = tuple(
            entry for entry in selected if lock(entry) == component_lock_identity
        )
    return selected

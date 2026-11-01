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

"""Read current authored SDK selections without acquiring or executing products."""

from literate_ai.adapters.component_lock_application import project_component_roots
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)


def current_retained_sdk_snapshots(project, component_locks):
    """Return genuine current locked source/Flavor authority in exact lock order."""
    reader = FilesystemLockedGenerationAuthorityReader()
    expected = {lock.identity: lock for lock in component_locks}
    selected = {}
    for root in project_component_roots(project):
        if not (root / "component.lock.json").exists():
            continue
        # Read the persisted target before resolving its current exact graph.
        from literate_ai.adapters.component_locks import ComponentLockStore

        catalog = reader.load_catalog(root, flavor_roots=project.roots("flavor"))
        lock = ComponentLockStore(root).read_from_catalog(authorings=catalog.authorings)
        snapshot = reader.resolve(
            catalog,
            target_name=lock.target_name,
            flavor_selectors=project.flavor_selectors_for(root, ()),
        )
        snapshot.require_unchanged()
        identity = snapshot.authority.lock.identity
        if expected.get(identity) != snapshot.authority.lock or identity in selected:
            raise ValueError("retained.sdk.current-lock-set-changed")
        selected[identity] = snapshot
    if set(selected) != set(expected):
        raise ValueError("retained.sdk.complete-lock-snapshots-required")
    return tuple(selected[lock.identity] for lock in component_locks)

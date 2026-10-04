"""Select complete accepted artifact dependency custody without host operations."""

from literate_ai.contracts import StandardComponentAcceptanceEvidence
from literate_ai.contracts.executable_components import ArtifactExport


def select_build_provider_receipts(providers, receipts):
    """Return exactly the receipt closure reachable from complete direct exports.

    The inventory comes from accepted lifecycle results. This checks descriptor
    membership only; callers must still verify transferred bytes and evidence.
    """
    if not isinstance(receipts, tuple) or len(receipts) > 4096:
        raise ValueError("provider receipt inventory exceeds bounds")
    if (
        not isinstance(providers, tuple)
        or len(providers) > 4096
        or any(not isinstance(item, ArtifactExport) for item in providers)
        or tuple(item.identity.uri for item in providers)
        != tuple(sorted({item.identity.uri for item in providers}))
    ):
        raise ValueError("direct provider exports must be bounded and canonical")
    by_revision, by_export = {}, {}
    for receipt in receipts:
        if not isinstance(receipt, StandardComponentAcceptanceEvidence):
            raise ValueError("provider receipt inventory is not typed")
        revision = receipt.component_revision
        if revision in by_revision:
            raise ValueError("provider receipt revision is duplicated")
        by_revision[revision] = receipt
        for export in receipt.build.exports:
            if export.identity in by_export or len(by_export) >= 4096:
                raise ValueError("provider export inventory is duplicated or oversized")
            by_export[export.identity] = (receipt, export)
    roots = {item.identity: item for item in providers}
    if len(roots) != len(providers):
        raise ValueError("direct provider exports are duplicated")
    for export in providers:
        match = by_export.get(export.identity)
        if match is None or match[1] != export:
            raise ValueError("direct provider has no exact accepted receipt")
        if any(item.identity not in roots for item in match[0].build.exports):
            raise ValueError("direct provider receipt exports are incomplete")
    pending = list(roots)
    selected, edges = {}, {}
    while pending:
        identity = pending.pop()
        match = by_export.get(identity)
        if match is None:
            raise ValueError("provider dependency has no accepted receipt")
        receipt, _ = match
        revision = receipt.component_revision
        if revision in selected:
            continue
        selected[revision] = receipt
        dependencies = set()
        for export in receipt.build.exports:
            for dependency in export.dependency_artifact_identities:
                target = by_export.get(dependency)
                if target is None:
                    raise ValueError("provider dependency has no accepted receipt")
                dependencies.add(target[0].component_revision)
                pending.append(dependency)
        edges[revision] = dependencies
    # A receipt may include several exports. Detect cycles at that admission unit,
    # without recursive traversal or a dependence on inventory order.
    remaining = dict(edges)
    while remaining:
        ready = {
            revision for revision, dependencies in remaining.items() if not dependencies
        }
        if not ready:
            raise ValueError("provider receipt dependency cycle")
        remaining = {
            revision: dependencies - ready
            for revision, dependencies in remaining.items()
            if revision not in ready
        }
    return tuple(selected[key] for key in sorted(selected, key=lambda item: item.uri))

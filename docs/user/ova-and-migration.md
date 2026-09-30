# OVA, migration, and rollback

OVA originated many of the framework ideas and is now a downstream product adapter and
demanding consumer. Literate AI owns provider-neutral contracts and lifecycle semantics;
OVA owns Omniverse, Isaac, ROS, Kit, CUDA/RTX, native integration, product policy, UI,
and product wording.

## Phase 2 ownership

After the Phase 2 cutover, Literate AI is canonical for:

- SemVer, coordinates, revisions, exact references, and serialization;
- Components, Flavors, targets, model groups, workflow, and skill references;
- generic source, evidence, security, package, publication, and settings contracts; and
- generic lifecycle request/result semantics.

OVA retains thin wire projections and downstream policy. It must not maintain another
provider-neutral version parser or lifecycle writer.

## Compatibility data

`literate_ai.compatibility.ova.OvaCompatibilityReader` reads supported legacy OVA
manifests, settings, caches, source/object packages, provenance, and publication records
without mutating them. Import copies verified exact objects forward on demand. It does
not rewrite or delete the legacy cache and does not republish imported objects.

The compatibility reader remains a rollback tool through its documented support
window. It is not permission to keep duplicate writers active.

## Rollback model

After duplicate writers are retired, rollback means:

1. stop new lifecycle writes;
2. restore the exact previously released OVA/Literate AI pair;
3. use the retained reader against unchanged legacy state;
4. quarantine framework-only state for diagnosis;
5. verify baseline, migrated-copy, and evidence identities; and
6. resume only after prior-pair health and read compatibility pass.

The stable cutover was explicitly owner-directed without completing the planned elapsed
production observation window. That accepted risk is recorded as a machine-visible
warning; the repository does not invent observation evidence.

For authoritative detail, read the [Phase 2 final cutover report](../migration/phase-2-final-cutover.md)
and [two-phase rebase plan](../migration/ova-two-phase-rebase.md). OVA users should then
follow the OVA repository's product documentation for launch and UI operations.

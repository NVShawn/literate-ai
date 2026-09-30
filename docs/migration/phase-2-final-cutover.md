# Phase 2 final cutover report

## Decision

Phase 2 is cut over to Literate AI as the canonical provider-neutral Component
framework. The repository owner explicitly directed completion on 2026-08-02. The
required elapsed production observation window was not performed, so this report does
not claim that it was. That missing evidence is an accepted cutover risk and remains a
machine-visible warning.

The stable framework distribution for the cutover is `literate-ai==0.1.1`. Its tag,
commit, wheel digest, and hosted verification are release-time evidence and must be
recorded by the publisher; they are not invented in the source tree before publication.

## Canonical ownership

| Concern | Authority after cutover |
| --- | --- |
| SemVer parsing and validation | Literate AI |
| Component coordinates, definitions, revisions, and exact refs | Literate AI |
| Flavor, target-profile, model, group, workflow, and skill refs | Literate AI |
| Generic lifecycle request/result contracts and orchestration | Literate AI |
| Source, evidence, security, bundle, publication, and settings contracts | Literate AI |
| Omniverse, Isaac, ROS, Kit, CUDA/RTX, and product policy | OVA |
| OVA UI, native adapters, launch behavior, and product wording | OVA |
| Legacy OVA artifact decoding | Literate AI read-only compatibility package |

OVA consumes these framework contracts through its adapter boundary. It does not own a
second implementation of provider-neutral version parsing, revision selection,
coexistence, lifecycle identity, or serialization.

## Compatibility release evidence

Two immutable compatibility releases preceded the stable cutover:

| Release | Git identity | Wheel SHA-256 |
| --- | --- | --- |
| `v0.1.0a1` | `a67dfdb5058c73cdb3f65c2517151ef21129a90e` | `03ee1a5216fe5e6435f269f6e18edf78a65d92fc76c50c2632da4be172eb9f58` |
| `v0.1.1a1` | `bd86984f4ca3b1cf9adb4d75d6d4105b7e345c6c` | `2b042455853fd292a9e1994d358c932b3d9ca1020befc9f2815fa47d13c76d45` |

The final `0.1.1` release is a new identity. Downstream pins must bind its immutable Git
commit or verified artifact, never a branch name or the `0.1.1a1` prerelease identity.

## Structured cutover evidence

`literate_ai.application.FinalCutoverManifest` is the normative decision record. It:

- requires all nine `LifecycleSeam` values exactly once and in canonical order;
- requires two distinct compatibility release identities;
- binds the exact framework release, per-seam comparison, compatibility reader,
  dependency audit, rollback rehearsal, and observation disposition;
- blocks readiness when a framework writer, duplicate-removal, retained-reader, or
  rollback fact is false; and
- hashes the complete report to a `ContentIdentity`.

The manifest treats `completed` observation as valid only with evidence. The actual
cutover uses `user-directed-without-elapsed-window`, an exact directive reference, and
the warning `production-observation-window-not-completed`. It is impossible to attach
invented observation evidence to that disposition.

## Compatibility-reader retention

`literate_ai.compatibility.ova.OvaCompatibilityReader` remains shipped and read-only.
It preserves supported Components, settings, caches, source/object packages, generation
provenance, publication settings, and publication records without changing the legacy
files. Current tests hash or retain the source bytes before reading and verify the same
bytes afterward.

The reader is retained until a separately documented major-version removal decision.
Removal is not implied by the Phase 2 cutover, cache migration, or duplicate writer
retirement. Deleting a legacy cache remains an explicit user action outside this API.

## Rollback after duplicate retirement

Rollback no longer means silently switching to a duplicate in-process writer. It means:

1. stop new lifecycle writes;
2. restore the exact previously released OVA/Literate AI pair;
3. use the retained reader against the unchanged pre-cutover state;
4. quarantine framework-only state for diagnosis;
5. verify baseline, migrated-copy, and evidence identities; and
6. resume only after the prior pair's health and read compatibility pass.

`RollbackRehearsal` records the exact baseline state, migrated copy, diagnostic evidence,
prior-release restore result, reader result, and baseline immutability. Any false result
blocks a cutover manifest.

## Verification boundary

Repository tests prove contract invariants, retained-reader immutability, deterministic
identity, rollback-report fail-closed behavior, exact lifecycle snapshot replication,
and supported host conformance. Snapshot replication is a verifier fixture, not a claim
of coding-CLI framework self-generation. OVA provides downstream dependency-audit,
application, launch, and product-policy evidence. Neither repository claims native
RTX/Omniverse hardware coverage when the corresponding SDKs and hardware are
unavailable.

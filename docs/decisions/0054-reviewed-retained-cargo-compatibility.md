# ADR 0054: Review retained Cargo compatibility without weakening admission

- Status: Accepted
- Accepted: 2026-11-02; directing user explicitly accepted both ADRs for 1.4
- Date: 2026-11-01
- Decision owners: directing user and literate-ai maintainers
- Roadmap: [CARGO-COMPAT-001](../roadmap/active-work.md#cargo-compat-001-qualify-reviewed-retained-cargo-workspace-compatibility) and [1.4 integration](../roadmap/1.4-integration.md)
- Issues: [#222](https://github.com/jordanhubbard/literate-ai/issues/222), [#223](https://github.com/jordanhubbard/literate-ai/issues/223), [#224](https://github.com/jordanhubbard/literate-ai/issues/224), [#225](https://github.com/jordanhubbard/literate-ai/issues/225), [#226](https://github.com/jordanhubbard/literate-ai/issues/226)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

The retained Cargo bridge currently cannot qualify several ordinary workspace
shapes. In an adopted tree its package destinations must be workspace members
beneath BUILD_DIR/OBJ_DIR, while those directories are forbidden beneath the
retained implementation's containing authority root. Its graph has no reviewed
registry-to-local patch relation. Its test inventory assumes every executable
uses libtest and every named case passes without ignored cases. Its custody
walk rejects all aliases and has fixed entry/byte allowances that real dependency
closures exceed. Relaxing these checks globally would discard their guarantees.

The source audit confirms that `read_retained_cargo_consumer_files` defaults to
10,000 entries / 64 MiB, while `read_retained_cargo_execution_inputs` defaults to
10,000 entries / 256 MiB. Neither default is a sufficient general workspace
contract. The proposed fix must preserve actual current defaults, rather than
silently substituting a single larger allowance.

## Decision

### Explicit versioned authority

Introduce opt-in version 2 workspace-plan, retained-library gate-policy and test
inventory contracts, together with versioned receiving binding/receipt codecs
where their exact field sets or provenance change. A v2 plan binds its graph,
manifest edits, scratch roots and custody policy into its canonical identity.
Every receiving check requires the same versions and identities. The existing
v1 codecs, defaults, identities and refusals remain unchanged; do not infer an
upgrade from discovered source contents or reinterpret an old receipt.

A plan or parser grants no execution, provider, independent acceptance or release
authority. Existing native artifact verification, measured compiler/input custody,
retained gate authority and independent acceptance remain required. Changed
policy, manifests, aliases, dependencies, commands or tool bytes invalidate the
corresponding evidence. No automatic dependency installation or network filling.

### Reviewed scratch roots inside retained workspaces (#222)

A v2 plan may declare narrow workspace-relative disposable roots inside the
retained wrapper implementation, for generated member packages and Cargo output.
These are an additional scoped materialization authority; they are not a global
exception permitting arbitrary BUILD_DIR/OBJ_DIR paths below `components`.
Ordinary cache-root resolution keeps its current protected-directory rule.

Validate the wrapper's actual retained ownership and the root's exact coordinate,
physical ancestry, portable name, ignore status and disjointness from authored
Components, manifests, sources, aliases and other roots. Initial provisioning
requires an absent destination or an empty directory with proved ownership;
existing ignored source is never disposable merely because Git ignores it.
Reuse requires exact current ownership and package identities. Refuse foreign
files, links/reparse points, collisions, stale provenance or ambiguous owners.

Materialization must reserve destinations, verify exact native package exports,
recheck inputs before publication and retain collision-safe rollback. Generated
packages stay ordinary members inside Cargo's workspace; reviewed manifest deltas
name their exact destinations. Exclude only declared owned scratch outputs from
source custody, while pinning their required package bytes separately. Do not
exclude all ignored files or the retained implementation as a whole.

### Registry patches explained by reviewed graph edges (#223)

The v2 graph explicitly records each patched source/package, exact local provider
coordinate and expected affected dependency edges. Bind the manifest patch table,
lockfile and observed resolved package identities. A registry consumer may resolve
to a local package only when that exact edge is explained by the reviewed patch.
Direct path dependencies retain their existing declaration checks. Refuse
unexplained registry-to-local edges, patch drift, extra local packages, ambiguous
replacements, source/version mismatches and unresolved reviewed edges. A patch
never becomes a blanket external-to-local dependency exemption.

### Explicit test protocols and honest coverage (#224)

The v2 inventory classifies every observed selected target exactly once:

- `libtest`: exact reviewed named cases, divided into must-run and reviewed ignored
  sets. Require every must-run case to pass and every ignored case to match its
  reviewed name/reason and observed ignored outcome. Unexpected skips, missing or
  duplicate results, failures and summary/count drift still refuse admission.
- `custom-harness`: an exact reviewed command, target executable identity,
  environment/input scope, timeout and output allowance. Run it through the existing
  bounded command owner and require verified completion with exit zero. Report it
  as a custom command result, not invented libtest case coverage.
- `excluded`: exact target, reason and permission from independent retained gate
  authority. A target being non-libtest is not permission to exclude it. Refuse an
  exclusion that removes required acceptance or an independently required gate.
  Record exclusions as exclusions, never as passed or executed cases.

Bind the manifest's harness declaration and actual Cargo executable attribution.
No target may disappear silently. Receipts distinguish executed cases, reviewed
ignored cases, custom commands and exclusions, with explicit reviewed coverage;
they cannot claim unconditional all-target execution when exclusions exist.
Ordinary v1 zero-ignored/all-libtest checks remain intact.

### Closed alias custody and scalable finite input budgets (#225, #226)

Reuse canonical tree topology and streaming digest owners to capture regular files,
directories and relative aliases. Every alias must resolve to an existing captured
member inside the same declared input tree, with recorded topology and referent
bytes. Refuse escape, dangling/cyclic aliases, ambiguous portable paths, unsupported
reparse types and unowned hard links. Reobserve link text, tree membership and
physical file identity around execution; changing a link or its referent invalidates
evidence. Do not flatten links into copies or skip linked entries.

A v2 gate policy supplies explicit per-owner and aggregate custody budgets. Preserve
v1 defaults. Framework hard ceilings are 1,000,000 regular files, 4,000,000 total
entries, 256 MiB per regular file and 4 GiB aggregate captured input bytes. All
values are finite strict integers, canonical identity inputs and receipt fields;
reject booleans, unknown fields, impossible combinations and overrides beyond the
ceilings before traversing or executing. Count aliases and directories as entries,
and count retained payload bytes once by their declared physical ownership.

Hash regular input bytes incrementally with bounded buffers and identity/size
checks before and after reading. Keep only bounded records/digests and the bytes
actually required by existing consumers; do not retain whole dependency caches in
memory or make a second unbounded snapshot. Preserve full declared CARGO_HOME
source-tree custody, explicit external-input scope and offline/locked behavior.
A larger allowance is not permission to omit ambient source inputs or treat a
lockfile checksum as proof of an unverified extracted tree.

## Acceptance and execution order

After explicit acceptance, record implementation owners and dependencies in the
1.4 queue and release-goal manifest before source changes. Implement versioned
contracts first, then scratch materialization and patch graph checks, test protocol
observation, alias/streaming custody, and finally public CLI composition/receipts.

Required evidence includes real native Cargo workspaces through the public CLI:

1. Adopted workspace replacement and admission with an internal reviewed scratch
   member, unchanged original source and no global cache-root exception; foreign
   ignored files and publication/rollback races must refuse safely.
2. A real offline registry-to-local patch graph, alongside unexplained-edge,
   manifest/lock drift, extra-package and stale patch controls.
3. Native libtest ignored cases, a custom harness and an authorized optional
   exclusion; unexpected skips, missing targets and required-gate exclusions fail.
4. Internal dependency aliases, alias/referrer changes and unsafe topology; native
   platform evidence is required for any link type claimed supported.
5. A closure above 10,000 entries / 256 MiB, with streaming memory observation,
   exact boundary/over-limit controls, mid-read mutation, and unchanged-input
   reobservation. Prove the configured budgets are in the resulting identity chain.
6. Existing v1 positive and refusal controls, installed non-editable framework
   qualification, exact integrated CI, and platform-specific evidence at the scope
   claimed. Synthetic metadata or mocked command success is not native acceptance.

## Consequences

Common retained Cargo layouts become representable through reviewed authority,
while old consumers retain their existing contract. This requires coordinated
versioned codecs and execution ownership, rather than independent permissive
parser fixes. It does not add retained binary-Component replacement (#216),
change generated library acceptance dependencies (#217), waive original retained
source authority, or satisfy FreeBSD/Windows qualification without native evidence.
Those separately included 1.4 requirements remain open; this proposal defers none.

# ADR 0046: Bind worktree placement and retirement to project custody

- Status: Accepted
- Date: 2026-09-24
- Accepted: 2026-09-25
- Decision owners: Literate AI maintainers
- Roadmap: WORKTREE-002 in `docs/roadmap/active-work.md`

## Context

Literate AI guidance now tells agents to create linked Git worktrees below an ignored
`.worktrees/` directory in the owning repository. The framework does not yet expose a
single typed placement contract that every command and generated skill can follow, and
older worktrees may still exist as top-level peers of their canonical checkout.

The current peer-work adapter lists registered worktrees and classifies their ordinary
status as clean or dirty. Its branch garbage collector can remove a registered clean
worktree after lifecycle-marker and review checks. That is not a migration proof. It
does not inventory ignored files, submodule worktrees, locks, active sessions, detached
useful commits, patch-equivalent history, independent clones, missing registrations or
retained qualification evidence. It also does not retain a per-worktree journal that
can resume after interruption and prove why removal was allowed.

Repository refresh already contains bounded common-directory, registered-worktree,
physical-file and concurrent-change custody primitives. Worktree migration should
reuse those boundaries where their contracts fit, but it must not treat a refresh
observation or a clean `git status` as authority to delete operator data.

## Decision

### Derive one canonical placement from Git custody

Introduce a typed project worktree location derived from the repository's absolute Git
common directory. For a non-bare repository whose common directory is the canonical
checkout's `.git`, persistent and command-owned linked worktrees live beneath the
ignored `<canonical-checkout>/.worktrees/` directory. The canonical checkout itself is
confirmed against `git worktree list --porcelain -z`; directory naming alone never
establishes ownership.

Names are short, portable slugs with a bounded digest suffix derived from their purpose
and exact branch or detached revision. Allocation uses an atomic reservation and
refuses collisions, symlink or reparse ancestors, case-fold conflicts, a changing Git
common directory, bare repositories and ambiguous canonical-checkout identity.
Commands, authored skills and initialized project copies consume the same public
location contract. Parent contribution checkouts remain under their separately owned
`parents/<id>/` boundary and are not reclassified as disposable worktrees.

Transient worktrees may be removed at command completion, but they use the same
project-scoped root and remain visible in Git registration and lifecycle evidence while
they exist. Disposable system temporary directories and top-level sibling paths are
not placement fallbacks.

### Separate discovery, audit, disposition and removal

Migration is an explicit four-stage application, not an extension of branch garbage
collection:

1. **Discover** candidates from the canonical repository's registered-worktree
   metadata and a bounded scan of its parent directory. A peer candidate is admitted
   only when its resolved Git common-directory identity matches the canonical
   repository. Independent clones, ordinary directories, missing registrations and
   contradictory identities are classified distinctly and never inferred from names.
2. **Audit** each admitted candidate into an immutable observation: registration and
   lock state, branch or detached HEAD, reachable and unreachable commits, merge
   ancestry and patch equivalence, index and tracked changes, untracked and ignored
   entries, stashes and per-worktree refs, submodule registrations and state, active
   process/session reservations, filesystem identity, and retained evidence references.
   Every collection is bounded; incomplete or drifting observation is a refusal.
3. **Dispose** every useful item through a reviewed destination in the canonical
   repository. Commits are landed by normal review policy; file or evidence bytes are
   copied into explicit retained custody; superseded material receives a human-reviewed
   rejection reason bound to exact digests. A tool may propose a disposition, but it
   cannot invent approval for a useful or ambiguous change.
4. **Remove** only an explicitly authorized candidate whose complete disposition is
   terminal. Immediately before `git worktree remove`, re-observe its registration,
   filesystem, Git and activity identities and require equality with the approved
   record. Use Git-aware non-force removal. Recheck registration and retained custody
   afterward; any disagreement leaves the journal pending and preserves the directory.

An independent clone is reportable but is never removed through the linked-worktree
application. Its refs, stashes, configuration and object reachability require a
separate reviewed clone disposition.

### Retain a resumable migration journal

Each run writes a canonical, content-addressed journal beneath the project's configured
evidence root. It binds repository identity, canonical checkout, placement-policy
identity, discovery bounds, candidate observations, proposed and accepted dispositions,
retained destinations, review references, removal authorization, revalidation records
and terminal outcomes. The operator can export a redacted report without exposing
credentials or private host paths.

Re-running the application opens the prior journal, revalidates completed steps and
continues pending candidates. It neither repeats a completed removal nor silently
reclassifies changed input. Concurrent mutation produces a new observation requiring a
new disposition. A summary lists every pending, refused, retained and removed candidate
so interruption cannot masquerade as completion.

The existing peer-work survey consumes this inventory for release visibility, while
branch lifecycle garbage collection remains a narrower operation. It may not delete a
legacy peer merely because the corresponding branch marker is terminal.

## Qualification and rollout

Implementation proceeds under the explicit maintainer acceptance recorded on
2026-09-25. Qualification must cover:

- location derivation from canonical and linked checkouts on supported platforms,
  including path length, case folding, symlink or reparse ancestors and collisions;
- provider-neutral parity across framework commands, authored skills and initialized
  project templates;
- two repositories sharing a parent without cross-project candidate admission;
- dirty, staged, untracked, ignored, detached, locked, prunable, missing, active and
  submodule-bearing worktrees;
- useful commits, already-merged ancestry, patch-equivalent changes and explicitly
  rejected superseded work;
- independent clones, ordinary peer directories and ambiguous or changing Git custody;
- failed review/landing, failed retention, failed removal, concurrent mutation and
  interrupted/resumed migration; and
- a reviewed journal proving that every removed worktree was freshly revalidated and
  every useful byte or commit has a durable destination.

Roll out in three reviewable slices: canonical placement and guidance parity; read-only
discovery, audit and journal resumption; then explicitly authorized migration and
Git-aware removal. The first two slices must not remove legacy directories.

## Consequences

Agents and commands gain one discoverable worktree root, and legacy cleanup becomes an
evidence-bearing project operation rather than an ad hoc sibling-directory sweep. The
extra observations and operator dispositions make migration slower, but preserve the
important distinction between a worktree that looks clean and one proven safe to
retire.

Repositories with bare, ambiguous or unsafe layouts receive a typed refusal instead of
an external temporary-directory fallback. Supporting independent-clone retirement or a
different persistent placement root requires a later explicit contract.

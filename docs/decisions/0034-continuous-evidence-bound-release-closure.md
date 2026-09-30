# ADR 0034: Make Release Closure Continuous and Evidence-Bound

- Status: Accepted
- Date: 2026-09-04
- Decision owners: literate-ai maintainers
- Release target: 0.10.0
- Roadmap: [RELEASE-CLOSURE-001](../roadmap/active-work.md#release-closure-001-make-release-closure-executable-and-continuous)
- Extends: [project release architecture](../architecture/project-releases.md) and the
  [release branching model](../history/roadmap/release-branching-model.md)

## Context

The 0.9.0 tag, release line, GitHub release, notes, and wheel were published, but the
required terminal presentation/narrative pair remained at its accepted local
pre-release edition while the stable Google Workspace resources still exposed 0.8.0.
The immediate attempt failed because `gcloud` had neither an active account nor
application-default credentials. The operator was not told to authenticate the correct
account before publication.

That credential failure was only the trigger. The release skills required a regenerated
and published document pair for a minor release, while the release policy, plan,
prepared record, Make gate, publication receipt, and published verifier modeled only
version files, changelog, Git refs, the GitHub release, and a wheel. The prose and the
executable contract could therefore disagree without any phase failing.

The same split existed for contributions. Cycle guidance listed issues, reviews, and
some local peer work, but no release artifact proved that every open issue, review,
unmerged branch, and attached worktree had been evaluated for the current release.
Milestones and comments could remain absent, so the next release engineer had to infer
prior decisions from conversation.

## Decision

### Derive release class from published history

The release helper derives the effective release class from the highest stable
repository tag and the proposed version. An explicit version equal to an already
advanced source version does not erase a minor or major transition. A plan records the
derived class and the stable predecessor that established it.

### Bind declared release deliverables

`literate.release.json` may declare release deliverables. A terminal document-pair
deliverable names its two repository artifacts, local verification report, publication
receipt, stable remote resource IDs, ecosystem, and the release classes for which it is
required. For a required class:

1. local regeneration and independent pair verification precede planning;
2. publication preflight resolves and displays one explicit active account and proves
   edit/export access to both stable resources before either is mutated;
3. publication updates the pair idempotently, preserves the stable resource IDs and
   access policy, exports both remote resources back, and verifies the exported pair;
4. the receipt binds release version, prepared revision, account identity, local and
   exported artifact hashes, remote IDs and versions, access observations, and verifier
   identity; and
5. release planning, prepared checking, provider publication, and published
   verification carry or revalidate that evidence as appropriate.

Authentication is an operator action, never an inferred or silent mutation. A missing,
ambiguous, or unexpected active `gcloud` account produces a typed diagnostic that names
the exact authentication and account-selection commands before any remote write.
Preflight is all-or-nothing; a partial update remains explicitly resumable and can
never be reported as a complete pair.

### Run a continuous contribution-disposition loop

The virtual release engineer runs a Python-owned, read-only contribution sweep at cycle
start, after each merge or scope change, before release planning, before publication,
and after publication. The sweep inventories:

- all open issues and pull/merge requests from the forge selected by repository
  tracker inspection;
- CI, draft, mergeability, author, source branch, and source revision when the forge
  exposes them;
- all unmerged local and remote branches other than the default and protected release
  lines; and
- every attached worktree, including dirty or detached work.

The LLM decides relevance; Python gathers, normalizes, hashes, and validates facts. An
open issue or review is resolved for a cycle only when its current milestone and a
machine-readable release-disposition comment agree. `include` selects the current
release milestone. `defer` names a later or maintenance milestone and records why.
Closing, merging, deleting, or mutating tracker state retains its existing explicit
authorization boundary.

An unmerged branch is resolved only by a classified open review, by exact inclusion in
the default branch, or by a durable branch-lifecycle disposition bound to its exact
head under a fetchable Git ref. Tracker comments are not the sole branch authority:
closing an issue must not make its branch disposition disappear from the next open-item
inventory. Active work must not be called dead merely to clear a release. Dirty or
detached worktrees remain visible and block final closure until their owner commits,
moves, or explicitly disposes of the work. Release branches are protected history, not
unmerged feature contributions.

Every sweep emits a content-identified report. Planning and publication fail closed on
unclassified contributions or unavailable tracker evidence for a configured supported
forge. Repeating a sweep is idempotent, and a tracker change invalidates the previous
snapshot instead of being hidden by a time-based cache.

### Bind the first stable candidate to release-intent trunk

For an initial, major, or minor stable release under repository release-state policy,
the candidate release line must contain the live remote default-branch revision during
plan, prepare, check, and publish. This closes the race where a line is cut early,
approved work lands on the pre-release trunk, and a later release phase publishes the
stale line despite an otherwise complete contribution sweep. A newly fetched default
revision invalidates the earlier phase and requires deliberate merge or backport plus a
new plan. Patch releases remain selective: they do not acquire unrelated later trunk
work merely because that work exists.

Every checked-in package version asserted by release gates is a declared release
version mirror. The Homebrew formula derives its `version` and archive URL from one
quoted Ruby constant; preparation updates that constant through the generic
quoted-constant binding and therefore keeps package metadata inside the same planned
atomic write and scope check as the Python, project, and schema versions.

Repair checkpoints preserve the global completion order across their TCB and
documentation-authority partitions. Reuse is allowed only when those completed gates
form the exact prefix of the current fail-fast plan and each gate remains attached to
the same pin; otherwise repair progress resets while durable known-failure annotations
remain intact. Final release evidence always starts from gate one.

### Keep one owning helper at each layer

Skills own semantic judgment and operator communication. Python owns tracker and Git
observation, marker parsing, account/resource preflight, hashing, receipt construction,
and release-phase contract enforcement. Make targets are thin named entry points over
those helpers. Root authority and initialized template copies remain byte-identical
where their contracts are shared.

## Consequences

A release can no longer be called complete merely because a tag and provider page
exist. A GitHub release must be stable rather than draft or prerelease, contain
non-empty release notes, and expose the required non-empty version-matching wheel.
Required collateral, stable assets, and contribution decisions become
independently inspectable release evidence. The release engineer must surface missing
credentials while recovery is still safe, and the next cycle begins with durable
scope decisions instead of conversational archaeology.

Projects without configured deliverables retain the existing release mechanics.
Projects without a supported configured forge report the unsupported boundary
explicitly; they may not fabricate an empty sweep. Additional deliverable ecosystems
and forges plug into the normalized contracts without enlarging the LLM prompt or
duplicating orchestration logic.

## Validation

- Prove major/minor derivation from stable tags, including an explicit already-bumped
  source version and patch/prerelease edges.
- Prove missing, stale, wrong-version, wrong-revision, wrong-account, wrong-resource,
  access-mismatch, export-mismatch, and partial-publication evidence fail closed.
- Prove preflight observes both pair members before any update and an idempotent retry
  completes a partially published pair without creating replacement resources.
- Exercise GitHub and GitLab issue/review normalization, disposition-marker parsing,
  milestone disagreement, newly arrived work, exact-head branch-lifecycle markers
  surviving issue closure, unmerged branches, protected release lines,
  dirty/detached worktrees, and repeat-sweep identity.
- Reject missing, draft, or prerelease GitHub releases, empty notes, absent or empty
  version-matching wheels, and provider evidence that does not match the prepared tag.
- Reject a first stable initial, major, or minor candidate that omits the live remote
  default branch, including a default-branch advance between planning and preparation;
  recheck the invariant at check and publish, prove that patch candidates remain
  selective, and preserve historical verification after the default branch later moves.
- Insert or reorder a gate after a partial run and prove that no previously completed
  gate is reused unless the combined checkpoint is the exact current fail-fast prefix.
- Recover the immutable premature 0.10.0 cut through a versioned 0.10.1 plan, prepare,
  check, publish, verify, and final contribution sweep; retain the exact release and
  collateral receipts.

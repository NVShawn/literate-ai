# ADR 0063: Qualify upstream branches through a separate shadow repository

- Status: Accepted
- Date: 2026-11-05
- Acceptance: The directing user explicitly accepted this design on 2026-11-05,
  recorded in [PR #320](https://github.com/jordanhubbard/literate-ai/pull/320#issuecomment-6665728874).
  This accepts the decision under ADR 0006; implementation and qualification remain open.
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0, per the included disposition on issue #311; an upstream
  repository's own release remains separate
- Roadmap: [SHADOW-001](../roadmap/active-work.md#shadow-001-continuously-qualify-upstream-branches-through-a-separate-shadow-repository)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)
- GitHub issue: [#311](https://github.com/jordanhubbard/literate-ai/issues/311)

## Context

An existing upstream repository may need continuing lifecycle qualification and
corrective contributions without adopting Literate AI's layout or transferring
source authority. A one-time import or a detached test checkout cannot retain
branch-specific evidence, follow new upstream commits and pull requests, or
reconcile a shadow repair after upstream accepts or rejects it. Calling an
upstream check green also says little about whether the tests ran, covered the
affected behavior, or used the current base and head.

The shadow must remain a separate Literate AI-defined repository. Its canonical
structure may describe and run mapped lifecycle operations, while the selected
upstream tree remains the authority for any contribution offered upstream.
Neither a shadow projection nor a saved result can confer current upstream
admission by itself.

## Decision

### Own a durable shadow and explicit branch selections

Add a `litai shadow` front door that initializes a separate, versioned shadow
repository from a reviewed upstream locator and an explicit branch set. Select
`main` by default only when that ref exists; otherwise require a branch name.
Allow later branch additions through the same reviewed plan/apply boundary.
Record forge/repository identity, exact upstream ref and commit, shadow branch,
projection version, target capabilities, and the source-layout mapping for each
selected branch. A branch's evidence never silently qualifies another branch.

Keep upstream source in an attributed, immutable per-attempt snapshot. Preserve
its relative layout when it makes synchronization and patch export reliable;
use canonical shadow Components, Flavors, workflows and evidence ownership for
the mapped lifecycle. No shadow command rewrites the upstream repository or
assumes its project manifest is a Literate AI manifest. Read-only inspection
must not fetch, run source, install tools, publish a PR, or mutate either tree.

### Synchronize exact Git and forge states

An acknowledged synchronization fetches selected refs, validates object and
ancestry relationships, and records the exact base/head pair observed. New
upstream commits enter each shadow branch through a durable, restartable
transaction. Persist an append-only operation journal with bounded referenced
objects and evidence, then atomically publish the current branch snapshot.
Reopening must reverify Git objects, mappings, current upstream refs and trust
policy; saved JSON alone never restores live admission. Repeating an already
completed synchronization is idempotent and preserves its evidence identity.

For an upstream pull request, bind forge identity, target branch, current base
and candidate head before creating an isolated evaluation lane. Recheck those
identities before interpreting a result or exporting a repair. A changed head,
base, force push, deleted branch or inaccessible object invalidates the plan;
the earlier attempt remains historical evidence. PR lanes cannot share mutable
workspaces, receipts or caches without an existing verified sharing contract.

### Map and qualify actual operations

Plan upstream build, test, execution, acceptance, packaging and other declared
operations against the capabilities Literate AI actually supports on the
selected target. The plan names each upstream command, source and dependency
input, tool/worker requirement, output, acceptance oracle and corresponding
shadow stage. Unsupported, unbound or unobservable operations become explicit
gaps with reasons and owner-facing next actions. They do not count as passing
or skipped tests, and a partial mapped run cannot claim complete upstream
qualification.

Run untrusted upstream source only in an acknowledged isolated attempt with
the same command, filesystem, network, credential, worker and evidence controls
as the underlying lifecycle. Capture actual execution and output observations;
retain failed and timed-out attempts. A green result is admitted only after
current input, execution, test-inventory, output and independent-acceptance
checks required by its mapped policy. Distinguish a reproducible code or test
defect from projection, environment, unsupported-operation, timeout and
nondeterminism failures by replaying exact inputs and recording the causal
counterexample. Uncertain classification remains uncertain.

### Repair in the shadow and validate in upstream layout

Develop a targeted repair against the failed shadow lane and rerun the relevant
mapped tests plus the required broader gates. An export plan maps the exact
change back into the upstream repository's paths and build context, checks
source ownership and upstream policy, and refuses ambiguous or lossy mappings.
Apply the patch to a fresh checkout of the current upstream target, reconcile
base drift through an explicit three-way review, and rerun the actual upstream
commands and acceptance on that layout. Shadow success alone cannot authorize
an upstream contribution. Publishing a forge PR is a separate acknowledged
operation using the user's existing forge credentials and branch policy.

On upstream acceptance, observe the actual merged commit and re-import it
through ordinary synchronization. Reconcile the local repair against that
content and its provenance so it is neither applied twice nor discarded by a
similar patch identifier alone. On rejection, retain the proposed patch,
decision, rationale and exact base/head evidence without silently retrying or
rewriting upstream. If upstream asserts that its tests are green, compare the
current test inventory, affected behavior, real execution and acceptance before
changing a proven defect's disposition. Incomplete or invalid green tests do
not erase the recorded failure.

### Bound and expose the operation

Version shadow registry, plans, mappings, journals and evidence schemas. Bind
finite ref, object, tree, file, byte, command, output, attempt and time budgets;
reject traversal, unsafe links, object substitution, stale plans and concurrent
foreign writes. Keep secrets and private worker routes in user-owned
configuration. Report branch and PR state, qualified operations, explicit gaps,
retries, accepted/rejected repairs and currentness through one read-only status
view. A lifecycle or release claim names the exact upstream ref and shadow
attempt that earned it.

## Planning and acceptance

The directing user accepted the decision under ADR 0006. SHADOW-001 now owns its
implementation and evidence checklists in the active roadmap and release-goals
manifest. Qualify the
public installed CLI against real Git and forge round trips: initialization,
multiple branches and later branch addition, upstream ref changes, PR base/head
drift, restart/idempotence, supported and unsupported mapped operations,
deterministic code/test failures and environmental counterexamples, targeted
repair, upstream-layout export and rebase, accepted merge-back without duplicate
application, and rejected proposals with incomplete or invalid green-test
claims. Require independent source review, platform and isolation controls,
full exact-head CI and a noneditable installed distribution before completion.

## Consequences

The shadow adds durable repository and evidence state plus ongoing runner work.
It does not change upstream authority or automatically publish contributions.
Some upstream operations will remain explicit gaps until a supported mapping
and acceptance oracle exist. A branch or PR must be requalified after relevant
upstream, tool, policy or mapping changes; historical evidence remains available
without being relabeled current.

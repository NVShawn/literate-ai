# ADR 0035: Make Operator Adoption Explicit and Evidence-Bound

- Status: Accepted
- Date: 2026-09-04
- Decision owners: literate-ai maintainers
- Release target: 0.10.0
- Roadmap: [ADOPTION-001](../roadmap/active-work.md#x-adoption-001-make-operator-adoption-the-post-090-product-headline)

## Context

The 0.9 line has deterministic project initialization, brownfield conversion,
retained-harness receipts, source-to-specification recovery, and regenerative
qualification. Those capabilities were exposed as separate low-level commands. An
operator had to infer host readiness, whether a tree was created or adopted, whether
locks and receipts were current, and which command should run next.

Brownfield authority was especially easy to misread. Successful conversion proves a
safe wrapper and retained-source parity; it does not make a generated specification
the release authority. Directory heuristics and the presence of generated documents
cannot safely represent that transition. Recording the stage in
`literate.project.json` would also be self-invalidating: the stage change would alter
the authored project-authority identity that its own evidence was intended to bind.

## Decision

1. `litai doctor`, `litai onboard create|adopt`, and `litai status` are the operator
   front door. TTY output is a presenter over the same versioned JSON results used by
   agents. Doctor is project-independent and performs only bounded host-path,
   dependency, executable, and authentication observations; it never invokes a model.
2. One application service owns plan-then-apply onboarding. Planning is read-only,
   content-identified, and includes host observations. Apply requires explicit
   acknowledgement, reconstructs the plan immediately before mutation, and can require
   the exact reviewed plan identity. Existing init and conversion services remain the
   only project mutators.
3. Adopt plans expose the existing conversion detector's facts: languages, selected
   Flavors, quarantine entries, retained runner IDs, Gitlink blockers, and the landing
   stage. The CLI does not reproduce harness discovery or filesystem algorithms in an
   LLM prompt.
4. An adopted project has exactly one canonical dynamic metadata file,
   `.literate/conversion-authority.json`. It is deliberately outside the authored
   project-authority graph, so an evidence-bound stage transition does not change the
   identity it cites. The filesystem adapter uses bounded canonical JSON,
   symlink-resistant reads, an exclusive lock, compare-and-swap identity checks, and
   atomic replacement.
5. The only forward stage edges are `wrapped` → `retained` → `drafted` → `qualified`.
   Conversion alone creates `wrapped`. A current retained-harness receipt authorizes
   `retained`; current derived-source-retained Component projections authorize
   `drafted`; current regeneratively-qualified projections and their independently
   verified promotion evidence authorize `qualified`. No observer infers or silently
   advances a stage.
6. `original-source` is the computed release-authority claim for wrapped, retained,
   and drafted states. Only qualified computes `specification`. Rebuild refuses an
   adopted tree before qualified rather than treating wrapper parity as regenerative
   authority. Existing authority, lock, receipt, and lifecycle validation still apply
   after the stage gate.
7. Status composes existing tracker, project-authority, lock, receipt, host-path,
   native dependency, coding-CLI authentication, and conversion-state readers. It does
   not add a second planner, project database, daemon, or cache-backed authority.

The proposed 1.0 `litai adopt` shorthand is a later vocabulary alias. It must route to
the same lower-level conversion implementation and does not change this state model.

## Consequences

Operators get one repeat-use snapshot and two guided first-hour paths without
weakening the existing contracts. A source tree can be wrapped and tested while still
truthfully naming original source as release authority. Automated agents can use JSON
without a TUI, and local Python performs deterministic inspection, hashing, path
resolution, and state validation instead of spending model tokens on host work.

The conversion metadata is project-local durable state but not authored generation
authority. It must therefore be copied and reviewed with the project while remaining
excluded from Component and documentation identities. A damaged, noncanonical,
cross-project, concurrently changed, or illegally advanced state fails closed.

## Validation

- Contract tests reject forged release-authority claims, noncanonical evidence,
  duplicate identities, skipped transitions, and advancement without the required
  receipt or Component evidence.
- Public CLI tests cover project-independent doctor output, status JSON and TTY
  rendering, missing projects, unauthenticated coding CLIs, plan identity drift, and
  acknowledgement refusal.
- Conversion integration tests prove the plan reports quarantine, runners, Gitlinks,
  and `wrapped`, then prove successful conversion writes exactly one canonical state.
- The installed-wheel gate creates and validates a hello project through onboard and
  converts a committed Make fixture through a current retained receipt and explicit
  `retained` transition with no checkout `PYTHONPATH`.

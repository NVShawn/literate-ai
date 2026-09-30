# ADR 0007: `litai update` Compares Against Resolved Parent-Catalog Identity, and Offers an Optional Coding-Agent Merge Review for True Conflicts

- Status: Accepted
- Date: 2026-08-19
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: `UPDATE-CLASSIFICATION-001` (bug fix, no ADR required), `UPDATE-MERGE-001`
  (this ADR's capability)
- Filed from: an anonymized downstream Physics Workbench report,
  [issue #141](https://github.com/NVIDIA-dev/literate-ai/issues/141)

## Context

A downstream project (Physics Workbench sample) reparented to Literate AI `v0.5.0` and ran `litai
update --apply`. 33 framework files were classified `conflict` and refused. Manual
inspection showed most of those 33 were **not** real product drift: their local bytes
already matched the parent repository's catalog copy at the resolved tag (`79ba1409`)
byte-for-byte — `litai update` was comparing them against the **installed wheel's
`literate_ai.project_template` init-template snapshot** instead, and that template can
legitimately differ from the same-revision catalog copy (a catalog file changes after
the template that scaffolded a project was captured, without every downstream project
re-running `init`). Byte-identity mismatch against the wrong reference was reported as
an uninterpreted `conflict`, indistinguishable from a project that had genuinely and
deliberately diverged.

Batch-overwriting those 33 files with template bytes would have been actively harmful:
it would downgrade several already-current inherited catalog files to a stale template
snapshot, and it would silently replace real Physics Workbench sample product overlays (`docs/README.md`,
`docs/user/getting-started.md`, `literate.release.json`'s project-specific fields) with
generic scaffold content. The operator's own words, carried over verbatim from the
originating direction: downstream Component/Flavor/skill drift does not always imply
the file is completely divorced from its parent, but it does mean the drift needs
analysis carefully — deciding whether the correct merge outcome favors the parent's
content or the downstream project's — rather than a byte-only overwrite-or-refuse
decision.

This surfaces two distinct problems, one a bug and one a missing capability:

1. **Wrong comparison reference.** `litai update`'s classifier should be comparing
   local bytes against the resolved parent catalog's identity at the update's target
   revision, not the installed wheel's init-template identity, for any file whose
   provenance is catalog-inherited rather than template-scaffolded. This is a
   correctness bug in existing classification logic, not a new capability — it does not
   need its own ADR and is tracked directly as `UPDATE-CLASSIFICATION-001`.
2. **No semantic path for genuine conflicts.** Once the comparison reference is fixed,
   some files will still be genuine three-way conflicts: local overlay vs. newer
   upstream vs. recorded baseline, all three different. Today `litai update` can only
   refuse those and leave a human to resolve them by hand, file by file, with no
   framework-assisted way to ask "does this specific divergence still make sense
   against the new upstream, or should it be reconciled?"

## Decision

### Fix the comparison reference (`UPDATE-CLASSIFICATION-001`, no ADR)

For any file whose provenance is a catalog import (tracked in `CatalogImportsFile`,
not template-scaffolded), `litai update`'s classifier compares local bytes against that
file's identity in the **resolved parent catalog at the update's target revision**, not
against the installed wheel's `project_template` snapshot. A file whose local bytes
already equal the parent-catalog bytes at the target revision is `already-current` (or
an equivalent non-destructive class), never `conflict`. Template-scaffolded files
(no catalog-import provenance) keep today's baseline-identity comparison unchanged —
this ADR does not touch that path, since it is not the one the defect report
identified.

### Add an optional coding-agent merge review for true conflicts (`UPDATE-MERGE-001`)

For a file that remains a genuine three-way conflict after the reference fix above,
`litai update` gains an **optional** review step, off by default, that a caller
explicitly requests (a new flag, e.g. `--review-conflicts`, exact name to be finalized
during implementation planning):

1. For each remaining conflict, assemble the three inputs already available to the
   classifier: the recorded baseline, the current local bytes, and the upstream bytes
   at the target revision — plus enough surrounding context (the file's declared role:
   Component, Flavor, skill, workflow, routing, documentation) for a coding agent to
   reason about intent, not just text.
2. Dispatch one coding-agent review per conflicted file (reusing this repository's
   existing coding-CLI adapter, the same selection/priority/fallback machinery
   `litai rebuild` already uses — this is not a new model-invocation surface).
   The agent proposes exactly one of: keep-local, take-upstream, or a merged result,
   each with a short identity-bound rationale citing which specific hunks motivated the
   choice.
3. **Nothing is written from this step alone.** The proposal is returned as a typed,
   inspectable result (plan-only, matching `litai update`'s existing plan/apply
   separation) that the operator reviews before any `--apply` mode acts on it. A silent
   semantic overwrite is never the default, matching this project's `release-project`
   skill's own "never publish only a subset unless explicitly permitted" discipline
   applied to the same class of destructive-by-default risk.
4. The reviewed proposal's rationale is retained alongside the update's other evidence
   (matching `record-user-directed-work`'s existing evidence-checklist convention) so a
   later reader can see why a conflict was resolved the way it was, not just that it
   was.

### What this does not change

- Mechanical `upstream-only` and `already-current` classification and apply remain
  exactly as they are today; this ADR only narrows what counts as a `conflict` (via the
  reference fix) and adds an opt-in resolution path for what remains one.
- No project-specific vocabulary, paths, or release evidence from any downstream
  project (including Physics Workbench sample) enters this repository's fixtures or gate. Test
  fixtures use anonymous initialized projects and a synthetic release where template
  and catalog deliberately differ for the same path, per the originating issue's
  stated non-goal.
- This does not change `litai update`'s plan/apply separation, the catalog-import
  collision handling already fixed in `UPDATE-APPLY-001`, or `_retired_local_authority_paths`'s
  divergence-preserving behavior for retired imports.

## Consequences

The comparison-reference fix (`UPDATE-CLASSIFICATION-001`) is a pure correctness
improvement with no new surface area: it should strictly reduce false `conflict`
classifications, never introduce new ones, and is safe to land without a review cycle
beyond ordinary code review.

The merge-review capability (`UPDATE-MERGE-001`) adds a new, real cost: one coding-agent
invocation per remaining conflict, opt-in and plan-only, so it costs nothing when unused
and costs one model call per conflict when requested.

Planning cycle (2026-08-28): the flag is `--review-conflicts`. Each review returns
`keep-local`, `take-upstream`, or `merge` with an identity-bound rationale in the
update JSON envelope (`literate-ai/project-update-conflict-review@1`). Nothing is
written from the review step; `--apply` still writes only the mechanical subset.
Rationale retention is the plan envelope (and work-item queue when
`--record-work-items` is used), not a second on-disk ledger.

0.9.0 amendment (2026-09-04): issue #244 exposed a dependency-closure gap in that
last sentence. A downstream could adopt a newly exported skill while a dependency
that it pins remained a true conflict. Validating the complete prospective tree then
correctly rejected the inconsistent selection, but there was no deterministic way to
apply the reviewed `take-upstream` choice inside the same rollback unit. A repeatable
`--take-upstream PATH` now authorizes only paths that the inherited-catalog plan
classified as `conflict`; complementary `--keep-local PATH` authorizes only planned
inherited-catalog removals whose bytes were otherwise safe to delete but remain a
dependency of local authority. Any other path fails before mutation. The selected
upstream bytes, retained compatibility paths, new additions, ordinary safe changes,
catalog provenance, framework changes, and repository lineage commit and validate
once or roll back together. The apply receipt records `taken_upstream` and `kept_local`
separately from mechanically safe `applied` and capability `adopted` paths. Unselected
conflicts and explicitly retained removals remain local authority and are omitted from
prospective import provenance rather than being falsely attributed to upstream. Agent
review remains plan-only: an operator must pass each accepted path explicitly.

# Operator adoption program

- **Status:** completed
- **Owning queue item:** [ADOPTION-001](active-work.md#x-adoption-001-make-operator-adoption-the-post-090-product-headline)
- **Completion / archival evidence:** [ADOPTION-001](active-work.md#x-adoption-001-make-operator-adoption-the-post-090-product-headline) and every child item are closed; clean commit `2f4a34a1` passes the isolated 0.10.0 wheel create/adopt golden paths.

## Outcome

`v0.9.0` is tagged. The 0.10.0 line is *being used*, not more
harness surface. A stranger can create a project or adopt an existing tree
without reading architecture docs. Repeat operators can see status, convert
stage, and the next verb from one command. Agents use the same state machine
over `--json`; humans may use a TTY presenter. This program does not invent a
second planner, daemon, or IDE.

## Why this is the next headline

Goal 1 in `PROJECT.md` is already `litai init` and `litai init --convert`.
Those verbs exist. What does not exist is a guided first hour, a repeat-use
status surface, or a visible convert authority stage (wrapped is not
spec-authoritative). The framework-score program still scores brownfield and
operational maturity lowest. This program moves those scores with operator
contracts, not with new languages.

## Out of 0.9.0 scope

Do not absorb this program into the closed 0.9.0 cut. See
[RELEASE-0.9-CUT-001](active-work.md#x-release-09-cut-001-qualify-and-publish-090).
`v0.9.0` is tagged. STATUS-001, ONBOARD-001, GOLDEN-PATH-001, and
BROWNFIELD-STATE-001 are 0.10.0 work.
[QUEUE-HYGIENE-001](active-work.md#x-queue-hygiene-001-archive-completed-queue-items-and-reconcile-the-open-tracker)
is closed.

Rejected from this program: an in-tree IDE sample, a mandatory CodeGraph
dependency, new language Flavors, and a Textual/VS Code shell that bypasses
`literate-ai/cli-result@1`. Related but separate: [#295](https://github.com/NVIDIA-dev/literate-ai/issues/295)
HTML5 observability; [#301](https://github.com/NVIDIA-dev/literate-ai/issues/301)
re-init `--force`; [#279](https://github.com/NVIDIA-dev/literate-ai/issues/279)
submodule monorepos; [#251](https://github.com/NVIDIA-dev/literate-ai/issues/251)
0.8.4-to-0.9.0 wheel migration.

## Ordered delivery

### 1. Readable queue (closed)

[QUEUE-HYGIENE-001](active-work.md#x-queue-hygiene-001-archive-completed-queue-items-and-reconcile-the-open-tracker)
archives completed programs to `docs/history/roadmap/`, refreshes
`PROJECT.md` completeness so it matches the current catalog, and reconciles
open GitHub issues that have no queue item. This is maturity work. It does
not block the 0.9.0 tag.

### 2. Status and doctor

[STATUS-001](active-work.md#x-status-001-expose-operator-status-and-doctor-as-the-repeat-use-front-door)
adds `litai status` (doctor as the host-preflight view of the same contract).
One screen: host tools, coding-CLI authentication, project vs convert state,
stale locks, last receipt identity, next verb. TTY prose and `--json` share
one typed envelope. Reuse init, convert-plan, lock, receipt, and host-path
services. Do not add a new authority store.

### 3. Convert stages in project metadata

[BROWNFIELD-STATE-001](active-work.md#x-brownfield-state-001-make-convert-authority-stages-first-class-project-state)
records an explicit convert authority stage on the project:
`wrapped` → `retained` → `drafted` → `qualified`. Status and onboard consume
that field. Original source stays release authority until regenerative
qualification passes. This is the load-bearing adopt-path slice, not a
cosmetic badge.

[DRAFT-PROMOTION-001](active-work.md#x-draft-promotion-001-allow-reviewed-static-promotion-from-retained-to-drafted)
closes the retained-to-drafted gap without re-enabling model egress. A static inverse
bundle may gain one explicit Component boundary only through
`spec review --component-graph FILE`: the reviewer supplies kind, profiles, public
capability contracts, entrypoints, build needs, source paths, observations, and exact
evidence. The review signature binds that graph. Acceptance reopens the signed graph,
checks it against the exact source snapshot and complete observation/evidence closure,
and only then creates retained-source Component authority projections. Static review
does not infer executable semantics or split one inventory into multiple Components.
`spec accept --integrate-project` installs that authority as a registered source-free
child of the retained project. The conversion gate validates the registry and aggregates
the child projection identities, allowing the owning adopted project to advance to
`drafted` without altering its retained source or catalog configuration.

### 4. Onboard create or adopt

[ONBOARD-001](active-work.md#x-onboard-001-guide-create-and-adopt-through-one-plan-then-apply-onboard-verb)
adds `litai onboard` as a state machine over existing verbs.

- **Create:** host preflight, host-derived Flavor defaults, optional
  `litai design refine` from a one-paragraph mission, typed init plan, apply,
  then the next three commands.
- **Adopt:** always plan first (`init --convert --plan`). Show detected
  languages, proposed Flavors, quarantine map, retained runners (no invented
  pytest), Gitlink/submodule blockers, and the stage the tree will land in.
  Apply only after acknowledgement. Coach into convert-project / spec merge /
  retained receipt, not into `rebuild` as if the spec were already authority.

The TTY presenter is optional. `--json` is the contract. Agents must not need
the TUI. No second planner.

### 5. Tested first hour from a wheel

[GOLDEN-PATH-001](active-work.md#x-golden-path-001-prove-a-stranger-first-hour-from-a-published-wheel)
is the acceptance contract for the program: a stranger, empty directory,
authenticated coding CLI, published wheel (not this checkout's
`make bootstrap`), reaches either hello via create or a retained receipt via
adopt. Getting-started leads with that path. Maintainer bootstrap stays
documented separately.

## Architecture

One application service owns operator session state (status snapshot, onboard
plan, apply). CLI adapters render TTY or JSON. MCP tools, if added later, call
the same service. Init and convert remain the mutators. Onboard never writes
`literate.project.json` except by calling those mutators.

Convert stage is project metadata with fail-closed transitions. It is not
inferred only from directory heuristics at print time, though heuristics may
propose a stage for review.

## Acceptance contract

- `litai status --json` and `litai onboard --json` are stable typed envelopes
  on TTY and non-TTY.
- Create and adopt both show a plan and require acknowledgement before apply.
- Adopt never claims specification release authority before qualification.
- Drafted promotion requires either a validated semantic inverse graph or an exact
  signed single-Component static-review graph; inert inventory alone never promotes.
- The golden-path test uses a published wheel, not `PYTHONPATH=src`.
- 0.9.0 release evidence does not depend on this program landing.

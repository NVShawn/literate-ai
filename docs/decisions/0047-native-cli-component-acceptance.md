# ADR 0047: Admit native CLI Components through verifier-owned cases

- Status: Accepted
- Date: 2026-09-24
- Accepted: 2026-09-25
- Decision owners: Literate AI maintainers
- Roadmap: NATIVE-CLI-001 in `docs/roadmap/active-work.md`
- Extends: ADR 0005 executable Component semantics and ADR 0026 per-entrypoint
  deployment units

## Context

The portable application contract intentionally accepts one JSON argument and emits one
JSON result. A native command-line program instead owns ordinary argument vectors, exit
status, standard streams, file inputs and output artifacts. Reclassifying such a
program as `portable-application` would misstate its public interface, while classifying
it as a generic Component currently makes project validation reject its authored
`native-cli-application` kind before lifecycle work can begin.

Draft PR #484 demonstrates a useful direction: a distinct Component and entrypoint kind
plus verifier-owned cases. It is not current authority. The draft describes its
argument model as POSIX despite supported Windows targets, admits executable paths from
ambient environment variables, reads those executables outside accepted dependency
custody, and validates only named output files rather than the complete filesystem
delta. It also lacks a synthetic end-to-end Component proving locks, selected native
Flavors, packaged executable custody and receipt invalidation.

## Decision

### Add a distinct kind without changing the portable ABI

Add `native-cli-application` to `ComponentKind` and `native-cli` to entrypoint kinds.
The kind requires at least one native CLI entrypoint and never inherits the JSON argv,
test-mode or smoke-mode protocol of `portable-application`.

ADR 0026 still governs multiple entrypoints and deployment units. A Component may have
multiple native CLI entrypoints, and each receives an independently named acceptance
binding. Mixing native CLI, portable CLI, persistent service or web entrypoints is
allowed only when the existing kind-set rules resolve the combination unambiguously;
otherwise validation refuses it with a diagnostic naming the conflicting kinds. This
ADR does not change kind priority to make one entrypoint silently dominate another.

The Component lock, package plan and receipt bind every native CLI entrypoint's name,
deployment unit, package-relative executable path, selected native build/platform
Flavors and command projection. Absolute host paths and executable environment
variables are not authored Component authority.

### Use a portable, exact verifier-owned case contract

Each native CLI deployment unit requires a versioned acceptance document in the
existing verifier-owned acceptance directory. Its identity binds the Component and
entrypoint identities, accepted specification-set identity, target tuple, fixed
environment policy, resource bounds and a canonical nonempty case list.

A case declares:

- an exact argument vector, with only typed verifier workspace/file placeholders;
- optional bounded standard-input bytes;
- an exact exit code or the explicit `nonzero` predicate;
- bounded stdout and stderr expectations, using exact bytes or declared UTF-8
  contains/does-not-contain predicates;
- immutable input fixtures with relative paths, modes, sizes and content identities;
- the complete allowed post-execution filesystem projection, including path, type,
  mode, size bound and content expectation for each retained output; and
- case-specific timeout, output and filesystem byte limits within project maxima.

Paths are normalized portable relative paths. Case identifiers, paths and expectation
sets are unique and canonical. Placeholders cannot select arbitrary host paths. Binary
fixtures and outputs are supported as identity-bound bytes; text predicates require
valid UTF-8 explicitly. Undeclared created, removed or changed filesystem entries,
links, devices, sockets and path escapes fail acceptance.

The acceptance schema is published in the installed schema catalog. Its loader rejects
unknown fields, empty case sets, conflicting expectations, prefix/path collisions,
unbounded data and a specification or entrypoint identity that is no longer current.

### Execute the packaged product in lifecycle custody

Normal packaged execution records that native CLI invocation is deferred to independent
acceptance. The independent verifier resolves the selected entrypoint only through the
accepted package plan/result and rechecks its executable identity and package-tree
custody before and after every case. It never substitutes a source-tree executable or
an ambient executable named by an environment variable.

Each case runs in a fresh directory below the lifecycle object root with a minimal,
fixed target-appropriate environment, locale, timezone and umask. Only explicitly
declared non-secret environment values may be added. The process receives an argv list
directly without shell parsing. Tree-wide termination, deadlines and bounded capture
apply on every supported platform. The verifier snapshots the complete case tree before
and after execution and compares the result with the declared projection before
recording evidence.

Acceptance runs on the selected compatible lifecycle worker that owns the packaged
artifact and target toolchain. Controller-local execution is valid only when the
controller is that selected worker. Returned evidence binds worker observation, target,
package plan/result, executable, acceptance contract, per-case argv and input/output
identities, exit status and stream identities. The finalized project receipt binds that
evidence, so changed cases, specifications, Flavors, executable bytes, target or worker
execution invalidate it.

Host tools needed to build the native CLI remain governed by resolved lifecycle tool
bindings. If a case needs an additional executable as part of the product contract, it
must be an accepted runtime dependency in package custody; ambient `PATH` or arbitrary
environment-variable executable injection is refused.

## Qualification and rollout

Implementation proceeds under the explicit maintainer acceptance recorded on
2026-09-25. Reconcile PR #484 by reimplementing reviewed slices against current
authority rather than merging it wholesale. Qualification must prove:

- authored Markdown, schemas, lock generation/validation and installed schema parity
  for the new Component and entrypoint kinds;
- single and multiple native CLI entrypoints with unambiguous ADR 0026 deployment-unit
  selection, plus targeted mixed-kind and malformed-kind refusals;
- help, invalid argv, standard input, file input, nonzero exit, stream and output-file
  cases on supported Linux, macOS and Windows targets;
- exact fixture modes and identities, complete filesystem-delta enforcement, binary
  artifacts, UTF-8 predicates, path/prefix collision and traversal refusal;
- timeout, process-tree termination, stdout/stderr and filesystem bounds;
- refusal of ambient executable substitution, changed packaged binaries, stale
  specifications/cases, changed target/Flavor selection and stale receipts;
- worker-owned execution with exact returned evidence and refusal of incompatible or
  changed workers; and
- a synthetic generated native CLI passing project validation, lock, build, test,
  package, independent acceptance and finalized receipt verification from an installed
  wheel.

Land in three reviewable slices: kind/schema/lock authority; local packaged acceptance
with the synthetic fixture; then worker and complete supported-platform qualification.
No slice may claim native CLI support from parser-only or mocked execution evidence.

## Consequences

Native command-line applications become honest first-class Components without weakening
the portable JSON application contract. Verifier-owned cases can express real CLI
behavior while locks and receipts remain bound to the exact packaged executable and
native target.

The contract is intentionally stricter than the retained draft: ambient helper
executables and undeclared filesystem changes are refused, and complete qualification
requires supported-platform and worker evidence. Interactive terminal sessions,
long-running services and shell-language command strings remain outside this Component
kind and require their existing or future dedicated contracts.

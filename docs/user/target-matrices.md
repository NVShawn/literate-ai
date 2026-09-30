# Concurrent target matrices

`litai matrix` runs one exact set of Component/target/ordered-Flavor cells through the
canonical full lifecycle. Cells share project authority and accepted source caches, but
they do not share mutable lock, resolution-audit, runtime, or receipt paths. The runner
never copies the project.

## Scope: same-host concurrency, not remote dispatch

`litai matrix` parallelizes multiple target/Flavor **cells** on the host where it
runs — each cell is a local `litai rebuild` subprocess with a different
`--target`/`--flavor` selection. It does not dispatch to remote workers: cells never
consult user `workers.json` or select an `ExecutionWorker`.

For execution on *different hardware* (a private macOS/Linux/Windows fleet), use
`--worker`/user `workers.json` with `litai build`/`test`/`rebuild` instead — see
the worker-fleet documentation. That mechanism and `litai matrix` answer different
questions and compose independently:

- Use `litai matrix` when you need several target/Flavor combinations of *one*
  Component validated together, with a single pass/fail aggregate receipt, without
  hand-rolling N sequential invocations or copying the project.
- Use `--worker` when the *build itself* needs to run on different hardware than the
  one issuing the command.

## Declare a matrix

Store mission-owned selection in a JSON file:

```json
{
  "schema": "literate-ai/target-matrix@1",
  "cells": [
    {
      "schema": "literate-ai/target-matrix-cell@1",
      "component": "components/renderer",
      "target": "linux-x86_64",
      "flavors": ["+lang-cpp", "+build-bazel"]
    },
    {
      "schema": "literate-ai/target-matrix-cell@1",
      "component": "components/renderer",
      "target": "macos-arm64",
      "flavors": ["+lang-cpp", "+build-make"]
    }
  ]
}
```

Cell order in authored JSON is not identity-bearing. Literate AI validates every target
as a portable selector, preserves Flavor order because it defines precedence, rejects
duplicate cells, and sorts the exact cell set by deterministic cell identity.

## Invoke and consume evidence

After reviewing Component, Flavor, skill, and lifecycle authority and explicitly
acknowledging host execution:

```console
litai matrix mission-matrix.json \
  --project . \
  --evidence-root _build/target-matrix \
  --jobs 4 \
  --cell-timeout 3600 \
  --allow-host-execution
```

Each cell first executes `litai lock` with its exact Component, target, and ordered
Flavor selectors, then executes `litai rebuild` with that same selection against the
same project. The matrix cell root scopes the Component lock, resolution audit, and
final cell receipt. Lock publication uses the canonical atomic lock/audit transaction:
a missing or stale scoped pair is materialized from current authority, while malformed,
unsafe, concurrently changed, or non-canonical state fails closed. Rebuild must report
the exact lock identity selected immediately before it or the cell is rejected. Locked
input closure capture reads that scoped lock path and exact bytes, never the committed
host lock, and the lifecycle reobserves the same scoped bytes before accepting later
evidence. A changed scoped lock fails as `component_lock.changed_during_lifecycle`.

A fresh external temporary directory holds the disposable runtime and candidate receipt
and is removed after every attempt, including reruns. Concurrent cells therefore cannot
overwrite or consume one another's mutable evidence. `--cell-timeout` bounds each lock
or rebuild subprocess. Accepted source-cache objects remain content addressed and may
be shared.

Each passing cell receipt binds:

- the Component/project authority observed by the lifecycle;
- target and ordered Flavor selectors through the cell identity;
- lifecycle plan/request and exact Component lock;
- accepted source/cache lifecycle evidence;
- the final lifecycle receipt; and
- a fixed `plan → lock → source-admission → lifecycle` chain in which every accepted
  stage names the preceding stage-evidence identity from the same cell.

`aggregate-receipt.json` binds the declaration identity, exact ordered cell identities,
and exact cell-receipt identities. It is accepted only when every exact cell receipt is
complete and accepted. Interruption or failure is durably represented as `partial` or
`failed`; neither status grants acceptance.

Cell directories are keyed by complete deterministic selection identity. Evidence can
be reused only after parsing the canonical receipt and matching every authority,
selection, plan, lock, admission, and stage-chain identity. A changed cell receives a
different directory; unchanged cell identities and receipts remain undisturbed. Pass
`--reuse` to admit an existing cell receipt only when it is canonical, accepted, names
the exact cell, and binds the current validated project authority. The receipt's own
identity transitively binds its plan, lock, source admission, and predecessor chain.

## Physics Workbench sample migration

Physics Workbench sample should:

1. Pin Literate AI to the exact merge commit of the upstream pull request, then
   regenerate its environment lock. Replace that temporary commit pin only with the
   first released version containing issue #42; never pin a moving pull-request branch.
2. Convert the Newton/PhysX cells from `scripts/prove_component_matrix.py` into one
   `literate-ai/target-matrix@1` declaration. Preserve target names and ordered Flavor
   selectors exactly.
3. Replace the downstream runner invocation with `litai matrix ... --evidence-root
   _build/target-matrix --jobs N --cell-timeout 3600 --allow-host-execution`.
4. Make downstream acceptance consume only accepted canonical per-cell receipts and the
   accepted aggregate receipt. Keep Newton/PhysX mission-specific acceptance inputs and
   expected outcomes downstream.
5. Delete whole-project-per-cell copying, generic thread/process orchestration,
   downstream lock isolation, and downstream aggregate-receipt construction.

Physics Workbench sample must retain only mission matrix declarations and mission-specific acceptance.
It must not fork these generic contracts or accept a partial aggregate.

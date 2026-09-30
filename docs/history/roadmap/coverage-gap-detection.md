# Coverage-gap detection

- **Status:** historical
- **Owning queue item:** [COVERAGE-GAP-001](../../roadmap/active-work.md#x-coverage-gap-001-mechanical-detection-of-declared-but-unimplemented-surface)
- **Completion / archival evidence:** [COVERAGE-GAP-001](../../roadmap/active-work.md#x-coverage-gap-001-mechanical-detection-of-declared-but-unimplemented-surface)

## Worker-path contract

Worker-dispatched `build`/`test` never retain generated source on the coordinating
host. The stub-marker scan therefore runs on the worker after the lifecycle rebuild
and before the runtime tree is deleted, then rides the existing dispatch envelope:

1. `execute_remote_request` scans `runtime_root/sources` with
   `scan_runtime_coverage_gaps`.
2. The report is an optional `coverage_gaps` field on
   `urn:literate-ai:schema:v2:execution-dispatch-result` (schema
   `literate-ai/coverage-gap-report@3`). Older workers may omit the key; new hosts
   treat a missing field as `null`. New producers always emit the key (`null` or a
   report). Adding the field is a bounded protocol change: mixed old-host/new-worker
   pairs fail closed on `additionalProperties` until both sides are current.
3. Command dispatchers return the full dispatch result on stdout, so the host reads
   `coverage_gaps` directly.
4. SSH dispatchers return `RemoteExecutionControlResult`; that control document
   carries the same optional field, and `bind_imported_manifest` copies it onto the
   reconstructed `ExecutionDispatchResult`.
5. `litai build` / `litai test` copy `coverage_gaps` into the component-build JSON
   for both local and worker paths.

Scan-side defects still yield `null` and never fail the lifecycle. High-confidence
findings fail the coordinating `litai build`/`test` with
`build.unimplemented_surface` before artifact export.

## False-positive characterization

No live generation traffic was used. The corpus is checked-in fixture and sample
trees plus labeled unit fixtures:

| Corpus | Role |
| --- | --- |
| `tests/fixtures/source_to_specification/**` | Inverse-work source, treated as generated-like trees |
| `tests/fixtures/spec_map_hello/source/` | Small authored source fixture |
| `samples/` | Sample Component trees (specifications; little generated product source) |
| Unit fixtures in `tests/unit/test_coverage_gaps.py` | Labeled true positives and plausible false positives |

`tests/unit/test_coverage_gaps.py` (`test_fixture_and_sample_trees_have_no_fail_closed_hits`)
requires that fail-closed markers fire on **zero** files in the fixture/sample
corpus. That is a 0% fail-closed false-positive rate against available generated-like
source. Advisory `TODO`/`FIXME` hits, if any, do not fail the build.

Labeled unit fixtures justified two pattern tightenings before promotion:

- A generic string-keyed `None` (`{'timeout': None}`) is a legitimate default, not a
  handler table. Fail-closed `none_bound_handler` now requires a `/`-prefixed path
  key, matching the #64 API-route example.
- `NotImplementedError` in generated tests (`tests/`, `test_*.py`) is a common
  assertion/stub and stays advisory. The same marker in a product file is
  fail-closed.

## Fail-closed decision

Promoted:

- `none_bound_handler` — `/`-prefixed string key bound to `None` (the #64 route
  table).
- `not_implemented` — `NotImplementedError` in generated **product** files.

Remain advisory:

- `todo_comment` — `# TODO` / `// FIXME` and equivalents.
- `pass_bodied_abstract` — `@abstractmethod` with a `pass` or `...` body.
- `not_implemented` in generated test paths.

`litai build` and `litai test` fail with `build.unimplemented_surface` when any
fail-closed finding is present, on both the local rebuild path and the worker
dispatch path, and they do not record an artifact export in that case.

## Direction (B): adversarial second pass

Direction (B) in COVERAGE-GAP-001 is a second coding-CLI pass that reads the
specification and the first pass's tests and reports uncovered behavior.

A bounded non-model check is cheaper, but it does not replace (B) for the remaining
#64 example (a worker that never calls its upstream client):

- Declared `Capability.provides` names are capability identifiers, not source
  symbols, so a name-presence scan would be both noisy and incomplete.
- Detecting "imported but never called" requires call-graph or type-aware analysis
  across generated languages. That is a different feature from stub-marker
  detection, with a much larger TCB.
- An adversarial model pass has per-build API cost, nondeterminism, and its own
  trust-boundary problem: a lazy second pass can rubber-stamp gaps.

This note therefore **defers (B)**. The mechanical scan plus fail-closed promotion
above is the cheaper honest slice. Revisit (B) only if fail-closed stub markers
prove insufficient against later live generation evidence, and treat any second
pass as an independent, non-authoritative reporter — never as a gate that can
weaken build, test, or acceptance.

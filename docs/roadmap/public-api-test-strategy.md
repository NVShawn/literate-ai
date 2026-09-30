# Public-API-First Test Strategy Migration

- **Status:** active
- **Owning queue item:** [TEST-STRATEGY-001](active-work.md#test-strategy-001-migrate-the-existing-suite-to-public-api-first-behavior-tests)
- **Completion / archival evidence:** pending while TEST-STRATEGY-001 remains open

## Outcome

Make the existing Literate AI test suite optimize for defects visible through supported
public interfaces. Preserve narrow unit tests only where isolated logic is
safety-critical, algorithmically subtle, or materially easier to diagnose directly.
Do not churn tests solely to change their layer or increase coverage.

## Migration order

1. Inventory tests by public contract, workflow, external boundary, and isolated
   algorithm. Identify duplicate behavioral assertions and mock-heavy tests before
   editing.
2. Establish public CLI and Python API contract tests for schemas, exit/status codes,
   documented errors, compatibility, and authentication or authorization boundaries.
3. Convert high-value feature areas to complete API-level scenarios using real
   planners, stores, adapters, and realistic filesystem persistence. Mock only network,
   host-tool, credential, model-provider, clock, and other true external boundaries.
4. Add focused boundary coverage for malformed or missing input, duplicate and
   idempotent requests, retries and timeouts, partial publication, recovery,
   concurrency, and authorization failures where each protects a distinct risk.
5. Remove redundant method-level tests after equivalent or stronger public behavior is
   covered. Retain unit tests for canonical identity, deterministic ordering, bounded
   traversal, atomic filesystem safety, parsers, and similarly subtle logic.

## Acceptance

- Every retained or added test states a distinct contract, regression, boundary, or
  failure mode through its name and setup.
- Public contract suites cover response schemas, statuses, documented errors,
  compatibility, and applicable authentication behavior.
- Representative stateful features have end-to-end create/read/update/delete or
  analogous lifecycle scenarios, including failure recovery.
- Mock-heavy tests mock only true external boundaries; internal call-sequence assertions
  and duplicate wrapper/getter tests are removed.
- Related input families use table-driven, parameterized, or property-based coverage
  where that is clearer and cheaper than repeated examples.
- The complete supported-platform suite remains deterministic and green throughout the
  migration.

## Inventory (2026-08-28)

Contract areas and the first migrated slices:

| Contract area | Public surface | Isolated unit still required | First migrated slice |
| --- | --- | --- | --- |
| Component-lock review | `litai lock --large-review` | semantic-diff bounds, page chaining | CLI transaction tests keep lock bytes on disk |
| Intent refinement | `litai design refine\|explain\|accept` | none beyond schema round-trip | `tests/unit/test_intent_refinement.py` |
| Release protocol | `litai release plan\|prepare\|check\|publish` | Git write-transaction rollback | already public in `test_project_releases.py` |
| Sample execution | `_load_sample` / execution interface | canonical identity of harness bytes | persistent-service and web-application kinds |
| Coding CLI fallback | `CodingCliSourceGenerator.generate`, `CodingCliTaskRunner.run_json_task` | phrase classification | JSON-task quota fallback |

Do not delete lock-review, identity, or filesystem-safety unit tests while migrating.

## Remaining mock-heavy slices after LOCK-004 (2026-08-29)

LOCK-004's Component-lock review CLI transaction tests already keep lock bytes on
disk. After that slice, mock counts still concentrate here (highest first):

| Slice | Tests | Why it is still mock-heavy | Next public-API conversion |
| --- | --- | --- | --- |
| Coding CLI generation | `test_coding_cli_generation.py` | patches `_run_bounded` / `shutil.which` for generate() | remaining `CodingCliSourceGenerator.generate` quota and auth paths |
| Release Git writes | `test_project_releases.py` | Git write-transaction rollback | keep; isolated unit still required |
| Sample ladder | `test_sample_ladder.py` | host/sample execution doubles | persistent-service and web-application kinds |
| Dependency host tools | `test_dependency_lifecycle.py` | inspector binaries (`readelf`, PE headers) | keep; true external host-tool boundary |
| Rebuild / workers / SSH | `test_cli_rebuild.py`, worker and SSH suites | process and transport seams | mock only those true external boundaries |

JSON-task quota fallback now drives `CodingCliTaskRunner.run_json_task` through
stub executables on `PATH` in `tests/unit/test_coding_cli_quota_fallback.py`.
Phrase classification remains an isolated unit around `_coding_cli_quota_denied`.

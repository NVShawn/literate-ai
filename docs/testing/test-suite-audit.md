# Test-suite audit (October 2026)

The suite had grown to 676 modules and about 6,732 test functions, mostly unit
and contract tests under `tests/unit`. Full CI took hours, and some macOS cells hit
GitHub's six-hour job limit (#13). The release owner directed a suite built around smoke
and end-to-end tests, with much less emphasis on unit tests.

Every module was classified against that goal, then deleted or trimmed:

| Decision | Modules |
|---|---|
| E2E (kept, trimmed to strongest scenarios) | 67 |
| Smoke (kept, trimmed to a few high-value cases) | 113 |
| Critical (kept, minimal fail-closed cases) | 148 |
| Merge (reduced to the cases worth keeping) | 57 |
| Delete (unit, contract, mock-replica or duplicate coverage) | 291 |

380 modules remain, in `tests/e2e`, `tests/smoke`, `tests/critical` and
`tests/conformance`. Shared fixtures moved to `tests/support`, so test modules no longer
import each other. Expensive fixtures (repositories, native SDK builds, initialized
projects, cargo crates) are built once per class and copied per test.

[`test-suite-audit.json`](test-suite-audit.json) records, for each original module, its
decision, its test count before the audit, the tests kept, where it lives now, and the
product behavior it protects (or why it had none). `CONTRIBUTING.md` describes the
resulting policy for new tests.

Remaining known costs:

- **Repository refresh tests:** their runtime is dominated by the refresh code
  re-observing the whole repository at every step (#27). That needs a reviewed design;
  it was deliberately not changed here.
- **`tests.conformance.test_self_hosting`:** its two clean replays take several minutes.

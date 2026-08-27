# Literate AI 0.7.1 (historical release marker)

Literate AI 0.7.1 was released on 2026-08-27 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `387da394e5666be8c8e42ae8bdc714ad5c516ae5`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.7.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.7.1 - 2026-08-27

- Host bootstrap POSIX search path reads `HOME` from the worker environment instead of
  `Path.home()`, so Linux/macOS discovery still works when the process has no usable
  home directory.
- Bind Component-lock review tests to the temp project's cache roots so they do not
  adopt an xdist worker `OBJ_DIR` that is already non-empty and unmarked.
- Add explicit paginated review transactions for genuine Component-lock transitions
  larger than 512 semantic differences. Every page is bounded and identity-chained;
  apply revalidates all authority and performs one atomic final replacement without
  ever writing a partial lock.
- Establish public-API-first test design as the contributor standard and begin a
  suite-wide migration toward contract and complete workflow tests with realistic
  persistence, retaining isolated unit tests only for distinct complex or
  safety-critical logic.
- Keep the Python test suite on public CLI, schema, and script contracts, complete
  workflows, and high-risk boundaries; drop private-helper, call-sequence, and
  coverage-only tests that do not protect a distinct failure mode.
- When a release policy names `default_branch`, `litai release plan` may run on
  that trunk only to cut a missing `release/<major>.<minor>.x` line;
  `prepare` creates and checks it out. `check` and `publish` refuse the default
  branch and any other name. The `release-project` skill no longer treats "the
  default-branch tip is the release" as a valid publish checkout, and no longer
  asks the operator to create the line with raw Git before `plan`.

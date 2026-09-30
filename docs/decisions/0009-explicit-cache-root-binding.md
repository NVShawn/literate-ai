# ADR 0009: Lifecycle Cache Roots Are Explicit Command-Line Bindings Recorded in the Receipt, With Environment Variables Only as Fallback

- Status: Accepted
- Date: 2026-08-20
- Decision owners: literate-ai maintainers
- Roadmap: `CACHE-010`

## Context

`BUILD_DIR` and `OBJ_DIR` select the two host roots every lifecycle uses: the
generated-source cache and the object/tool cache. Today they are resolved exclusively
from process environment. `resolve_cache_directories()`
(`src/literate_ai/cache_directories.py:106`) reads `os.environ` and falls back to
`<project>/generated` and `<project>/_build`. The `Makefile` sets both with `?=`, so
`make OBJ_DIR=... samples` works, and `literate.project.json`'s
`lifecycle_driver.environment_keys` allowlists both so they propagate into an external
driver.

Three consequences follow, and all three were observed directly during the 0.5.2
backport release cycle:

1. **There is no command-line surface.** `litai` has no `--build-dir`/`--obj-dir`
   flags at all. The nearest thing is `src/literate_ai/cli/rebuild.py:214`, which
   rejects the *separate* accepted-source-cache overrides (`--source-cache-root`/
   `--source-cache-entry`) for the Standard driver on the grounds that it "uses
   BUILD_DIR-backed cache custody" — an accurate description of exactly the ambient
   coupling this ADR removes. For `litai rebuild` — the only entry point that produces
   a promotable test receipt — ambient environment is therefore the *only* lever. An
   operator who wants a different cache root must mutate the environment of the process
   tree, which is neither reviewable nor expressible in a recorded command.

2. **The roots a run actually used are not recorded.** `CacheDirectories.identity`
   (`:87`) already computes an exact `literate-ai/cache-directory-custody@1` identity
   over `project_root`/`build_dir`/`obj_dir`, and
   `bind_cache_directories()` (`:128`) already binds explicit roots "without consulting
   process environment" — the correct seam exists and is already used by
   `adapters/remote_execution.py`. But nothing carries that identity into the test
   receipt. Two runs of the same revision, one with a warm cache and one cold, produce
   receipts that are indistinguishable after the fact.

3. **This actively obstructs diagnosis.** During the 0.5.2 cycle the `samples` release
   gate passed while `litai rebuild` failed on the same revision, repeatedly. The
   difference was entirely cache-root custody: `make samples` ran against a warm
   `OBJ_DIR` and never regenerated, while `rebuild` regenerated cold. Nothing in either
   result made that distinction visible; it took several full-matrix runs to establish.
   A related custody defect in the same cycle — a runtime root placed where the nested
   coding CLI could not write — surfaced only as a generic
   `coding_cli.empty_generation`.

This is the same class of problem this project already solved elsewhere: a host-shaped
input that silently changes behavior must be an explicit, bound, recorded parameter, not
ambient state.

## Decision

### 1. Add `--build-dir` and `--obj-dir` flags to lifecycle-bearing `litai` commands

Both flags resolve through the existing `bind_cache_directories()`, not
`resolve_cache_directories()`, so an explicitly supplied root never consults process
environment and is subject to the same existing safety rules (`_require_safe_root`, the
distinctness check, and the no-nesting check).

### 2. Environment variables remain a supported fallback

`BUILD_DIR`/`OBJ_DIR` continue to work exactly as they do now when the corresponding
flag is absent, so `make`, CI, and every existing invocation are unaffected. Precedence
is explicit flag, then environment, then the portable default. A flag and an environment
variable that disagree are not an error; the flag wins, because it is the more explicit
statement of intent.

### 3. The resolved roots are bound into the receipt

The `CacheDirectories.identity` of the roots a lifecycle actually used is recorded in
the candidate test receipt, so a receipt states its own cache custody rather than
leaving it to be inferred. This makes a warm-cache run and a cold-cache run of the same
revision distinguishable in evidence.

### What this does not change

- No change to the default roots (`<project>/generated`, `<project>/_build`) or to the
  `Makefile`'s `?=` overrides.
- No change to the runtime root (`--runtime-root`), which is already an explicit flag
  and already required to sit outside the repository.
- **Cache roots do not move into workflow authority.** A workflow describes what to run,
  not where a particular host caches artifacts. Binding host paths into a workflow would
  make the same workflow non-portable across machines, which this project's portability
  rules already forbid.

## Consequences

The immediate benefit is that a lifecycle's cache custody becomes reviewable, exactly
expressible in a single recorded command, and visible in evidence after the fact — which
is what would have made the 0.5.2 investigation short instead of long.

The cost is a widened receipt surface: the receipt schema gains a custody identity that
must stay correct, and a receipt from before this change will not carry it, so the field
must be tolerated as absent rather than required on read. Adding flags to multiple
commands also widens the CLI surface that must stay consistent; the mitigation is that
every one of them routes through the single existing `bind_cache_directories()` seam
rather than reimplementing resolution per command.

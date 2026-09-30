# ADR 0008: The `samples` Release Gate Needs Retry and Checkpoint Resilience for Transient Live-Generation Failures

- Status: Accepted
- Date: 2026-08-19
- Accepted: 2026-08-20, after the 0.5.2 backport cycle independently reproduced this
  exact failure shape six times (see the Context addendum below)
- Decision owners: literate-ai maintainers
- Roadmap: `SAMPLES-RESILIENCE-001`; related: `SAMPLE-PORTFOLIO-002` (corrected; the
  Rust-skill-wiring theory this ADR originally cited as a likely root cause was
  disproven in the same session — see the Context correction below)

## Context

During the 0.5.1 patch-release cycle, with the organizational Codex spend cap forcing
generation through `claude` and then `cursor-agent` instead of the usual `codex`-first
default, the `samples` release gate (`make samples`, `scripts/run_samples.py` /
`tests/conformance/support/sample_runner.py`) failed on three consecutive full runs —
each time on a *different* sample, with a *different* error class:

1. `dependency-planner/rust`: `coding_cli.timeout` (exceeded the configured
   per-generation timeout, even after raising it from the 900s default to 2100s).
2. An unnamed sample: also `coding_cli.timeout`, at the same 2100s limit, via a
   different exception path that bypassed the harness's own `SampleFailure` wrapping
   entirely (a raw `GenerationFailure` propagated to the top level uncaught).
3. `critical-path-scheduler/python`: `coding_cli.generation_failed`, via `cursor-agent`
   instead of `claude`.

Isolating `dependency-planner/rust` alone and rerunning it standalone succeeded once
after two identical-content-hash failures in full-matrix runs — not fully random, but
not deterministic either. The underlying error behind both `dependency-planner/rust`
failures was a build failure, not a timeout at the algorithm level: `couldn't read
source/main.rs`. `dependency-planner`'s specification is precise (bounded inputs, exact
tie-break rules, exact output field names) — the failure is not spec ambiguity about
*what* to compute.

**Correction (same session, before this ADR's planning cycle):** the investigation
initially concluded `dependency-planner` used the wrong skill — the generic
`portable-application-implementation` rather than the dedicated
`rust-portable-json-application`, which has exact, prescriptive Rust build guidance
(`source/main.rs`, one direct `rustc` invocation, no Cargo) the generic skill lacks —
and a same-session sample-portfolio audit claimed that dedicated skill, and 10 siblings
(one per language/build-system Flavor), were orphaned (never referenced by any sample).
Both claims were wrong, and both stemmed from the same methodology gap: checking only a
Component's `component.md` `authoring_inputs`, never checking whether the *Flavor*
itself contributes an `authoring_inputs` skill. It does: `flavors/lang-rust/flavor.md`,
`flavors/lang-cpp/flavor.md`, `flavors/lang-python/flavor.md`, `flavors/lang-go/flavor.md`,
`flavors/lang-javascript/flavor.md`, `flavors/lang-swift/flavor.md`, `flavors/build-cmake/flavor.md`,
`flavors/build-make/flavor.md`, `flavors/build-bazel/flavor.md`, and `flavors/accel-nvidia-cuda/flavor.md` each
already declare their corresponding specification-to-source skill as an
`authoring_inputs` contribution, injected automatically whenever that Flavor is
selected — confirmed by attempting to add `rust-portable-json-application` to
`dependency-planner/component.md` directly, which failed generation immediately with
`selected specification-to-source skill is duplicated: rust-portable-json-application`,
proving the skill was already present in the resolved recipe. `dependency-planner`
building via `rustc`-direct, no-Cargo, `source/main.rs` guidance the whole time — the
`couldn't read source/main.rs` failure happened *despite* the model having that exact
instruction, which is a more surprising and still-unexplained finding than the
originally suspected missing-skill theory. It most plausibly remains genuine
generation non-determinism (the model occasionally not following its own skill's
explicit file-placement rule under time/context pressure), which this ADR's retry
layer addresses regardless of the specific cause. `SAMPLE-PORTFOLIO-002`'s skill-
breadth claim is corrected there; only `repository-layout` and `python-repository-
layout` (init-time scaffolding skills, not Flavor-contributed, though already covered
by `tests/unit/test_make_generation_skill.py` at the unit level) remain plausibly
uncovered by the sample conformance ladder specifically, and that gap is much smaller
and lower-priority than originally recorded.

The `samples` gate has no resilience for this today. A single sample's transient
failure aborts `scripts/run_samples.py`'s entire run (`ThreadPoolExecutor.map`/a plain
generator expression over all samples, first exception propagates and stops
everything), discarding whatever the run had already generated, built, and verified
for every other sample. A retry redoes the complete matrix from nothing. This is a
different problem from what `GENERATION-008` (already shipped) solves: that fix falls
back to the next configured coding CLI on a *hard denial* (an authentication or quota
error the CLI itself reports immediately) — it does not help a *transient* timeout or a
single bad generation attempt on whichever CLI is already selected, which is what all
three of tonight's failures actually were.

This repository already has exact, working prior art for the retry half of this
problem: `GENERATION-006` (`c46d2c74`) added a bounded automatic retry of the whole
coding-CLI subprocess invocation inside `CodingCliSourceGenerator.generate()` — fresh
workspace per attempt via `_reset_generation_output_root()`, up to 2 extra attempts,
recording a retry-count field and an operation-log event per attempt — but scoped
narrowly to one error code, `coding_cli.generated_metadata_invalid`, because that was
the specific probabilistic failure observed at the time. Its own investigation note
already identified the right shape (retry the external subprocess call, not just
downstream parsing, since the fix works because the call itself is non-deterministic)
and explicitly scoped the *bound* of that pass to one error code, not the mechanism.

This repository already has a working pattern for exactly this class of problem:
`scripts/run_checkpointed_unittests.py` and `scripts/run_checkpointed_gates.py`
checkpoint their own multi-stage, potentially-flaky work to a state file
(`_build/python-test-checkpoint.json`, `_build/release-checkpoint.json`) so a retry
after a failure resumes from the last completed stage instead of redoing everything.
`make python-check`'s own checkpoint is exactly why that gate could be iterated on
efficiently earlier this same release cycle, while `samples` could not.

**Addendum (2026-08-20, the 0.5.2 backport cycle — the evidence this ADR was accepted
on).** The same failure shape recurred independently while refreshing the test receipt
for `release/0.5.2`. Six consecutive full `litai rebuild` runs failed, each costing
roughly 25 minutes, on three different samples with three different error classes:
`full-stack-rust-js` (a Rust generated-source role-boundary violation —
`source/tests/litai_test.rs` pulled into the backend compile), `service-stack`
(`coding_cli.empty_generation` on its `money-calculation` node), and
`regenerative-roundtrip` (a Bazel genrule `node` exit 127 plus a syntax error in
generated JavaScript). Every one of them passed on a later run with no code change, and
`service-stack` passed in isolation while failing in the full matrix.

Two findings sharpen this ADR's design rather than change it:

1. **Cache custody, not the sample, decided the outcome.** `make samples` passed against
   a warm `OBJ_DIR` while `litai rebuild` failed cold on the same revision, because the
   warm run never regenerated at all. This is why the checkpoint in decision 2 must key
   on the same content identity the gate already computes, and why `CACHE-010`/ADR 0009
   made the roots explicit and recorded.
2. **Three of the six failures were an operator error the diagnostics actively hid** — a
   runtime root placed where the nested coding CLI could not write, which surfaced only
   as a generic `coding_cli.empty_generation`. The CLI's own explanation ("I need
   permission to write into `source/`") *was* captured at
   `adapters/models/coding_cli.py`, but `litai rebuild` truncated the driver's stderr to
   its last four lines, which kept the least informative traceback frames and discarded
   the one line that named the cause. Retry and checkpointing must not paper over this:
   a retry budget spent on a deterministic misconfiguration is strictly worse than a
   prompt, legible failure. This motivated widening the driver-failure excerpt and
   retaining the complete driver output, landed alongside this ADR's implementation.

## Decision

`scripts/run_samples.py` / `sample_runner.py`'s sample matrix execution gains two
independent resilience layers, addressing the two distinct failure shapes observed:

### 1. Bounded per-sample retry for transient generation errors

When a single sample's generation attempt fails with a class of error known to be
transient — `coding_cli.timeout`, `coding_cli.generation_failed`, and any other error
code this ADR's implementation confirms is retried-successfully in practice, not a
deterministic content/spec defect — retry that one sample's generation a small, bounded
number of times (a default of 2 extra attempts, configurable) before letting the
failure propagate. This directly addresses observed failures 1 and 3 above, where a
bare retry with no code change succeeded.

### 2. Sample-level checkpointing across the matrix

Following the existing `run_checkpointed_gates.py` pattern, `scripts/run_samples.py`
records which samples have already completed successfully to a state file under
`OBJ_DIR` (e.g. `_build/samples-checkpoint.json`), keyed by the same content-identity
inputs the gate already computes per sample (recipe/generation-key identity), so a
resume after a failure — whether from bounded retry exhaustion or an operator-initiated
rerun — skips already-passed samples instead of regenerating the entire matrix. A
checkpoint entry is invalidated the same way `python-test-checkpoint.json`'s entries
are: when its recorded identity no longer matches current inputs.

### What this does not change

- `GENERATION-008`'s hard-denial CLI fallback is unaffected and remains the correct
  response to an immediate authentication/quota rejection; this ADR's retry layer
  handles the separate case of a transient failure on the currently-selected CLI.
- This does not relax what counts as a passing sample — a sample that fails
  deterministically (a real spec defect, a real build error) still fails after
  exhausting its retry budget; the checkpoint records only genuine successes.
- No change to `make python-check`'s or `make release-check`'s own top-level
  checkpointing; this is additive, scoped to the `samples` gate's internal execution.

## Consequences

The immediate benefit is that iterating on a real samples-gate failure — including
this release's own investigation, which cost several full-matrix reruns to isolate one
transient case from a genuine one — no longer pays the cost of regenerating every
already-passing sample on each retry. The bounded retry should also reduce how often a
transient failure is even visible as a gate failure at all.

The cost is new state to keep correct: a checkpoint whose invalidation logic is wrong
(too loose) could mask a real regression as a stale-but-still-recorded pass, exactly
the risk this project's own "checkpoint skips are not passes" discipline exists to
guard against elsewhere. The retry budget and the specific error codes it applies to
need to be conservative and explicit, not a blanket catch-and-retry-anything, so a
genuinely deterministic defect still fails promptly rather than burning three attempts
before surfacing.

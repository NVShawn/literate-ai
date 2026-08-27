# Literate AI 0.7.0 (historical release marker)

Literate AI 0.7.0 was released on 2026-08-27 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `afe52d099fcb5ba98a3c269eb70307d934915d64`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.7.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.7.0 - 2026-08-27

- Restore opt-in external project source intelligence under ADR 0019:
  `provider_id: none` remains dependency-free, while explicitly configured
  `codegraph-cli` projects regain bounded `project source-intelligence sync|check`,
  validation, verification, and lifecycle enforcement without provider installation
  or worker capabilities.
- Each OS Flavor requires remote-worker host configuration to attempt NTP
  synchronization to `time.nist.gov`; detect-before-install records failures in
  `clock_sync` evidence without failing sample-worker readiness.
- Plan and generate now take a project-local language Flavor's source entrypoint
  from its Standard command profile, so a target such as `cpp-posix` does not
  have to be added to the catalog language allowlist and does not receive the
  hardcoded C++ JSON `argv[1]` prompt.
- Installed wheels include nested specification-to-source skill `agents/*.yaml`
  files, so CUDA generator provider metadata is present after `litai update`.
- Require documentation for deployable services to include an end-user installation,
  configuration, startup, readiness, verification, upgrade/rollback, and cleanup path;
  contributor rebuild commands and internal smoke harnesses no longer qualify as getting
  started instructions.
- Component-lock semantic diffs now bound emitted review entries independently of
  traversal work, so large mostly-equal locks stay reviewable without raising the
  512-entry review cap.
- `make samples` still requires an explicit live coding CLI and model; unit-test
  invocations of the sample runner no longer fail closed when those pins are
  absent, matching ADR 0017.
- Component lock catalog walks skip a sibling `implementation/` directory next to
  `component.md`, so convert retained source (symlinks, host trees) is not treated
  as lock catalog.
- Add `litai project documentation-update`: deterministic drift planning is read-only
  and model-free, while explicit `--apply --allow-model-egress` permits bounded,
  identity-checked edits only to existing declared Markdown and leaves final authority
  review recording separate.
- Agent skills under `skills/agent/` now inherit a parent wrap-Python rule, and
  the three development postures nest as `develop-in-production-workflow/staging/dev`
  so the filesystem is the scoping mechanism. Generation workflows nest the same
  way, and routing policies use a matching `routing.json` sentinel in each
  nested directory instead of a sibling `.json` file beside a child directory.
  `release-project` wraps `litai release` instead of restating the CLI, and
  GitHub CI runs on `release/**` branch pushes as well as `main`.
- Source generation now retries a bounded number of times when a coding CLI
  emits a well-formed generated-test suite that still fails uniqueness,
  acceptance-signature, or expected-result-shape admission. Those failures stay
  hard after the retry bound; empty generation is still not retried.
- Convert inspects a live tree before quarantine: `litai init --convert --plan`
  reports readiness without writing; mutating convert records remote CI instead of
  requiring a local `make ci`, wraps nested `repo.sh` stages, runs local-cheap
  baselines (Make, CMake, cargo, python, Bazel, npm, Go) unless `--run-baseline`
  for host-heavy `repo.sh`, and stamps one language-ecosystem Flavor on mixed
  trees.
- Live qualification (`installed-project-e2e --live`, `make samples`, and remote
  fan-out) requires an explicit coding CLI and model from ignored
  `literate.test.json`, with `--coding-cli` / `--model` then `CODING_CLI` /
  `LITAI_LIVE_MODEL` taking precedence. PATH first-available search is not a
  live-test default. Remote workers require `opencode` and `OPENAI_API_KEY` in
  the login environment.
- Each coding-CLI session keeps a per-agent model stack that never reaches
  depth 0 ([ADR 0018](docs/decisions/0018-never-empty-per-agent-model-stack.md)).
  Nested Component, Flavor, skill, and specification-language pushes restore on
  pop; attempting to pop the only remaining frame warns and logs the precise
  caller and specification location.
- Owned sample, installed-project, wheel, roundtrip, and installed-E2E runtime
  roots are now registered in release evidence when created, so failures before
  execution still point to surviving scratch; successful cleanup records the
  historical pointer as pruned.
- Initialization now reports an explicit disabled tool-bootstrap state when no
  source-intelligence provider is configured, unblocking the isolated installed
  project gate without provisioning a source-graph tool.
- A release now closes over its own evidence. Every stage of a release — the
  release root, each gate, each host or worker dialog, each sample variant and
  phase — records one node in an append-only ledger under
  `<obj-root>/evidence/<run>/`, carrying that stage's exact pins and pointers to
  its retained outputs with digest, size, and retention. Child processes inherit
  the run through `LITAI_EVIDENCE_RUN` and `LITAI_EVIDENCE_PARENT`, so a release
  is a hierarchy of steps rather than a flat pile of logs, and
  `litai release evidence explain` answers "what failed and where is its
  evidence" in one bounded page instead of a filesystem search. The ledger
  indexes and points; it never authorizes a build or accepts a release.
- Failure custody replaces delete-on-exit. Sample scratch, installed-CLI
  workspaces, wheel and roundtrip workspaces, lifecycle driver diagnostics, and
  lifecycle failure diagnostics are retained when their step fails, reported by
  absolute path, and registered as pointers; successful steps still clean up.
  This closes the `v0.5.2` host-E2E case, where the failing sample's scratch was
  deleted with the run that produced it and the crash could not be reconstructed
  afterwards. Sample scratch stays outside the checkout in every case: custody is
  a pointer to that external scratch, not a reason to relocate a host build.
- Host and target dialogs are first-class evidence. SSH worker probes and gate
  transcripts, per-worker fan-out logs and failed workspaces, and GitHub Actions
  failed-job logs are captured under bounded envelopes, with worker identity
  recorded as an endpoint digest so no private hostname enters persisted
  evidence. Remote custody is marked `host-only` rather than copied.
- A single-host VM remains a complete release path. The default target runs the
  gate directly on the invoking machine and records the fan-out and provider
  paths as explicit `skipped`/`unavailable` nodes with reasons, so a release on
  one VM is honest about what it did not run instead of silently omitting it. A
  fan-out invoked without a configured fleet reports `authoritative: false`.
- Steps that used to run blind now run through an instrumented harness.
  Single-command release gates, the checkpointed unit-test run, and the installed
  launcher route through `scripts/litai_step.py`, which records a node with live
  teed output and retained redacted transcripts when a run is attached and is a
  pure passthrough when it is not. The Python bootstrap shells keep only their
  candidate loop and delegate the version decision to `scripts/python_resolver.py`
  run by the candidate interpreter itself. CI uploads the evidence root and
  explains the failure in the job log.

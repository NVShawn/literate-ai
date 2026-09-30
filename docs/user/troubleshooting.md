# Troubleshooting

## `litai` is not found

Activate the environment in which the package was installed, then verify:

```console
. .venv/bin/activate
python -m pip show literate-ai
litai --help
```

## Python is too old

The framework requires Python 3.11 or newer. Check `python --version`; create the virtual
environment with an explicit newer interpreter if your system `python3` is older.

## Derivation succeeds but acceptance is blocked

This is expected for unsigned source, unresolved coverage, or blocking uncertainty.
Inspect:

```console
litai spec coverage bundle.json
```

Attest the exact source, derive again with `--attestation` and `--trust-key`, and resolve
every blocking uncertainty through a signed review. If source changes after attestation,
make a new attestation and bundle rather than bypassing the mismatch.

## An attestation or review no longer verifies

The envelope binds exact content. Confirm you are using the same key and source bytes.
A changed file, replaced key, modified bundle, or different review requires a new signed
envelope. Keep keys and envelopes outside analyzed source.

## A case descriptor is rejected

`conformance` requires a JSON descriptor with schema
`literate-ai/source-to-specification-case@1`. Paths are relative to the descriptor,
source must remain inside its declared root, and analyzed source may not contain
symlinks. Start from a case under [`tests/fixtures/source_to_specification`](../../tests/fixtures/source_to_specification/).

## Dynamic observation is unavailable

Observation fails closed unless a separate authorization exists and supported sandbox
tooling is installed: `sandbox-exec` on macOS or `bwrap` on Linux. Do not substitute
direct execution. Static derivation remains available.

## A version resolves to unexpected content

Treat a version range or mutable alias as a query, not a lock. Inspect the recorded
`ComponentRevisionRef` and require the expected SHA-256 identity. Multiple exact
revisions can coexist; do not delete one merely because another has the same SemVer.

## Flavor resolution fails

Look for an unfilled exactly-one slot, ambiguous candidate, typed contribution conflict,
or a security/requirement weakening attempt. Resolve the target profile or policy; do
not patch the base spec with target-specific details.

## Sample generation cannot find a coding CLI

Install and authenticate one supported command, then confirm it is on `PATH`: `codex`,
`claude`, `cursor-agent`, or `opencode`. Set `CODING_CLI` when you need an exact selection. If that
selection is missing, generation fails deliberately rather than falling through to a
different provider. A sample build also needs a C++17 compiler; set `CXX` to a compiler
command when normal `PATH` discovery is insufficient.

## A coding CLI is installed but requires authentication

`coding_cli.authentication_required` means the selected executable was found but has no
usable machine-local login or supplied credential. Check the selected user profile with
`codex login status`, `claude auth status`, `cursor-agent status`, or
`opencode auth list`; authenticate that same profile on this machine with `codex login`,
`claude auth login`, `cursor-agent login`, or `opencode auth login`. For headless use,
pass a supported credential in the environment
of the `litai` process:
`OPENAI_API_KEY` through `codex login --with-api-key` or `CODEX_ACCESS_TOKEN` through
`codex login --with-access-token` for Codex;
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` for Claude; or
`CURSOR_API_KEY` for Cursor; or the selected OpenCode model provider's documented key,
such as `OPENCODE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or
`OPENROUTER_API_KEY`. Do not put credential values in specifications, Flavors,
generated source, command arguments, or committed project configuration.

Pipe the secret without placing its value in the command line:

```console
printenv OPENAI_API_KEY | codex login --with-api-key
printenv CODEX_ACCESS_TOKEN | codex login --with-access-token
```

In PowerShell, use `$env:OPENAI_API_KEY | codex login --with-api-key` or the analogous
access-token command. `CODEX_API_KEY` is forwarded for custom-provider compatibility,
but it is not the standard Codex login credential documented by the CLI.

Persisted authentication is a per-machine, per-user-profile prerequisite normally done
once and repeated only after a token expires or is revoked. Literate AI does not silently
select another installed agent when the chosen one is unauthenticated. The diagnostic
names the login command and supported environment variables but deliberately omits the
provider's raw authentication output, which may contain credential fragments.

If a Flavor replacement reports an axis conflict, subtract the active value before
adding the replacement, for example `--flavor=-flavor://literate-ai/lang-python
--flavor=+flavor://literate-ai/lang-cpp`.

## Generation rejects the generated test manifest

Every generation is a `major-rebuild` and must create
`source/tests/manifest.json`. Missing or malformed content, a stale recipe identity,
fewer than three or more than 256 cases, missing example/boundary/invariant coverage,
non-current specification references, duplicate IDs or arguments, or reuse of an
acceptance argument fails generation.

Fix the owning specification, selected Flavor, or exact generation skill, remove the
failed external output, and generate again into a new empty directory. Do not copy a
manifest or source files from an earlier build into the project or the replacement
tree. The hidden verifier oracle is deliberately unavailable to the coding CLI and is
not a repair input.

## A generated candidate is replaced automatically

The repository sample driver has a deliberately narrow automatic replacement policy.
It recognizes exactly four versioned rejection kinds:

- `generated-source-validation-rejected` requires an exact terminal failed `validate`
  event and a `DependencyObservationError` cause carrying the same code. The generation
  output must bind the candidate tree, source bundle, and generated suite, and no
  lifecycle step may have an admitted result. The closed allowlist is
  `dependencies.bzlmod-authority-unsupported`,
  `dependencies.bzlmod-dependency-duplicate`,
  `dependencies.bzlmod-generated-lock-forbidden`,
  `dependencies.bzlmod-literal-invalid`, `dependencies.bzlmod-module-ambiguous`,
  `dependencies.bzlmod-module-invalid`,
  `dependencies.bzlmod-module-location-invalid`,
  `dependencies.bzlmod-module-name-invalid`,
  `dependencies.bzlmod-source-intent-invalid`, and
  `dependencies.bzlmod-workspace-unsupported`.
- `generated-test-behavior-mismatch` requires an exact terminal `test-generated`
  mismatch whose failed run binds the generation output, source bundle, built artifact,
  generated suite, and expected/observed result identities.
- `generated-application-nonzero-exit` requires
  `host_execution.nonzero_exit` while—and only while—an admitted generated
  implementation case runs. The typed error must identify the `application` or
  full-stack `backend`, carry a nonzero integer return code, and bind exact stdout and
  stderr SHA-256 digests. The rejection also binds the case and expected-result identity;
  absent or malformed process evidence fails closed.
- `generated-source-build-rejected` requires an exact terminal C++ `build` failure after
  validation, classification, and authorization completed. The actual `BuildError` and
  failed event must both carry `builder.cpp_generated_source_rejected`, the generation
  output must produce exact candidate-tree, source-bundle, and suite identities, and
  neither a build result nor any downstream step may exist. The C++ builder emits that
  candidate-specific code only after its generated-source compile/link fails and a
  second compile-and-link canary succeeds with the same pinned compiler and linker.

The driver discards the complete candidate and repeats that language variant in a new
empty attempt root. It uses the unchanged recipe and does not send the previous source,
tests, mismatch, compiler diagnostic, or verifier-only facts to the coding CLI. The
limit is three total attempts.

If the canary also fails, the toolchain or environment remains a plausible cause and the
builder returns terminal `builder.cpp_compile_failed`. Authentication, missing-tool,
policy, other dependency, toolchain, build authorization, malformed-suite, and
independent-verifier failures are not retryable. Execution timeouts, process launch or
authorization failures, invalid output, and a verifier-side nonzero exit are terminal.
Python, Rust, JavaScript, generic native, and all Bazel build/resolution/analysis failures
are also terminal. No category retries when its run, process, or terminal evidence is
missing or inconsistent. Do not weaken these checks to make a run retry. Fix the owning
specification, Flavor, skill, host prerequisite, or adapter and start a new clean run.

Bzlmod resolver, dependency-evidence, toolchain, framework, and unknown-code failures are
not static generated-source admission evidence. A failure after `validate` or any later
lifecycle result has been admitted is likewise terminal. For example,
`dependencies.bzlmod-evidence-missing` must never be added to the static allowlist.

When Bazel is selected, resolution, analysis, source queries, the complete Bazel build,
and its checks normally run first. Only a nonzero full build invokes the exact authorized
native delegate diagnostically. If that native build succeeds, the adapter rethrows
`builder.bazel_build_failed`; it does not convert the generic failure into source
provenance. Only a diagnostic C++ failure followed by a successful same-toolchain canary
becomes `builder.cpp_generated_source_rejected`. Do not make the generic Bazel code
retryable as a shortcut.

On eventual success, inspect the version-7 conformance execution's
`generation_candidate_attempt_count` and `rejected_generation_candidates`. On
exhaustion, inspect the latest
`candidate-attempts/VARIANT/rejections-NN.json` beneath the configured external runtime
root. Each write-once harness file is a compact, cumulative, content-identified
`literate-ai/generated-candidate-attempts@1` envelope; rejected source and raw
diagnostics are intentionally absent from Git receipts and source-cache authority.

## Generation or build rejects a CycloneDX SBOM

Fresh generation must create canonical CycloneDX 1.7 JSON at
`source/.literate/sbom.cdx.json` with lifecycle `pre-build`. The post-build evidence must
use lifecycle `post-build` and exact versions. Both documents need the complete exact
managed graph: root, every transitive Component, every declared repository-source
dependency, and every managed relationship's source, target, kind, optionality, and
identity. They also need one explicit dependency entry per inventory object (including
empty leaves) and no dangling or disconnected node. The source composition is normally
`complete`; it may use `incomplete_third_party_only` only when a selected authorized
resolver will close that external transitive graph. The post-build composition is
always `complete`. Timestamps and random serial numbers are rejected because they make
equal observations produce different bytes.

Do not delete a troublesome dependency or mark a partial graph complete. Fix the
specification/Flavor/skill or dependency resolver, regenerate into a new empty runtime
root, and rerun the full lifecycle. The resolved document must bind the exact source-BOM
identity and preserve every source reference, edge, and managed relationship; an honest
range may become an exact version, but removal, reparenting, relabeling, or alternate-ref
substitution is invalid.

Source admission reconciles supported manifests, locks, and imports with the source SBOM
before compilation. Dependency resolution repeats that unchanged-source check after the
build and before tests, then recursively inspects binaries without launching generated
code. On macOS check that exact `xcrun` can resolve `dyld_info`; on Linux check exact
`readelf` and `ldconfig`. On Windows the reference adapter chooses `dumpbin` first and
otherwise `llvm-readobj --coff-imports`, recursively resolves PE imports, and maps
virtual API-set contracts through exact `System32\apisetschema.dll` evidence. Confirm
the chosen inspector, its version/digest, the API-set file digest, and the parsed-map
identity. Unavailable delay-only imports remain explicit; an unresolved required import
fails closed. Never use `ldd` on an untrusted generated binary. A missing or changed
inspector, unresolved library, ambiguous search
result, malformed/oversized output, missing manifest/lock/import entry, or incomplete
npm or native closure must fail the resolved-SBOM phase. Fix the selected adapter or
dependency declaration; do not bypass the phase or edit the evidence.

For generated Bazel 9/Bzlmod projects, `bazel_dep` is supported only as a literal direct
request in the current profile. Its version is not a final lock: Minimal Version
Selection may select another version while resolving the transitive graph. Do not ask
the coding agent to invent `MODULE.bazel.lock` or add guessed transitive modules. Its
source-BOM node must use CycloneDX type `library`, be external, omit an exact version
and hashes, retain the literal
request as `vers:generic/>=<version>` plus
`literate-ai:bzlmod-requested-version`, and have build kind/scope. The
authorized Bazel adapter resolves an external copy, captures the public lock, module
graph, repository evidence, and exact Bazel identity, and replays with
`--lockfile_mode=error`. It retains `buildfiles(//...)`, the `deps(//...)` source-file
closure, and a source-bound typed consumption record; do not add build metadata or Bazel
test sources to a native builder's lifecycle allowlist or disable its unused-file check.
Legacy `WORKSPACE` dependency authority, overrides, extensions,
includes, or dynamic MODULE logic currently fail closed. Select a supported declarative
Bzlmod graph, choose another build-system Flavor, or implement and pin an adapter for
that authority; do not weaken the dependency gate.

For C++ projects, keep native test translation units under `source/tests/`, or name
root-level test files `test.cpp`, `test_*.cpp`, or `*_test.cpp`. The portable native
builder excludes those conventional test units from the application link so their
`main` functions cannot collide with the runnable entrypoint; the selected Bazel graph
still compiles the complete target set. A production build integration should derive
this partition from the typed build graph described by `BUILD-220`, rather than relying
on the bootstrap convention.

For Rust projects, `source/tests/manifest.json` is generated lifecycle metadata. Do not
compile it into a `rust_test` or use it as a proxy for application behavior; Literate AI
validates and executes those cases independently. Put real Rust behavior tests in a
`#[cfg(test)]` module reachable from the application sources and make the Bazel
`rust_test` compile those sources. Any other compile-time data must be an explicit Bazel
input.

## A source-cache hit is absent, ambiguous, or rejected

If rebuild fails with `rebuild.derivation_planning_failed`, the configured lifecycle
driver did not implement the mandatory read-only `plan-derivations` handshake, crossed
the host-execution boundary, timed out, or failed to emit its bounded canonical typed
manifest. Update the driver to calculate the exact recipe/plan/tool/model/request keys
without invoking a coding agent or build tool. Do not synthesize a partial key list:
the subsequent decision and lifecycle must cover the manifest exactly.

Cache matching uses exact recipe, execution plan, coding-CLI tool, model binding, and
request identities—not timestamps. A missing read-only root is an empty cache and stays
unmodified. A canonical format-only committed root is the equivalent portable Git
representation because Git does not store empty directories; it is also read as an
empty non-mutating miss. A populated target must retain its entry, key, and CAS
namespaces. If one is missing or unsafe, `source-cache.path-unsafe` names the exact path
that failed validation. Multiple different eligible entries fail when `require_unique`
is enabled.
Corrupt immutable objects, unsafe/overlapping roots, wrong SBOM bindings,
and source-tree drift are hard failures.

A materialized candidate always reports `current_acceptance_trusted: false`. This is
expected: historical evidence cannot skip current indexing, authorization, build,
resolved-SBOM verification, generated tests, or independent acceptance. Use a forced
major rebuild when you intentionally want to bypass lookup; do not edit immutable cache
membership or copy cached source into an authority catalog.

## Documentation authority review is missing, duplicate, or stale

`litai project validate` requires exactly one current
`literate-ai:authority-reviewed` marker in declared Markdown. A change to the project
definition, `SKILL.md`, Components, Flavors, exact skills, workflow/routing catalogs,
declared documentation, or documentation assets invalidates its digest. The tactical
execution queue under `docs/roadmap/` is still link-checked as declared documentation,
but checkbox and plan-document edits there do not change the review digest.

First review the documentation against the changed authority. Then calculate the exact
marker without changing the checkout:

```console
litai project documentation-review .
```

After that review, record the current identity atomically:

```console
litai project documentation-review . --record
```

`--record` replaces exactly one placeholder or stale marker and fails closed when a
marker is missing or duplicated, without modifying files. Do not copy a digest from
another project or treat the read-only command as the review itself: it proves freshness
and uniqueness, not prose quality. Run `litai project validate .` again and commit the
reviewed documentation.

## The project test receipt policy is absent, or the receipt is missing or stale

`policy-unconfigured` is normal immediately after `litai init`: the scaffold has a
storage path but no authority to accept a receipt. Add a policy that pins the intended
suite ID/version, exact runner identity, required evidence, and minimum test count.
`missing` then means no successful admitted run has been recorded. `stale` means the
receipt binds an older complete authority-review identity; `policy-mismatch` means it no
longer satisfies the configured admission policy. Run the full test workflow again and
have its authorized adapter produce a new candidate outside the project, then use:

```console
litai project test-receipt update /tmp/test-candidate.json --project .
litai project test-receipt check /tmp/test-candidate.json --project .
litai project test-receipt require-current --project .
```

A failure or skipped test cannot be represented by the passing-only receipt and must
leave `verification/current.json` unchanged. A project mismatch, noncanonical existing
receipt, symlink, or concurrent replacement also fails closed. Do not edit the receipt
by hand or append logs to it; replace it through the command after a passing run, then
commit that single file so Git records history.

## `make python-check`/`make release-check` reruns everything after a small edit

Both gates are fail-fast **repair-resume** checkpoints
(`scripts/run_checkpointed_unittests.py`, `scripts/run_checkpointed_gates.py`), not a
change-DAG-aware selective test runner. Two different granularities exist and they
behave differently:

- `python-check`'s checkpoint (`OBJ_DIR/python-test-checkpoint.json`) stores completed
  individual test names beneath one aggregate suite pin. That pin covers the lifecycle-
  driver TCB plus the ordered discovered test list and all discovered test-class source
  bytes. Changing any covered test source invalidates reuse for the whole suite; an
  unchanged aggregate pin may reuse the exact tests that passed in an unfinished run.
- `release-check`'s checkpoint (`OBJ_DIR/release-checkpoint.json`) tracks whole named
  gates (`repository-layout-check`, `python-check`, `lint`, `wheel-check`, ...). Each
  gate is bound to either the resolved lifecycle-driver TCB pin or the documentation-
  authority pin as appropriate. The checkpoint also records global completion order;
  prior passes are reusable only when the combined completed names are the exact prefix
  of the current ordered gate plan and each remains under the same pin. A changed plan
  resets repair progress. This is still coarser than a dependency-aware selective test
  graph and does not fingerprint the entire commit or the worker environment.

Both reset themselves automatically the instant a full run succeeds -- the checkpoint
file only survives between invocations when the previous run *failed* partway through,
specifically so the next run can resume from the point of failure instead of starting
over.

**Do**: after a failure, just rerun the same `make` target plain. It resumes from
where it stopped; this is the entire point of the mechanism.

**Don't**: delete the checkpoint file (or pass `--reset`) before every run "to be
safe." That discards genuine resume state and forces a full rerun from test/gate one
every time, for no benefit, on every gate/test whether or not it was affected by your
change.

**Do** reset explicitly (`make release-check-reset`, or the finer-grained
`run_checkpointed_unittests.py --state ... --reset` equivalent) before re-validating a
**different commit or worker** whose checkpoint might hold gate-level "passed" state
recorded against a different exact state. The framework pins cover declared TCB and
documentation authority, not every repository path, external dependency, or host-state
input, and final release evidence must begin at gate one. This matters most for a
persistent SSH worker fleet (`litai release check --target local`): reset every worker's
checkpoint whenever you resync it to new source before trusting its next release
attestation, even when the automatic pin comparison would invalidate most prior work.

### A classified failure wastes every repair rerun

Do not turn a known failure into `unittest.skip`, `xfail`, or an ignored process
status. An operator may instead add one typed annotation to the existing checkpoint
ledger with `litai project test-checkpoint annotate`. The annotation must name the
exact source pin and test key, the observed worker-context identity (or an explicit
universal-authority identity), the original failure fingerprint and evidence node, a
credential-free cause/reason, and either manual revalidation or an expiry.

The built-in Python adapter derives that pin from the exact discovered test-source
bytes as well as the framework authority. Changing a test therefore invalidates every
annotation bound to the old suite pin before the adapter decides whether to execute
it. External runners must supply their own exact source pin when importing events.

A matching repair run does not execute that one test. It emits a synthetic
`known-failure`, continues with later tests, persists the full outcome totals as
`last_report` in the checkpoint, and exits nonzero. It can never create or replace a
passing project-test receipt. Changed source, context, failure fingerprint, expiry, a
malformed record, or a requested revalidation fails closed; the test executes.
Release-evidence runs always start at test one and execute annotated tests.

Use `inspect` to make retained debt visible, `revalidate` to force the next exact test
execution, and `clear` only after remediation or an explicit operator decision. Never
put credentials, private endpoint tokens, or raw logs in the annotation.
A clean run in another context clears reusable pass progress but preserves annotations
for the contexts in which their failures were observed.

## OpenSpec validation tools are missing

Node.js is needed for repository contributor validation, including the locally pinned
OpenSpec/documentation tree. Install it with `make tools-install`, then run
`make openspec-check`. Normal Python import does not require Node.js. Canonical project
users install documentation tooling through npm so Literate AI can verify package ownership and
bypass the package's launcher scripts; standalone shell or `.cmd` wrappers are not an
admitted runtime.

## SSH lifecycle evidence transfer failed

An SSH build/test/run is not accepted merely because the worker lifecycle passed. Errors
such as `execution.remote_evidence_bundle_mismatch`,
`execution.remote_evidence_bundle_size_mismatch`,
`execution.remote_evidence_manifest_size_mismatch`,
`execution.remote_evidence_file_mismatch`, or
`execution.ssh_cleanup_acknowledgement_invalid` mean the coordinator could not prove
custody of the exact returned evidence. The cell fails closed.

`execution.ssh_output_too_large` means the worker violated the fixed one-MiB control
limit. Do not raise that limit or put compressed opaque JSON in stdout. Install a worker
wheel containing the compact-control protocol: the control response references the
manifest and bundle by digest, size, and fixed transfer handle while the existing custody
channel carries large stage evidence and diagnostics.

Do not delete the worker request, pending cleanup ticket, or worker CAS to retry this
condition. The worker intentionally retains attempt custody until the coordinator
imports the digest-bound bundle and acknowledges its exact manifest and bundle
identities. Repeating the exact dispatch is safe: coordinator import and worker cleanup
acknowledgement are content-addressed and idempotent, with bounded transfer and cleanup
retries. Inspect the typed error code rather than raw SSH stderr; transported lifecycle
diagnostics already apply the `literate-ai-secret-private-paths-v1` policy and preserve a
bounded nested cause plus available stdout/stderr.

## Coding-agent login or TLS fails on a remote worker

Skewed host clocks are a frequent cause of token and TLS failures. Each OS Flavor
requires detect-before-install to attempt NTP synchronization to `time.nist.gov` during
host configuration. Inspect `clock_sync` in `host-bootstrap.json`: `ok` means the
attempt succeeded, `failed` or `skipped` means it did not, and neither status fails
sample-worker capability readiness. Retry with noninteractive privilege or a reachable
NTP path; do not treat a failed clock sync as a missing compiler or coding CLI.

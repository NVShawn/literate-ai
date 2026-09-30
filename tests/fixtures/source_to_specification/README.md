# Source-to-specification conformance fixtures

The sample ladder covers a state machine, public library versus internal implementation,
narrow two-revision refresh, contradictory tests, prompt-injection isolation, and
target-neutral base versus OS/GPU/language Flavor drafts.

Run every case from the repository root:

```console
PYTHONPATH=src python3 scripts/run_source_to_specification_fixtures.py
```

Run one case or produce a reusable deterministic result bundle:

```console
litai spec conformance \
  tests/fixtures/source_to_specification/state-machine/case.json
litai spec derive \
  tests/fixtures/source_to_specification/state-machine/case.json > result.json
```

All analyzed files live beneath each case's `source/` directory. Case descriptors,
skills, result bundles, reviews, and acceptance targets remain separate from that source.

The same workflow accepts an arbitrary local checkout. Static derivation is safe to run
without a signature, but that result is quarantined and cannot be accepted. A complete
local bootstrap flow is:

```console
head -c 32 /dev/urandom > local-source.key
litai spec attest path/to/source \
  --signer local-user --machine local-host --key local-source.key > attestation.json
litai spec derive path/to/source \
  --attestation attestation.json --trust-key local-source.key > bundle.json
litai spec review bundle.json \
  --actor local-user --key local-source.key \
  --resolve <each-blocking-uncertainty-id> > review.json
litai spec accept path/to/source bundle.json \
  --review review.json --target path/to/new-spec-root \
  --project-target path/to/new-project \
  --qualification-target host --flavor=+python \
  --trust-key local-source.key
```

With `--project-target`, acceptance writes canonical readable `component.md`, retains
`component.json` only for the compatibility window, and resolves the exact
`component.lock.json` without invoking a coding model or generating source. Omit
`--flavor` only when every recovered Flavor axis has one unambiguous reviewed candidate.
The local `spec qualify` measurement remains non-authorizing v1 evidence unless an
integrated trusted lifecycle adapter supplies complete typed v2 evidence bound to that
exact current lock; there is no JSON evidence-ingestion option.

The key and generated envelopes belong outside analyzed source. This HMAC key is a local
bootstrap trust mechanism; it is not a substitute for a project PKI. Dynamic observation
also requires a separately issued authorization plus `sandbox-exec` on macOS or `bwrap`
on Linux. Missing sandbox tooling is a fail-closed condition, never permission to execute
the analyzed source directly.

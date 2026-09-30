# Check, provision, and admit a retained Cargo library

`litai project retained-cargo check` verifies reviewed importer files, current
provider qualification, the supplied archive and any packages already present.
It reports missing packages without creating them. `materialize` performs the same
verification and provisions complete packages under the declared disposable roots.
An existing package must match exactly; neither command repairs foreign changes.

These commands implement the retained native-library bridge from
[ADR 0040](../decisions/0040-retained-cargo-library-bridge.md). `check` and
`materialize` do not run consumer gates, admit an importer, edit Cargo manifests or
locks, or retire source. `admit` is the separately authorized final operation: it
provisions the exact package, requires reviewed retired roots already to be absent,
runs the complete ordered gates and independently reviewed per-target test inventory
under current custody, and returns a canonical admission receipt.

## Prepare reviewed inputs

The importing maintainer supplies the reviewed retained-library binding, Cargo
workspace plan and ordered gate plan. Use the exact canonical JSON binding bytes:
`--reviewed-binding` is its `sha256:…` content identity and `--binding-size` is its
byte count. Independently supply the gate-plan identity and size through
`--reviewed-gates` and `--gate-size`. A candidate's own claims do not substitute for
this review. The workspace's authored manifests and lock must already match the
reviewed after-state; apply no source retirement to prepare this check.

The gate file uses `literate-ai/retained-library-gate-policy@1`, with
`importer_project_id` matching the consumer, `commands` containing the ordered
typed build commands, and `toolchains` containing unique tool content identities
in identity order. It has no repository source lock: normal consumer source edits
belong to a new execution snapshot and invalidate consumer evidence without
changing this policy or requalifying the provider. Changing commands or tools
requires a new policy review. Policy parsing alone does not prove full test
coverage or authorize consumer execution.

Select the current provider project, Component, qualification profile and target.
The configured installed framework, provider lock, recipes, oracle and native tools
must still match its qualification. `--provider-id` and optional `--model` select
that existing generation authority; neither command generates or requalifies code.
Native tool measurement can invoke installed tools.

Supply the exact qualification archive as a local file with `--archive`, or choose
an explicit HTTPS evidence CAS base URL with `--archive-https`. Both use the
binding's logical artifact source selected by `--store-id` and verify its exact
archive digest, size and media type before package provisioning. Remote bytes
pass through the same qualification verifier as local bytes. Neither path falls
back to a registry, local cache or model regeneration.

For private HTTPS artifacts, `--archive-token-env NAME` reads a bearer token from
that explicitly named environment variable; an absent or invalid token refuses
the operation. Keep credentials out of URLs and command arguments. The transport
uses TLS verification, refuses redirects and ambient proxy/authentication settings,
and issues a read-only GET at `BASE/blobs/sha256/DIGEST_PREFIX/DIGEST`, where the
prefix is the first two digest characters. The server must return the exact pinned
media type and byte count without content or transfer encoding. The per-I/O timeout
is 30 seconds; it is not a total elapsed-time limit.

Use `--archive FILE --offline` for supplied-artifact delivery without network
access. `--offline` with `--archive-https` refuses before provider discovery or
transport. It controls artifact delivery and does not provide OS network containment
for native tool measurement. These commands create no artifact service or account.

## Run the check

```sh
litai project retained-cargo check \
  --project ./consumer \
  --binding retained-binding.json --plan retained-plan.json \
  --gates retained-gates.json \
  --reviewed-binding "$REVIEWED_BINDING_ID" --binding-size "$BINDING_BYTES" \
  --reviewed-gates "$REVIEWED_GATES_ID" --gate-size "$GATE_BYTES" \
  --provider-project ./provider --provider-component components/library \
  --provider-profile components/library/qualification/profile.json \
  --provider-id "$CODING_PROVIDER" --model "$QUALIFIED_MODEL" \
  --target host --archive ./qualified-library.zip --store-id reviewed-store
```

Binding, plan and gate paths are relative to the consumer project. The Component
and profile paths are relative to the provider project. The archive path is relative
to the invoking working directory. Use the reviewed values for the shell variables.

To provision missing packages, replace `check` with `materialize`. Configure
`BUILD_DIR` and `OBJ_DIR` consistently with the reviewed package destinations and
Cargo output directory. They must be distinct, and `BUILD_DIR` cannot be beneath
`OBJ_DIR`. Materialization uses project and package write custody and refuses
conflicting destinations. A repeat invocation reuses exact existing packages.

The result reports the binding, plan and qualification identities, verified present
package count and missing package count. For `check` and `materialize`,
`consumer_gates_executed`, `importer_admission` and `source_retirement` remain false.
A successful provisioning result is not permission to delete the original source or
omit retained tests.

## Admit after reviewed source retirement

Commit a canonical `literate-ai/retained-cargo-test-inventory@1` document covering
every target in the reviewed Cargo graph. Commit a separate canonical
`literate-ai/retained-cargo-source-retirement@1` document naming the importer project,
the exact retained-library binding identity and sorted project-relative source roots
which the boundary transfer retires. The roots must already be absent and must not
overlap authored after-state manifests or materialized package destinations. `admit`
does not delete files.

Set an explicit absolute `CARGO_HOME` populated with the reviewed offline registry or
Git dependency closure. Then run the same arguments as `materialize`, plus:

```sh
litai project retained-cargo admit \
  ... \
  --tests retained-tests.json \
  --reviewed-tests "$REVIEWED_TESTS_ID" --tests-size "$TESTS_BYTES" \
  --retirement retained-retirement.json \
  --reviewed-retirement "$REVIEWED_RETIREMENT_ID" \
  --retirement-size "$RETIREMENT_BYTES" \
  --offline --allow-host-execution --acknowledge-source-retirement
```

The initial public adapter supports reviewed Cargo and GNU Make gate executables.
Every selected executable is measured and must match the identities in the reviewed
gate policy. Admission refuses without both explicit acknowledgements, complete
package and consumer-input custody, exact tool matches, a current positive test
inventory, and absent retired roots. The returned
`literate-ai/retained-cargo-admission-receipt@1` binds the provider qualification,
binding, workspace plan, gate policy, test inventory, source retirement, consumer
inputs and every successful command observation. Retain that JSON as CI evidence;
changing consumer source, tools, policy, tests, package bytes, provider authority or
retirement declaration requires a new receipt.

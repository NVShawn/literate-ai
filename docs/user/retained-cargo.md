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

## Public Python v2 receiving transaction

The CLI commands above retain their v1 wire contracts. Versioned workspace
patches, internal scratch ownership, complete input custody and typed test
protocols use the public Python adapters below. A v2 document must never be cast
to a v1 importer or passed through the v1 CLI to discard those declarations.

The importing maintainer first authors and independently reviews the exact v2
binding, after-manifests and lock, workspace plan, gate policy and complete test
inventory. Resolve the provider through `read_retained_provider_authority` and
`read_retained_provider_native`; their installed distribution, lifecycle policy,
recipes, tools and oracle must still match the actual retained qualification.
A new receiving SDK is not permission to rebind an older provider distribution.
`compose_retained_cargo_current_inputs_v2` joins those live native inputs to the
reviewed files, an independently supplied binding identity and configured store
identifier. It does not generate or requalify source.

Use this operation order:

1. `read_retained_cargo_review_v2` reopens the exact independently reviewed files.
2. Enter `retained_cargo_wrapper_context_v2(reviewed)`, then enter
   `reserve_retained_cargo_scratch_v2(context)`. Both context managers must remain
   live through every later step. The scratch declarations name ignored, absent
   enclosing member-package and Cargo-output roots. Package destinations must be
   absent direct children of their enclosing member root; they are not the root
   itself. Existing output cannot be adopted or reopened by initial provisioning.
3. `read_retained_cargo_files_v2(reviewed, manifest_state="after",
   package_state="absent")` binds actual authored inputs and destination absence.
4. Call `materialize_retained_cargo_archive_v2(files, scratch=scratch,
   read_current=read_current, read_archive=read_archive, ...)`. The current reader
   must reopen the same live importer/provider authority; archive transport must
   honor the supplied bounds and configured store. All graph exports are
   published, including non-library artifacts. Exact package bytes and original
   source custody are checked around publication. On failure only unchanged
   owned publications may be moved back; foreign changes and uncertain private
   stages are preserved for review.
5. Finish authorized dependency provisioning and exact-command cache warming,
   then call `read_retained_cargo_execution_inputs_v2(materialized,
   environment=environment)`. It captures the entire Cargo home, complete
   workspace and declared external roots, plus ancestor Cargo configuration.
   Only the still-live scratch owner supplies output exclusions. Do not renew an
   input snapshot after drift to erase a failed execution.
6. `execute_retained_cargo_consumer_v2` requires independently measured Cargo,
   Rust and gate tool bindings, the exact environment and explicit
   `allow_host_execution=True`. It verifies native patch-aware metadata before
   and after the full ordered gates and every reviewed test target. One finite
   deadline and output budget cover all commands. This is same-user execution,
   not an operating-system sandbox.
7. Durable admission additionally requires
   `read_retained_cargo_retirement_authority_v2` and
   `admit_retained_cargo_consumer_v2`. Retirement must name nonempty independent
   reviewed original roots that were actually retired before the live receiving
   snapshot. The reader never deletes them, permits overlap with current
   manifests/packages/review/scratch, or supplies a no-retirement mode. Admission
   reruns the actual guarded consumer transaction, requires explicit retirement
   acknowledgement and returns the v2 receipt from its real observations.

The typed inventory distinguishes named libtest passes and reviewed ignored
cases from custom-harness command success and permitted exclusions. A custom
protocol binds exact measured executable bytes, fixed input identities and finite
command bounds; it cannot invent libtest counts. Exclusion requires an independent
current permission reader, rechecked through the operation. Returning a candidate's
permission identity without reopening its authority is not a valid implementation
of that reader. An excluded target records no executed command or passing case.

Publication and execution alone do not admit a consumer, retire source, prove an
entire application migrated, or authorize reuse of stale provider authority.

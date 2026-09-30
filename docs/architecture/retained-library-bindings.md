# Retained library bindings

[ADR 0040](../decisions/0040-retained-cargo-library-bridge.md) owns the retained
Cargo bridge design. Its implementation begins with a provider-neutral export
set and bounded explicit delivery. This document separates those integrity checks
from the remaining qualification admission, Cargo integration and source retirement.

`literate-ai/retained-library-export-set@1` binds an existing `ArtifactBuildGraph`,
one exact link-plan identity from that graph, and the existing typed library
product for every graph export whose role is `library`. Every selected link root
must be a library. Products are unique and ordered by their full export identity;
a missing, extra or substituted product refuses. The full export binds Component,
ABI, target, producer, source tree, toolchain, authorization, transitive artifact
dependencies and exact package bytes. The product additionally binds its import
surface and public interface identities.

The graph remains authoritative for missing-dependency, cycle and exact-closure
checks. Explicit non-library dependencies remain in the graph; the export set
does not pretend every native dependency is an importable library. Multi-output
manifests retain their declared outputs. Selecting one link plan does not silently
rewrite those manifests or remove their other outputs.

Parsing is pure structural validation. A caller can describe an incorrect import
surface and obtain a different content identity; that cannot qualify the new
surface. No accepted/authenticated flag is allowed. Admission must separately
reopen current independent qualification and match it to the exact export set,
reviewed importer trust, target/toolchain/features, materialization destinations
and Cargo integration plan. Those admission bindings, transactional materialization and acknowledged boundary
transfer remain open. The bounded delivery adapter below is not consumer admission.
The frozen v1 catalog is unchanged; the export-set record belongs to current v2.

`literate-ai/retained-library-binding@1` records importer intent in the current v2
catalog. It names the importing project, an explicitly configured evidence-store
name, the exact export set, qualification archive and selected run, verifier and
policy identities, and a content-addressed workspace plan. Every graph export,
including non-library dependencies, has exactly one destination in export-identity
order. Destinations must be portable project-relative paths, without case aliases
or parent/child overlap. Store names cannot contain paths or URLs.

The binding has no accepted or trusted flag. Parsing neither reads the archive nor
materializes packages. Admission must resolve the named store through reviewed
importer configuration, reopen current provider evidence, and validate the concrete
workspace plan's manifest/lock changes, target/features and full consumer gates.
Changing consumer source does not change the provider export identity; consumer
qualification remains a separate requirement. Transactional materialization and
boundary transfer are still required before the bridge is usable.

The Rust workspace reference can now contain a canonical
`literate-ai/retained-cargo-workspace-plan@1` document. It records the project-relative
workspace root, full expected native graph, Cargo/rustc identities, named target
triple, explicit feature switches and existing gate commands. Manifest changes pin
before/after blob references; absence is explicit, and unchanged references remain
read preconditions. Prospective inputs must include the workspace manifest, lockfile
and every expected local package manifest. Paths cannot alias, and Cargo output
cannot overlap these inputs or target sources. Source inventory and ownership
remain with the separate boundary-transfer plan.

The graph uses the same typed expectation as the native Cargo verifier, preserving
dependency aliases/kinds, inactive edges, target sources and test flags through
serialization. Plan-derived metadata commands use `--locked`, an explicit target
and feature configuration, optional `--offline`, and a separate `CARGO_TARGET_DIR`.
Reopening checks bounded exact canonical bytes against the binding, then matches
each Rust library's package name and destination to the planned graph and refuses
output overlap with any bound export. It performs no I/O or execution. Later
admission must resolve current tool identities, compare the recorded gates with
current retained authority, verify actual manifest bytes and Cargo resolution, and
keep package custody throughout full consumer qualification.

`verify_retained_cargo_inputs` composes this plan reopening with a read-only
qualified-promotion check. It requires the importing project's independently
reviewed binding identity and configured store name, the current provider lock and
reopened promotion evidence, explicit current generation/verifier/policy values,
current Cargo/rustc identities and the exact existing gate commands. A different
project, binding, store, qualification/run, provider revision, tool or gate command
refuses. The promotion service shares its existing lock/lifecycle consistency
checks with this read-only path and does not append another transition.

Callers must derive current values independently rather than copying fields from
the candidate binding or historical evidence. The preflight returns a plan, not a
consumption receipt: it neither reads the qualification archive nor proves its
export membership. The integration boundary must still reopen the complete archive
against current authority and preserve/recheck filesystem custody across artifact
reads, materialization and consumer gates.

`verify_retained_cargo_archive` now composes the preflight with exact archive and
product reopening. The caller supplies a current-input resolver with a mandatory
custody guard and an explicit named-store reader with a byte limit. Invalid review
or an oversized archive reference refuses before transport. All qualification runs,
the selected export set and package bytes pass the existing composed verifier before
any products return. The operation checks its original custody guard again,
resolves current inputs a second time, repeats preflight, requires equal current
values and checks the original guard once more. A changed policy, recipe set or
filesystem input prevents return. Recipe and command maps are copied into immutable
snapshots so mutation of a caller's map does not rewrite the first observation.

This operation writes no files and emits no persistent admission receipt. The
concrete filesystem resolver still must independently read and guard all importer,
provider, tool and retained-gate inputs; callbacks are trusted adapter boundaries,
not supplied evidence. Transactional publication and native consumer execution must
retain that custody and verify the actual manifest/lock bytes and full gate results.

`read_retained_cargo_files` implements the importer-file part of that resolver. It
uses `PinnedInputClosure` to read the project configuration, an explicitly supplied
reviewed binding reference, the pinned plan and every manifest/lock input. The
caller must select `before` or `after`; a missing reference means the corresponding
path must be absent, including dangling links. Existing path ancestors must be
ordinary directories. Revalidation checks both captured bytes and expected
absences, refusing concurrent file creation without deleting it. Binding/project
mismatches, duplicate binding fields, cross-platform path aliases and read-budget
overruns refuse. The reader creates no files and returns its custody guard for
composition with provider/tool/gate guards.

This file snapshot does not prove that the binding was reviewed: its exact blob
reference must already come from importing-maintainer authority. It also does not
resolve current provider recipes, measure native tools or derive full retained gate
authority. Those remaining filesystem observations must join the importer guard
before exposing the transactional consumer operation.

`read_retained_provider_generation` reopens current provider locks and catalogs
from an independently selected preparation request and logical provider/model.
It projects every node through the same model-selection and recipe code as
Standard, and returns an immutable complete recipe map with the existing input
custody guard. It does not require a coding executable or allocate a generation
workspace, cache or CAS. These are current recipe expectations for reopening;
they do not establish qualification. Promotion, installed lifecycle authority,
native tool observations and retained consumer gates still require composition.

`read_current_qualification_authority` reads the independently selected current
parity profile through a bounded regular-file input guard and derives its verifier
identity and case map from that profile and the current promotion's source snapshot
identity. Qualification execution, evidence recording and archive reopening share
the same derivation, preserving the existing record shape. Profile changes invalidate
custody and change the expected verifier; duplicate JSON fields and unsafe paths
refuse. This lookup needs no original source tree and runs no cases. The caller must
still resolve and guard the current promotion, profile selection and installed policy;
the candidate archive cannot supply those current expectations.

`read_current_qualified_promotion` reopens the provider's current authority head,
content-addressed promotion audits and qualification result from the filesystem.
It checks them against independently supplied current lock, generation closure,
verifier and policy authority, then checks the current head again. Its guard repeats
the reopening and compares the semantic evidence, rejecting invalidation, changed
audited inputs, substituted records and a concurrent head change. Reads append no
authority event and perform no cleanup. This guard must join the separate guards
for current generation, profile and installed policy: passing historical values as
current arguments does not establish their currency.

The current generation snapshot derives `ComponentGenerationClosure` from its
locked workflow/routing references and the verified promotion audit's selected
Flavor and forward-skill subsets. Every selected Flavor must have audited entries;
a nonempty subset for another Flavor cannot hide an omission. Audit files and
generation custody are rechecked around derivation. No historical closure is
required, and no workflow execution or model selection occurs in this operation.
The explicitly supplied qualification identity must still come from the reopened
result, and the derived closure must pass current qualified-promotion admission.

`read_retained_provider_authority` composes those current readers with the actual
project configuration and installed Standard binding observer. It derives the
qualification identity from reopened evidence, the verifier from the selected
current profile, and policy/distribution expectations from the installed authority.
Every recorded run must match the configured driver and measured distribution.
Its combined guard rechecks configuration, generation, profile, installed payload
and promotion, including installed bytes after promotion revalidation. It neither
advances a distribution pin nor creates runtime, workspace or cache state. The
importer still independently selects project, target, profile and logical model;
native commands/tools, library oracle, retained gates and transactional consumption
remain additional admission requirements.

Optional producer capture now retains immutable package bytes and existing lifecycle,
root-package, component-stage, oracle and parity payloads before qualification
scratch is discarded. One synchronized recorder bounds unique records and bytes
across concurrent component producers. Raw SBOM, generated-suite and harness bytes
retain their original identities. npm replay retains its existing process/build
observations, target/source authority and exact dependency-evidence files under
the same recorder bounds. Captures become visible only after every run
succeeds and fits the aggregate budget; failure releases runtime references and
exposes no partial capture. The public qualification CLI enables this capture only when the caller supplies
`--retained-evidence-store PATH` for a library root. It writes the bounded canonical
archive to the existing local immutable evidence store, verifies stored bytes and
reopens the lifecycle result before deleting owned scratch. The selected store must
be outside scratch and the retained source baseline. The successful result and result file retain the exact archive
blob reference, qualification identity and each run's export-set identity. The
limit is 256 MiB, including archive overhead, and 100,000 records; exceeding either
refuses rather than dropping evidence. No endpoint is provisioned or uploaded to.

Publication failure prevents qualification admission. Final current-authority,
source-baseline and promotion checks still run. A later failure may leave an
unreferenced immutable CAS object, which is not removed because another transaction
could reference it. Persisted transport bytes do not authorize consumer admission;
the importing maintainer's binding and current evidence checks remain required.

Before exposing retained captures, the producer rechecks its prepared authority
snapshot, installed Standard distribution/policy and verifier-owned acceptance
oracle. Final qualification admission repeats those checks and reopens the current
promotion evidence before writing qualification records. Mid-run harness, oracle,
distribution or promotion-evidence drift refuses admission. These checks do not
replace the remaining required-stage verification and durable bundle transaction.

A bounded reader verifies canonical unique record membership and every digest,
then reopens exact bytes or canonical JSON. Run-product reopening first binds the
selected run to its lifecycle result,
aggregate receipt, root graph/package, target, policy and project receipt. The root
graph and selected link must match the export set before package reads. Every
clean run must then reconstruct its complete typed lifecycle and derive the exact
source, index, build, SBOM, test, acceptance, cache and workspace memberships using
the producer's qualification validator. Accepted-node custody and generated source
tree/SBOM/test-suite bindings must agree. Reopening requires the exact retained
candidate document and source-custody record for each accepted node. Custody must
name that candidate, generation result, source tree, source BOM, managed graph
and generated suite without extra fields. Rehashed substitutions or missing
records in any run refuse before product bytes. This record binding does not
replace source-file inventory and byte verification. Producer capture opens the
existing candidate CAS read-only and preserves its exact generation manifest,
typed `SourceBundleClosure` root, all referenced source bytes, and final model-stage
record before retaining a product context. It checks manifest/candidate fields,
canonical records, the semantic path/size/digest tree, file integrity and shared
byte/record limits. Missing, changed or oversized input refuses capture.
The generator also retains its existing invocation, execution-plan, stage-request
and final route-decision payloads in CAS under their unchanged identities.
Capture preserves these canonical records with the shared byte/record limits;
missing authority payloads refuse capture. Every retained-product run then binds
the final stage and selected route to its accepted provenance, the invocation's
root/readiness identities, exact stage request and plan, source tree/bundle, and
planned coding-CLI request. The final plan stage must produce the tree and its
route must match the typed retained route decision. Provider-evidence membership
must agree; missing records, extra stage fields and rehashed substitutions refuse.
Each run also reopens its local source-index record, exact security build request,
build intent and historical grant. Component/source identities, classification,
compiler, privileges and declared outputs must agree with the realized build plan;
revoked grants or missing records refuse before package reads. The index's typed
file count includes auxiliary metadata and is not the semantic source-bundle count.
The caller must also supply current typed command contracts for every and only
locked Component. Each run binds its request's builder and sandbox, resolver,
build-system toolchain, compiler, runtime, export shapes and existing build/resolve
action names to those contracts. The exact contract, commands and per-entrypoint
command records must be retained. Missing authority, changed commands or rehashed
foreign tool/export bindings refuse before package reads. These comparisons do
not establish which argv was launched or authenticate measured executable bytes.
Filesystem parity observations decode strict UTF-8 JSON with unique object keys
and finite numbers. They use qualification's existing semantic JSON digest, which
supports finite floats and integers beyond artifact JSON v1's signed 64-bit range.
Baseline/generated results compare by that canonical content identity, preserving JSON types (including boolean versus number and integer
versus floating-point representation), while ignoring whitespace and object-key
order. Malformed results retain their raw output bytes and a false validity flag.
The producer also retains the existing baseline `SourceInventory` document under
its already-checked snapshot identity, with the shared evidence size limit. Parity
reopening restores that record through the existing inventory parser and checks its
identity before accepting observations. The inventory describes paths, file digests,
classifications and exclusions; it does not include original file contents or prove
which original bytes a process executed.
Producer capture rechecks current provider authority, Standard binding and the
independent oracle after all package reads and before exposing retained packages
or evidence. A failed final check clears scratch references and exposes neither.
Product reopening also requires the caller's current typed Standard driver.
Every run's driver, lifecycle-policy and framework-distribution identities must
match that binding; agreement between historical runs is insufficient. The driver
contract reconstructs its own identity from the two pinned authority identities.
The caller remains responsible for independently resolving and guarding the current
binding; this comparison does not observe an installed distribution by itself.
Product reopening requires the caller's current qualification profile and its
minimum clean-run count. It reconstructs the verifier identity and complete case
map, reopens the profile/provider/map/parity/case records, and verifies every
baseline/generated observation's exact command (including unexpanded generated
path placeholders), integer zero exit status and true JSON-validity flag. Raw
stdout/stderr must fit the profile's output bound; the same strict parser and
canonical result comparison recompute each passing parity claim. This verifies
baseline/generated equivalence; independent expected-result acceptance remains a
separate oracle gate. Source-baseline custody, actual launcher/invocation proof
and importer trust still require completion.
Historical record integrity does not grant execution permission: attributable
execution-time validity, current execution policy and importer trust remain required. Current tool/route policy admission and verification of any referenced
provider attestation remain required. These
are the existing generator records, not a new source serialization. Every clean
run reopens that source closure through the same verifier before product reads,
without a filesystem or generation provider. Referenced byte lengths must match;
the source BOM and generated suite must occupy their declared paths with the
candidate's exact identities. Missing manifest/tree/file/stage records, false
sizes and rehashed path substitutions refuse. Full generation-stage authority
verification remains required. The caller supplies the current provider
lock and verifier-owned library oracle. Every clean run must reopen its root
package acceptance, exact harness, cases and observed results against those inputs
before the selected run's typed acceptances and pinned package bytes are returned.
Library packages also require their existing packaged-root generated-test and smoke
execution process records, bound to the exact package plan with integer zero status
and retained text stdout/stderr. Packaged tests must pass every current root suite
case exactly once; duplicate JSON fields, extra/failed/missing cases, foreign plans
and blank smoke output refuse. This verifier applies to library packages with no
product entrypoints. It does not prove invocation custody, native execution or
independent acceptance by itself. Existing
embedded receipt evidence-set commitments are recomputed rather than assuming
every digest names a standalone payload.

Lifecycle reopening reconstructs nodes, component/project plans, schedules, cache
memberships, context evidence and root integration through the existing producer
validators. Every reconstructed identity must match its pinned record. Repeated
references share one decoded object within the operation, bounding repeated
expansion of malicious duplicate references; lifecycle validators still reject
inconsistent membership. Run reopening also checks the existing execution-process
records against each exact build plan, execution phase, successful integer exit
status and retained text stdout/stderr. Multiple-entrypoint execution requires
every unit record and the exact aggregate observations, runtime set and output
maps. Missing records, rehashed substitutions or extra fields refuse before
product bytes. Build reopening also requires the exact build wrapper and command
process, or npm target/dependency/install/inventory/syntax-check records. Retained
npm manifest and lock bytes use the same bounded graph parser as the filesystem
loader; inventory and dependency membership must match that graph and build plan.
The filesystem loader still owns package-root discovery. Complete source/export
custody and command authorization remain separate checks.
Capture retains the existing artifact tree identity documents before and after
the build metadata manifest is written, plus that manifest's exact bytes. The
build observation commits to the former tree; artifact custody commits to the
latter. These distinct identities must not be compared as if they described the
same file set. Every reopened build requires the final tree to add exactly that
manifest to the observed tree, with unique valid file entries, matching resolved
SBOM bytes identity, and exact custody plan/export membership. The raw manifest
must retain its producer serialization and name the same tree and observation;
duplicate JSON keys and rehashed file substitutions refuse. Missing tree or
manifest records in any clean run block package bytes. Retention remains bounded
by the shared evidence budget. Complete custody verification still needs source
files, export path/byte relationships, and provider-material authority checks.
Generated-test reopening requires every original case record and attributed
observation, one matching successful process per suite/entrypoint, and exact
case results in retained text stdout. Missing/duplicate/failed cases, duplicate
JSON keys, foreign phases/plans/entrypoints, non-text outputs and extra fields
refuse. Multiple-entrypoint aggregates must reconstruct from the exact unit
records and observations. Product reopening requires a caller-projected current
recipe for every and only locked Component. Every clean run validates its suite
and exact case membership against that recipe, and both source candidate and
provenance must name the same recipe. A missing suite in a later run blocks all
package-byte reads. Producer capture reprojects each recipe through the
runtime's configured node-preparation adapter against guarded current locked
authority. It reuses generated-suite validation for recipe identity, permitted
specification references, invocation coverage and result shape, then checks every
retained case definition and exact per-entrypoint membership. A refusal prevents
retention of that run's product context. Source/artifact custody, command
authorization, root-package and parity process checks remain required before
complete importer admission.

Product reopening also projects each Component's managed CycloneDX graph from the
current lock, requires its retained graph record, and revalidates both raw source
and resolved BOMs against that graph. Resolved validation uses the exact retained
source bytes to check the lifecycle transition. Recomputed bindings must equal
the typed build evidence; missing documents, substituted Component identities or
misstated counts refuse before package bytes. This verifies dependency evidence;
source/artifact file-tree custody and execution authorization remain separate.

Independent library-acceptance reopening takes the current provider lock and
verifier-owned oracle supplied by the trusted caller. It requires their exact
Component/interface/import bindings and retained oracle, harness, case and expected
result records. The recorded observations must cover every exact case once and
bind the same root artifact, package plan/result and root test/execution evidence.
The native JavaScript fixture reopens an actual Node oracle's observations after
its package directory is removed, without executing the package again. These are
content and relationship checks; authenticating the provider and importing trust
remain separate obligations. Run-product reopening now composes the typed
lifecycle and independent library-oracle checks. The complete required-record
verifier must still add remaining stage checks and current provider authority
before durable publication or consumption.

The archive adapter can encode retained immutable file records using the existing
canonical ZIP format. It budgets headers, names and content before allocation and
reuses the reader's path and format checks. Encoding performs no filesystem writes
or execution. Closed evidence publication, importer admission, transactional
materialization and the complete Cargo/source-retirement flow remain open.

The directory-archive adapter also provides `read_directory_export`, a pure reader
for the existing canonical ZIP bytes. The caller supplies the pinned `BlobRef` and
positive byte/entry limits; transport must enforce the byte limit before allocating
the input. The reader checks the digest and size, walks the central directory before
allocating ZIP entries, and admits only sorted regular files with ordinary permission
bits and portable, noncolliding paths. It rejects compression, ZIP64, extra fields,
comments, links, special modes, malformed records and noncanonical header bytes.
The returned immutable file records preserve exact bytes and file modes. Reading
performs no extraction or execution and does not admit qualification evidence.

The producer’s evidence output includes the exact captured package bytes under
their content identities, sharing its existing record/byte limits. Failed capture
or final authority checks expose neither evidence nor products.
The qualification archive adapter transports immutable evidence and package bytes
as read-only `records/<sha256>` members in that existing ZIP format. Reopening
requires an explicit archive blob reference and byte/record bounds, checks every
member name, mode and content digest, and returns the existing evidence reader.
The caller still pins the qualification, run and export identities and supplies
current authority to the composed verifier; the archive grants no admission.

Persistence uses the existing `FileSystemEvidenceStore`: atomic immutable CAS
publication followed by verified readback. The storage round-trip fixture discards
the writer and encoded archive, reopens through a fresh read-only store, removes
the transport store, and verifies both runs from retained bytes and current
authority. This proves storage composition, not a qualified publication command
or the importing maintainer’s trust decision.

The bounded reader does not replace the existing qualified-local-tree transport
verifier or authorize transactional materialization. Admission must enforce the
reviewed importer binding and current evidence before making packages consumable.

The internal `verify_cargo_workspace_graph` adapter compares an observed Cargo
format-version-1 document with an independently supplied `CargoWorkspaceExpectation`.
It requires every local package at its exact workspace-relative manifest root,
name and version; the complete local dependency declarations and resolved edges;
workspace and default membership; resolved feature sets; and every Cargo target,
including test/doctest flags, source path, edition and required features. Explicit
output-directory agreement keeps the observed Cargo output separate from nested
package roots; an unreviewed separate build-directory override refuses. Inactive
optional declarations remain part of the check. External packages cannot substitute
for expected local packages, and a dependency from an external package back to a
local package refuses because this expectation does not describe external consumers.
Ordinary external dependency qualification remains with the reviewed Cargo.lock
and provisioned dependency policy.

Package IDs are opaque, and additive metadata fields remain compatible with
[Cargo's metadata contract](https://doc.rust-lang.org/cargo/commands/cargo-metadata.html).
A filtered report can retain a conditional dependency kind when another declaration
resolves the same package. The expectation therefore states whether each edge is
present in the observed resolution; that field is not a claim that its target
predicate executed. Invocation, toolchain, target and feature custody still belong
to the importing integration plan.

The graph verifier performs no I/O and has no public apply/CLI operation. Its
immutable expectation is shared with the workspace-plan contract; parsing it does
not qualify the graph or prove that the caller reviewed it. The trusted importer must still reopen current
provider evidence, bind the manifests and lockfile, run the exact locked Cargo
command, maintain materialized-package custody through build/test, and preserve
full workspace acceptance before source retirement. The native fixture exercises
metadata only: a fresh Cargo home, offline resolution, normal/dev/build aliases,
a conditional edge, an inactive optional edge, a transitive local library and
workspace globs. Exact file inventories before and after metadata are unchanged.

`adapters/retained_bundle_delivery.py::RetainedBundleDelivery` supplies explicit file
and HTTPS retrieval with caller-selected offline policy and finite byte/entry limits.
A local input must be an exact-sized regular file with safe ancestors. POSIX reads
walk directory descriptors without following links; all reads compare the opened
file and final path metadata and verify the pinned bytes before decoding the archive.
A changed input is refused; no cleanup touches a foreign replacement.

HTTPS reuses `HttpsEvidenceStore`'s fixed-origin CAS protocol: the configured base URL
is followed by `/blobs/sha256/<first-two-digest-characters>/<digest>`. This first
transport supports those explicit CAS endpoints, not arbitrary redirecting download
pages. TLS hostname/certificate verification, exact media/size headers and bounded
reads are mandatory. Bearer authentication is explicit local/CI configuration;
there is no ambient authentication or proxy discovery. Redirects, encoding changes,
wrong bytes and unavailable objects fail without fallback. Offline mode refuses
HTTPS before constructing a transport. Errors contain no URL, path, token or content.
The inherited timeout is per network I/O, not a whole-operation elapsed-time bound.

Both methods return the existing immutable `DirectoryExportFile` records only after
canonical archive validation. They do not extract files, publish artifacts, locate
an ambient cache, invoke tools/models, or admit provider qualification.

The public `project retained-cargo check|materialize|admit` commands accept an explicit
local qualification archive or HTTPS CAS base URL. Qualification archives use
`HttpsEvidenceStore` to read the binding's exact `BlobRef`, then reopen their
qualification closure through the current-provider archive verifier. This differs
from decoding a standalone directory export with `RetainedBundleDelivery`; a
directory export alone cannot replace the qualification archive. Local archives
stay under pinned file custody. HTTPS credentials are read only from an explicitly
selected environment variable. Offline HTTPS selection fails before provider
discovery and transport. Checks do not provision packages; materialization verifies
and exclusively publishes or reuses exact package trees. Admission additionally
requires independently reviewed test-inventory and source-retirement documents,
explicit host-execution and retirement acknowledgements, and an absent retired root.
It runs the ordered consumer gates plus attributable tests under the same current
package, source, external-input and measured-tool custody, then emits a canonical
receipt binding every command observation. It never performs source deletion.

Reviewed `RetainedLibraryGatePolicy` records bind the importing project, ordered
commands and measured tool identities. They do not bind a repository source lock.
Each execution separately captures mutable consumer inputs, so source edits
invalidate consumer receipts without changing provider qualification or policy.
Policy review does not by itself prove complete gate coverage.

Gate policy can declare `external_input_variables`: sorted, unique environment names
whose execution-time values select complete external input directories. The field
is omitted when empty, preserving existing policy identities. Host-specific paths
remain outside committed policy. Input capture with that policy requires present,
safe directories disjoint from the project, Cargo home and one another, and applies
the existing entry and byte limits to every captured file. Execution compares the
captured policy identity and rejects tool/command overlays selecting another root.
External source edits invalidate the consumer capture without requalifying the
provider. Declaration is the maintainer's input-coverage review, not automatic
inference of every path an arbitrary script might read.

Internal native execution requires `RetainedCargoExecutionInputs`, captured from
local consumer files, verified package trees and an explicit `CARGO_HOME`. The
capture includes provisioned registry/Git files and both Cargo configuration names
in the Cargo home and external ancestors, including guarded absences. The execution
environment must match the capture, and gate/tool environment overlays cannot select
another Cargo home. Metadata before and after gates must name captured manifests
and target sources. A caller-supplied identity callback is no longer accepted.

Cargo hard-links Git pack files between its database and checkout metadata. That
cache tree permits a hard-link group only when every alias is captured beneath the
same Git root. Link counts, physical identity and bytes are rechecked; an external
alias, replacement or mutation invalidates custody. Consumer source, registry files
and materialized packages retain their stricter no-hard-link rules. Cargo's mutable
home-level usage/lock metadata is outside the source trees and is not source evidence.
For the root `git/CACHEDIR.TAG` and `registry/CACHEDIR.TAG` backup markers, capture
retains exact bytes, device, inode, mode, size and link count while allowing timestamp
rewrites. [Older Cargo writes these markers on access](https://github.com/rust-lang/cargo/blob/0.76.0/crates/cargo-util/src/paths.rs#L675).
Changed bytes, replacements, additions and removals still invalidate custody;
markers inside package source trees receive no timestamp exception.
Arbitrary extra paths selected by configuration or build scripts still require
reviewed authority; this capture does not by itself complete consumer admission.

The internal Cargo test-observation adapter matches the JSON artifact stream from
`cargo test --workspace --all-targets --no-run --message-format=json` to every
selected reviewed target, accounting for required features and excluding build
scripts. It requires the complete target set, successful compilation, distinct
executables under the selected output root and matching package/target fields.
The metadata `test` flag is not a selection filter for explicit `--all-targets`;
see the [Cargo test target-selection rules](https://doc.rust-lang.org/cargo/commands/cargo-test.html#target-selection).

For each guarded libtest binary, the checker compares terse discovery with reviewed
case names and requires each name exactly once in the passing pretty-format result.
Ignored, filtered, missing, duplicated or unrecognized results refuse. Identical
case names in different binaries remain separate observations. Empty targets are
represented explicitly; a receipt still requires positive tests overall. These
parsers do not run or authenticate binaries, finalize receipts, or establish custom
harness semantics. The runner must bind measured compilation, current input and
binary custody, original gates and retained logs before admission.

`RetainedCargoTestInventory` is the versioned importing-maintainer expectation.
It binds the importer project, exact workspace-plan identity and gate-policy identity
to sorted, unique target keys and case names. Its target set must equal the shared
workspace/all-targets selection. Empty targets are explicit, while total expected
cases must be positive and bounded. The artifact matcher and inventory checker
share target selection and case-name validation.

`read_retained_cargo_test_authority` requires an independently reviewed inventory
blob reference, pins its exact bytes and size, and compares all three authority
bindings before returning a guarded reader. A changed case list requires new review;
binary discovery cannot silently rewrite expectations. Missing/foreign targets,
wrong project/plan/policy identities, duplicate or malformed fields, path aliases and
symlinked files refuse. This reader does not execute tests or finalize a receipt.

Internal consumer execution can take the matching `RetainedCargoTestAuthority`.
After every original reviewed gate, it compiles the full workspace target set with
locked dependencies and the reviewed target/features into a fresh owned directory.
It retains that directory on success and failure. Before any test process, every
selected binary is captured under bounded byte custody with physical file and
parent-directory observations. Each list/run boundary rechecks the entire binary
set, inventory, measured tools and consumer inputs. Per-command observations bind
the executable authority and retain stdout/stderr, including failed results.

This path executes each binary in its package directory with `CARGO_MANIFEST_DIR`.
It queries the measured compiler for the reviewed target's runtime library directory
and captures those files before compilation. Before test execution it also captures
the generated base/dependency directories and in-output build-script link-search
directories, then prepends these paths to the platform's loader environment variable.
Every immediate file, directory entry and physical replacement is rechecked around
test processes; the executable authority binds the runtime capture identity.
Compiler runtime capture explicitly permits file symlinks only in its selected
compiler directory, including when captured alongside generated directories. This
includes distro links to libraries outside the reported directory. It binds each link's text and physical
identity, all traversed ordinary parent directories, the resolved regular file and
its bytes. Cycles, more than 64 links, directory aliases, dangling targets and
interior parent traversal refuse; leading relative parents support distro layouts.
Alias and parent records share the 10,000-entry budget, and target bytes retain the
128 MiB per-file and 512 MiB aggregate limits. Re-observation rejects retargeting,
same-byte file replacement and parent replacement. Generated runtime directories
continue to reject symlinks. This capture is not independent compiler admission.
The native fixture passes all seven standard-harness targets with dynamic Rust
runtime linkage and rejects runtime-file drift.

The checked compilation stream also supplies package-specific `rustc-env` values
for direct test processes, matching Cargo's
[documented runtime projection](https://doc.rust-lang.org/cargo/reference/build-scripts.html#rustc-env).
The projection binds to executable authority and stays within the emitting package;
it preserves empty values and values containing `=`. Unknown package IDs, malformed
entries, duplicate keys and conflicting host/target build outputs refuse. Bounds are
4,096 script records, 256 variables per record, 128 characters per name, 65,536 per
value and 1 MiB of aggregate UTF-8 name/value bytes. A script cannot redirect the
package's `CARGO_MANIFEST_DIR`; existing consumer guards still reject changes to
reviewed external roots, Cargo home, compiler and output selection. Loader paths
are composed after the package overlay. The native seven-target fixture includes a
runtime variable lookup checked against its compile-time value.

Every `build-script-executed` record must name a known package and an `out_dir`
strictly beneath the fresh compilation root. Before tests run, the runner captures
every nested file and directory in these output trees, including empty directories,
and the physical parent chain. Each executable authority binds the output capture;
each process boundary rechecks its bytes, entries and parent identity. Missing or
overlapping output trees, symlinks, hardlinks and file replacement refuse. The
capture allows at most 128 output roots, 10,000 total entries (including captured
parents), 16 MiB per file and 256 MiB overall. Limits are shared across all output
trees. The native fixture reads nested generated data through `env!("OUT_DIR")`;
changing that data after discovery stops execution.

These are bounded captures: at most 128 search directories, 10,000 entries, 128 MiB
per file and 512 MiB overall. Nested directories need their own selected search path.
Cargo's [documented loader-path behavior](https://doc.rust-lang.org/cargo/reference/environment-variables.html#dynamic-library-paths)
also includes caller paths and platform defaults. Complete authority for those paths,
system/shared-cache loaders, other paths named by build-script environment values
and arbitrary external build-script inputs remains required
before public admission. The internal
observations do not finalize a durable consumer receipt or provide containment.

The existing `PortableHostDependencyObserver` accepts explicit native library roots
and forwards them to ELF, PE and Mach-O observation. Mach-O `@rpath` and bare runtime
names can resolve through these roots; multiple valid images still refuse as
ambiguous. This allows the Cargo runtime fixture's generated library and Rust
standard library to join the same recursively observed graph as system/shared-cache
images. The portable observer can also take `windows_environment`; PE observation
copies and normalizes its case-insensitive keys, then resolves system/API-set files
and PATH directories from those supplied values. Missing explicit SystemRoot,
ambiguous keys, relative paths and empty search-path elements refuse. Omitting the
argument preserves observation-time host lookup for existing callers. This does not
change how independently measured inspector tools are selected.

Native observers also accept an explicit `artifact_files` selection (1–4,096
unique absolute paths). Every selected file must remain a regular native-format
file beneath the artifact root with a safe parent path. Missing files, links,
directory selections and format mismatches refuse. Omitting the selection preserves
whole-tree discovery. This lets the consumer supply its reviewed test binaries at
their original paths without copying them or treating unrelated Cargo object files
as runtime seeds. The caller still owns selection completeness and process-time
custody; explicit selection is not a complete-directory SBOM claim.

The 608-component macOS diagnostic graph is locally verified. Composition with
process-time custody and complete execution-environment resolution on every host is
still required before it becomes consumer admission evidence.
The same native diagnostic now observes the original Cargo binary in place and
retains 608 components and 7,162 edges, including that exact binary path, its
generated provider library and the Rust standard library. Its graph and byte
identities were verified independently after observation.

`capture_retained_native_files` binds the materialized portion of that observation
to current files. It snapshots the graph bytes, requires each recognized native or
inspector path to match its recorded SHA-256, and captures the physical file and
parent identities. Materialized Mach-O records also require the observed loader
path, resolved file and complete symbolic-link chain to agree. Rechecks reject
byte drift, same-byte file replacement, parent replacement and link replacement.
Duplicate component references to one file share its captured byte budget. Limits
are 10,000 components/physical nodes, 128 MiB per file, 256 MiB of file bytes and
16 MiB of observation JSON.

The native probe captures 81,483,752 bytes across nine distinct files, 42 physical
path nodes and nine loader paths. Its 597 unmaterialized components remain
explicit in the result. This is file custody for a supplied observation; it does
not authenticate a supplied graph or qualify shared-cache/system-image authority.
Consumer integration must bind current native observation, these guards and the
remaining host authority around execution before issuing admission evidence.

`observe_retained_native_dependencies` composes native observation and file custody.
It freezes the artifact files, library roots, Windows observation environment, host
platform and graph root reference. It re-observes the entire native graph after
capture and at each requested execution boundary, with materialized-file checks
before and after inspection. A changed system-image record or dependency edge
refuses even when every captured file still matches. Inspector discovery and native
graph construction run again; a stored graph is not used as a substitute for that
current observation. This deliberately incurs native-inspection work at each check.

The internal Cargo test runner now creates a native guard for each exact test
binary at its original path, using the captured generated/compiler library roots.
On Windows it supplies that target's composed process environment to PE
observation; on macOS it projects the same composed environment through the explicit
library/framework path controls. Each list/run process checks the binary/input guards, re-observes the
native graph and checks file custody again; a graph change stops execution before
accepting test results. The process executable authority includes the native guard
identity. This does not yet make the generated/compiler roots a complete model of
all caller loader overrides or arbitrary dynamic imports, and it does not issue a
durable importer admission receipt.

The native Rust diagnostic passes both dynamic sum/overflow cases with graph checks
around list/run and exact libtest verification. Consumer execution must still bind
the complete reviewed selection, actual loader environment, input/tool guards and
durable results to this primitive. It does not independently establish arbitrary
dynamic imports or a production containment boundary.

Explicit macOS loader-path observation uses `MacOsLoaderPaths` for library and
framework override/fallback directories. It preserves ordering, keeps framework
version suffixes, and rejects relative/noncanonical roots, implicit current-directory
entries, more than 128 total directories and unmodeled `DYLD_*` controls. Application
seed context propagates through transitive imports; inspector/toolchain traversal
retains its sanitized context even when it shares image bytes with the application.
Override candidates precede the original lookup, and fallback candidates follow a
missing original. The retained native guard freezes this projection for every
re-observation; callers that omit it retain the previous selection identity.
This models the four path controls, not all dyld policy: embedded load-command
settings, inserted/versioned images, process security and other platform-specific
loader behavior remain separate qualification requirements. The live fixtures
exercise an absolute library override and a versioned framework override.

ELF imports containing a slash select that path directly, without loader-cache or
explicit-library-root fallback. `$ORIGIN` and `${ORIGIN}` expand against the
importing image's directory. Relative path imports and other dynamic tokens refuse
because their execution context is not yet modeled. This corrects direct import
selection. A `RUNPATH` tag suppresses `RPATH`, including when its value is empty;
nonempty path strings containing empty directory entries refuse. Traversal retains
`RUNPATH` presence separately from its effective directories. An image without
`RUNPATH` searches its own `RPATH` before inherited ancestor paths; an image with
`RUNPATH` suppresses inherited paths for its direct imports. Only `RPATH` passes
to children, with `$ORIGIN` expanded at the declaring image. Each executable seed
keeps its own first-visit traversal context, even when application and inspector
share image bytes; interpreter dependencies start without application inheritance.
Explicit `LinuxLoaderPaths` applies `LD_LIBRARY_PATH` after inherited `RPATH` and
before direct `RUNPATH`. It accepts at most 128 canonical absolute POSIX paths,
preserves colon/semicolon-delimited order, ignores a wholly empty variable and
refuses empty directory entries, dynamic tokens, other `LD_*` controls and
`GLIBC_TUNABLES`. Runtime context follows application imports; build/inspector
contexts remain sanitized even when the same seed has both roles. With an explicit
projection, arbitrary supplied library roots are not appended as fallback loader
directories. Omitting the projection preserves legacy observation behavior.
The retained native guard freezes and reconstructs this projection on every check.
Retained-Cargo test observation supplies its composed Linux process environment,
including measured compiler library directories and package overlays. Loaded-object
aliases and symbol interposition, secure
execution, cache/hardware-capability fidelity and native Linux qualification remain
required before claiming complete consumer loader authority.

The read-only and provisioning commands do not execute consumer gates or issue
importer-admission receipts. `admit` composes complete source/external-dependency
custody, attributable positive consumer results, durable admission and acknowledged
source retirement as required by ADR 0040. Endpoint provisioning belongs to the
operator.

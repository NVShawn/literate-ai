# ADR 0048: Retained projects use explicit Standard lifecycle admission without source-authority transfer

- Status: Accepted
- Date: 2026-10-02
- Decision owners: literate-ai maintainers and directing operator
- Issue: https://github.com/jordanhubbard/literate-ai/issues/22
- Roadmap: [RETAINED-PROJECT-001](../roadmap/active-work.md#retained-project-001-admit-retained-projects-through-explicit-standard-lifecycle-authority)

## Context

An adopted project can retain its implementation as source authority while adopting
new build and packaging providers. Today `rebuild` rejects any conversion stage
other than `qualified` before even a retained-source review plan is prepared.
The existing retained-source adapter captures a small UTF-8-only tree and qualifies
it through generated-source custody. It cannot represent a complete retained project
with binary assets, large inputs, locked SDKs, native producers, documentation and
an existing test inventory. Raising its limits or marking conversion qualified would
misrepresent the authority being exercised.

This proposal serves framework Goals 1, 4 and 6. It changes lifecycle admission,
not the authority of retained implementation or the existing conversion criteria.
The ZIP provider in issue #17 remains an independent packaging capability.

## Decision

Add an explicitly selected retained-project execution mode to the Standard lifecycle.
It must have a distinct typed admission record and receipt origin. Existing generated
and small retained-source modes retain their current behavior and fail-closed gates.
No adopted project opts in implicitly, and execution never advances conversion stage.

A reviewed manifest declares the complete input closure, including source, binary
assets, build/test/documentation definitions, dependency locks, SDK/toolchain identities,
platform filters, and the applicable test and package inventories. Every member binds
its canonical relative path, byte length, executable mode, digest and ownership role.
Declared entry/per-file/aggregate byte limits are positive, enforced, and bound to the
plan; capture streams large files into content-addressed storage rather than loading
the project into one text dictionary. Missing, extra, changed, colliding or redirected
inputs fail admission. Links and special files are rejected until a separate explicit
contract qualifies their custody. Mutable caches and credentials are never inputs.
External dependencies are admitted by exact identities and manifests; linking an
ambient checkout or finding an installed SDK does not qualify it.

Read-only planning validates project/Component/Flavor/lock authority and the existing
conversion record without executing build tools or acquiring unreviewed dependencies.
The resulting plan names its exact input, target, worker/toolchain, command-profile,
output, resource-limit and acceptance identities. Execution requires acknowledgment
of that identity plus the existing host/worker authorization. Any changed plan input
requires a new review; the ordinary regenerative release route continues to reject
unqualified conversion authority.

Standard execution materializes an isolated immutable input snapshot and runs the
locked build profile and complete declared test/documentation inventory. Staging,
generation, native compilation and dependency acquisition are explicit graph inputs
and actions; root command names are not evidence of stage coverage. Existing Standard
validation, artifact/SBOM custody, containment, acceptance, cache invalidation and
receipt verification remain mandatory. Failures, missing suites, unauthorized skips,
missing artifacts or changed post-execution inputs prevent acceptance. Retained-project
receipts must bind original test identity/skip policy and packaged-payload provenance
when the project's contract requires them. A baseline exception is not a test pass.

Packaging consumes only the accepted closure through the selected existing package
provider. A separately authorized release can publish that retained-origin artifact
only after its current receipt and original release-authority policy pass. Such a
receipt never asserts regenerative source authority or permits a conversion-stage
transition. Ordinary receipts and retained-project receipts cannot substitute for one
another. Legacy repository commands may adapt argv/exit status to these public verbs;
they may not invoke an independent build/test/publication acceptance path.

## Implementation and acceptance

1. Define typed manifest, plan and retained-origin receipt contracts, bounded streaming
   capture, stale-input rejection and explicit authority-mode validation.
2. Expose read-only planning and integrate Standard execution/verification using locked
   command profiles and compatible workers. Keep runtime acquisition and cache custody
   in existing framework owners; do not duplicate shell pipelines in CLI handlers.
3. Bind package and release verification to the distinct retained-origin evidence;
   reject attempts to use it as regenerative conversion proof.
4. Add a provider-neutral mixed-source/binary-asset fixture whose build, complete tests,
   docs and deterministic package pass through the public installed CLI. Verify exact
   worker/target identity, no-op/repeat behavior, dependency invalidation and relocation.
5. Adversarially reject stale or extra source/assets, mutated manifests, missing/failed
   tests or docs, substituted artifacts, wrong workers/targets, unsafe paths, exceeded
   limits, incomplete dependency closure and unauthorized publication or authority transfer.
   Existing generated and small retained-source rejection tests must still pass.

## Consequences

Adoption can qualify a different build/package provider while remaining honestly
original-source authoritative. The framework gains explicit retained-project custody
rather than weakening conversion gates or treating archived baseline logs as a current
receipt. Projects must author complete manifests and acceptance inventories, and existing
worker observations remain provisional until admitted by this route.

The directing human explicitly accepted this ADR on 2026-10-02: “I accept the ADR.”
Acceptance permits implementation and its tests under ADR 0006. It does not itself
authorize a release, claim qualification, or merge a PR. Implementation and full
qualification remain open under RETAINED-PROJECT-001.

## Portable retained executable intent

Input discovery accepts a sorted, unique tuple of reviewed executable paths. On
POSIX, every declaration must also have a native execute bit; an explicit name
cannot promote inert bytes. Windows has no corresponding POSIX mode bit, so the
declaration supplies the portable `0755` intent that is sealed into the retained
manifest. Later Windows verification reuses only that sealed selection while still
rechecking the complete path, byte, size and role closure. Missing paths, aliases,
duplicates and symlinks refuse. Changing executable intent therefore changes the
manifest identity and cannot silently widen an accepted plan.

Produced executable intent is not inferred from a Windows filesystem either. The
withheld acceptance policy is the independent reviewed owner of executable output
paths; output capture projects only cases that fall under the action's declared
output roots. The sealed output manifest then carries that intent through verifier
execution, admission and package projection. An absent declared executable fails
capture instead of being silently packaged as inert data.

## Captured native runtime search paths

Profiles may opt into an ordered `runtime_library_paths` tuple of at most 32
canonical input-relative directory names. Omission preserves existing wire
identities and the credential-free default environment. Each selected directory
must exist directly in the captured manifest, contain selected files, and contain
only SDK, toolchain or dependency members. Missing, aliased, redirected, source,
test or output directories refuse. A profile cannot supply arbitrary environment
variables or import the caller's loader state.

On Linux and FreeBSD the execution owner projects these directories to
`LD_LIBRARY_PATH`; on macOS it uses `DYLD_LIBRARY_PATH`. Other hosts refuse this
opt-in until their loader selection is explicitly implemented. The projected
paths are rooted in the immutable captured snapshot and retain declared search
order. The same environment reaches commands, dependency observers, SDK scopes
and independent acceptance. Existing input and native dependency guards bracket
execution, and the profile/plan and environment identities bind the selection.
The macOS/Linux observer explicitly selects the runtime search context for
captured build tools; its own host inspectors keep their independent host
context. The existing FreeBSD observer already projects selected paths for
its native tool closure.

This permits captured native tools to select captured shared libraries without
inheriting ambient compiler/loader injection. It does not prove a complete
compiler closure, suppress system loader fallback, admit SDK products, establish
tool source-build provenance, or qualify public retained execution. Compiler
resource directories, sysroots, startup/link inputs and relocated execution
remain separate explicit requirements.

## Canonical per-Component input ownership

An optional retained manifest ownership map binds normalized input prefixes to
exact authored Component coordinates and identities from the actual project
catalog. The most specific declared prefix owns each retained file and directory;
there must be one owner for every input, with no duplicate/aliased or unused
prefixes. Unknown or stale Component references fail planning. The ownership map
is part of retained manifest identity; changing a rule invalidates the reviewed
plan. Original file roles, byte/mode custody and selected target locks remain
independent. Legacy manifests omit this opt-in field and retain their identities.

This is input ownership, not a second Component catalog, native dependency
inference, target/action binding, pkg production or independent acceptance. A
complete native Component model still requires these later gates and shared
consumer relationships. The public execution refusal stays in force until the
retained lifecycle is qualified. Full-tree owners must also retain bounded
validation cost rather than comparing every file with every directory.


## Explicit internal source symlinks

Retained members may explicitly bind POSIX symlink target text, normalized Git
link mode 120000 and the byte size/SHA-256 of that text. Links are input nodes,
never an instruction to traverse more of the host filesystem during inventory.
Every destination must exist in the declared input closure. Reject absolute or
escaping paths, missing destinations, cycles, and intermediate link/non-directory
traversal; final link chains may terminate at an admitted regular file or directory.
A directory link does not duplicate or recursively enumerate its contents.

Filesystem discovery requires explicit `allow_internal_symlinks=True`; its default
refusal is unchanged. POSIX capture stores target text in immutable content blobs
and recreates links after copying regular files. Original and snapshot trees are
revalidated, including target text, kind and mutation during reads. Native Windows
link materialization is unsupported and refuses explicitly. Absent optional member
fields preserve legacy wire identities. Ownership rules cover links as ordinary
input nodes, and a changed link target invalidates the manifest even when the old
and new destination files have identical contents.

This input custody extension does not admit native actions, executable tool
aliases, per-unit packages or retained lifecycle acceptance. The public execution
refusal and independent source/SDK/variant/receipt gates remain unchanged.


## Explicit independent acceptance of retained exports

Profiles may opt into `acceptance_policy_identity`, the exact SHA-256 of
`verification/retained-project-acceptance.json`. Omitted fields preserve legacy
profile and plan identities. An explicit null or malformed identity refuses.
The verifier file must be outside every retained manifest root and is never
materialized into the command input snapshot. Planning and execution reopen its
bounded exact bytes; duplicate JSON keys, redirection and drift refuse.

The typed policy binds the complete selected Component-lock tuple and exactly
the case inventory of every acceptance action. Cases name regular executable
exports produced by build actions and selected by the package inventory. Each
case specifies bounded argv and binary stdin, exact exit/stdout/stderr expectations
and a deadline. Execute it only after complete declared tests and documentation,
with the existing bounded process owner and explicit credential-free environment.
Revalidate original inputs, tools, policy and sealed exports at command boundaries;
observe exported native dependencies before and after execution and reject drift.
Retain bounded actual results, including mismatched cases, with their content
identities; failed-case observations cannot become successful lifecycle evidence.

These observations are independent executable-case evidence, not retained
lifecycle receipts, complete source/resolved BOM admission, qualified SDK products
or packaged-artifact acceptance. They never advance conversion or release
authority. The public retained execution refusal remains while these later
consumers are integrated and qualified. Legacy profiles produce no new independent
acceptance observation merely by labeling a zero-exit command as acceptance.


## Retained source and resolved BOM custody

An explicit managed retained graph may bind command execution to pre-build source
and post-build resolved CycloneDX documents. Reuse the existing strict CycloneDX
construction and transition validators. Preserve the retained project root and
selected Component graph, reconstructed from the actual ordered lock tuple;
reject a substituted graph even when it keeps the retained root. Never replace the retained project with a Component
revision or generated-source candidate.

The source document binds the complete input-manifest identity and every declared
SDK/toolchain/dependency file. Tool modes and digests and the complete role
closures must match the reviewed plan. The manifest represents the full binary
and source input inventory without duplicating every source file as a BOM node.
Runtime materialization still verifies those input bytes through its existing
streaming owner.

After the complete declared build, capture the sealed build-export manifest and
merge frozen tool/native-export observations through the existing native graph
owner. Produce and validate the resolved document against the exact source BOM
before running tests or documentation. Explicitly data-only exports may have no
native images; an empty tool observation remains invalid. Retain immutable BOM
bytes, native observation bytes and their exact identities. Reject substituted
BOMs, graph/source mismatches, conflicting or dangling native graphs and forged
managed claims. Later test/documentation exports do not rewrite the frozen
build-stage observation.

This internal observation gate does not admit SDK producers, qualify final
packaged payloads, issue retained lifecycle receipts or enable public retained
execution. Those consumers must retain their independent current-authority and
qualification gates. Legacy command profiles remain unchanged when the managed
graph is omitted; no new BOM acceptance is implied on that path.


## Whole-run evidence join

The live retained workspace may join observations only after every declared action
has completed successfully, both source/resolved BOMs exist and the explicit
independent verifier has run. Bind the complete ordered command transcript, original
test policy and reports, verifier policy and actual case results, the build-stage
BOM closure and sealed final exports. Require exact plan and observation identities;
reject missing/foreign stages, failed cases, missing selected package members or
changed previously sealed outputs. Canonical producer roots remain distinct from
ancestor directories included while inspecting the complete export tree.

Reopen current plan, original and snapshot inputs, tools, policies and exports at
this boundary, and reobserve final native export dependencies against the last
independent case. Return an immutable joined record; mutating its rendered mapping
cannot rewrite its identities. A standalone record checks internal relationships;
it cannot authenticate facts transported from a different process or worker.

This join is input to later SDK, receipt and package admission, not a substitute
for them. It issues no lifecycle receipt, permits no conversion-stage transition
and leaves the public retained execution guard in force.


## Producer-backed retained SDK inputs

An opt-in manifest member with role `sdk-policy` binds canonically ordered exact
Component-lock/SDK-consumer-input pairs to disjoint relative placement prefixes.
Planning reads its bounded, duplicate-key-free bytes without acquisition. Runtime
requires existing live `NativeSdkConsumerInputs` owners for the complete ordered
lock tuple; a caller-supplied SDK snapshot or ambient library is insufficient.
The policy must cover exactly the selected producer products. Every SDK-role
member must match its product file size, digest and executable mode, and each
placement must have the complete file/directory closure without extra files,
links or empty directories. Prefix lookup walks indexed ancestors.

Materialize and inspect through the existing owner, then bind the same verified
product/runtime observations to the identical retained snapshot files. Reuse the
existing native SDK execution manifest and command scope for every retained
command and independent verifier case. Its actual request binds the selected
consumer, retained manifest, exact command, environment, tool and plan; the
caller supplies the existing explicit authorization and evidence-retention ports.
The adapter never acquires/builds a producer or creates its own grants. Check live
producer revocations before and after execution, and revalidate the most recent
verifier grants at whole-run completion without repeating their commands.

Bind SDK source admission, repository resolution, license, target and native
dependency evidence into the resolved BOM using existing SDK merge/CycloneDX
owners. Command/case records and the whole-run record bind SDK execution and final
authority-check identities. Omitted optional fields retain legacy wire identities.
BOM-bound execution with SDK members or selected external repository sources
requires producer bindings; observation-only legacy execution does not gain an
admission or receipt. This internal join reuses producer admission but does not
itself qualify a retained receipt, final package or public execution route.


## Local retained receipts and package-input custody

Only a complete current live retained workspace may mint the distinct
`retained-project-receipt` contract. It binds the exact plan/run, input and final
export manifests, selected payload manifest, source/resolved BOMs and original
verifier bytes. Retain canonical command/test/verifier records and actual binary
streams, with SDK execution and fresh final authority-check records when selected.
Use existing streamed CAS storage and revalidate the live run after retention;
partial work, missing SDK evidence or changed bytes cannot mint admission.

The receipt preserves original source authority, exact passing/authorized-skip
counts and a `local-runtime` verification scope. It is not an ordinary generated
project receipt, advances no conversion stage and grants no publication. Parsing
its JSON never reconstructs the in-process local admission capability. Remote
receipt authentication and native package/public CLI consumption remain separate
qualification requirements.

Package consumers may reopen original plan/source/verifier authority and every
retained blob, then materialize only the declared sealed payload using bounded
CAS copies. Preserve modes and directory structure, including selected parent
directories. Reuse exclusive directory publication so an existing/concurrent
destination is not replaced. Verify complete payload inventory and bytes before
and after publication. Scratch may be discarded and CAS relocated while original
project authority stays current; stale source/policy/plan or corrupt metadata/data
blocks consumption. This materialization is package input, not a native package,
installation or release.

A complete admitted manifest may include explicit internal source links. Select
its regular native images for existing observation APIs without following aliases;
retain exact link text and target validation. An explicit empty native-image
selection describes data-only inputs. Default unselected tree observation still
rejects symlinks.

## Retained package provider integration

An admitted retained run may project its sealed package inventory into a distinct
retained package plan/result. Bind the original retained plan and receipt, the
exact target, packager implementation and a packaging Flavor selected by the root
node of one actual selected Component lock. A retained project is never encoded
as a generated Component revision or artifact graph. A parsed package record is
not admission, execution authority or a release grant.

For ZIP, reuse the existing provider's canonical construction and independent
member/metadata/hash verification. Include exact selected authored specifications,
source/resolved BOMs, receipt and input/output manifests under a reserved evidence
namespace alongside the accepted payload. Reject namespace collisions, missing
specification bytes, mismatched lock/provider/target, stale original authority and
changed retained blobs. Revalidate admission before and after construction and
verification. Preserve both ordinary package contracts and their wire identities.

This archive integration does not supply native pkg construction, installation,
remote receipt authentication, release approval or whole-system memory/scale
qualification. Those remain explicit prerequisites for their corresponding
public lifecycle operations.

## Retained FreeBSD package projection

The local retained package owner may select the existing native pkg provider only
when the actual selected Component lock names the exact pkg Flavor revision.
Preserve ZIP plan/result wire identities. Native plans retain the distinct
retained origin and bind the exact accepted payload, specifications, receipt and
all run records, with explicitly typed source and resolved BOM inputs. Bind the
native package metadata, full installed-path projection, ABI and static tool
identity through the existing native provider; never substitute an ordinary
generated Component revision for retained origin.

Construction and reinspection require live local retained admission and unchanged
native tooling. Retain constructed bytes in the same CAS and recheck current
source/plan and input blobs before and after packaging. Parsed receipt or package
JSON grants no runtime admission. Reuse the native archive inspector, including
independent ELF/checksum/metadata verification, rather than adding another parser.
This backend integration does not install or publish, authenticate remote
receipts, enable public retained execution, infer a complete per-unit package
dependency graph, or qualify source/ports components. Those require their existing
separate contracts and actual native/installed qualification.

## Independent filesystem effects

An opt-in retained acceptance case may declare bounded private input files and
exact expected regular-file, directory or absent states. Bind fixture bytes,
file lengths/hashes, optional permission expectations and explicit entry/per-file/
aggregate limits to verifier policy. Cases without this field preserve their
existing identities and execution behavior.

Execute the sealed product in a fresh verifier-owned case directory under the
existing object workspace. Bind an explicit case-root argument substitution and
private HOME/temporary paths only for opted-in cases. Keep the existing SDK
command scope, process-tree/output/deadline limits and source/export/dependency
revalidation. Never let case effects modify accepted exports or reuse a prior
case directory. This is observation and cooperative custody, not a hostile-code
filesystem sandbox or a new privilege grant.

Independently enumerate and hash bounded actual states without following links.
Reject aliases, redirects, hardlinks, special nodes, cross-device traversal, mutation,
unexpected output and resource overruns. A zero exit with wrong size/content is
a failed case. Retain immutable actual filesystem observations with ordinary
process results, including mismatches, and recheck their policy binding during
whole-run completion and receipt retention. Existing independent authority,
original test coverage, source/SDK admission and release gates remain mandatory.

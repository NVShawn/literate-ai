# Original-source native SDK consumption

- **Status:** active
- **Current stage:** integration candidate; exact-head hosted and installed qualification pending
- **Owning queue item:** [NATIVE-SDK-001](active-work.md#native-sdk-001-admit-vendor-native-sdks-into-standard-generated-consumers)
- **Completion / archival evidence:** pending while NATIVE-SDK-001 remains open

A generated Component must be able to consume a vendor's native SDK without moving
source authority out of the vendor repository. A verified vendor build is necessary
but insufficient: Standard must bind the public SDK imports and native runtime bytes
through generation, build, tests, packaging, independent acceptance and worker replay.
This direction differs from the retained Cargo importer in issue #380.

## Current boundary and reusable mechanisms

`RepositorySourceDependency` and `RepositorySourceLock` already distinguish an
authored revision selector from exact source bytes. `LockedComponentRevision` and
`ResolvedComponentNodeInput` already carry repository source locks. The filesystem
planner now resolves authored source dependencies using exact Git captures. Inputs
whose source cannot be captured still fail closed. Source locking does not issue
build admission.

Generated Python requirements and project dependencies currently raise
`dependencies.python-lock-unsupported`. Removing that check, or adding an SDK name
to a generated BOM, does not acquire or admit a native SDK. The SDK path must supply
real source and artifact custody, public import metadata and complete dependency
evidence before those imports can become available.

Reuse the existing host dependency observers for native images. In particular,
`LinuxElfDependencyObserver` binds inspector tools, native image bytes, architecture,
loader imports and recursive dependency edges without executing the inspected image.
Reuse the equivalent platform observers when their target is selected. Preserve
their rejection of missing imports, changed inspectors and unsafe paths.

Standard already binds Component provider exports at build intent, packaging and
execution. External SDK products must join those lifecycle boundaries explicitly;
they must not impersonate generated Component products or inherit an unrelated
Component's acceptance identity. Keep ambient `PYTHONPATH` excluded.

## Required authority flow

1. The Component declares its repository source dependency and public integration
   contract. Its selected Flavors own target, compiler and SDK options. Resolve exact
   original source and recipe identities without giving vendor internals to the
   consumer's generation prompt.
2. Prepare a target-specific SDK build plan over that exact source, selected tools,
   declared outputs and the applicable license decision. Execute original-source
   commands only under the existing authorized build and worker mechanisms.
3. Capture the actual SDK product: public Python package or other language import
   surface, native libraries, declared runtime resources and dependency closure.
   Preserve original vendor implementation as dependency content, distinct from
   generated consumer source. Source trees containing build-time symlinks require
   explicit regular-file materialization and identity verification before export.
4. Verify the product against its plan, target/ABI requirements and exact source
   authority. Bind the resulting admission to the consumer's locked SDK dependency.
   A caller-supplied artifact hash or successful vendor test log alone cannot issue
   that admission.
5. Supply only the admitted public imports, usage contract and binding metadata to
   generation. Project SDK package/import records into dependency evidence from this
   authority, rather than accepting model-invented package records.
6. Materialize the same verified SDK closure for generated tests, independent
   acceptance and packaged execution. Package and worker custody must retain both
   the product and its admission; a successful build-directory import cannot stand
   in for a relocated package or fresh-worker run.

Source locks remain target-neutral. SDK products and admissions are target-specific.
Private checkout roots and credentials remain materializer state; portable authority
uses exact identities and relative artifact paths. Generic registry dependency
resolution is separate work and must not be made a prerequisite for a source-first SDK.

## Implementation order and proof

The byte-custody stage is implemented by `NativeSdkSnapshot` and the native SDK
capture/materialization adapter. Its real C ABI fixture transfers files between
independent content-addressed stores, removes producer storage and the build tree,
then executes from a fresh verified relocation. SDK, storage and schema regressions
pass 79 tests on macOS. This is not SDK admission: the input identities are claims,
and no public Standard consumer path is enabled by this stage.

Continue through the existing `RepositorySourceResolver`, which already orders
exact acquisition/capture, index binding, build planning, authorization, verified
outputs and cache admission. Its concrete SDK builder must retain the actual
snapshot as a declared build output and verify source and native runtime closure;
constructing `RepositorySourceAdmission` from caller-provided hashes is insufficient.

The concrete `NativeSdkRepositoryBuilder` now executes exact repository build plans
through pinned local tool bindings and the existing build-authorization verifier.
Its default verifier refuses execution; acknowledged ambient-host grants retain the
same privilege checks as other native builders. Source capture and authorization
are rechecked across commands. Layout/import/license metadata participates in the
plan, the license must be part of the captured vendor source and the exported SDK,
and verified output snapshots and command logs enter content-addressed storage.
The real fixture now traverses `RepositorySourceResolver`, compiles its C ABI, deletes
the vendor checkout, and executes the relocated public SDK. Its repository/index/
approval fixtures are explicit test authorities, not deployed Standard admission.
Seven builder tests plus 16 existing repository-source tests pass on macOS.

Build output now also retains the existing platform observer's native-loader graph.
Every declared SDK library must occur exactly once with its captured hash, and its
recursive native dependencies must match selected OS/architecture. Changed runtime
files and disconnected or unresolved required graphs refuse. Missing delayed
imports in SDK-owned runtime images also refuse: an incomplete vendor product
cannot become complete by treating its own files as host prerequisites.

Windows external runtime images can contain conditional feature dependencies.
Microsoft's [delay-load contract](https://learn.microsoft.com/en-us/cpp/build/reference/delayload-delay-load-import?view=msvc-170)
loads such a dependency on its first function call; its
[failure hooks](https://learn.microsoft.com/en-us/cpp/build/reference/error-handling-and-notification?view=msvc-170)
can handle unavailable libraries. Preserve an unavailable external import only
when its importing native image has verified bytes and its inspected import table
identifies that exact name as delayed, with no required import of the same name.
An unavailable virtual API-set host likewise requires only verified external
importers with matching delay-only import records. SDK-owned importers, missing or
contradictory inspection metadata, unverified image hashes and missing load-time
imports remain errors. The retained graph keeps every unavailable-import marker
and API-set availability fact, so changes alter dependency and command authority.
These observations describe a conditional host environment; they do not establish
API availability or make the package standalone. Current command and independent
API acceptance must still prove the selected SDK operations before qualification.

The real two-library fixture survives relocation; removing its linked helper from
a newly captured package fails native observation before consumer loading. SDK build,
runtime, transfer and existing dependency regressions pass 88 tests on macOS, including
synthetic Linux/macOS/Windows rejection cases. The broader SDK/storage/schema/source
and dependency run plus version-authority checks passes 187 tests on macOS.
At `438fb43e`, a real Linux x86-64 execution-worker source run also passes all 24 SDK
builder/runtime/custody/relocation tests. Its exact source archive was verified before
execution, and the returned manifest and logs were imported with bundle identity
`sha256:4dc55b631f59fabf872befb5438b39dcc79fd7c42997a8581498147997a8917a`.
This is source qualification, not an installed-wheel or Standard application proof.
Live Windows qualification remains pending.

Next expose exact SDK lock resolution and the admitted product at Standard consumer
boundaries. OS/architecture and loader observations do not prove language ABI/API
compatibility, consumer acceptance or permission to execute the SDK. Those remaining
requirements stay distinct and must be exercised with the public integration contract
and independent native acceptance harness.

First add a portable original-source fixture: a compiled C ABI, a public Python SDK
package, a declared recipe and a generated consumer with finite numeric acceptance.
Use it to drive the complete admission path rather than stopping at contract round
trips. The fixture must retain vendor source authority and export actual native bytes.

Resolve exact source and SDK plans through the existing lock services; connect their
verified products to Standard dependency and artifact boundaries. Then exercise
package relocation and fresh-worker replay with the ambient SDK unavailable.

At each boundary, test changed source, recipe, toolchain, import metadata and artifact
bytes; missing runtime dependencies; incompatible target/ABI; and replay of a stale
admission. Failure must precede the consumer's first native load. Verify that private
vendor source and independent expected results do not enter the generation prompt.

The public filesystem composition root now builds and runs a specification-led real
consumer through generation, build, generated tests, packaging, packaged execution and
independent acceptance. Independent acceptance rematerializes the exact admitted SDK in
a fresh authorized scope and records that execution identity; the original vendor source
is deleted before consumer generation. This is local integration evidence. Completion
still requires the exact candidate to reproduce through hosted worker custody and
installed qualification. No permissive import exception or ambient host setup counts as
that result.

## Exact source lock boundary

The filesystem planner resolves each authored repository selector with Git in an
isolated temporary checkout. It reads and hashes original tracked bytes without
compiling, invoking a coding provider or executing repository hooks or filters.
Mutable selectors become exact commits; unresolved LFS, submodules, unsafe paths
and dirty captures remain unsupported. Public integration-contract bytes join the
lock input closure. Source locking grants no native execution or SDK admission.

When admitting a previously locked dependency, reacquire its exact commit while
preserving the authored selector in authority, and compare the complete captured
source lock before indexing, planning or executing. A moved branch must not retarget
an existing application plan. An unverifiable or changed lock fails before build.

This source-lock stage now passes 106 combined source, lock, public CLI, generation
and SDK tests with one skip on macOS. Real Git fixtures exercise moved branches,
reacquisition of the original locked commit, rejection of changed lock identities
before indexing/building, configuration isolation, missing revisions and unresolved
LFS. The public lock command writes the exact source and integration contract, while
execution planning and generation explicitly refuse unadmitted SDK dependencies.

At source revision `1fbc918b`, a real Linux x86-64 execution worker passes all 32
SDK and Git source-lock tests. Source and returned manifest/log custody are verified
with bundle `sha256:b4d1a1de6e612e3f61e98f1097f6db484ac337c8e25a031454137e08d06f3167`.
This qualifies framework source on that worker, not an installed wheel or generated
Standard consumer. The current lifecycle still requires SDK admission integration.

## Authored SDK recipe selection

SDK recipes belong to selected Flavor builder contributions, using a distinct
`native-sdk-build-recipe` content kind and one exact recipe per dependency. Their
portable authority binds the dependency ID, OS/architecture, declared SDK layout,
license and public import surface, named tools, and fixed original-source commands.
The locked source dependency's integration contract must match every imported public
capability. Recipes do not carry private host executable paths or grant execution.

Plan derivation binds the actual source lock, recipe, selected target, captured
source index and resolved tool identities. It runs no coding provider: the retained
repository-plan decision field records deterministic authored-recipe selection.
Changed or ambiguous recipe contributions, public contracts and target selections
fail before build authorization. Standard SDK admission and consumer imports remain
closed until that plan produces and verifies the required native artifact.

The typed layout and recipe are now v2 contracts with strict schema/catalog and
compatibility metadata. The selected-Flavor resolver reads current pinned content
and rejects ambiguous recipes, missing dependencies and mismatched public imports
or selected targets. The normal project-validation parser validates recipe content
and the exact dependency builder slot. The deterministic planner retains recipe,
layout and selection records, checks current source/target/tool bindings and emits
the existing repository build plan without a coding-provider call.

The real C/Python fixture now reaches that planner through public source locking
and actual Git acquisition. Its authored recipe compiles the SDK; after source
deletion, isolated Python calls the relocated native implementation. Index, approval
and cache authorities remain explicit fixtures in this proof. Standard admission,
generated consumer imports, ABI/API acceptance and worker application execution
remain required; an authored recipe alone enables none of those stages.

Recipe/SDK/source/schema coverage and version-authority tests pass 95 focused tests
on macOS. On the real Linux worker, 32 SDK/source-lock tests pass at `b5828e12`;
recipe qualification exposed a fixture's assumption about the default Git branch.
After selecting the fixture commit explicitly, all six recipe tests pass at
`376769d0`, with verified manifest/log bundle
`sha256:7bcbbf99039946eb8ea56a056a11d61efb2866a3d70643776335d32d719630d3`.
The repair changes fixture selection, not production code. These are focused source
runs, not fresh full-suite, installed-wheel or generated Standard consumer evidence.

The installed wheel passes at `7895c05b`, retaining wheel
`sha256:8a7fb66da2625c0582959aca77d6d778f8c4b8151bdd138f0b0c379b523171dd`.
Hosted Python 3.11 qualification then exposes an older SBOM fixture that constructs
a source-dependent node without its source lock before replacing that field. Repair
the fixture to supply the complete dependency lock at construction; preserve the
new validation that rejects incomplete production inputs. Resume the failed lock
and SBOM coverage and require the corrected revision's hosted gates.

Consumer relocation exposed a separate currentness defect: every unchanged-authority
check re-resolved repository selectors, requiring upstream availability after an SDK
had already been captured. The captured generation snapshot now replays its admitted
exact source locks while rechecking local catalog, file, lock and audit authority.
Replay rejects incomplete or conflicting locks without fetching; explicit new lock
and reader operations still resolve source and detect moved branches. Repository,
locked-authority and filesystem-planner coverage passes 38 tests with one skip,
including origin deletion, moved-branch fresh reads and changed local contracts.

## Production source-build composition

`NativeSdkSourceBuildService` replaces fixture index/approval/cache callbacks with captured-file inventory and
deterministic source scanning, the existing security policy/authorizer and live
revocation verifier, and the existing quarantine repository-source cache. The
source inspector verifies every file against the actual Git capture and locked
source, retains its inventory and scan report, and rechecks project/recipe custody.
This file index is not a semantic graph or a proof that arbitrary native code is safe.
Selected source-intelligence policy remains explicit; required unavailable providers
continue to refuse before a build.

The local origin observation attests only that this coordinator checked the exact
source against current locked authority. It is not a vendor signature, authorship
claim or portable trust receipt. Policy grants bind that observation, scanned source,
recipe plan and consumer identity, require the existing ambient-host acknowledgement,
and re-read revocation before each command. Imported metadata alone cannot create
those grants. Successful products still require Standard import binding, native
ABI/API acceptance, packaging and execution authority at their later boundaries.

The service retains the exact index binding, inventory, scan, local origin
observation, classification and issued grant in the CAS. Source and authority bytes
are rechecked at each build authorization boundary. The builder returns a product
only for a complete verification produced by that builder instance; changing other
verification fields while retaining a result digest does not select a product.
Source-build services are single-use because acquired checkout custody ends when
resolution finishes. These local objects do not authenticate transported records.

Seven focused local cases now cover this production composition, using a real
CMake-built SDK and substituting only local Git transport. The relocated SDK still
calls its native implementation after both acquired and origin source deletion.
Unacknowledged host execution, revocation or source changes after configure, invalid
live revocation state, required unavailable intelligence and a changed authored
plan all refuse before compilation or cache admission as applicable. A forged
verification cannot retrieve the real builder's product. Initial assertion errors
were repaired against the existing wrapped-error and quarantine APIs; passing
focused cases span those runs and are not a full-suite or remote qualification.

The surrounding SDK/recipe/repository/security regressions pass 32 tests, followed
by 12 version-authority tests, complete lint/format checks, current driver review
and project validation. The exact source at `4d77e8bc` passes all seven source-build
cases on the configured Linux x86-64 Literate execution worker in 74.271 seconds.
The coordinator verifies and imports the returned manifest/log bundle
`sha256:385cd0bfb730f70dfe747351ce6532fe89fa0851161b95abc2fe20a71004f92d`.
This qualifies the framework source composition on that worker; it is not an
installed-wheel, generated consumer or application fanout qualification.

## Consumer input composition

Bind completed source-build services to the same current locked consumer/target
before exposing SDK input directories. Retain the full source admission, product,
public import surface and runtime observation in a deterministic consumer input
manifest; reject missing, extra, duplicated or conflicting SDK selections. Bindings
are build inputs, not portable authentication or consumer execution grants.

Materialize only the recorded SDK bytes into fresh consumer-owned directories,
re-observe native runtime dependencies at their new locations and keep public import
paths outside generated source. Recheck SDK and authority bytes before use. Standard
must carry these input identities through build authorization, resolved dependency
evidence, generated tests, independent acceptance and packaging. Keep planning and
generation guards until that integration is complete; ordinary plan inspection
must never acquire permission to execute a source build as a side effect.

`NativeSdkConsumerInputs` now requires completed producer services, exact recipe and
consumer/target coverage, and non-conflicting public package names. It retains input
manifests and materializes the producer's verified CAS bytes into fresh temporary
directories, re-observing the native loader closure at the consumer location. It
checks authority and SDK custody again and removes only its own directories.
Five focused real-build cases pass: relocated native execution after origin deletion,
incomplete/unbuilt/self-asserted inputs, duplicate/foreign target inputs, changed SDK
bytes, consumer exceptions and corrupted CAS. The positive case exercises several
binding refusals; four tests pass together and the added CAS test passes separately.
These adapters are not yet connected to Standard build/import/BOM/package execution.

## Standard build authority for SDK inputs

The Standard build intent must retain canonical identities of the exact completed
SDK input manifests. Its security build request must bind the locked command
builder and those identities together. The materialization plan must retain the
same identities, so the composite request and its grant change when an SDK input
changes. SDK inputs are separate from generated Component artifact dependencies.
Empty SDK input sets retain the existing wire representation and identities.

Local ports receive live completed producer services through the verified consumer
input adapter. They revalidate producer and local authority before creating an
intent, authorizing it and finalizing a plan. A caller-supplied identity list cannot
stand in for that custody, and a grant for another input set must refuse. Until
SDK materialization, dependency evidence, test and package integration is complete,
build execution with SDK-bearing plans remains explicitly unavailable.

## SDK dependency projection

A materialized SDK must supply resolver-owned dependency evidence derived from its
completed source admission and its fresh native-loader observation. The resolved
BOM records a distinct SDK package, its consumer edge, its original repository-source
edge and the complete observed runtime graph. Repository resolution uses the existing
typed source-resolution contract. Each SDK node binds its exact input, snapshot,
recipe, target, license and public import surface. These records do not grant
execution or establish language ABI/API acceptance.

Rebase SDK-owned native image references and path properties onto their exact
snapshot-relative paths so relocated SDKs preserve the same dependency graph and do
not publish temporary materialization roots. Preserve external native and inspector
observations rather than omitting those dependencies. Reject mismatched observation
identities, incomplete library bindings and unresolved or disconnected runtime edges.
The projection must pass the existing source-to-resolved CycloneDX continuity checks
with the complete managed Component/repository graph intact.

Standard's BOM writer collects these projections for the consumer's complete managed
Component closure. A shared repository inventory node uses the canonical proof of
identical source bytes; each SDK package retains its own source lock, admission and
full resolution identity. Conflicting source bytes or component facts refuse rather
than replacing an observation. Transitive SDK evidence does not expose transitive
imports to generated consumer code.

## SDK package resources

Standard package plans must include every admitted SDK in the root Component's
managed dependency closure as resource inputs, distinct from generated Component
artifact exports. Retain exact SDK files and executable modes, the typed snapshot,
public input binding and a package-relative index associating each consumer with
its SDK imports. Preserve the source admission and dependency-evidence identities.
Package construction never fetches or rebuilds an SDK and never grants execution.

Use the existing PackageInput/PackageResult resource mechanism. An optional explicit
executable bit on PackageInput preserves native SDK file modes; false remains omitted
from the existing wire form. Reject unknown blobs, target/lock mismatch, conflicting
paths, changed live producer custody and corrupted files. Verify fresh package SDK
trees against their original snapshots before retaining package custody. A relocated
package must retain usable native bytes after producer source and CAS are unavailable;
current runtime authorization and independent consumer acceptance remain separate.

Package requirements distinguish the supplied snapshot file set from the native
loader environment, whose producer observation is only an exact prerequisite
reference. That environment remains externally supplied and requires fresh host
validation before execution; packaging never makes the bundle standalone.

## SDK runtime import boundary

Each Standard command consuming an SDK must receive an explicit locked command role
for a digest-addressed import manifest, scoped to that direct consumer. The manifest
retains relative materialized roots and exact file hashes/modes. Python starts in
isolated mode, verifies the manifest and SDK files before importing the application,
and retains native DLL search handles when required. No ambient PYTHONPATH supplies
SDK imports; persistent services retain the import driver in their owned process.

A new command request binds the exact argv, environment, tool, consumer, target, SDK
inputs and freshly observed dependency graph. A source-build grant cannot authorize
it. Check live revocation, expiry, manifest/files and the runtime graph immediately
before and after the command, retaining the exact execution evidence. Preparation
alone grants no execution. Standard integration must retain existing no-SDK behavior
and keep public generation closed until generated/private and packaged execution
acceptance is complete.

Runtime revocation checks use completed SDK custody and the current host revocation
set, not the source-build verifier's temporary captured checkout. Standard removes
ambient Python/native-loader overrides before request binding to match the native
inspector environment; explicit bound-tool overrides remain rejected. Current
qualification readers still reject SDK-extended process records. Add verification
of those execution/manifest/dependency records, then finish generated and packaged
acceptance before opening public SDK builds or generation.

## Retained SDK execution verification

Retain versioned command-execution records that bind the locked command contract,
phase command, argument/environment/cwd identities, SDK manifest and runtime graph
to the exact request. SDK input manifests must be retrievable under the identities
already bound into the build plan; each snapshot and dependency projection must
agree with its consumer and target. Retain before/after revocation states and check
times so readers can validate the historical grant interval and scope.

Qualification must reject omitted SDK evidence for SDK-bearing plans, foreign input
sets/snapshots/targets, changed phase/tool/request bindings, expired or revoked
historical grants and malformed dependency graphs. Preserve existing no-SDK records.
This proves retained-record consistency and membership; it never authenticates a
transported producer or grants permission on the importing host. Packaged execution
must separately bind the package and reobserve the launch host.

The reader now reopens these records, including the repository build plan that
links authored recipe selection to the SDK snapshot. SDK snapshots bind resolved
build-plan identities; authored recipe identities are separate evidence members.
The real macOS SDK TEST process supplies the retained positive record. Reopening
it passes missing-record, changed scope/manifest/native-image and historical grant
mutation checks. Each mutation uses independent bounded storage, with fixture
construction outside the expected rejection so capacity failures cannot mask bugs.
The 80 local lifecycle and 62 capture/project-service regressions pass; a further
41-case local command run proves the newly required retained plan cannot be omitted.
The complete updated TEST/EXECUTE fixture and live grant refusal suite still need
exact-source worker qualification. Public SDK generation and build remain closed.

## Packaged SDK launch

Prepare direct SDK imports from the verified package resource table after relocation,
without reading the producer source or CAS. Observe the launch host's native graph
before authorization and recheck it around the command. Bind each command grant to
the exact package plan, result and immutable tree as well as the SDK/runtime scope.
Keep current revocation providers as explicit live host authority; retained package
metadata is not a transferable grant or importer admission.

Integrate this preparation into Standard packaged TEST/EXECUTE commands, retain its
package-bound execution evidence, and refuse package/input/target/runtime drift.
Qualify real native imports after producer deletion and package relocation. Persistent
service acceptance, managed-provider SDK import closure, fresh-worker admission and
generated application acceptance must also pass before opening public SDK builds.

The local Standard package launch now prepares direct SDK inputs from its retained
resource table into owned temporary storage, observes the current host graph and
uses a package-bound command grant with live revocation checks. The initial real
macOS fixture passes packaged TEST and EXECUTE after relocation and deletion of
vendor source, producer CAS, consumer source and original artifact directories.
The package reader additionally verifies project-plan membership, resource byte
and mode bindings, package plan/result identities and retained tree membership.
Its independently rehashed negative cases and pre-launch corrupt-package refusal
still need final exact-source qualification. Surrounding lifecycle/capture coverage
passes 118 tests with two skips. This local package path does not admit transferred
worker packages or complete persistent-service or managed-provider SDK acceptance.

## SDK service acceptance scope

Keep packaged SDK imports and their fresh command grant alive for the complete
service acceptance lifetime: startup, readiness, verifier requests and process-tree
shutdown. Bind final argv/environment before launch, preserve the existing bounded
process/output controls, and perform the after-command SDK/package/revocation checks
after the service is stopped. SDK acceptance records must link the retained command
evidence; failure and revocation must not leave a server or temporary SDK directory.
Use the same scope for persistent HTTP services, served browser/IPC acceptance and
one-shot packaged commands. Qualify actual native calls in a verifier-probed service
after producer deletion, plus failed-request teardown and live revocation refusal.

Teardown must retire the owned process group or job even when the root exits
gracefully or before shutdown begins. A surviving descendant must not retain a
server or SDK file after the acceptance scope closes. Cover both root-exit cases
with a real child process that ignores graceful termination.

The native service fixture also exposes #405's finite-product-JSON gap in HTTP
acceptance: service oracle identities and response comparisons still use the
authority JSON encoder, which rejects finite floating-point results. Use the existing
product JSON encoder for those product values, retaining old integer/null identities
and rejecting non-finite values before treating a response as accepted.

The service scope is implemented for persistent HTTP, served browser and IPC
acceptance. The initial native HTTP fixture passes on macOS in 411.082 seconds;
the final fixture adds SDK-presence checks during shutdown and non-finite oracle
refusal before any grant. That final native run remains pending. The updated
138-test lifecycle, browser, IPC, finite-JSON and process-cleanup regression run
passes with nine platform/tool skips. The POSIX cleanup test covers descendants
that ignore graceful termination after both live-root and already-exited-root
shutdown. Browser/IPC regression coverage does not prove a real native browser
application. Managed-provider imports, library-harness SDK acceptance and fresh
worker admission remain open before public generated SDK builds can be enabled.

## SDK library acceptance scope

Independent Python library acceptance must receive the same verified direct SDK
package closure as packaged tests and execution. Preserve the verifier-owned
harness, exact oracle and public import surface; do not treat that harness as the
application's locked TEST or EXECUTE command. Bind a distinct acceptance command
to the oracle, harness, pinned runtime driver and declared acceptance toolchain.
Revalidate package, SDK files, harness bytes and live authorization around execution,
then require the same bindings when reopening retained library acceptance evidence.
Retain ordinary non-SDK acceptance records unchanged. Refuse unavailable language
support and ambient loader overrides instead of launching with partial imports.
Qualify an actual native library call after producer deletion and package relocation,
plus failed results, changed harness/SDK bytes and revoked grants. Managed-provider
SDK closure and transferred-worker admission remain separate required work.

The library fixture exposed a retained-intent defect: enabling evidence recording
unconditionally projected executable entrypoint contracts for a library. Retain
library commands directly and only project entrypoints for executable products;
cover the same path with the existing non-SDK library lifecycle test.

The implementation uses a distinct `library-acceptance` command binding over the
verifier oracle, harness, SDK runtime driver and acceptance toolchain. SDK-extended
library acceptance records are versioned and their reader recomputes that binding
from the caller's current oracle. The current 190-test lifecycle, capture, Python/
JavaScript library, finite-JSON, browser and IPC regression run passes with nine
platform/tool skips. Real native library acceptance and final source qualification
remain pending. A fixture field-name error was corrected before proceeding; the
retained-intent entrypoint defect was then fixed and covered by the existing
non-SDK library lifecycle test with evidence recording enabled.

At `3b3f65b4`, Linux and macOS both complete the native library invocation but
refuse retained verification because the command-authority reader also projects
executable entrypoints for libraries. Apply the same library distinction there;
the existing non-SDK library lifecycle fixture now reopens its retained command
authority and passes in 0.110 seconds. The obsolete candidate wheel check was
stopped. Final native and exact-source qualification must cover this reader repair
and the independently rehashed verifier-binding refusal cases.

## Linked provider SDK execution scope

A package already retains the SDK inputs from its managed Component graph, but
execution prepares only the launching Component's direct SDK inputs. An application
that links a native-backed library therefore cannot run from its package. Bind an
explicit execution scope to the package's exact lock, artifact graph and link plan,
and retain each linked Component's build plan and command contract. Select only SDK
owners in that linked closure; preserve their direct materialization identities and
source authority rather than attributing every SDK to the root application.

Fresh runtime preparation and command revocation checks must cover every selected
owner. Retained verification must validate package/project-plan membership and each
owner's command/build authority before accepting its SDK binding. Namespace conflicts,
missing owner plans, foreign artifacts, targets or source bindings fail closed. Keep
the existing direct-only package record shape when there are no provider-owned SDKs.
Qualify a real application-to-library-to-native call after producer and consumer source
deletion and relocation, plus missing/rehashed owner evidence and live revocation.
This package runtime scope does not by itself open generation/build or grant fresh
admission to transferred worker packages.

The first linked-provider fixture exposed two concrete gaps: reopening a Component
lock needs its exact external authorings, and packaged command construction was
resolving provider paths in deleted producer directories before substituting the
package destinations. The scope now retains those authorings, and package commands
bind provider roles directly from verified package custody. Retained SDK imports
must cover the package's complete native SDK file set, preventing a smaller direct
scope from silently omitting a packaged provider. Surrounding coverage after the provider-path repair passes 146 tests with two
skips, plus 12 version-authority checks and a focused test proving packaged provider
roles no longer require producer paths or rewrite literal command text. The repaired
native relocation proof remains in progress.

## Admitted SDK generation inputs

Generation recipes must name each direct SDK's exact consumer input, source lock,
selected recipe, verified build plan, snapshot, target, and public import surface.
Project these only from completed live source-build services for the same Component
lock and owner revision. Give the generator the locked public integration contract,
not private producer source or host paths. Include the projection in recipe identity
and prompt content, reject incomplete or conflicting namespaces, and preserve recipe
identities when no SDKs are selected. A plain deserialized manifest is not admission.

This projection is the next prerequisite for wiring the public path. Generation keys,
provider-only command selection, service creation and host authorization still need
end-to-end integration before removing the existing source-admission guard. Test the
projection with a real native SDK producer and verify that contract/source changes
refuse before generation.

The direct SDK recipe projection now passes a real two-Component source-build test:
only the SDK owner receives exact native import mappings and the locked integration
contract. The outer application keeps its own recipe identity and does not receive
provider SDK details. Rebinding different admitted inputs, foreign lock/owner graphs,
conflicting namespaces and changed public-contract bytes refuse. The six-case native
projection/preparation-service run passes on macOS, along with 135 existing coding-CLI
and node-preparation cases (two skips). The public preparation guard remains closed;
these checks do not claim model-generated consumer source or public build integration.

## SDK-aware generation planning and preparation

An SDK owner's generation key must include its exact admitted consumer-input IDs.
Keep these IDs local to the owner, so changing private provider SDK builds does not
invalidate an outer consumer's source when its public interface is unchanged. The
pure planner may represent explicit input IDs, but those records convey no admission:
filesystem preparation must match them to completed live source-build custody for
the exact locked graph before exposing any recipe or bounded model request.

Include each SDK input record as required local metadata in the bounded context and
its journal. Missing, substituted, duplicate or misattributed SDK segments must refuse.
Retain the existing wire shape and identities for Components without direct SDKs.
Public source generation may use injected completed SDK services; automatic source
acquisition and native-aware build command selection remain separate unfinished
integration requirements. Test real two-Component preparation, context serialization,
SDK-owner-only invalidation, missing/forged custody and unchanged no-SDK behavior.

The explicit filesystem planning, node preparation, source-generation adapter and
Standard runtime factory now accept completed live SDK consumer inputs. A real
two-Component CMake producer test verifies SDK-owner key invalidation, unchanged
outer keys, public library projection, required bounded metadata, full workspace
preparation and rejection after integration-contract drift. It passes locally in
23.768 seconds. Pure IDs, missing custody and substituted owner inputs refuse.
No model is invoked by this preparation proof. Automatic SDK service acquisition,
native-aware command selection and SDK-compatible generation skill authority still
need integration before the public default can generate and run native consumers.

## Automatic native-aware Standard command projection

A plan that binds direct SDK inputs must not project ordinary Python runtime
commands. Validate each plan key against live SDK consumer custody before host
tool discovery, then derive isolated SDK TEST/EXECUTE commands from locked
application or library profiles. Preserve the original SDK runtime driver bytes
while sharing its manifest verification with a Python library import verifier.
Library tests retain the canonical source/main.py test entrypoint; independent
import verification checks every locked capability and symbol against the exact
export while permitting verified SDK imports. Bind SDK export targets to the
consumer's exact selected target. Unsupported native consumer languages refuse
before discovery. No-SDK command contracts must remain unchanged.

Qualify actual projected build/test/import commands against a real CMake SDK,
including changed-manifest rejection, empty/forged custody, multi-entrypoint
projection and ordinary command compatibility. Linked-provider runtime scope,
automatic source-build acquisition and generated-consumer acceptance remain
required integration work beyond direct command projection.

The projected application and library commands now execute against a real native
producer without substituting command fixtures. The three-case run, including
multi-entrypoint projection, passes locally in 101.478 seconds. Libraries reject
missing public symbols, and both runtimes reject changed SDK manifests before
consumer output. The 43 command/factory/preparation regressions and 56 adapter/
architecture/CLI regressions (two skips) pass. A source-byte comparison confirms
that factoring the SDK bootstrap preserves the existing runtime driver verbatim.
The SDK library import command derives its encoded surface from the contract
itself. This qualifies command projection with explicit consumer source, not
model generation, linked-provider runtime imports or public SDK acquisition.

## Acquiring SDKs for acknowledged Standard rebuilds

Resolve the complete selected recipe set and exact named tools before any SDK
build. Reuse source capture, quarantine, project source-intelligence policy,
security scanning, acknowledged host build authorization and live revocation.
Only return consumer inputs after every selected service has completed. Missing
acknowledgement must refuse before tool discovery or cache writes; no-SDK projects
retain ordinary preparation. Carry live inputs with the prepared generation
object and through Standard planning, source generation, command projection and
lifecycle ports. Metadata or a partial build must never substitute for custody.

The public rebuild already acknowledges host execution. Its SDK acquisition
must run after exact lock resolution and before recipe preparation, using its
validated cache directories and bound Standard policy. Source-only generation
retains its existing no-build boundary. Python Flavor and skill authority must
permit the declared SDK imports while prohibiting substitute implementations,
ambient installs and guessed cache paths. For SDK-dependent runtime self-checks,
the generated tests execute through the authorized Standard lifecycle; a model
must not claim those tests passed based on syntax checks or mocks.

Prove acquisition from real locked recipes, preflight refusal, full input coverage,
public adapter wiring, unchanged no-SDK behavior and current skill evaluation.
Full model generation and linked-provider runtime closure remain distinct
completion gates.

Automatic acquisition now passes a real locked two-Component preparation test
in 31.244 seconds, including acknowledgement, missing-tool and wrong-host
refusal before cache creation, plus stale-contract rejection after acquisition.
Public Standard rebuild supplies the project source-intelligence policy, bound
Standard security-policy identity and current project/driver checks, and SDK
cache writes hold the existing project lifecycle lock. Completed inputs travel
with preparation through source generation and the Standard runtime. The 43
rebuild/preparation/Python-skill cases and 47 source-generation/factory/boundary
cases pass. The updated Python skill and its packaged copy pass SkillEvaluator;
Agent Skills format migration remains a separate incomplete repository task.

Final acquisition coverage passes in 29.897 seconds after structured policy and
authorization refusal handling was added. Source-intelligence policy failures
retain their error codes; changed project authorization refuses before tool
discovery. The final 28 public rebuild regression tests pass in 91.435 seconds.

## Linked Component runtime imports

A runtime consumer must receive the SDK imports required by its exact runtime
Component dependency closure, including transitive providers. Generation recipes
and build identities retain direct SDK ownership. Runtime scope binds the lock,
executing revision, every selected owner plan and command contract; foreign or
missing owners, changed direct input identities, incompatible targets and duplicate
import namespaces refuse before execution. Finalized owner plans are required,
not inferred from portable SDK metadata. Each command grant binds all selected
SDKs and checks the original live revocation callbacks before and after execution.
Retained records carry the exact scope and independently validate each binding
against its owner's plan and public contract. Direct SDK and package records keep
their existing interpretation. Prove real linked TEST/EXECUTE through Standard
ports and independent record verification, plus missing-owner and tamper refusal.

The first live linked test exposed the unpackaged provider resolver's file-only
assumption. Declared library contracts may bind a directory export, with path
containment and link rejection retained. The no-driver CLI fixture now removes
an initialized lifecycle driver explicitly, so an installed wheel cannot change
that test's intended precondition. Initial command/factory/architecture/CLI
regressions pass 123 cases with two skips; the provider follow-up passes 58 cases
with two skips. Actual locked acquisition and outer SDK command projection pass
one real SDK case in 26.927 seconds. These are partial checks while the complete
linked native execution and retained-record test remains running.

The complete real linked native test now passes in 612.428 seconds on macOS.
Standard TEST and EXECUTE call the SDK through a linked Python provider before
packaging, preserve the outer Component's empty direct SDK input list, reject a
missing finalized provider plan, and independently validate version-3 execution
scope records. Rehashed records with missing owner plans refuse. The existing
relocated-package execution and owner-authority mutation checks also pass in the
same test. Consumer source and build observations remain explicit fixtures;
OpenCode source generation, fresh-worker admission and full application acceptance
are not established by this test. Working-source Linux regression is in progress.


## Compiling consumers after SDK admission

SDK-aware Standard compilation requires the exact finalized plan and its retained
build request/grant. Recheck that grant against current producer revocations before
backend dispatch, after compilation, and before publishing the artifact. Materialize
and verify SDK bytes around compilation and cache reuse; close that custody scope
before sealing a new artifact. The compiler receives the existing locked build
command, without SDK import paths or a native execution manifest. Runtime imports
remain the responsibility of the separately authorized TEST/EXECUTE commands.

Include nonempty direct SDK input identities and the full SDK dependency closure
in the local build cache key. Preserve the existing key for consumers with no SDK
dependencies. This also separates linked consumers whose unchanged provider export
bytes accompany changed SDK dependency evidence. Retain the existing resolved-BOM projection
of direct and transitive SDK dependencies. Missing finalization, stale grants,
revocation, corrupted SDK storage or custody drift must refuse without publishing a
new artifact. Qualification requires actual Standard builds and cache replay with
a real SDK producer, plus independent retained-build verification. Fixture source
is not evidence of model-generated application acceptance.


Retain compilation as `native-sdk-consumer-build@1` with an exact locked BUILD
command, SDK snapshot/dependency manifest, existing Standard build request/grant,
and before/after revocation observations. The process uses the distinct
`native_sdk_build_identity` field. Independent readers reject runtime records in
this field, changed command/owner/input bindings, incomplete dependency evidence,
missing checks, and mismatched grants. Standard build-authorization reconstruction
must retain the plan's direct SDK input identities when reopening the intent.

## Exact runtime SDK metadata

Python consumers that report SDK identities must read the verified runtime's
`literate_ai_native_sdk.binding(package_name)` mapping. It contains exactly
`sdk_snapshot_identity` and `target_identity`, each a `sha256:` identity string.
The snapshot identity hashes the complete admitted SDK snapshot manifest; the
consumer-input and build-result identities are distinct. Opaque digests must not
be transcribed or calculated by generated application code.

The isolated Standard bootstrap publishes this module only after verifying every
SDK file and the exact execution manifest. The mapping is read-only, and absent
or ambiguous packages raise `LookupError`. A missing module means the current
runtime did not supply SDK metadata; consumers must not install or simulate it.
This metadata does not authorize native execution or replace actual native API
validation. The existing fresh grant, custody and revocation checks continue to
own the process boundary. TEST, EXECUTE, library import and packaged runtime
paths share this bootstrap.

Independent acceptance can use the same verified projection to compare a
consumer's reported identities with the admitted SDK. Wrapped acceptance failures
retain the existing bounded 8192-character diagnostic allowance so that an earlier
traceback frame does not hide the final exception.

The dependency resolver treats the metadata import as supplied by the runtime only
when Standard passes explicit import authority from its verified SDK dependency
evidence. It does not add a fictitious pip distribution to the BOM or exempt the
name globally. Ordinary consumers, unrelated imports, package declarations and
non-Python imports retain their existing dependency checks.

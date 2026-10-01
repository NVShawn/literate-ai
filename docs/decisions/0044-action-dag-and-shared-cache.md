# ADR 0044: Schedule exact lifecycle actions across compatible workers and share verified caches

- Status: Accepted
- Date: 2026-09-24
- Accepted: 2026-09-25
- Decision owners: Literate AI maintainers
- Roadmap: RELEASE-INTEGRATION-002 in `docs/roadmap/active-work.md`

## Context

The 1.1 implementation can overlap whole-Component work inside static topological
layers when a caller supplies one exact worker assignment per Component. Production
CLI composition does not supply that routing, command and SSH overlap tests use local
test handlers, and every node in a layer must finish before any node in the next layer
starts. The scheduler also unions action dependencies into whole-Component edges, so a
consumer action cannot start when its own prerequisites are ready while unrelated later
provider actions continue. This does not meet the requested make/Bazel-like behavior:
use all compatible entries in private `workers.json` whenever independent SDLC work is
ready, while preserving correctness.

The repository separately has accepted-source caching, resumable local test
checkpoints, Bazel's ordinary local state, and provider-specific package outputs. These
do not establish a trustworthy shared test cache, a reusable compiler cache, or shared
package/container custody. The existing test-cache plan explicitly deferred a team-wide
cache, and its dependency-key completeness remains open. A cache hit therefore cannot
currently be treated as accepted lifecycle evidence.

[Bazel remote caching](https://bazel.build/remote/caching) natively supports a local
disk cache and shared HTTP/HTTPS or gRPC remote caches with action-cache and
content-addressed-storage namespaces. [`sccache` WebDAV
storage](https://github.com/mozilla/sccache/blob/main/docs/Webdav.md) supports a
self-hostable HTTP service, while its broader configuration also supports local disk,
Redis, and memcached backends and local-first multi-level chains. A WebDAV-capable HTTP
service can therefore be deployed on a LAN or behind an operator-configured DNS name
without requiring any cloud provider.

## Decision

### Schedule the action DAG, not whole Components

Project the exact `(Component revision, lifecycle action)` graph from the resolved
execution plans. Preserve generation, indexing, source admission, build, test,
execution, independent acceptance, link/import, package, and finalization boundaries
as distinct nodes when their contracts permit independent execution. An action becomes
ready immediately after all of its own required predecessor evidence is accepted; it
does not wait for unrelated members of a former topological layer.

Build intent is a distinct action after source indexing and before authorization.
Build/toolchain dependencies gate that intent on provider acceptance, because intent
binds the exact accepted build inputs. Runtime dependencies gate execution and
packaging/deployment dependencies gate linking; they must not become transitive
barriers to consumer build intent or compilation. Projection checks must inspect
transitive predecessors, since checking direct edges alone misses serialization.
Production late binding of runtime/package inputs remains required before the full
phase graph can replace the existing local continuation implementation.

Runtime and packaging assembly dependencies are distinct from compilation
inputs. Bind each late assembly edge to its exact consumer/provider artifacts, locked
dependency-edge identity, kind, and accepted provider evidence. The artifact graph
includes these edges in its verified transitive link closure without rewriting build
manifests or export identities, and without promoting provider artifacts to application
entrypoints. Reject missing endpoints, duplicate edges, cycles, and substituted link
closures. An empty late-edge set preserves legacy graph identities. Standard assembly
derives these bindings from the locked execution plan and accepted Component results.
Deployment-evidence dependencies must retain their own evidence semantics; they do not
authorize adding provider artifacts to a runtime package.

The local phase runner must not wait for packaging-only providers at build intent,
nor include their artifacts in compile requests or compiled export provenance.
Project assembly waits for accepted Component results and supplies these artifacts
through the explicit assembly bindings. A failed package provider still prevents
project acceptance and publication, while independent consumer compilation can finish.
The explicit complete-node transport retains its existing input handoff until the
phase protocol replaces it; that remaining transport work is part of this program.

Execution evidence must name the canonical exact provider artifact identities used
by the execution phase, separately from the built output identities. A provider
substitution must change that evidence identity, and the lifecycle must reject an
execution result that omits or substitutes the admitted inputs. Independent reopening
of retained execution evidence must check the same input binding. Local single- and
multi-entrypoint execution record the same explicit provider custody. Empty-input
legacy documents retain their identity; absence is not evidence for a nonempty
input set. This custody is required before runtime-only providers can be admitted
after compilation without relying on the build plan to bind them implicitly.

Execution inputs have a separate immutable scope binding the project execution plan,
consumer build plan and exact built exports, compile-time providers, and locked
runtime edges with accepted provider artifacts. Derive that scope from the current
plan and precisely its accepted runtime providers; reject missing, duplicate, foreign,
failed, or substituted inputs. Scope identity must change when runtime artifacts or
provider acceptance change, without rewriting the consumer build plan or exports.
The scope is input authority for execution authorization and evidence reopening,
not itself an execution grant or proof that a provider has been independently accepted.
Scoped local execution requires a separate bounded grant for the input scope, source
tree, locked command contract and runtime tool identities. The deterministic request
binds this execution-input bundle, declares only component execution and stdout/stderr
outputs, and is revalidated immediately before and after each entrypoint launch.
Reject blocked, revoked, expired, mismatched or privilege-widened grants. Every process
observation and aggregate execution record retain the same authority identity;
independent reopening checks the scope against the retained build and provider inputs.
Legacy evidence with no scoped authority retains its existing wire identity.
The local service selects the scoped executor when available, excludes runtime-only
providers from compilation inputs, and waits for their accepted results at EXECUTE.
A failed runtime provider prevents consumer execution and project publication while
independent compilation can finish. The service derives and checks the exact scope
and current grant at this boundary. Opaque accepted-node resume identities cannot
bypass this validation; retain source/build cache eligibility but revalidate scoped
execution until current phase-specific reuse evidence is available. Legacy complete-
node transports retain their existing gates pending production phase dispatch.
Execution input scope includes transitive runtime edges and provider artifacts reached
through build/toolchain inputs. Keep original edge endpoints and acceptance bindings;
do not relabel indirect edges as direct consumer dependencies. Bind additional
compiled provider artifacts separately from the build plan’s direct inputs. Derive
the whole closure from the locked plan and accepted results, reject cycles and absent
artifacts, and supply every admitted runtime input to the process environment.

Generation-only edges consume exact public interfaces already admitted by the lock.
They must not create a provider-completion barrier for the consumer's build intent
or subsequent phases. A provider failure still prevents project-wide acceptance,
packaging and publication; it does not invalidate independent execution using that
locked interface. Edges consuming generated artifacts retain accepted-provider
barriers until their phase-specific custody is implemented.

Use one deterministic event-driven ready queue for the run. Canonical action identity
orders equally ready work. Source generation and resumed/repaired Component phases enter
that same queue; a separate generation loop must not give every post-source phase
implicit priority. Advance controller continuations without occupying a worker slot,
then admit only ready actions under the shared slot bound. Candidate repairs re-enter
the queue at their actual phase and preserve their original failure and retry evidence.
The scheduler maintains explicit per-worker slots and a global cap. Omitted `--jobs` means use every safe available slot; an explicit value is
only a cap and never permission to violate worker capacity.

### Select every safe compatible worker

Read the private execution-worker catalog and fresh private observations through the
existing user-configuration adapters. Derive an identity-bound eligibility plan from
action requirements, target profile, OS/architecture, toolchain and accelerator
capabilities, health/capacity decision, transport support, and configured slot count.
Default a worker with no explicit safe capacity to one slot. Unknown, stale, unhealthy,
incompatible, or transport-incomplete workers are not eligible.

Assign ready work dynamically to idle eligible slots. Prefer a worker with an already
verified exact cache/artifact affinity, then stable worker ID, so scheduling is
reproducible without sacrificing available parallelism. Bind the selected worker,
catalog/observation identities, action, inputs, predecessors, cache decisions, and
deadline into the dispatch request and result. A changed route or stale observation
cannot resume as exact-current evidence.

Command and SSH handlers must execute a serializable action request on the selected
worker. A controller callback that runs locally is not a remote implementation.
Predecessor exports cross workers only through verified content-addressed custody.
Failure cancels transitive descendants, not independent branches. Accepted independent
work remains reusable under its exact identity.

### Use layered, provider-neutral cache configuration

Define private cache configuration with a local content-addressed baseline and an
optional HTTP(S)/WebDAV endpoint addressed by operator-configured URL or DNS name.
Configuration carries scope (`user`, `team`, `organization`, or `company`), namespace,
read/write mode, size/retention policy, TLS requirements, and credential references;
it never stores credentials or resolved private hosts in project authority.

Use separate namespaced adapters over that configuration:

- Bazel actions use Bazel's supported disk and remote-cache protocols.
- Non-Bazel supported compiler actions use `sccache`; Bazel actions do not add a
  redundant `sccache` layer unless an explicit measured policy proves it useful.
- Test results use the existing finalized receipt model only after dependency closure,
  toolchain, environment, action, and oracle identities are complete.
- Packages and containers store immutable bytes plus independently verifiable manifests
  keyed by the exact accepted product closure, provider/tool identity, target/ABI, SBOM,
  and packaging or image configuration.

Local-only configuration remains fully supported. The framework does not provision a
cloud account or require a particular server product. A shared WebDAV-capable service
is the portable baseline because it can run on the same LAN and is supported by both
Bazel-style HTTP caching and `sccache`; native provider adapters may additionally use
gRPC, Redis, memcached, or another explicitly configured backend.

### Cache hits are untrusted inputs

Every restored object, test result, package, image, manifest, and receipt is rehashed
and revalidated against the current action and complete authority before use. A cache
hit skips only the work whose exact output is proven; it never skips indexing,
validation, required execution, independent acceptance, or release verification unless
the owning contract explicitly defines a current reusable receipt for that gate.

Corruption, ambiguity, missing content, incompatible toolchains, unsafe transport,
changed scope, incomplete keys, or stale evidence is a miss or a typed refusal according
to the owning contract. Shared writers are least-privilege and may be restricted to CI;
read-only consumers remain supported. Cache availability is an optimization, never a
correctness dependency.

## Qualification and rollout

Implementation proceeds under the explicit maintainer acceptance recorded on
2026-09-25. Qualification must prove:

- a dependent action starts as soon as its own prerequisites finish while an unrelated
  slow action continues;
- interface-only consumer work can overlap later provider phases, while artifact
  consumers wait for exact accepted exports;
- all independent ready actions occupy distinct compatible worker slots, explicit
  caps are honored, and no worker is double-booked beyond declared capacity;
- incompatible, stale, unhealthy, or unreachable workers never receive work;
- command and SSH fixtures perform real remote/process actuation, byte handoff,
  cancellation, recovery, and overlap rather than invoking a local callback;
- failure cancels descendants only and exact-current recovery rejects changed routing;
- cold, warm, partial, corrupted, poisoned, unavailable, read-only, and concurrent
  local/LAN cache cases preserve identical accepted outputs;
- the second exact build demonstrates measured Bazel and/or sccache hits without
  weakening toolchain identity, and the test-cache assertion is proven rather than
  assumed;
- package and container restores verify complete manifests, payloads, modes, SBOMs,
  target/ABI and provider identities before acceptance; and
- installed-wheel Linux, macOS, and Windows workers plus hosted CI exercise the same
  configuration and fail-closed behavior.

Roll out in slices: shared scheduler primitive and deterministic fixtures; production
worker composition and real transports; local cache adapters; optional LAN endpoint;
then package/container reuse and cross-platform release qualification. Each slice keeps
uncached execution available and authoritative.

## Consequences

The controller becomes a bounded action scheduler, but not a fleet provisioner. Private
dispatchers still own provisioning, queueing, leases, priorities, and machine lifecycle.
More concurrency increases resource pressure, so worker-health admission and explicit
slot limits are part of correctness rather than optional telemetry.

Shared caching reduces rebuild time only for reproducible actions with complete keys.
It adds server security, namespace, eviction, poisoning, and credential-management
concerns; the design contains those in private configuration and verifier-owned
admission. Network cache failure may reduce performance but cannot make a valid build
or release depend on the cache being online.

## Command action wire boundary

The command-action adapter sends data through `lifecycle-action-wire@1`, separately
from the existing whole-lifecycle dispatcher protocol. Requests embed the exact
action, admitted worker/catalog/observation, slot, ordered predecessor results, and
an absolute deadline whose identity enters the request. Required payload and
predecessor records cross the wire as verified SHA-256 bytes. Missing, extra,
duplicate, corrupt, foreign, or changed records and identities refuse execution.
The envelope allows at most 4,096 input records, 16 MiB of aggregate record bytes,
and 24 MiB of encoded wire bytes. A deadline must be current and within one day.

The adapter invokes only the selected private command argv, using stdin or the
existing `{request_file}` placeholder and explicitly bound environment variables.
The shared bounded pipe runner drains both streams concurrently, enforces byte and
time limits, and terminates the owned dispatcher process tree on cancellation or
completion. Request-file custody is temporary and private. Responses bind the exact
request and admitted worker; result bytes are rehashed before recording, with worker
admission and deadline rechecked. Stderr never becomes an action failure message.

Real subprocess fixtures prove protocol custody and two-worker scheduler overlap.
They do not establish production phase execution, remote descendant cleanup, SSH
actuation, automatic CLI routing, or verified transfer of transitive artifact bundles.
Those remain required under RELEASE-INTEGRATION-003. A legacy command worker does
not become phase-capable merely because it is present in the private catalog.

## Artifact transport implementation

The HTTP artifact adapter publishes payloads before manifests using conditional
`PUT` (`If-None-Match: *`) and verifies each published entry with a bounded `GET`.
The operator provisions the namespace directories on a server that honors conditional
writes. Redirects are refused, including redirects to the same host. Private bearer
credentials are resolved outside project authority and are never included in error
text. Every read verifies the full expected manifest and the payload digest and size.
Missing partial entries are misses; conflicting or corrupt entries are typed refusals.
The local-first adapter reports remote unavailability as a miss and retains successful
local publications when the network is unavailable.

Manifest cache identity now accepts a storage projection of scope and namespace.
Access mode, credential reference, endpoint, local placement, and retention remain bound
by the full dispatch configuration, but do not change shared content identity. This
allows read-only consumers to verify writer entries and allows verified local mirrors.
Exact legacy configuration-bound manifests remain readable with their original
configuration. Product authority, provider, target/ABI, SBOM, payload identity, and
mode remain mandatory verification inputs; the storage projection grants no acceptance.
Production action composition, compiler measurements, and platform qualification remain
release-blocking work under RELEASE-INTEGRATION-003.

Local artifact publication serializes writers across keys within each artifact
namespace. Its quota counts manifest bytes and distinct payload bytes, excluding
coordination-lock metadata; Bazel/compiler storage and remote service policies are
separate. Publication collects expired manifests, then the oldest remaining entries
until the incoming entry fits. Shared payloads remain while any retained manifest
references them. Orphan objects from interrupted publication are collectable, and
manifests are removed before their unreferenced objects. Inventory is bounded by
100,000 entries, 16 MiB of manifests, and 30 seconds; malformed, foreign, link-like,
hardlinked, or changing entries refuse collection. Collisions refuse before eviction.
Writers reserve those same inventory limits for incoming entries, including a 1 MiB
per-manifest reader limit, so publication cannot make subsequent inventory unbounded.

Readers return a miss for expired or concurrently removed entries without rewriting
bytes or modification times. An exact republication renews an expired manifest only
after verifying its existing bytes and payload. A local quota refusal remains advisory
to the layered cache: verified remote bytes can be used without local hydration, and
remote publication can still succeed. Other custody failures remain typed refusals.
These rules do not establish current product acceptance or remote retention enforcement.

## Private production configuration and read-only Bazel custody

Standard rebuild loads `shared-cache.json` from the private configuration directory
reported by `litai config paths`; `LITAI_SHARED_CACHE_CONFIG` may select an absolute
alternative. An absent default file leaves caching unconfigured. Explicit missing,
unsafe, malformed, duplicate-key, or changed configuration is refused. The cache
contract binds the rebuild identity, and credential references resolve through
`env:NAME`. Bearer credentials live in a temporary private Bazel RC for each
invocation and are excluded from command arguments and diagnostics.

Bazel's remote read-only flag does not prevent disk-cache writes. Read-only builds
therefore use a disposable copy of local cache entries, independently of the shared
storage directory, with remote uploads disabled. The copy excludes expired entries
and is bounded by configured bytes, 100,000 entries, and 30 seconds; unavailable,
changing, or over-budget contents become an empty view. No writable hard links are
used. Bazel verifies restored content, and ordinary staging cleanup removes the view.
Planning without an execution workspace disables the disk cache for read-only mode.
This preserves cache correctness at the cost of copying; large-cache measurements and
shared writer retention enforcement remain qualification work.

## Cargo compiler-cache composition

Cargo compiler caching binds the selected sccache executable's content identity
into the private cache binding before rebuilding. Each build owns its foreground
server and private configuration, bounded startup, statistics, and shutdown calls;
an unavailable server falls back to the compiler. The caller's ambient sccache
settings and compiler wrappers do not select a different cache process. Credentials
remain private, and retained process output is redacted.

Rust cache keys include working-directory and Cargo-environment paths. Exact Cargo
targets therefore use stable disposable staging under the runtime object root,
protected by the existing cross-process cache lock. Staging is rebuilt from the
admitted source and removed after use. Incremental Rust compilation is disabled;
link steps are not claimed as cacheable. Read-only sccache disk storage also requires
a disposable view because the native reader updates file modification times.

Captured Cargo build observations may include strictly validated cache-tool and
configuration identities and advisory counters. These counters do not establish
acceptance or replace the exact compiler/source/dependency and independent acceptance
contracts. Cache-tool native dependency observation now binds recursive native image,
inspector, and loader-resolution evidence into cache identity and the Standard build
dependency graph. The runtime revalidates it at lifecycle entry/exit (including
artifact-cache reuse) and around each compiler-cache session with that process's
loader environment. Unsupported loader controls and identity drift refuse execution;
they are not availability fallbacks. Observation uses immutable canonical bytes and
preserves distinct cache-runtime roles when a compiler also uses the same library.
Full recursive observation has measurable per-build cost; throughput and platform/LAN
qualification remain open under RELEASE-INTEGRATION-003.

Standard native C++ command drivers share a stdlib-only compilation helper: each
translation unit produces one disposable object through the selected cache tool,
and the locked compiler links those objects without a cache wrapper. The helper
is embedded in the exact command authority, with documentation removed and bytes
compressed to preserve the existing command-token size limit. Single and multiple
entrypoints use the same boundary. Only recognized framework command drivers receive
the private wrapper; an ambient wrapper variable does not select a tool. Captured
native build observations retain the same advisory cache fields as Cargo.
The helper's source is a package resource, not an assumed physical `__file__` path.
Driver construction must work when the framework is imported directly from its
verified runtime ZIP as well as from a checkout or installed wheel, without borrowing
source from another checkout or changing the emitted compiler command semantics.

## Worker-side source indexing

A source-index action retains the production planner's static action payload. Its
accepted generation predecessor supplies the candidate and bounded portable file
manifest with exact `BlobRef` values. The generation result binds the same execution
plan, and the candidate must match the payload's generation plan and Component.
Generating or repairing source changes the predecessor result, not the already planned
action identity. Source payloads live in the worker's privately bound CAS; they are not
base64-embedded in action messages. The receiver verifies the full source-tree identity,
all file paths and byte/count limits before materialization. Each blob is then copied
through the existing streaming, same-handle CAS verifier into a disposable private
workspace. Only the production source-index policy's exact result is returned.

The receiver binds its expected worker identity independently of incoming requests,
checks the admitted deadline throughout materialization, and removes temporary source
custody after success or refusal. Unsupported phases fail explicitly. A successful
index result establishes only source indexing; it grants neither build authorization
nor current acceptance. Production routing and the remaining phase handlers are
separate release-blocking obligations.

The initial receiver is `python -m literate_ai.action_worker`, reading the bounded
request from stdin. Its command/environment configuration supplies absolute private
CAS and workspace directories and a worker identity through
`LITAI_ACTION_WORKER_IDENTITY` (or an explicitly selected environment variable).
This identity is configured independently of the incoming request. File manifests
are limited to 100,000 files and 256 MiB of materialized bytes; the existing action
record byte limit also applies. Temporary workspaces are removed on normal success
and refusal. Host interruption cleanup and remote CAS staging still need their
production integration and qualification.

The index receiver may fetch manifest-declared missing blobs from an operator-bound
HTTP(S) source CAS endpoint using the existing filesystem CAS blob layout. Endpoint
and optional bearer credential bindings are private receiver configuration, never
accepted from the action payload. HTTPS is the default; plaintext HTTP requires an
explicit operator opt-in. Redirects and encoded responses are refused. Reads obey the
remaining action deadline and exact declared size, and verify the SHA-256 digest before
publishing into worker CAS. Existing corrupt local blobs refuse rather than silently
changing custody. Partial verified transfers confer no index or acceptance result.

Receiver flags `--source-cas-url`, optional `--source-token-env`, and `--allow-http`
carry those private bindings. A named token variable must resolve; it does not fall
back to anonymous access. Fetches use GET only and precede source-workspace allocation.
Verified worker blobs are reused without network access; corruption is never repaired
implicitly. Operator provisioning of the read-only CAS service and automatic source
publication/routing remain separate production integration obligations.

## Production source-index port

The command indexer implements the same `GenerationIndexer.index(component, source)`
interface accepted by the Standard runtime factory. Controller source resolvers and
candidate readers provide custody only; phase execution occurs in the selected command
worker. The indexer snapshots bounded regular portable source files, verifies the complete
candidate tree before any CAS publication, and retains the production static action node.
Slot reservation uses the admitted workers' declared capacity and the run deadline.
Returned evidence must equal the existing disabled-index policy's canonical result for
that exact Component and source, then enter the existing qualification recorder.
Automatic CLI routing and non-index phase handlers remain separate obligations.

## Phase capability admission

Private command workers opt into action probing with an optional `action_protocol`
declaration. Its absence retains the legacy worker document and identity and prevents
capability invocation; appending a probe flag to an arbitrary legacy command is unsafe.
A declared worker answers a bounded, finite-deadline challenge with its independently
configured worker identity, exact receiver code identity, Python runtime identity,
supported actions and configured source-handoff support. Responses bind the challenge
identity and never disclose private paths or credential values. Hardware/health admission
and automatic routing remain required alongside these phase capabilities.

The receiver describes only implemented actions (currently INDEX). Its Python code
identity covers canonical relative paths and exact bytes of package `.py` files,
excluding bytecode caches; inventory is bounded to 65,536 entries, 4,096 Python files
and 64 MiB. Links refuse. The Python executable is identified separately. This records
code/runtime observations, not native toolchain attestation. Challenge identity and
observation time distinguish fresh observations, while a separate stable capability
identity permits comparison of unchanged facts. Changed routes, receiver code, future
observations and observations older than five minutes refuse current admission.

The controller caps each capability subprocess at the earlier of the action deadline
and 60 seconds, including controller identity preparation. Native Windows evidence
shows identity reads and process startup can exhaust the previous 30-second cap.
The outer action deadline remains authoritative even when shorter than this cap.
Its canonical challenge carries that same shortened deadline. Probe
stdin/stdout are bounded to 16 KiB and stderr to 4 KiB; owned descendants are terminated
on refusal or timeout. Required environment bindings use the same isolated resolver as
action dispatch. File-mode requests are private temporary files and are removed afterward.

Command phase admission composes fresh hardware eligibility with live protocol
capabilities and an explicit health-admission identity. Source handoff and phase
requirements are mandatory inputs. The exact private catalog, hardware observation,
capability facts and health evidence bind the scheduler worker observation. Route,
hardware freshness, health and live receiver/runtime facts are revalidated before
dispatch and before accepting a result. A failed candidate is excluded with a bounded
public reason; no eligible workers is an explicit refusal. This admission port must
be composed into the production indexer before CLI automatic selection can use it.

Read-only capability description validates HTTP source configuration without
initializing transport or discovering host proxy settings. Initialize the HTTP opener
only when fetching a source blob, within the existing bounded action deadline.

The public Standard rebuild factory can bind an admitted INDEX worker pool and an
explicit source-publication CAS as one indivisible configuration. The selected
command indexer reads candidates from the runtime registry's verified source evidence,
not caller-supplied candidate values. Fresh generation, source-cache restoration, and
checkpoint composition retain the same index port and qualification recorder. The
rebuild invocation binds the worker admission identity. Without a pool, existing local
indexing is unchanged; incomplete worker configuration refuses before execution.

Command source publication must bound every pre-publication read, including registry
lookup. Registry registration metadata may be read separately from current-byte
verification, but it grants no dispatch authority. The command indexer verifies a
no-follow, byte/file/deadline-bounded snapshot against the exact registered tree
identity before publishing any blob. Other registry users retain full current-tree
verification through the existing resolve/evidence methods.

Private automatic action execution is configured in `action-execution.json` beneath
the resolved user configuration root, with `LITAI_ACTION_EXECUTION_CONFIG` as an
explicit path override. Its versioned record names an absolute source CAS root,
source handoff, finite action duration, hardware freshness bound, and per-worker
health configuration paths. Existing worker catalog and observation path resolution
remains authoritative. An absent default preserves local indexing; an explicit
missing or invalid configuration, or an empty eligible command pool, refuses. The
Standard factory binds configured INDEX admission automatically and pins configuration
and health policy custody. Source publication must remain outside project authority.
This initial automatic route covers indexing; other remote phases remain mandatory
release work, and stale hardware observations require refreshed admission.

Explicitly opted-in command workers expose hardware observation through a separate
`--describe-hardware` request. It carries the existing finite challenge/deadline
record plus the portable worker ID. The response binds the complete request, private
worker identity, receiver code identity and typed hardware observation. The receiver
uses the existing platform collectors locally; the controller never substitutes its
own hardware for a command worker. Transport bounds, environment isolation, owned
process cleanup and private diagnostics match phase-capability probing. The controller
stamps observation time from the start of its bounded probe, conservatively including
collection latency. Legacy commands remain unprobed. Public worker probing persists
only fully verified typed observations; this protocol does not itself admit dispatch.

Automatic command admission obtains live hardware facts from each opted-in candidate
with a matching target and configured health policy. Persisted observation files are
operator reports, not a prerequisite or a substitute for that live collection. Within
one admission, a verified observation may be reused only while fresh and bound to the
same worker identity; expiry triggers a new bounded probe. Failed probes exclude the
candidate. Freshness and compatibility remain mandatory on every revalidation. A
refreshed timestamp alone does not invalidate unchanged hardware; changes to any
hardware fact do. The pool retains the current typed observation catalog while its
initial admission identity continues to bind the original evidence. Catalog, receiver
code, phase capability and health revalidation remain independent mandatory checks.

Public multi-worker probing retains verified successful peers and removes stale
observations for failed selected workers. If every probe fails and no observation
file exists, it must not create an empty success artifact. Existing stale catalogs
still receive the failure removals. The result reports whether a file was written
and returns failure status when any selected probe fails. This composes private
worker-management operations with command hardware discovery without weakening
request/worker/code verification.

Windows CAS hardlink publication must support an admitted absolute destination beyond
MAX_PATH without assuming registry long-path policy. Use extended-length drive or UNC blob paths consistently for native publication,
reads, metadata checks and enumeration, preserving already extended paths. Keep the
configured CAS root and content identities unchanged. A hardlink-only conversion is
insufficient because verification must reopen the same long destination. Publication remains atomic
create-if-absent; never replace an existing digest or fall back to mutable copying.
The long-path portable cache archive fixture remains long, and native Windows
qualification must preserve duplicate, corrupt-blob and concurrent-writer behavior.
The Win32 path contract is documented by
[CreateHardLinkW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw).

The same Windows namespace discipline applies to source-cache metadata directories,
immutable memberships, bounded reads and portable archive capture. Retain the public
configured cache root in its canonical form; derive native I/O paths beneath it and
compare containment in that same namespace. Portable archive members remain relative
POSIX paths and include no native namespace prefix. Long-path regression cleanup must
also address the owned temporary tree through a native path, without shortening the
fixture or relaxing corruption, symlink or immutable-publication checks.

SSH action workers opt in with the existing action protocol and a private
`action_command` argument vector. That vector names a worker-owned receiver with its
own CAS, workspace and credentials; it is independent of the whole-lifecycle launcher.
Requests and observation challenges travel over SSH stdin, never in shell arguments.
The controller forwards no credential environment to the SSH receiver. POSIX argument
quoting and encoded PowerShell invocation preserve literal arguments. Both transports
retain identical bounded output, deadline, receiver-code identity and result custody
checks. A legacy SSH worker without this vector remains ineligible. Transport fixtures
do not establish native SSH qualification or support for unimplemented action phases.

On a Windows controller, action transport environment lookup must preserve Windows'
case-insensitive variable names when private bindings are copied into a plain mapping.
Retain only the existing runtime allowlist and explicit worker bindings. In particular,
uppercase SYSTEMROOT and COMSPEC must reach child processes as runtime bindings;
PowerShell may fail before receiver startup when these are accidentally discarded.
This correction does not authorize forwarding additional ambient credentials.
